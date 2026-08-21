#!/usr/bin/env python3
"""Unit tests for forced manual reconnect recovery."""

import asyncio
import concurrent.futures
import os
import sys
import threading
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "kindle_hid_passthrough",
))

import controller  # noqa: E402


class FakeHost:
    def __init__(self, alive):
        self.alive = alive

    def _is_connection_alive(self):
        return self.alive


class FakeDaemon:
    def __init__(
        self,
        events,
        suspend_error=None,
        suspend_hangs=False,
        host=None,
    ):
        self.events = events
        self.suspend_error = suspend_error
        self.suspend_hangs = suspend_hangs
        self.host = host

    async def suspend(self):
        self.events.append("suspend")
        if self.suspend_hangs:
            await asyncio.Event().wait()
        if self.suspend_error:
            raise self.suspend_error

    async def resume(self):
        self.events.append("resume")

    async def disconnect(self):
        self.events.append("disconnect")


class FakeChip:
    def __init__(self, events):
        self.events = events

    def power_off(self):
        self.events.append("power_off")


def make_controller(daemon):
    subject = object.__new__(controller.DaemonController)
    subject.daemon = daemon
    subject._op_lock = asyncio.Lock()
    subject._suspended_by_system = True
    subject._chip_powered_off_for_suspend = True
    subject._manual_reconnect_future = None
    subject._manual_reconnect_watchdog = None
    subject._hard_restart_lock = threading.Lock()
    subject._hard_restart_requested = False
    return subject


def test_manual_reconnect_drops_healthy_link_without_session_restart():
    events = []
    subject = make_controller(
        FakeDaemon(events, host=FakeHost(alive=True))
    )
    with patch.object(subject, "_hard_restart") as hard_restart:
        asyncio.run(subject._do_resume())

    assert events == ["disconnect"]
    hard_restart.assert_not_called()


def test_manual_reconnect_recycles_disconnected_session_without_power_cycle():
    events = []
    subject = make_controller(FakeDaemon(events))
    with patch.object(subject, "_hard_restart") as hard_restart:
        asyncio.run(subject._do_resume())

    assert events == ["suspend", "resume"]
    hard_restart.assert_not_called()
    assert subject._suspended_by_system is False
    assert subject._chip_powered_off_for_suspend is False


def test_manual_reconnect_hard_restarts_after_suspend_failure():
    events = []
    subject = make_controller(
        FakeDaemon(events, suspend_error=RuntimeError("stale session"))
    )
    with patch.object(subject, "_hard_restart") as hard_restart:
        asyncio.run(subject._do_resume())

    assert events == ["suspend"]
    hard_restart.assert_called_once()
    assert hard_restart.call_args.kwargs["power_off"] is True


def test_manual_reconnect_hard_restarts_after_stale_suspend_timeout():
    events = []
    subject = make_controller(FakeDaemon(events, suspend_hangs=True))
    with patch.object(controller, "MANUAL_RECONNECT_SUSPEND_TIMEOUT", 0.01), \
            patch.object(subject, "_hard_restart") as hard_restart:
        asyncio.run(subject._do_resume())

    assert events == ["suspend"]
    hard_restart.assert_called_once()
    assert hard_restart.call_args.kwargs["power_off"] is True


def test_watchdog_hard_restarts_only_while_request_is_pending():
    subject = make_controller(FakeDaemon([]))
    pending = concurrent.futures.Future()
    with patch.object(subject, "_hard_restart") as hard_restart:
        subject._manual_reconnect_watchdog_expired(pending)
        hard_restart.assert_called_once()
        assert hard_restart.call_args.kwargs["power_off"] is False

        hard_restart.reset_mock()
        pending.set_result(None)
        subject._manual_reconnect_watchdog_expired(pending)
        hard_restart.assert_not_called()


def test_repeated_manual_reconnect_requests_are_coalesced():
    subject = make_controller(FakeDaemon([]))
    subject.loop = object()
    future = concurrent.futures.Future()
    timers = []

    class FakeTimer:
        def __init__(self, interval, function, args):
            self.interval = interval
            self.function = function
            self.args = args
            self.started = False
            self.cancelled = False
            timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

    def schedule(coro, loop):
        coro.close()
        assert loop is subject.loop
        return future

    with patch.object(controller.asyncio, "run_coroutine_threadsafe", schedule), \
            patch.object(controller.threading, "Timer", FakeTimer):
        subject.request_connect()
        subject.request_connect()

    assert len(timers) == 1
    assert timers[0].started is True
    assert subject._manual_reconnect_future is future


def test_hard_restart_powers_off_before_process_exit():
    events = []
    subject = make_controller(FakeDaemon(events))
    with patch.object(controller, "chip", return_value=FakeChip(events)), \
            patch.object(controller.os, "_exit", Mock()) as exit_process:
        subject._hard_restart("wedged", power_off=True)

    assert events == ["power_off"]
    exit_process.assert_called_once_with(1)


def test_hard_restart_is_idempotent():
    subject = make_controller(FakeDaemon([]))
    with patch.object(controller.os, "_exit", Mock()) as exit_process:
        subject._hard_restart("first", power_off=False)
        subject._hard_restart("second", power_off=False)

    exit_process.assert_called_once_with(1)
