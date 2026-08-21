#!/usr/bin/env python3
"""
Daemon Controller - Coordination layer between HTTP thread and async daemon.

Provides thread-safe access to daemon operations (scan, pair, connect,
disconnect) from the HTTP server thread via asyncio.run_coroutine_threadsafe().
"""

import asyncio
import logging
import os
import subprocess
import threading

from bt_setup import chip
from config import Protocol, config, normalize_addr
from device_cache import DeviceCache
from logging_utils import errstr

logger = logging.getLogger(__name__)

# A healthy cleanup can spend up to 7 seconds in its bounded L2CAP,
# connection, and transport-close waits.  Leave enough room for those waits
# before treating the whole session as stuck.
MANUAL_RECONNECT_SUSPEND_TIMEOUT = 10.0

# Broadcom prepare/pre_open currently contains synchronous firmware and UART
# recovery work.  If that blocks the event loop, an asyncio timeout cannot
# fire, so an independent thread is the final escape hatch.  The installed
# Upstart job has `respawn`, giving the replacement daemon a completely fresh
# task graph and a freshly closed UART fd table.
MANUAL_RECONNECT_WATCHDOG_TIMEOUT = 15.0

__all__ = ['DaemonController']


class DaemonController:
    """Coordinates between the HTTP server thread and the async daemon.

    All request_* methods are called from the HTTP thread and schedule
    coroutines on the daemon's event loop via run_coroutine_threadsafe().
    """

    def __init__(self, daemon):
        self.daemon = daemon
        self.loop = None  # Set when event loop starts

        self._op_lock = asyncio.Lock()
        self._suspended_by_system = False
        self._chip_powered_off_for_suspend = False
        self._manual_reconnect_future = None
        self._manual_reconnect_watchdog = None
        self._hard_restart_lock = threading.Lock()
        self._hard_restart_requested = False

        # Scan state
        self.scan_result = None
        self.is_scanning = False
        self._scan_live_devices = []

        # Pair state
        self.pair_result = None
        self.is_pairing = False

        # Device list cache (mtime-based)
        self._devices_cache = None
        self._devices_mtime = 0
        self._devices_lock = threading.Lock()

        # Mouse cursor overlay process
        self._cursor_proc = None
        self._cursor_lock = threading.Lock()

    def get_status(self) -> dict:
        """Thread-safe read of daemon state. Called from HTTP thread."""
        devices = self._get_devices_cached()

        status = {
            "daemon_running": self.daemon.running and not self.daemon._suspended,
            "devices": devices,
            "device_count": len(devices),
            "scanning": self.is_scanning,
            "pairing": self.is_pairing,
            "cursor_running": self.cursor_running(),
        }

        conn = self.daemon.connection_state
        if conn.get("connected"):
            status["connected_device"] = conn.get("address")
            status["connected_protocol"] = conn.get("protocol")
            status["connected_name"] = conn.get("name")
            status["hid_ready"] = conn.get("hid_ready", False)
            if conn.get("uhid_name"):
                status["uhid_name"] = conn["uhid_name"]
            if conn.get("input_paths"):
                status["input_paths"] = conn["input_paths"]
            if conn.get("descriptor_size"):
                status["descriptor_size"] = conn["descriptor_size"]

        return status

    def _get_devices_cached(self) -> list:
        """Device list from devices.conf, cached by file mtime."""
        try:
            mtime = os.path.getmtime(config.devices_config_file)
        except OSError:
            mtime = 0

        with self._devices_lock:
            if self._devices_cache is not None and mtime == self._devices_mtime:
                return self._devices_cache

            devices = config.get_all_devices()
            self._devices_cache = [
                {
                    "address": addr,
                    "protocol": proto.value,
                    **({"name": name} if name else {}),
                }
                for addr, proto, name in devices
            ]
            self._devices_mtime = mtime
            return self._devices_cache

    # ---- Scan ----

    def request_scan(self):
        """From HTTP thread: schedule scan on event loop."""
        if self.is_scanning:
            return
        self.scan_result = None
        asyncio.run_coroutine_threadsafe(self._do_scan(), self.loop)

    def _on_device_found(self, device):
        """Callback from scanner when a device is discovered."""
        self._scan_live_devices.append({
            "address": device.address,
            "name": device.name,
            "protocol": device.protocol.value,
            "rssi": device.rssi,
        })

    async def _do_scan(self):
        async with self._op_lock:
            self.is_scanning = True
            self._scan_live_devices = []
            try:
                await self.daemon.suspend()
                config.validate_keystore()

                # Re-warm the chip if a prior /stop powered it off; opening the
                # transport against a cold chip makes HCI Reset time out.
                chip().ensure_powered()

                await self.daemon.scan(
                    duration=10.0,
                    on_device_found=self._on_device_found,
                )
                self.scan_result = {
                    "ok": True,
                    "devices": self._scan_live_devices,
                }
            except Exception as e:
                logger.error(f"Scan failed: {errstr(e)}")
                self.scan_result = {"ok": False, "error": str(e)}
            finally:
                self.is_scanning = False
                await self.daemon.resume()

    # ---- Pair ----

    def request_pair(self, address, protocol, name=None):
        """From HTTP thread: schedule pair on event loop."""
        if self.is_pairing:
            return
        self.pair_result = None
        self.is_pairing = True  # Set immediately so status polls see it
        asyncio.run_coroutine_threadsafe(
            self._do_pair(address, protocol, name), self.loop
        )

    async def _do_pair(self, address, protocol, name):
        async with self._op_lock:
            try:
                await self.daemon.suspend()
                config.validate_keystore()

                # Re-warm the chip if a prior /stop powered it off; opening the
                # transport against a cold chip makes HCI Reset time out.
                chip().ensure_powered()

                success = await self.daemon.pair(address, protocol, name)
                if success:
                    config.add_device(address, protocol, name)
                self.pair_result = {
                    "ok": success,
                    "address": address,
                    **({"message": "Paired successfully"} if success
                       else {"error": "Pairing failed"}),
                }
            except Exception as e:
                logger.error(f"Pair failed: {errstr(e)}")
                self.pair_result = {"ok": False, "address": address, "error": str(e)}
            finally:
                self.is_pairing = False
                await self.daemon.resume()

    # ---- Connect / Resume ----

    def request_connect(self, address=None, protocol_str=None):
        """From HTTP thread: connect to a device or resume the daemon.

        With address: suspend → save device to config → resume.
        Without address: recycle the current Bluetooth session and reconnect
        configured devices (used by /start).
        """
        if not address:
            # Coalesce repeated button presses.  Multiple queued power cycles
            # make recovery slower and can race system suspend notifications.
            pending = self._manual_reconnect_future
            if pending is not None and not pending.done():
                logger.info("Manual reconnect already in progress")
                return

            future = asyncio.run_coroutine_threadsafe(
                self._do_resume(), self.loop
            )
            self._manual_reconnect_future = future

            watchdog = threading.Timer(
                MANUAL_RECONNECT_WATCHDOG_TIMEOUT,
                self._manual_reconnect_watchdog_expired,
                args=(future,),
            )
            watchdog.daemon = True
            self._manual_reconnect_watchdog = watchdog

            def reconnect_done(_future):
                watchdog.cancel()
                if self._manual_reconnect_future is _future:
                    self._manual_reconnect_future = None
                if self._manual_reconnect_watchdog is watchdog:
                    self._manual_reconnect_watchdog = None

            future.add_done_callback(reconnect_done)
            watchdog.start()
            return

        protocol = Protocol.CLASSIC if protocol_str == 'classic' else Protocol.BLE
        asyncio.run_coroutine_threadsafe(
            self._do_connect(address, protocol), self.loop
        )

    async def _do_resume(self):
        async with self._op_lock:
            self._suspended_by_system = False
            self._chip_powered_off_for_suspend = False

            # If the live link is healthy, dropping it is enough: daemon.run()
            # owns the normal cleanup and reconnect loop.  Do not needlessly
            # reload Broadcom firmware for this case.
            host = self.daemon.host
            if host and host._is_connection_alive():
                logger.info("Manual reconnect: dropping healthy device link")
                await self.daemon.disconnect()
                return

            logger.info("Manual reconnect: recycling Bluetooth session")
            try:
                await asyncio.wait_for(
                    self.daemon.suspend(),
                    timeout=MANUAL_RECONNECT_SUSPEND_TIMEOUT,
                )
            except asyncio.TimeoutError:
                self._hard_restart(
                    "old Bluetooth session did not stop before timeout",
                    power_off=True,
                )
                return
            except Exception as e:
                self._hard_restart(
                    f"Bluetooth session cleanup failed: {errstr(e)}",
                    power_off=True,
                )
                return

            # A successful suspend has closed the old transport.  On resume,
            # BrcmChip.pre_open() probes raw HCI Reset and performs the full
            # power-off/btfd/BSA re-warm only when the controller is actually
            # unresponsive.
            await self.daemon.resume()

    def _manual_reconnect_watchdog_expired(self, future):
        """Hard-stop a daemon whose event loop cannot service reconnect."""
        if future.done():
            return
        self._hard_restart(
            "manual reconnect watchdog expired (event loop or cleanup stuck)",
            power_off=False,
        )

    def _hard_restart(self, reason, power_off):
        """Terminate this wedged daemon; Upstart respawns a clean process.

        This is intentionally a process-level fallback.  Resuming in the same
        process after an uncooperative cancelled task can leave that task using
        a stale Bumble transport while the replacement session opens the same
        UART.  Process exit closes every fd and makes that overlap impossible.
        """
        with self._hard_restart_lock:
            if self._hard_restart_requested:
                return
            self._hard_restart_requested = True

        logger.critical(f"Forcing clean daemon restart: {reason}")
        if power_off:
            try:
                chip().power_off()
            except Exception as e:
                logger.warning(
                    f"Bluetooth controller power-off before restart failed: "
                    f"{errstr(e)}"
                )

        # Do not run normal async shutdown here: this path exists precisely
        # because an old task or transport cannot be trusted to finish it.
        os._exit(1)

    async def _do_connect(self, address, protocol):
        async with self._op_lock:
            try:
                await self.daemon.suspend()
                config.add_device(address, protocol)
                await self.daemon.resume()
            except Exception as e:
                logger.error(f"Connect failed: {errstr(e)}")
                await self.daemon.resume()

    # ---- System suspend (powerd) ----

    def on_system_suspend(self, event):
        """From power monitor thread: BT off before the system sleeps."""
        asyncio.run_coroutine_threadsafe(self._do_system_suspend(event), self.loop)

    async def _do_system_suspend(self, event):
        async with self._op_lock:
            if event == 'goingToScreenSaver':
                if self.daemon._suspended:
                    return
                logger.info(
                    "System preparing to suspend: closing Bluetooth transport"
                )
                self._suspended_by_system = True
                self._chip_powered_off_for_suspend = False
                await self.daemon.suspend()
                return

            # readyToSuspend follows goingToScreenSaver on a real sleep. Keep
            # the slow disconnect in the early phase, but delay the physical
            # power cut until this final event. If the screen saver transition
            # is cancelled, outOfScreenSaver simply reconnects without a power
            # cycle. Handle a missing early event defensively as well.
            if not self._suspended_by_system:
                self._suspended_by_system = True
                self._chip_powered_off_for_suspend = False
            if not self.daemon._suspended:
                await self.daemon.suspend()
            if not self._chip_powered_off_for_suspend:
                logger.info(f"System suspend ({event}): powering BT off")
                chip().suspend_power_off()
                self._chip_powered_off_for_suspend = True

    def on_system_resume(self, event):
        """From power monitor thread: re-warm BT after wake."""
        asyncio.run_coroutine_threadsafe(self._do_system_resume(event), self.loop)

    async def _do_system_resume(self, event):
        async with self._op_lock:
            if not self._suspended_by_system:
                return
            self._suspended_by_system = False
            self._chip_powered_off_for_suspend = False
            if not self.daemon._suspended:
                return
            logger.info(f"System resume ({event}): restarting BT")
            await self.daemon.resume()

    # ---- Remove ----

    def request_remove(self, address: str) -> dict:
        """Remove a device from config, clear its cache, and disconnect."""
        result = config.remove_device(address)
        if result["removed"]:
            DeviceCache(config.cache_dir).clear(normalize_addr(address))
            self.request_disconnect()
        return result

    # ---- Clear Cache ----

    def request_clear_cache(self) -> int:
        """Clear all descriptor cache files. Returns count of files removed."""
        return DeviceCache(config.cache_dir).clear()

    # ---- Mouse Cursor Overlay ----

    def cursor_running(self) -> bool:
        with self._cursor_lock:
            return self._cursor_proc is not None and self._cursor_proc.poll() is None

    def request_cursor_start(self):
        """Launch the mousecursor overlay binary (driven by pointer connect)."""
        with self._cursor_lock:
            if self._cursor_proc is not None and self._cursor_proc.poll() is None:
                return
            # reap any stray overlay (e.g. orphaned by a prior daemon restart)
            subprocess.run(['pkill', '-x', 'mousecursor'], capture_output=True)
            binary = os.path.join(config.base_path, 'scripts', 'mousecursor')
            errlog = os.path.join(config.base_path, 'cache', 'mousecursor.log')
            try:
                err = open(errlog, 'ab')
                self._cursor_proc = subprocess.Popen(
                    [binary], stdout=subprocess.DEVNULL, stderr=err)
                logger.info(f"Cursor overlay started (pid {self._cursor_proc.pid})")
            except OSError as e:
                logger.error(f"Cursor overlay failed to launch ({binary}): {e}")
                self._cursor_proc = None

    def request_cursor_stop(self):
        """Kill the mousecursor overlay binary (driven by pointer disconnect)."""
        with self._cursor_lock:
            proc = self._cursor_proc
            self._cursor_proc = None
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            # safety net: reap any stray/orphaned overlay too
            subprocess.run(['pkill', '-x', 'mousecursor'], capture_output=True)

    # ---- Disconnect / Stop ----

    def request_disconnect(self, suspend=False):
        """From HTTP thread: drop connection.

        suspend=False: drop connection, daemon keeps running (reconnect loop).
        suspend=True:  suspend daemon entirely (/stop).
        """
        asyncio.run_coroutine_threadsafe(
            self._do_disconnect(suspend), self.loop
        )

    async def _do_disconnect(self, suspend):
        async with self._op_lock:
            try:
                if suspend:
                    await self.daemon.suspend()
                    chip().power_off()
                else:
                    await self.daemon.disconnect()
            except Exception as e:
                logger.error(f"Disconnect failed: {errstr(e)}")
