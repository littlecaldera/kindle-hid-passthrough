import ast
import unittest
from pathlib import Path


BLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "kindle_hid_passthrough"
    / "ble.py"
)
HOST_PATH = (
    Path(__file__).resolve().parents[1]
    / "kindle_hid_passthrough"
    / "host.py"
)


def _ble_mixin_methods():
    tree = ast.parse(BLE_PATH.read_text(encoding="utf-8"))
    host = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BLEMixin"
    )
    return {
        node.name: node
        for node in host.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _class_methods(path, class_name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    host = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name: node
        for node in host.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _self_calls(method):
    return [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]


class BLEReconnectRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.methods = _ble_mixin_methods()
        cls.host_methods = _class_methods(HOST_PATH, "HIDHost")

    def test_reconnect_discovery_method_exists(self):
        self.assertIn("_discover_ble_hid_service", self.methods)

    def test_setup_resets_connection_state_and_rediscovers_reports(self):
        setup = self.methods["_setup_ble_hid"]
        self.assertTrue(
            any(
                call.func.attr == "_stop_ble_hid_keepalive"
                for call in _self_calls(setup)
            )
        )
        reset_fields = {
            target.attr
            for node in ast.walk(setup)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        }
        self.assertTrue(
            {"hid_reports", "_ble_hid_control_point", "_ble_keepalive_characteristic"}
            <= reset_fields
        )

        discovery = next(
            call
            for call in _self_calls(setup)
            if call.func.attr == "_discover_ble_hid_service"
        )
        process_reports = next(
            keyword.value
            for keyword in discovery.keywords
            if keyword.arg == "process_reports"
        )
        self.assertIsInstance(process_reports, ast.Constant)
        self.assertIs(process_reports.value, True)

    def test_notification_callback_keeps_report_id_before_payload(self):
        subscribe = self.methods["_subscribe_to_ble_reports"]
        callback_call = next(
            call
            for call in _self_calls(subscribe)
            if call.func.attr == "_on_ble_hid_report"
        )
        self.assertEqual([arg.id for arg in callback_call.args], ["rid", "value"])

    def test_keepalive_uses_control_point_and_active_gatt_read(self):
        keepalive = self.methods["_ble_hid_keepalive_loop"]
        called_attributes = {
            node.func.attr
            for node in ast.walk(keepalive)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("write_value", called_attributes)
        self.assertIn("read_value", called_attributes)
        self.assertGreaterEqual(
            sum(
                1
                for node in ast.walk(keepalive)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "asyncio"
                and node.func.attr == "wait_for"
            ),
            2,
        )

    def test_disconnect_cancels_keepalive_and_cleanup_stops_it(self):
        disconnect = self.host_methods["_on_disconnection"]
        cleanup = self.host_methods["cleanup"]
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "cancel"
                for node in ast.walk(disconnect)
            )
        )
        self.assertTrue(
            any(
                call.func.attr == "_stop_ble_hid_keepalive"
                for call in _self_calls(cleanup)
            )
        )


if __name__ == "__main__":
    unittest.main()
