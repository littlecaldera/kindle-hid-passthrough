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

    assert 'function HIDPassthrough:_autoReconnectIfNeeded(reason)' in source
    assert 'status.daemon_running ~= true' in source
    assert '(status.device_count or 0) < 1' in source
    assert 'status.connected_device and status.hid_ready == true' in source
    assert 'self:_httpGetJson("/start")' in source
    assert 'UIManager:scheduleIn(self.AUTO_RECONNECT_DELAY' in source


def test_startup_and_resume_only_schedule_the_new_check():
    source = PLUGIN.read_text(encoding="utf-8")

    assert 'function HIDPassthrough:onResume()' in source
    assert 'self:_scheduleAutoReconnect("resume")' in source
    assert 'self:_scheduleAutoReconnect("KOReader startup")' in source


if __name__ == "__main__":
    test_auto_reconnect_is_health_gated_and_one_shot()
    test_startup_and_resume_only_schedule_the_new_check()
    print("2/2 passed")
