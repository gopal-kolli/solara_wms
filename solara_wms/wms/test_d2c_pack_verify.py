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

    def test_submit_requires_photo_before_any_lookup(self):
        result = pack_verify.pack_verify_submit('AWB-1', photo_url=None)

        self.assertEqual(result['status'], 'error')
        self.assertIn('photo', result['message'].lower())
