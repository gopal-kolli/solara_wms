import json
from unittest import TestCase
from unittest.mock import patch

import frappe

from solara_wms.wms import d2c_dispatch as dispatch
from solara_wms.wms import d2c_pack_verify as pack_verify


class _Doc:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class TestParcelPieceResolution(TestCase):
    @patch.object(pack_verify.frappe, "get_all", return_value=[])
    def test_dispatched_delivery_note_is_blocked_from_packing(self, _get_all):
        dn = _Doc(
            custom_dispatched=1,
            custom_dispatched_at="2026-08-05 14:38:01",
            custom_dispatched_by="clickpost-track",
            shopify_order_number="SOL1243084",
        )

        result = pack_verify._dispatch_hold(dn, "SF3721969088OLL")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["order"], "SOL1243084")
        self.assertIn("ALREADY DISPATCHED", result["message"])
        self.assertIn("DO NOT PACK", result["message"])

    @patch.object(
        pack_verify.frappe,
        "get_all",
        return_value=[{
            "scanned_at": "2026-08-05 15:24:02",
            "scanned_by": "security",
        }],
    )
    def test_parcel_dispatch_scan_blocks_partially_dispatched_multibox_dn(
            self, _get_all):
        dn = _Doc(custom_dispatched=0, shopify_order_number="SOL-MULTIBOX")

        result = pack_verify._dispatch_hold(dn, "AWB-BOX-1")

        self.assertEqual(result["status"], "error")
        self.assertIn("security", result["message"])

    @patch.object(pack_verify.frappe, "get_all", return_value=[])
    def test_todays_undispatched_parcel_has_no_hold(self, _get_all):
        dn = _Doc(
            custom_dispatched=0,
            posting_date=pack_verify.nowdate(),
            courier_partner="Delhivery",
            shopify_order_number="SOL-READY",
        )

        self.assertIsNone(pack_verify._dispatch_hold(dn, "AWB-READY"))

    @patch.object(
        pack_verify,
        "_cp_track",
        return_value={"29044411443061": "bucket:6|delivered"},
    )
    @patch.object(
        pack_verify,
        "_awb_courier_pairs",
        return_value=[("29044411443061", "Delhivery")],
    )
    @patch.object(pack_verify.frappe, "get_all", return_value=[])
    def test_old_delivered_awb_is_blocked_by_live_clickpost(
            self, _get_all, _pairs, cp_track):
        dn = _Doc(
            custom_dispatched=0,
            posting_date="2026-07-31",
            courier_partner="Delhivery",
            shopify_order_number="SOL1242811",
        )

        result = pack_verify._dispatch_hold(dn, "29044411443061")

        self.assertEqual(result["status"], "error")
        self.assertIn("delivered", result["message"])
        cp_track.assert_called_once_with(["29044411443061"], 4)

    @patch.object(
        pack_verify,
        "_cp_track",
        return_value={"AWB-PENDING": "bucket:1|pickup pending"},
    )
    @patch.object(
        pack_verify,
        "_awb_courier_pairs",
        return_value=[("AWB-PENDING", "Delhivery")],
    )
    @patch.object(pack_verify.frappe, "get_all", return_value=[])
    def test_old_unmoved_awb_remains_packable(
            self, _get_all, _pairs, _cp_track):
        dn = _Doc(
            custom_dispatched=0,
            posting_date="2026-07-31",
            courier_partner="Delhivery",
            shopify_order_number="SOL-PENDING",
        )

        self.assertIsNone(pack_verify._dispatch_hold(dn, "AWB-PENDING"))

    def test_multibox_returns_only_scanned_box_contents(self):
        dn = _Doc(custom_parcel_plan=json.dumps([
            {'items': [{'item_code': 'AIR-FRYER', 'qty': 1}]},
            {'items': [{'item_code': 'JUICER', 'qty': 1}]},
        ]))
        lines = [
            {'item_code': 'AIR-FRYER', 'item_name': 'Air Fryer', 'qty': 1},
            {'item_code': 'JUICER', 'item_name': 'Juicer', 'qty': 1},
        ]

        resolved, error = pack_verify._pieces_for_parcel(dn, lines, 2, 2)

        self.assertIsNone(error)
        self.assertEqual(resolved, [
            {'item_code': 'JUICER', 'item_name': 'Juicer', 'qty': 1.0},
        ])

    def test_multibox_incomplete_plan_is_a_hard_stop(self):
        dn = _Doc(custom_parcel_plan=json.dumps([
            {'items': [{'item_code': 'AIR-FRYER', 'qty': 1}]},
            {'items': []},
        ]))
        lines = [
            {'item_code': 'AIR-FRYER', 'item_name': 'Air Fryer', 'qty': 1},
            {'item_code': 'JUICER', 'item_name': 'Juicer', 'qty': 1},
        ]

        resolved, error = pack_verify._pieces_for_parcel(dn, lines, 2, 2)

        self.assertEqual(resolved, [])
        self.assertIn('JUICER', error)

    def test_multibox_bundle_parent_resolves_exploded_components(self):
        dn = _Doc(custom_parcel_plan=json.dumps([
            {'items': [{'item_code': 'SOL-CI-C11', 'qty': 1}]},
            {'items': [
                {'item_code': 'SOL-TSKD-101', 'qty': 1},
                {'item_code': 'SOL-TSFP-101', 'qty': 1},
            ]},
        ]))
        combo_components = [
            'SOL-CI-DT-101', 'SOL-CI-KD-102', 'SOL-CI-PNY-101',
            'SOL-KIT-CHB-101', 'SOL-KIT-KNF-COM-P2', 'SOL-NWSPA-COM-P3',
        ]
        lines = [
            {'item_code': code, 'item_name': code, 'qty': 1,
             'bundle': 'SOL-CI-C11'}
            for code in combo_components
        ] + [
            {'item_code': 'SOL-TSKD-101', 'item_name': 'Kadai', 'qty': 1},
            {'item_code': 'SOL-TSFP-101', 'item_name': 'Fry Pan', 'qty': 1},
        ]

        first_box, first_error = pack_verify._pieces_for_parcel(dn, lines, 1, 2)
        second_box, second_error = pack_verify._pieces_for_parcel(dn, lines, 2, 2)

        self.assertIsNone(first_error)
        self.assertEqual([row['item_code'] for row in first_box], combo_components)
        self.assertIsNone(second_error)
        self.assertEqual([row['item_code'] for row in second_box],
                         ['SOL-TSKD-101', 'SOL-TSFP-101'])

    def test_bundle_quantity_is_split_across_parcels(self):
        dn = _Doc(custom_parcel_plan=json.dumps([
            {'items': [{'item_code': 'COMBO', 'qty': 1}]},
            {'items': [{'item_code': 'COMBO', 'qty': 1}]},
        ]))
        lines = [
            {'item_code': 'COMPONENT-A', 'item_name': 'Component A',
             'qty': 2, 'bundle': 'COMBO'},
            {'item_code': 'COMPONENT-B', 'item_name': 'Component B',
             'qty': 4, 'bundle': 'COMBO'},
        ]

        resolved, error = pack_verify._pieces_for_parcel(dn, lines, 1, 2)

        self.assertIsNone(error)
        self.assertEqual([row['qty'] for row in resolved], [1.0, 2.0])

    def test_order_service_instruction_does_not_block_physical_parcel(self):
        dn = _Doc(custom_parcel_plan=json.dumps([
            {'items': [
                {'item_code': 'SIGNATURE-COMBO', 'qty': 1},
                {'item_code': 'SOL-INS-PERSONALISATION', 'qty': 1},
            ]},
            {'items': [
                {'item_code': 'BOTTLE', 'qty': 1},
                {'item_code': 'PARCHMENT', 'qty': 1},
                {'item_code': 'SOL-INS-PERSONALISATION', 'qty': 1},
            ]},
        ]))
        lines = [
            {'item_code': 'AIR-FRYER', 'item_name': 'Air Fryer', 'qty': 1,
             'bundle': 'SIGNATURE-COMBO'},
            {'item_code': 'BOTTLE', 'item_name': 'Bottle', 'qty': 1},
            {'item_code': 'PARCHMENT', 'item_name': 'Parchment', 'qty': 1},
        ]
        services = [
            {'item_code': 'SOL-INS-PERSONALISATION',
             'item_name': 'Item Personalization', 'qty': 1},
        ]

        first_box, first_error = pack_verify._pieces_for_parcel(
            dn, lines, 1, 2, service_lines=services)
        second_box, second_error = pack_verify._pieces_for_parcel(
            dn, lines, 2, 2, service_lines=services)

        self.assertIsNone(first_error)
        self.assertEqual([row['item_code'] for row in first_box], ['AIR-FRYER'])
        self.assertIsNone(second_error)
        self.assertEqual([row['item_code'] for row in second_box],
                         ['BOTTLE', 'PARCHMENT'])

    def test_submit_requires_photo_before_any_lookup(self):
        result = pack_verify.pack_verify_submit('AWB-1', photo_url=None)

        self.assertEqual(result['status'], 'error')
        self.assertIn('photo', result['message'].lower())


class TestPriorVerifyHold(TestCase):

    @patch.object(pack_verify.frappe, "get_all", return_value=[])
    def test_unverified_parcel_is_not_held(self, _get_all):
        dn = _Doc(shopify_order_number="SOL-FRESH")
        self.assertIsNone(pack_verify._prior_verify_hold(dn, "AWB-FRESH"))

    @patch.object(
        pack_verify.frappe,
        "get_all",
        return_value=[frappe._dict(
            name="PACKV-1", verified_at="2026-08-06 12:28:00",
            verified_by="atlas-automation@solara.in")],
    )
    def test_verified_parcel_is_hard_blocked(self, get_all):
        dn = _Doc(shopify_order_number="SOL1242643")

        result = pack_verify._prior_verify_hold(dn, "AWB-DUP")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["order"], "SOL1242643")
        self.assertIn("ALREADY PACK-VERIFIED 2026-08-06 12:28", result["message"])
        self.assertIn("DO NOT REPACK", result["message"])
        self.assertIn("atlas-automation@solara.in", result["message"])
        get_all.assert_called_once()
        self.assertEqual(get_all.call_args.kwargs["filters"], {"awb": "AWB-DUP"})

    @patch.object(
        pack_verify.frappe,
        "get_all",
        return_value=[frappe._dict(name="PACKV-2", verified_at=None,
                                   verified_by=None)],
    )
    def test_hold_survives_missing_audit_fields(self, _get_all):
        dn = _Doc(shopify_order_id="7001")

        result = pack_verify._prior_verify_hold(dn, "AWB-BARE")

        self.assertEqual(result["status"], "error")
        self.assertIn("DO NOT REPACK", result["message"])


class TestSiblingDispatchHold(TestCase):

    @patch.object(pack_verify.frappe, "get_all", return_value=[])
    def test_no_siblings_is_not_held(self, _get_all):
        dn = _Doc(shopify_order_id="6001", shopify_order_number="SOL9001")
        self.assertIsNone(pack_verify._dispatched_sibling_hold(dn, "AWB-NEW"))

    @patch.object(pack_verify.frappe, "get_all")
    def test_dispatched_sibling_blocks_new_awb(self, get_all):
        get_all.return_value = [
            frappe._dict(name="SHPDN27-1", custom_dispatched=1,
                         custom_dispatched_at="2026-07-20 10:00:00",
                         custom_dispatched_by="security", posting_date="2026-07-20"),
        ]
        dn = _Doc(shopify_order_id="6001", shopify_order_number="SOL9001")

        result = pack_verify._dispatched_sibling_hold(dn, "NEW-AWB-777")

        self.assertEqual(result["status"], "error")
        self.assertIn("SHPDN27-1", result["message"])
        self.assertIn("DO NOT PACK", result["message"])

    @patch.object(pack_verify.frappe, "get_all")
    def test_self_dn_is_excluded_from_its_own_sibling_query(self, get_all):
        get_all.return_value = [
            frappe._dict(name="SHPDN27-SELF", custom_dispatched=1,
                         posting_date="2026-08-05"),
        ]
        dn = _Doc(name="SHPDN27-SELF", shopify_order_number="SOL9002")

        self.assertIsNone(pack_verify._dispatched_sibling_hold(dn, "AWB-1"))

    @patch.object(pack_verify.frappe, "get_all")
    def test_sibling_dispatch_scan_blocks_even_without_flag(self, get_all):
        get_all.side_effect = [
            [frappe._dict(name="SHPDN27-2", custom_dispatched=0,
                          posting_date="2026-08-04", courier_partner="Shadowfax")],
            [frappe._dict(delivery_note="SHPDN27-2",
                          scanned_at="2026-08-04 18:00:00", scanned_by="security")],
        ]
        dn = _Doc(shopify_order_id="6002")

        result = pack_verify._dispatched_sibling_hold(dn, "NEW-AWB")

        self.assertEqual(result["status"], "error")
        self.assertIn("security", result["message"])

    @patch.object(pack_verify.frappe, "get_all")
    def test_undispatched_sibling_does_not_block(self, get_all):
        get_all.side_effect = [
            [frappe._dict(name="SHPDN27-3", custom_dispatched=0,
                          posting_date=pack_verify.nowdate(), courier_partner="Delhivery")],
            [],
        ]
        dn = _Doc(shopify_order_number="SOL9003")
        self.assertIsNone(pack_verify._dispatched_sibling_hold(dn, "AWB-1"))

    @patch.object(pack_verify.frappe, "get_all")
    def test_replacement_dn_is_exempt_no_query_issued(self, get_all):
        dn = _Doc(is_replacement=1, shopify_order_id="6003")
        self.assertIsNone(pack_verify._dispatched_sibling_hold(dn, "AWB-REP"))
        get_all.assert_not_called()

    @patch.object(pack_verify.frappe, "get_all")
    def test_matches_on_order_id_when_order_number_absent(self, get_all):
        get_all.return_value = [
            frappe._dict(name="SHPDN27-4", custom_dispatched=1,
                         posting_date="2026-08-01", courier_partner="Delhivery"),
        ]
        dn = _Doc(shopify_order_id="6004", shopify_order_number=None)

        result = pack_verify._dispatched_sibling_hold(dn, "AWB-X")

        self.assertEqual(result["status"], "error")
        called_or_filters = get_all.call_args.kwargs["or_filters"]
        self.assertEqual(called_or_filters, {"shopify_order_id": "6004"})

    @patch.object(
        pack_verify,
        "_tracking_for_dns",
        return_value={"SHPDN27-5": [("OLD-AWB", "bucket:6|delivered")]})
    @patch.object(
        pack_verify,
        "_awb_courier_pairs",
        return_value=[("OLD-AWB", "Delhivery")])
    @patch.object(pack_verify.frappe, "get_all")
    @patch.object(pack_verify.frappe, "get_doc")
    def test_old_unflagged_unscanned_sibling_caught_by_live_tracking(
            self, get_doc, get_all, _pairs, _track):
        get_all.side_effect = [
            [frappe._dict(name="SHPDN27-5", custom_dispatched=0,
                          posting_date="2026-07-15", courier_partner="Delhivery")],
            [],
        ]
        get_doc.return_value = _Doc(name="SHPDN27-5")
        dn = _Doc(shopify_order_id="6005")

        result = pack_verify._dispatched_sibling_hold(dn, "NEW-AWB-2")

        self.assertEqual(result["status"], "error")
        self.assertIn("delivered", result["message"])


class TestCancelledOrderHold(TestCase):

    @patch.object(dispatch.frappe, "get_all", return_value=[])
    def test_unknown_code_is_not_cancelled(self, _get_all):
        self.assertIsNone(dispatch._cancelled_dn_lookup("NOPE123"))

    @patch.object(dispatch.frappe, "get_all")
    def test_order_ref_finds_cancelled_dn(self, get_all):
        get_all.return_value = [frappe._dict(
            name="SHPDN27-61162", shopify_order_number="SOL1248233")]
        row = dispatch._cancelled_dn_lookup("SOL1248233")
        self.assertEqual(row["name"], "SHPDN27-61162")
        self.assertEqual(get_all.call_args.kwargs["filters"]["docstatus"], 2)

    @patch.object(dispatch.frappe, "get_all")
    def test_parcel_and_replacement_refs_normalise(self, get_all):
        get_all.return_value = [frappe._dict(
            name="SHPDN27-1", shopify_order_number="SOL1246834")]
        for ref in ("SOL1246834-P2", "SOL1246834-P2-R1", "sol1246834_p1"):
            row = dispatch._cancelled_dn_lookup(ref)
            self.assertIsNotNone(row, ref)
            self.assertEqual(
                get_all.call_args.kwargs["filters"]["shopify_order_number"],
                "SOL1246834")

    @patch.object(dispatch.frappe, "get_all")
    def test_awb_comma_token_must_match_exactly(self, get_all):
        get_all.return_value = [frappe._dict(
            name="SHPDN27-2", shopify_order_number="SOL9",
            awb_number="50940273716,50940273999")]
        self.assertIsNotNone(dispatch._cancelled_dn_lookup("50940273716"))
        get_all.return_value = [frappe._dict(
            name="SHPDN27-2", shopify_order_number="SOL9",
            awb_number="150940273716")]
        self.assertIsNone(dispatch._cancelled_dn_lookup("50940273716"))

    def test_hold_response_message(self):
        r = dispatch.cancelled_hold_response("X", frappe._dict(
            name="SHPDN27-61162", shopify_order_number="SOL1248233"))
        self.assertEqual(r["status"], "error")
        self.assertIn("ORDER CANCELLED", r["message"])
        self.assertIn("DO NOT SHIP", r["message"])
        self.assertIn("SOL1248233", r["message"])


class TestCommaListAwbResolution(TestCase):

    @patch.object(dispatch.frappe, "get_all")
    def test_second_awb_in_comma_list_resolves(self, get_all):
        def fake(doctype, filters=None, fields=None, limit_page_length=0, **kw):
            filters = filters or {}
            if filters.get("awb_number") == "29044411462064":
                return []            # equality miss
            if filters.get("custom_awb_2") == "29044411462064":
                return []
            if isinstance(filters.get("awb_number"), list):
                return [frappe._dict(name="SHPDN27-60805",
                                     awb_number="29044411462053,29044411462064")]
            return []
        get_all.side_effect = fake
        self.assertEqual(dispatch._find_dn_by_awb("29044411462064"),
                         "SHPDN27-60805")
