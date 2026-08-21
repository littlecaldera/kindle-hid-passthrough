#!/usr/bin/env python3
"""Regression tests for the BLE methods required by HIDHost dispatch."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _class_methods(path, class_name):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    subject = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name
        for node in subject.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_ble_mixin_implements_every_method_dispatched_by_hid_host():
    host_methods = _class_methods(
        ROOT / "kindle_hid_passthrough" / "host.py",
        "HIDHost",
    )
    ble_methods = _class_methods(
        ROOT / "kindle_hid_passthrough" / "ble.py",
        "BLEMixin",
    )

    assert "pair_device" in host_methods
    assert "continue_after_pairing" in host_methods
    assert {
        "_pair_ble",
        "_discover_ble_hid_service",
        "_continue_ble_after_pairing",
    } <= ble_methods


if __name__ == "__main__":
    test_ble_mixin_implements_every_method_dispatched_by_hid_host()
    print("1/1 passed")
