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

    def test_prepacked_appliance_bundle_stays_one_physical_carton(self):
        combo = "SOL-AF-501-SIL-BASKET-P6-SPY-101"
        source = SimpleNamespace(
            items=[
                SimpleNamespace(item_code="SOL-AF-501", qty=52),
                SimpleNamespace(item_code=combo, qty=248),
            ],
            packed_items=[
                SimpleNamespace(parent_item=combo, item_code="SOL-AF-501", qty=248),
                SimpleNamespace(parent_item=combo, item_code="SOL-SPY-101", qty=248),
                SimpleNamespace(parent_item=combo,
                                item_code="SOL-AF-SIL-BASKET-P6", qty=248),
            ],
        )
        masters = {
            "SOL-AF-501": SimpleNamespace(
                item_name="AF Oven 12L", is_stock_item=1, disabled=0,
                qty_per_carton=1, carton_weight_kg=0, gst_hsn_code="85167990",
                carton_length_cm=0, carton_width_cm=0, carton_height_cm=0,
            ),
            combo: SimpleNamespace(
                item_name="AFO + Basket + Sprayer", is_stock_item=0, disabled=0,
                qty_per_carton=1, carton_weight_kg=0, gst_hsn_code="85167990",
                carton_length_cm=0, carton_width_cm=0, carton_height_cm=0,
            ),
        }
        original_prekit = outbound._prekit_bundle_codes
        original_master = outbound._item_master
        original_ean = outbound._ean
        outbound._prekit_bundle_codes = lambda: {combo}
        outbound._item_master = lambda code: masters[code]
        outbound._ean = lambda code: {
            "SOL-AF-501": "8906162884118",
            combo: "8906162885917",
        }[code]
        try:
            rows = outbound._physical_items(source)
        finally:
            outbound._prekit_bundle_codes = original_prekit
            outbound._item_master = original_master
            outbound._ean = original_ean

        self.assertEqual(sum(row["expected_qty"] for row in rows), 300)
        self.assertEqual(projected_cartons(rows), 300)
        self.assertEqual(
            [(row["item_code"], row["expected_qty"], row["ean"]) for row in rows],
            [
                ("SOL-AF-501", 52.0, "8906162884118"),
                (combo, 248.0, "8906162885917"),
            ],
        )

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
