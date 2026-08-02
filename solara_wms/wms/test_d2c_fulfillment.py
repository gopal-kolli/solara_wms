from unittest import TestCase
from unittest.mock import MagicMock, patch

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


class TestOpdReplacementEvidence(TestCase):
    def _approved_so(self, **overrides):
        snapshot = {
            "version": 1,
            "resolution_id": "RES-001",
            "resolution_type": "replacement",
            "approval_status": "captain_approved",
            "approved_by": "captain@solara.in",
            "approved_at": "2026-08-02T10:00:00+05:30",
            "original_order_id": "SOL123",
            "customer": "CUST-1",
            "shipping_address_name": "ADDR-1",
            "alternate_address": None,
            "items": [{"item_code": "SOL-APP-X", "qty": 1, "rate": 0}],
        }
        raw = fulfillment.json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        values = dict(
            name="REP-2627-00001", po_no="RES-001", customer="CUST-1",
            shipping_address_name="ADDR-1", net_total=0, grand_total=0,
            taxes_and_charges=None, taxes=[],
            custom_opd_resolution_id="RES-001",
            custom_opd_approved_by="captain@solara.in",
            custom_opd_approved_at="2026-08-02 10:00:00",
            custom_opd_approval_snapshot=raw,
            custom_opd_approval_hash=fulfillment._canonical_snapshot_hash(raw),
            items=[_Row(item_code="SOL-APP-X", qty=1, rate=0,
                        gst_treatment="Nil-Rated")],
        )
        values.update(overrides)
        return _Row(**values)

    def test_valid_approval_evidence_passes(self):
        self.assertEqual(
            fulfillment._validate_opd_replacement(
                self._approved_so(), "Main Warehouse - WTBBPL", require_stock=False),
            [],
        )

    def test_tampered_snapshot_is_rejected(self):
        so = self._approved_so()
        so.custom_opd_approval_snapshot += " "
        errors = fulfillment._validate_opd_replacement(
            so, "Main Warehouse - WTBBPL", require_stock=False)
        self.assertIn("approval_snapshot_hash_mismatch", errors)

    def test_nonzero_or_non_nil_line_is_rejected(self):
        so = self._approved_so(
            grand_total=499,
            items=[_Row(item_code="SOL-APP-X", qty=1, rate=499,
                        gst_treatment="Taxable")],
        )
        errors = fulfillment._validate_opd_replacement(
            so, "Main Warehouse - WTBBPL", require_stock=False)
        self.assertIn("replacement_not_zero_value", errors)
        self.assertIn("non_zero_item_rate", errors)
        self.assertIn("item_not_nil_rated", errors)

    def test_alternate_address_is_rejected(self):
        so = self._approved_so()
        snapshot = fulfillment.json.loads(so.custom_opd_approval_snapshot)
        snapshot["alternate_address"] = {"address_line1": "Different"}
        raw = fulfillment.json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        so.custom_opd_approval_snapshot = raw
        so.custom_opd_approval_hash = fulfillment._canonical_snapshot_hash(raw)
        errors = fulfillment._validate_opd_replacement(
            so, "Main Warehouse - WTBBPL", require_stock=False)
        self.assertIn("alternate_address_requires_manual_review", errors)

    def test_tax_template_is_rejected_even_when_total_is_zero(self):
        so = self._approved_so(taxes_and_charges="Shopify IGST 18% Inclusive - WTBBPL")
        errors = fulfillment._validate_opd_replacement(
            so, "Main Warehouse - WTBBPL", require_stock=False)
        self.assertIn("replacement_tax_template_or_amount_present", errors)

    @patch.object(fulfillment, "_settings")
    @patch.object(fulfillment.frappe, "get_all")
    def test_release_gate_off_does_not_query_orders(self, get_all, settings):
        settings.return_value = frappe._dict(opd_replacement_release_enabled=0)
        self.assertIsNone(fulfillment._release_opd_replacements())
        get_all.assert_not_called()


class TestOpdReplacementWaveScope(TestCase):
    @patch.object(fulfillment.frappe, "get_all")
    @patch.object(fulfillment.frappe, "get_meta")
    def test_wave_selects_shopify_or_replacement_dns(self, get_meta, get_all):
        meta = MagicMock()
        meta.has_field.return_value = True
        get_meta.return_value = meta
        get_all.return_value = []

        fulfillment._todays_d2c_dns(frappe._dict(prepare_lookback_days=1),
                                    "2026-08-02")

        call = get_all.call_args_list[0]
        self.assertEqual(call.kwargs["or_filters"]["is_replacement"], 1)
        self.assertEqual(call.kwargs["or_filters"]["shopify_order_id"], ["is", "set"])


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


class TestAwbCourierPairs(TestCase):
    """Every parcel of a multi-box order must be discoverable from the DN.

    The pick list prints one line per pair; printing only `awb_number` (as it did
    until 2026-07-28) under-reported 251 AWBs in a single 1,309-order batch, so the
    sheet disagreed with the labels PDF and the floor de-dup'd real parcels away.
    """

    def test_single_awb(self):
        pairs = fulfillment._awb_courier_pairs(
            {"awb_number": "SF3720543984OLL", "courier_partner": "Shadowfax"})
        self.assertEqual([a for a, _ in pairs], ["SF3720543984OLL"])

    def test_second_parcel_via_custom_awb_2(self):
        pairs = fulfillment._awb_courier_pairs({
            "awb_number": "SF3720543984OLL", "courier_partner": "Shadowfax",
            "custom_awb_2": "SF3720543400OLL"})
        self.assertEqual([a for a, _ in pairs],
                         ["SF3720543984OLL", "SF3720543400OLL"])

    def test_awb_list_json_is_authoritative(self):
        pairs = fulfillment._awb_courier_pairs({
            "awb_number": "IGNORED", "courier_partner": "Shadowfax",
            "custom_awb_list": '[{"awb": "A1", "courier": "Shadowfax"},'
                               ' {"awb": "A2", "courier": "Shadowfax"},'
                               ' {"awb": "A3", "courier": "Delhivery"}]'})
        self.assertEqual([a for a, _ in pairs], ["A1", "A2", "A3"])

    def test_comma_separated_awb_number(self):
        pairs = fulfillment._awb_courier_pairs(
            {"awb_number": "A1,A2", "courier_partner": "Delhivery"})
        self.assertEqual([a for a, _ in pairs], ["A1", "A2"])

    def test_duplicates_collapse(self):
        """Same waybill twice is ONE parcel — must not inflate the Box count."""
        pairs = fulfillment._awb_courier_pairs({
            "awb_number": "A1", "courier_partner": "Shadowfax",
            "custom_awb_2": "A1"})
        self.assertEqual([a for a, _ in pairs], ["A1"])

    def test_no_awb_yet(self):
        self.assertEqual(
            fulfillment._awb_courier_pairs({"awb_number": None}), [])

    def test_awb_list_beats_stale_awb_2(self):
        """3+ box orders: custom_awb_2 only ever holds parcel 2, so a reader that
        stops at it silently drops parcels 3..N. That is exactly how the legacy
        repush cron truncated 133 orders / 150 tracking numbers in July 2026."""
        dn = {"awb_number": "A1", "courier_partner": "Shadowfax",
              "custom_awb_2": "A2",
              "custom_awb_list": '[{"awb": "A1", "courier": "Shadowfax"},'
                                 ' {"awb": "A2", "courier": "Shadowfax"},'
                                 ' {"awb": "A3", "courier": "Shadowfax"}]'}
        self.assertEqual([a for a, _ in fulfillment._awb_courier_pairs(dn)],
                         ["A1", "A2", "A3"])

    def test_bad_awb_list_json_falls_back(self):
        """Corrupt JSON must degrade to the legacy pair, never to zero parcels."""
        pairs = fulfillment._awb_courier_pairs({
            "awb_number": "A1", "courier_partner": "Shadowfax",
            "custom_awb_2": "A2", "custom_awb_list": "{not json"})
        self.assertEqual([a for a, _ in pairs], ["A1", "A2"])


class TestRepairTracking(TestCase):
    """A short fulfillment must be healable, and only when it is OURS.

    Until 2026-07-29 push_shopify_fulfillment was create-only: it correctly
    detected that Shopify was missing an AWB, then returned "no_open_fo" (the
    order being already fulfilled) and gave up — silently, uncounted. Combined
    with the one-way custom_shopify_fulfilled latch, parcels 3..N were never
    recoverable without a manual backfill.
    """

    def _resp(self, numbers, errors=None):
        return {"data": {"fulfillmentTrackingInfoUpdateV2": {
            "fulfillment": {"id": "gid://shopify/Fulfillment/1",
                            "trackingInfo": [{"number": n} for n in numbers]},
            "userErrors": errors or []}}}

    def _run(self, resp, awbs):
        """Drive _repair_tracking against a stubbed Shopify response.
        requests.post is patched out so the test never touches the network."""
        with patch("requests.post", return_value=None), \
                patch.object(fulfillment, "_shopify_json", return_value=resp):
            return fulfillment._repair_tracking(
                {"name": "DN-TEST"}, {}, "https://shop/graphql.json", "1",
                awbs, ["u"] * len(awbs), "Shadowfax")

    def test_repair_confirms_every_awb(self):
        self.assertEqual(self._run(self._resp(["A1", "A2", "A3"]),
                                   ["A1", "A2", "A3"]), "repaired")

    def test_partial_landing_is_a_failure_not_a_success(self):
        """Shopify echoing back only 2 of 3 must NOT be latched as fulfilled —
        that is the truncation bug reasserting itself one layer down."""
        self.assertEqual(self._run(self._resp(["A1", "A2"]),
                                   ["A1", "A2", "A3"]), "failed")

    def test_user_errors_are_a_failure(self):
        self.assertEqual(
            self._run(self._resp(["A1", "A2", "A3"], errors=[{"message": "nope"}]),
                      ["A1", "A2", "A3"]), "failed")

    def test_throttled_response_is_a_failure(self):
        self.assertEqual(self._run(None, ["A1", "A2", "A3"]), "failed")


class TestReprintBatchRelinks(TestCase):
    """A reprint must be a DROP-IN replacement for the batch's existing links.

    Before 2026-07-28 it attached nothing and re-pointed nothing, so:
      - unchanged content reused the same file_url and the new UNATTACHED File doc
        shadowed the attached one -> permission fell back to owner-only -> the
        warehouse dashboard got 403 on a batch that had been working;
      - changed content got a suffixed url nothing referenced -> the dashboard
        kept serving the stale PDF.
    """

    @patch.object(fulfillment.frappe.db, "commit")
    @patch.object(fulfillment.frappe.db, "set_value")
    @patch.object(fulfillment, "_attach_outputs_to_batch")
    @patch.object(fulfillment, "_render_batch_files")
    @patch.object(fulfillment, "_todays_d2c_dns")
    @patch.object(fulfillment, "_settings")
    @patch.object(fulfillment.frappe, "get_doc")
    def test_reprint_attaches_outputs_and_repoints_batch(
        self, get_doc, settings, todays, render, attach, set_value, _commit
    ):
        batch = _Row(name="D2CB-2026-07-28-050", date="2026-07-28", batch_no=1,
                     batch_stamp="07280902",
                     delivery_notes=[_Row(delivery_note="DN-1")])
        get_doc.return_value = batch
        settings.return_value = {}
        todays.return_value = [{"name": "DN-1", "shopify_order_number": "SOL1"}]
        render.return_value = {
            "pick_list_url": "/private/files/pick-NEW.pdf",
            "labels_pdf_url": "/private/files/labels-NEW.pdf",
            "missing_labels": [], "labelled": 1,
        }

        out = fulfillment.reprint_batch("D2CB-2026-07-28-050")

        # the regenerated files must be linked to the batch (else 403 for everyone
        # but Administrator)
        attach.assert_called_once_with("D2CB-2026-07-28-050", render.return_value)
        # ...and the batch must now POINT at them (else the dashboard proxy, which
        # resolves via these fields, keeps serving the stale file)
        set_value.assert_called_once()
        args = set_value.call_args.args
        self.assertEqual(args[0], "D2C Prepare Batch")
        self.assertEqual(args[1], "D2CB-2026-07-28-050")
        self.assertEqual(args[2], {"pick_list_url": "/private/files/pick-NEW.pdf",
                                   "labels_pdf_url": "/private/files/labels-NEW.pdf"})
        self.assertEqual(out["orders"], 1)

    @patch.object(fulfillment.frappe.db, "commit")
    @patch.object(fulfillment.frappe.db, "set_value")
    @patch.object(fulfillment, "_attach_outputs_to_batch")
    @patch.object(fulfillment, "_render_batch_files")
    @patch.object(fulfillment, "_todays_d2c_dns")
    @patch.object(fulfillment, "_settings")
    @patch.object(fulfillment.frappe, "get_doc")
    def test_reprint_does_not_blank_urls_when_nothing_rendered(
        self, get_doc, settings, todays, render, attach, set_value, _commit
    ):
        """All labels pending -> no pick list. Must NOT null out the batch's
        existing links, which would strand the floor with no sheet at all."""
        batch = _Row(name="D2CB-2026-07-28-050", date="2026-07-28", batch_no=1,
                     batch_stamp="07280902",
                     delivery_notes=[_Row(delivery_note="DN-1")])
        get_doc.return_value = batch
        settings.return_value = {}
        todays.return_value = [{"name": "DN-1", "shopify_order_number": "SOL1"}]
        render.return_value = {"pick_list_url": None, "labels_pdf_url": None,
                               "missing_labels": ["SOL1"], "labelled": 0}

        fulfillment.reprint_batch("D2CB-2026-07-28-050")

        set_value.assert_not_called()


def _pdn(name, pieces, run="X"):
    """Minimal DN dict for partition tests: _lines carries the piece count,
    _sortkey[1] the SKU-run group (what the boundary-slide keys on)."""
    return {"name": name, "_lines": [{"qty": pieces}], "_sortkey": (0, run, name)}


class TestPartitionForPackLines(TestCase):
    """Contiguous, piece-balanced split of the pack sequence across N lines.

    Contiguity is load-bearing: it keeps a multi-box order's labels inside one
    line's stack and preserves the single-SKU assembly-line runs."""

    def test_partition_is_exact_and_contiguous(self):
        dns = [_pdn(f"DN-{i}", 1) for i in range(10)]
        chunks = fulfillment._partition_for_pack_lines(dns, 4)
        flat = [d["name"] for c in chunks for d in c]
        self.assertEqual(flat, [d["name"] for d in dns])   # order preserved, none lost/duplicated
        self.assertEqual(len(chunks), 4)
        self.assertTrue(all(c for c in chunks))

    def test_balances_by_pieces_not_orders(self):
        # one 12-piece combo + twelve 1-piece orders, 2 lines:
        # by ORDERS the combo side would get 6 more orders; by PIECES it stands alone-ish
        dns = [_pdn("BIG", 12, run="combo")] + [_pdn(f"S{i}", 1, run=f"r{i}") for i in range(12)]
        chunks = fulfillment._partition_for_pack_lines(dns, 2)
        p = [sum(fulfillment._dn_pieces(d) for d in c) for c in chunks]
        self.assertEqual(len(chunks), 2)
        self.assertLessEqual(abs(p[0] - p[1]), 3)

    def test_run_boundary_slide(self):
        # greedy cut would land mid-run; the boundary slides (<=3) to finish the run
        dns = ([_pdn(f"A{i}", 1, run="AF-501") for i in range(6)]
               + [_pdn(f"B{i}", 1, run="BLN-401") for i in range(4)])
        chunks = fulfillment._partition_for_pack_lines(dns, 2)
        first_runs = {d["_sortkey"][1] for d in chunks[0]}
        self.assertEqual(first_runs, {"AF-501"})   # run not split across lines

    def test_n_leq_one_is_passthrough(self):
        dns = [_pdn("A", 1), _pdn("B", 2)]
        self.assertEqual(len(fulfillment._partition_for_pack_lines(dns, 1)), 1)
        self.assertEqual(len(fulfillment._partition_for_pack_lines(dns, 0)), 1)
        self.assertEqual(fulfillment._partition_for_pack_lines([], 4), [])

    def test_more_lines_than_orders_degenerates(self):
        dns = [_pdn(f"D{i}", 1, run=f"r{i}") for i in range(3)]
        chunks = fulfillment._partition_for_pack_lines(dns, 4)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) == 1 for c in chunks))
