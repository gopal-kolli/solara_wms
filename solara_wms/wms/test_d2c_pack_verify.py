import json
from unittest import TestCase

from solara_wms.wms import d2c_pack_verify as pack_verify


class _Doc:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class TestParcelPieceResolution(TestCase):
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
