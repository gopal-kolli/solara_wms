from unittest import TestCase

from solara_wms.wms.d2c_pack_qc import sampling_decision, validate_scans


class TestPackQCSampling(TestCase):
    def test_sampling_is_stable_for_same_awb_and_day(self):
        lines = [{"item_code": "SKU-1", "qty": 1}]
        first = sampling_decision("AWB-1", lines, single_rate=17,
                                  multi_rate=50, day="2026-08-02")
        second = sampling_decision("AWB-1", lines, single_rate=17,
                                   multi_rate=50, day="2026-08-02")

        self.assertEqual(first, second)
        self.assertEqual(first["rate"], 17)

    def test_multi_piece_parcels_use_higher_rate(self):
        out = sampling_decision("AWB-2", [{"item_code": "SKU-1", "qty": 2}],
                                single_rate=5, multi_rate=20,
                                day="2026-08-02")

        self.assertEqual(out["rate"], 20)
        self.assertIn("Multi-piece", out["reason"])


class TestPackQCScanValidation(TestCase):
    def test_requires_exact_ean_count(self):
        req = [{"item_code": "SKU-1", "qty": 2, "barcodes": ["8901"],
                "manual_allowed": False}]

        incomplete = validate_scans(req, ["8901"], {})
        complete = validate_scans(req, ["8901", "8901"], {})

        self.assertFalse(incomplete["ok"])
        self.assertEqual(incomplete["missing"], {"SKU-1": 1})
        self.assertTrue(complete["ok"])

    def test_rejects_wrong_or_duplicate_barcode(self):
        req = [{"item_code": "SKU-1", "qty": 1, "barcodes": ["8901"],
                "manual_allowed": False}]

        wrong = validate_scans(req, ["9999"], {})
        duplicate = validate_scans(req, ["8901", "8901"], {})

        self.assertFalse(wrong["ok"])
        self.assertFalse(duplicate["ok"])
        self.assertIn("Unexpected", duplicate["message"])

    def test_manual_confirmation_only_for_allowed_contents(self):
        allowed = [{"item_code": "FREEBIE", "qty": 1, "barcodes": [],
                    "manual_allowed": True}]
        blocked = [{"item_code": "SKU-1", "qty": 1, "barcodes": ["8901"],
                    "manual_allowed": False}]

        self.assertTrue(validate_scans(allowed, [], {"FREEBIE": 1})["ok"])
        self.assertFalse(validate_scans(blocked, [], {"SKU-1": 1})["ok"])
