import json
from unittest import TestCase
from unittest.mock import patch

from solara_wms.wms import d2c_returns as returns


class _Row:
    def __init__(self, **values):
        self.__dict__.update(values)


class TestReturnQcGuardrails(TestCase):
    def _good(self, **overrides):
        values = {
            "item_code": "SOL-AF-501",
            "received_qty": 1,
            "expected_qty": 1,
            "accessories_complete": 1,
            "visual_pass": 1,
            "power_test": "Pass",
            "function_test": "Pass",
            "serial_required": 1,
            "serial_number": "SERIAL-1",
            "serial_match": 1,
        }
        values.update(overrides)
        return _Row(**values)

    def test_good_requires_serial_match(self):
        row = self._good(serial_match=0)
        raw = {
            "accessories_complete": 1, "visual_pass": 1,
            "power_test": "Pass", "function_test": "Pass",
        }
        with patch.object(returns.frappe, "throw", side_effect=ValueError):
            with self.assertRaisesRegex(ValueError, "serial"):
                returns._validate_good(row, raw)

    def test_good_requires_every_component(self):
        row = self._good(accessories_complete=0)
        raw = {
            "accessories_complete": 0, "visual_pass": 1,
            "power_test": "Pass", "function_test": "Pass",
        }
        with patch.object(returns.frappe, "throw", side_effect=ValueError):
            with self.assertRaisesRegex(ValueError, "accessory"):
                returns._validate_good(row, raw)

    def test_good_with_full_qc_passes(self):
        row = self._good()
        raw = {
            "accessories_complete": 1, "visual_pass": 1,
            "power_test": "Pass", "function_test": "Pass",
        }
        returns._validate_good(row, raw)

    def test_json_inputs_are_type_checked(self):
        self.assertEqual(returns._as_json(json.dumps([{"x": 1}]), []), [{"x": 1}])
        self.assertEqual(returns._as_json('{"x": 1}', []), [])
        self.assertEqual(returns._as_json('not-json', {}), {})
