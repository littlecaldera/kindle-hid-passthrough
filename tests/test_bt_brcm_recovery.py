#!/usr/bin/env python3
"""Unit tests for Kindle Broadcom reconnect recovery."""

import os
import sys
import threading
from unittest.mock import mock_open, patch

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'kindle_hid_passthrough'))

import bt_brcm  # noqa: E402


class FakeLatency:
    def __init__(self):
        self.releases = 0

    def release(self):
        self.releases += 1


def chip_for_test(probes):
    chip = object.__new__(bt_brcm.BrcmChip)
    chip._power_lock = threading.RLock()
    chip._warm = True
    chip._latency = FakeLatency()
    chip._probe_awake = lambda: next(probes)
    return chip


def test_live_controller_does_not_rewarm():
    chip = chip_for_test(iter([True]))
    chip.power_off = lambda: (_ for _ in ()).throw(
        AssertionError('unexpected power off'))
    chip.prepare = lambda: (_ for _ in ()).throw(
        AssertionError('unexpected prepare'))
    chip.pre_open()


def test_dead_controller_gets_one_full_rewarm():
    chip = chip_for_test(iter([False, True]))
    calls = []
    chip.power_off = lambda: calls.append('off')
    chip.prepare = lambda: calls.append('prepare') or True
    chip.pre_open()
    assert calls == ['off', 'prepare'], calls


def test_failed_rewarm_aborts_before_bumble_open():
    chip = chip_for_test(iter([False, False]))
    calls = []

    def power_off():
        calls.append('off')
        chip._warm = False

    chip.power_off = power_off
    chip.prepare = lambda: calls.append('prepare') or True
    try:
        chip.pre_open()
        raise AssertionError('pre_open should have failed')
    except RuntimeError as error:
        assert 'still does not answer HCI' in str(error), error
    assert calls == ['off', 'prepare', 'off'], calls
    assert chip._warm is False


def test_power_off_restarts_frozen_btd_even_if_already_disabled():
    chip = chip_for_test(iter(()))
    with patch('builtins.open', mock_open(read_data='0')), \
            patch.object(bt_brcm, '_restart_btd') as restart:
        chip.power_off()
    restart.assert_called_once_with()
    assert chip._warm is False
    assert chip._latency.releases == 1


def test_suspend_power_off_only_resumes_btd():
    chip = chip_for_test(iter(()))
    with patch('builtins.open', mock_open(read_data='1')), \
            patch.object(bt_brcm, '_resume_btd') as resume, \
            patch.object(bt_brcm, '_restart_btd') as restart:
        chip.suspend_power_off()
    resume.assert_called_once_with()
    restart.assert_not_called()
    assert chip._warm is False
    assert chip._latency.releases == 1


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failed = 0
    for test in tests:
        try:
            test()
            print(f'ok   {test.__name__}')
        except Exception as error:
            failed += 1
            print(f'FAIL {test.__name__}: {error}')
    print(f'\n{len(tests) - failed}/{len(tests)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
