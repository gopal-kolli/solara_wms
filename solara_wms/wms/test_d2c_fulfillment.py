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
