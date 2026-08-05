from unittest import TestCase
from unittest.mock import patch

from solara_wms.wms import b2b_returns


class _Row:
    def __init__(self, **values):
        self.__dict__.update(values)


class _Doc:
    def __init__(self, items):
        self.items = items


class TestB2BReturnControls(TestCase):
    def _doc(self, expected=10):
        return _Doc([_Row(
            item_code="SOL-CI-101", expected_qty=expected, received_qty=0,
            good_qty=0, repairable_qty=0, scrap_qty=0, investigation_qty=0,
            warehouse_finding=None, accessories_complete=0, visual_pass=0,
            power_test="Not Applicable", function_test="Not Applicable", notes=None,
        )])

    def _throws(self):
        return patch.object(
            b2b_returns.frappe, "throw",
            side_effect=lambda message, *args, **kwargs: (_ for _ in ()).throw(
                ValueError(message)),
        )

    def test_bucket_total_must_equal_received(self):
        with self._throws(), self.assertRaisesRegex(ValueError, "must equal Received"):
            b2b_returns._validate_results(self._doc(), [{
                "item_code": "SOL-CI-101", "received_qty": 5,
                "good_qty": 4, "repairable_qty": 0, "scrap_qty": 0,
                "investigation_qty": 0,
            }])

    def test_excess_must_stay_in_investigation(self):
        with self._throws(), self.assertRaisesRegex(ValueError, "above the expected"):
            b2b_returns._validate_results(self._doc(expected=4), [{
                "item_code": "SOL-CI-101", "received_qty": 5,
                "good_qty": 5, "repairable_qty": 0, "scrap_qty": 0,
                "investigation_qty": 0, "accessories_complete": 1,
                "visual_pass": 1, "power_test": "Not Applicable",
                "function_test": "Not Applicable",
            }])

    def test_good_requires_complete_qc(self):
        with self._throws(), self.assertRaisesRegex(ValueError, "complete accessories"):
            b2b_returns._validate_results(self._doc(), [{
                "item_code": "SOL-CI-101", "received_qty": 5,
                "good_qty": 5, "repairable_qty": 0, "scrap_qty": 0,
                "investigation_qty": 0, "accessories_complete": 0,
                "visual_pass": 1, "power_test": "Not Applicable",
                "function_test": "Not Applicable",
            }])

    def test_short_return_prepares_received_stock_and_exception(self):
        rows, exceptions = b2b_returns._validate_results(self._doc(expected=10), [{
            "item_code": "SOL-CI-101", "received_qty": 8,
            "good_qty": 6, "repairable_qty": 1, "scrap_qty": 1,
            "investigation_qty": 0, "accessories_complete": 1,
            "visual_pass": 1, "power_test": "Not Applicable",
            "function_test": "Not Applicable", "warehouse_finding": "Transit damage",
        }])
        self.assertEqual([(row["condition"], row["qty"]) for row in rows],
                         [("Good", 6), ("Repairable", 1), ("Scrap", 1)])
        self.assertEqual(len(exceptions), 1)
        self.assertIn("short 2", exceptions[0])

    def test_excess_can_be_quarantined_as_investigation(self):
        rows, exceptions = b2b_returns._validate_results(self._doc(expected=4), [{
            "item_code": "SOL-CI-101", "received_qty": 5,
            "good_qty": 4, "repairable_qty": 0, "scrap_qty": 0,
            "investigation_qty": 1, "accessories_complete": 1,
            "visual_pass": 1, "power_test": "Not Applicable",
            "function_test": "Not Applicable", "warehouse_finding": "Excess quantity",
        }])
        self.assertEqual(rows, [{"item_code": "SOL-CI-101", "qty": 4,
                                 "condition": "Good"}])
        self.assertTrue(any("excess 1" in reason for reason in exceptions))
        self.assertTrue(any("investigation" in reason for reason in exceptions))

    def test_parser_rejects_wrong_json_shape(self):
        self.assertEqual(b2b_returns._parse('[{"x": 1}]', list, []), [{"x": 1}])
        self.assertEqual(b2b_returns._parse('{"x": 1}', list, []), [])
