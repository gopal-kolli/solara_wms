import sys
import types
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase


if "frappe" not in sys.modules:
    fake = types.ModuleType("frappe")
    fake.whitelist = lambda *args, **kwargs: (
        (lambda fn: fn) if args == () else args[0]
    )
    fake.utils = types.ModuleType("frappe.utils")
    fake.utils.cint = lambda value: int(float(value or 0))
    fake.utils.flt = lambda value: float(value or 0)
    fake.utils.now_datetime = datetime.now
    sys.modules["frappe"] = fake
    sys.modules["frappe.utils"] = fake.utils

from solara_wms.wms.b2b_outbound import (
    _match_job_item,
    _infer_channel,
    bulk_scan_allowed,
    carton_data_status,
    projected_cartons,
)
import solara_wms.wms.b2b_outbound as outbound


class TestB2BOutboundRules(TestCase):
    def test_channel_is_inferred_from_series_or_customer(self):
        self.assertEqual(_infer_channel("REZSI27-1"), "Amazon VC")
        self.assertEqual(_infer_channel("FLKDN27-1"), "Flipkart VC")
        self.assertEqual(_infer_channel("X", "Blinkit Commerce"), "Blinkit")

    def test_missing_case_quantity_is_visible(self):
        self.assertEqual(carton_data_status(0, 0, "3924"), "Missing")

    def test_heavy_non_appliance_single_case_is_suspicious(self):
        self.assertEqual(carton_data_status(1, 12, "3924"), "Suspicious")

    def test_chapter_85_single_factory_carton_is_direct(self):
        self.assertEqual(carton_data_status(1, 12, "85167990"), "Direct Carton")

    def test_projected_cartons_uses_case_pack(self):
        rows = [
            {"expected_qty": 25, "qty_per_carton": 12, "carton_data_status": "Atlas"},
            {"expected_qty": 3, "qty_per_carton": 1, "carton_data_status": "Direct Carton"},
        ]
        self.assertEqual(projected_cartons(rows), 6)

    def test_projection_stays_unknown_when_master_data_is_not_safe(self):
        self.assertEqual(projected_cartons([
            {"expected_qty": 25, "qty_per_carton": 1, "carton_data_status": "Suspicious"},
        ]), 0)

    def test_bulk_scan_requires_exact_verified_case_pack(self):
        self.assertTrue(bulk_scan_allowed("Atlas", 12, 12))
        self.assertFalse(bulk_scan_allowed("Atlas", 12, 10))
        self.assertFalse(bulk_scan_allowed("Suspicious", 12, 12))
        self.assertFalse(bulk_scan_allowed("Missing", 0, 20))
        self.assertTrue(bulk_scan_allowed("Missing", 0, 1))

    def test_physical_barcode_must_match_an_item_on_the_po(self):
        original_get_all = getattr(outbound.frappe, "get_all", None)
        original_throw = getattr(outbound.frappe, "throw", None)
        outbound.frappe.get_all = lambda *args, **kwargs: [
            SimpleNamespace(parent="SOL-CAST-IRON-101")
        ]
        outbound.frappe.throw = lambda message: (_ for _ in ()).throw(ValueError(message))
        job = SimpleNamespace(items=[
            SimpleNamespace(item_code="SOL-TRIPLY-101", ean="8900000000001")
        ])
        try:
            with self.assertRaisesRegex(ValueError, "WRONG PRODUCT"):
                _match_job_item(job, "8900000000002")
        finally:
            if original_get_all is None:
                delattr(outbound.frappe, "get_all")
            else:
                outbound.frappe.get_all = original_get_all
            if original_throw is None:
                delattr(outbound.frappe, "throw")
            else:
                outbound.frappe.throw = original_throw

    def test_item_code_is_not_accepted_as_physical_scan_evidence(self):
        original_get_all = getattr(outbound.frappe, "get_all", None)
        original_throw = getattr(outbound.frappe, "throw", None)
        outbound.frappe.get_all = lambda *args, **kwargs: []
        outbound.frappe.throw = lambda message: (_ for _ in ()).throw(ValueError(message))
        job = SimpleNamespace(items=[
            SimpleNamespace(item_code="SOL-TRIPLY-101", ean="8900000000001")
        ])
        try:
            with self.assertRaisesRegex(ValueError, "do not type the expected SKU"):
                _match_job_item(job, "SOL-TRIPLY-101")
        finally:
            if original_get_all is None:
                delattr(outbound.frappe, "get_all")
            else:
                outbound.frappe.get_all = original_get_all
            if original_throw is None:
                delattr(outbound.frappe, "throw")
            else:
                outbound.frappe.throw = original_throw
