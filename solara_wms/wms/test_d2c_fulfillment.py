from unittest import TestCase
from unittest.mock import patch

import frappe

from solara_wms.wms import d2c_fulfillment as fulfillment


class TestD2CPrepareBatch(TestCase):
    @patch.object(fulfillment.frappe, "get_all")
    def test_only_successfully_labelled_rows_block_future_batches(self, get_all):
        get_all.side_effect = [
            [frappe._dict(name="D2CB-2026-07-23-001")],
            [frappe._dict(delivery_note="SHPDN27-00001")],
        ]

        result = fulfillment._batched_dn_names("2026-07-23", lookback=1)

        self.assertEqual(result, {"SHPDN27-00001"})
        child_filters = get_all.call_args_list[1].kwargs["filters"]
        self.assertEqual(child_filters["label_found"], 1)

    @patch.object(fulfillment, "_build_pick_list_pdf")
    @patch.object(fulfillment, "_build_combined_labels_pdf")
    def test_pick_list_uses_exact_labelled_subset(self, build_labels, build_pick):
        dns = [
            {"name": "DN-READY", "shopify_order_number": "SOL1"},
            {"name": "DN-PENDING", "shopify_order_number": "SOL2"},
        ]
        build_labels.return_value = ("/private/files/labels.pdf", ["SOL2"])
        build_pick.return_value = "/private/files/pick.pdf"

        result = fulfillment._render_batch_files(
            dns, "2026-07-23", 1, "07231200"
        )

        printable = build_pick.call_args.args[0]
        self.assertEqual([d["name"] for d in printable], ["DN-READY"])
        self.assertEqual(result["labelled"], 1)
        self.assertEqual(result["missing_labels"], ["SOL2"])

    @patch.object(fulfillment, "_build_pick_list_pdf")
    @patch.object(fulfillment, "_build_combined_labels_pdf")
    def test_no_pick_list_when_every_label_is_pending(
        self, build_labels, build_pick
    ):
        dns = [{"name": "DN-PENDING", "shopify_order_number": "SOL2"}]
        build_labels.return_value = (None, ["SOL2"])

        result = fulfillment._render_batch_files(
            dns, "2026-07-23", 1, "07231200"
        )

        build_pick.assert_not_called()
        self.assertIsNone(result["pick_list_url"])
        self.assertEqual(result["labelled"], 0)


class _Row:
    """Stand-in for a Frappe doc/child row: attribute access + .get(), and a real
    `items` attribute (frappe._dict would shadow it with dict.items)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def get(self, key, default=None):
        return self.__dict__.get(key, default)


def _so(*lines):
    return _Row(items=[_Row(item_code=code, qty=qty) for code, qty in lines])


# Item.custom_boxes_per_unit for the SKUs under test (0 = nestable/virtual rider,
# 1 = own box, 2 = known combo split into 2 children).
_BOXES = {
    "SOL-KIT-CHB-101": 1, "SOL-AF-501-SIL-BASKET-P6-SPY-101": 1,
    "WARRANTY-2YR-AFO": 0, "WARRANTY-2YR-CPJ": 0, "SOL-SPY-101": 0,
    "SOL-GIFWRAP": 0, "SOL-AF-PP-101": 0, "SOL-AF-501-CVR-BAG": 0,
    "SOL-JUC-BAG-121": 0, "SOL-TSTK-301": 0,
    "SOL-AFO-501-JUC-121": 2, "SOL-BLN-401": 1, "SOL-CI-KD-103-FP-102": 1,
    "SOL-AF-SIL-BASKET-P6-SPY-101-AF-PP-101": 1, "SOL-AF-501": 1,
    "SOL-JUC-121-GLSTUM-101": 1, "SOL-APP-X": 1,
}


class TestBoxBearingParcelCount(TestCase):
    """The jumbo guard counts BOX-BEARING parcels, not distinct lines: 0-box
    nestable accessories and virtual (warranty) lines never inflate box_count."""

    def setUp(self):
        self.settings = frappe._dict()  # empty -> defaults; split_combos default has AFO-JUC
        self._p1 = patch.object(fulfillment, "_item_boxes",
                                side_effect=lambda code, box_map: _BOXES.get((code or "").upper(), 1))
        self._p2 = patch.object(fulfillment, "_item_category", side_effect=lambda code: None)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def _bc(self, so):
        return fulfillment._order_box_count(so, {}, self.settings)

    def test_seven_lines_two_real_boxes_not_jumbo(self):
        # 7 lines, but 5 are 0-box riders (warranty, sprayer, gift-wrap, parchment,
        # cover-bag) -> 2 real boxes. Old line-count guard wrongly forced this to sheet.
        so = _so(("SOL-AF-501-SIL-BASKET-P6-SPY-101", 1), ("SOL-KIT-CHB-101", 1),
                 ("WARRANTY-2YR-AFO", 1), ("SOL-SPY-101", 1), ("SOL-GIFWRAP", 1),
                 ("SOL-AF-PP-101", 1), ("SOL-AF-501-CVR-BAG", 1))
        self.assertEqual(self._bc(so), 2)

    def test_seven_lines_four_real_boxes(self):
        so = _so(("SOL-AFO-501-JUC-121", 1), ("SOL-BLN-401", 1), ("SOL-CI-KD-103-FP-102", 1),
                 ("SOL-AF-501-CVR-BAG", 1), ("SOL-AF-PP-101", 1), ("SOL-TSTK-301", 1),
                 ("SOL-JUC-BAG-121", 1))
        self.assertEqual(self._bc(so), 4)

    def test_seven_lines_three_real_boxes_two_warranties(self):
        so = _so(("SOL-AF-SIL-BASKET-P6-SPY-101-AF-PP-101", 1), ("SOL-AF-501", 1),
                 ("SOL-JUC-121-GLSTUM-101", 1), ("WARRANTY-2YR-AFO", 1),
                 ("WARRANTY-2YR-CPJ", 1), ("SOL-JUC-BAG-121", 1), ("SOL-AF-501-CVR-BAG", 1))
        self.assertEqual(self._bc(so), 3)

    def test_single_appliance_is_one(self):
        self.assertEqual(self._bc(_so(("SOL-APP-X", 1))), 1)

    def test_known_combo_alone_is_two(self):
        self.assertEqual(self._bc(_so(("SOL-AFO-501-JUC-121", 1))), 2)

    def test_over_cap_reports_true_count(self):
        # 6 own-box appliances -> 6; the release gate routes >max_release_parcels to sheet.
        self.assertEqual(self._bc(_so(*[("SOL-APP-X", 1)] * 6)), 6)

    def test_virtual_only_floors_to_one(self):
        self.assertEqual(self._bc(_so(("WARRANTY-2YR-AFO", 1))), 1)
