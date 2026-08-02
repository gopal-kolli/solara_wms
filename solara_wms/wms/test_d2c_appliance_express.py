from unittest import TestCase

from solara_wms.wms.d2c_appliance_express import classify_lines


def row(code, qty=1, bundle=None):
    return {"item_code": code, "qty": qty, "bundle": bundle}


class TestApplianceExpressEligibility(TestCase):
    def test_bare_appliance_is_express(self):
        out = classify_lines([row("SOL-AF-501")])
        self.assertTrue(out["eligible"])
        self.assertEqual(out["kind"], "single_appliance")

    def test_pre_kitted_afo_combo_is_express(self):
        bundle = "SOL-AF-501-SIL-BAS-P6-SPY-101"
        out = classify_lines([row("SOL-AF-501", bundle=bundle),
                              row("SOL-SPY-101", bundle=bundle),
                              row("SOL-AF-SIL-BASKET-P6", bundle=bundle)])
        self.assertTrue(out["eligible"])
        self.assertEqual(out["kind"], "pre_kitted_combo")

    def test_loose_addon_combo_stays_normal(self):
        out = classify_lines([row("SOL-AF-501"), row("SOL-SPY-101")])
        self.assertFalse(out["eligible"])

    def test_unknown_bundle_stays_normal(self):
        out = classify_lines([row("SOL-AF-501", bundle="OTHER"),
                              row("SOL-SPY-101", bundle="OTHER")])
        self.assertFalse(out["eligible"])

    def test_multi_box_stays_normal(self):
        out = classify_lines([row("SOL-AF-501")], box_count=2)
        self.assertFalse(out["eligible"])

    def test_two_appliances_stay_normal(self):
        out = classify_lines([row("SOL-AF-501", qty=2)])
        self.assertFalse(out["eligible"])
