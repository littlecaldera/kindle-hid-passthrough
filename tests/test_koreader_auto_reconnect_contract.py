#!/usr/bin/env python3
"""Contract checks for KOReader's additive auto-reconnect hooks."""

from pathlib import Path


PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "koreader-plugin"
    / "hidpassthrough.koplugin"
    / "main.lua"
)


def test_auto_reconnect_is_health_gated_and_one_shot():
    source = PLUGIN.read_text(encoding="utf-8")

    assert 'function HIDPassthrough:_autoReconnectIfNeeded(reason, after_spawn)' in source
    assert 'status.daemon_running ~= true' in source
    assert 'self:_daemonWanted()' in source
    assert 'self:_spawnBinary()' in source
    assert 'self.AUTO_RECONNECT_SPAWN_DELAY' in source
    assert '(status.device_count or 0) < 1' in source
    assert 'status.connected_device and status.hid_ready == true' in source
    assert 'self:_httpGetJson("/start")' in source
    assert 'UIManager:scheduleIn(delay or self.AUTO_RECONNECT_DELAY' in source


def test_startup_and_resume_only_schedule_the_new_check():
    source = PLUGIN.read_text(encoding="utf-8")

    assert 'function HIDPassthrough:onResume()' in source
    assert 'self:_scheduleAutoReconnect("resume")' in source
    assert 'self:_scheduleAutoReconnect("KOReader startup")' in source


def test_daemon_preference_defaults_on_and_remembers_manual_changes():
    source = PLUGIN.read_text(encoding="utf-8")

    assert 'DAEMON_ENABLED_SETTING = "hidpassthrough_daemon_enabled"' in source
    assert 'if wanted == nil then return true end' in source
    assert 'function HIDPassthrough:startRemembered()' in source
    assert 'self:_rememberDaemonWanted(true)' in source
    assert 'function HIDPassthrough:stopRemembered()' in source
    assert 'self:_rememberDaemonWanted(false)' in source
    assert 'self:_runActionAsync(_("Starting HID Passthrough daemon…"), self.startRemembered)' in source
    assert 'self:_runActionAsync(_("Stopping HID Passthrough daemon…"), self.stopRemembered)' in source


def test_input_recovery_is_finite_and_manual_reconnect_remains_available():
    source = PLUGIN.read_text(encoding="utf-8")

    assert 'INPUT_RECOVERY_DELAYS = { 1, 3, 7 }' in source
    assert 'function HIDPassthrough:_scheduleInputRecovery(reason)' in source
    assert 'self:_scanInputs(true)' in source
    assert 'function HIDPassthrough:reconnectPairedDevices()' in source
    assert 'text = _("重新连接已配对设备")' in source
    assert 'self:_httpGetJson("/start")' in source
    assert '正在重新连接翻页器' in source
    assert 'free2' not in source.lower()


if __name__ == "__main__":
    test_auto_reconnect_is_health_gated_and_one_shot()
    test_startup_and_resume_only_schedule_the_new_check()
    test_daemon_preference_defaults_on_and_remembers_manual_changes()
    test_input_recovery_is_finite_and_manual_reconnect_remains_available()
    print("4/4 passed")
