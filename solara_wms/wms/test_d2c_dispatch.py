from unittest import TestCase
from unittest.mock import patch

from solara_wms.wms import d2c_dispatch as dispatch


class _Row:
    def __init__(self, **values):
        self.__dict__.update(values)


class _Doc:
    def __init__(self, **values):
        self.name = values.pop("name")
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class TestDeliveryNoteResolution(TestCase):
    @patch.object(dispatch, "_awb_courier_pairs", return_value=[("WB123", "Shadowfax")])
    @patch.object(dispatch.frappe, "get_doc", return_value=_Doc(name="SHPDN27-58359"))
    @patch.object(dispatch.frappe, "get_all", return_value=[_Row(name="SHPDN27-58359")])
    def test_submitted_single_parcel_dn_resolves_to_its_awb(self, get_all, _get_doc, _pairs):
        resolved = dispatch._resolve("shpdn27-58359")

        self.assertEqual(resolved, ("SHPDN27-58359", "WB123", 1, 1))
        get_all.assert_called_once_with(
            "Delivery Note",
            filters={"name": "SHPDN27-58359", "docstatus": 1},
            fields=["name"],
            limit_page_length=1,
        )

    @patch.object(
        dispatch,
        "_awb_courier_pairs",
        return_value=[("WB123", "Shadowfax"), ("WB456", "Shadowfax")],
    )
    @patch.object(dispatch.frappe, "get_doc", return_value=_Doc(name="SHPDN27-60000"))
    @patch.object(dispatch.frappe, "get_all", return_value=[_Row(name="SHPDN27-60000")])
    def test_multibox_dn_still_requires_each_parcel_awb(self, _get_all, _get_doc, _pairs):
        resolved = dispatch._resolve("SHPDN27-60000")

        self.assertEqual(resolved, ("SHPDN27-60000", None, None, 2))

    @patch.object(dispatch.frappe, "get_all", return_value=[])
    def test_unknown_or_cancelled_dn_is_not_found(self, _get_all):
        self.assertEqual(
            dispatch._resolve("SHPDN27-99999"),
            (None, None, None, None),
        )
