from unittest import TestCase
from unittest.mock import patch

from solara_wms.wms import d2c_dispatch as dispatch


class _Row:
    def __init__(self, **values):
        self.__dict__.update(values)

    def get(self, key, default=None):
        return self.__dict__.get(key, default)


class _Doc:
    def __init__(self, **values):
        self.name = values.pop("name")
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class TestDeliveryNoteResolution(TestCase):
    @patch.object(
        dispatch,
        "_awb_courier_pairs",
        return_value=[
            ("29044411440946", "Delhivery"),
            ("29044411440950", "Delhivery"),
            ("29044411440961", "Delhivery"),
        ],
    )
    @patch.object(
        dispatch.frappe,
        "get_doc",
        return_value=_Doc(name="SHPDN27-57424"),
    )
    @patch.object(dispatch.frappe, "get_all")
    def test_third_parcel_awb_resolves_without_age_cutoff(
            self, get_all, _get_doc, _pairs):
        get_all.side_effect = [
            [],
            [],
            [_Row(name="SHPDN27-57424")],
        ]

        self.assertEqual(
            dispatch._find_dn_by_awb("29044411440961"),
            "SHPDN27-57424",
        )
        self.assertEqual(
            get_all.call_args_list[2].kwargs["filters"],
            {
                "docstatus": 1,
                "custom_awb_list": ["like", "%29044411440961%"],
            },
        )

    @patch.object(
        dispatch,
        "_awb_courier_pairs",
        return_value=[("290444114409610", "Delhivery")],
    )
    @patch.object(
        dispatch.frappe,
        "get_doc",
        return_value=_Doc(name="SHPDN27-57424"),
    )
    @patch.object(dispatch.frappe, "get_all")
    def test_awb_json_candidate_requires_exact_match(
            self, get_all, _get_doc, _pairs):
        get_all.side_effect = [
            [],
            [],
            [_Row(name="SHPDN27-57424")],
        ]

        self.assertIsNone(dispatch._find_dn_by_awb("29044411440961"))

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


class TestPackVerifySecurityGate(TestCase):
    @patch.object(dispatch.frappe, "get_all", return_value=[])
    def test_unverified_awb_is_held(self, get_all):
        self.assertIn("has not been pack-verified",
                      dispatch._pack_verify_dispatch_hold("AWB-1"))
        get_all.assert_called_once_with(
            "D2C Pack Verify",
            filters={"awb": "AWB-1"},
            fields=["name", "mismatch", "photo_url", "verified_at"],
            limit_page_length=1,
        )

    @patch.object(dispatch.frappe, "get_all")
    def test_mismatched_awb_is_held(self, get_all):
        get_all.return_value = [_Row(
            name="PACKV-00001", mismatch=1, photo_url="/private/files/box.jpg"
        )]
        self.assertIn("count mismatch",
                      dispatch._pack_verify_dispatch_hold("AWB-1"))

    @patch.object(dispatch.frappe, "get_all")
    def test_missing_photo_is_held(self, get_all):
        get_all.return_value = [_Row(name="PACKV-00001", mismatch=0, photo_url="")]
        self.assertIn("no open-box photo evidence",
                      dispatch._pack_verify_dispatch_hold("AWB-1"))

    @patch.object(dispatch.frappe, "get_all")
    def test_clean_awb_can_reach_security(self, get_all):
        get_all.return_value = [_Row(
            name="PACKV-00001", mismatch=0, photo_url="/private/files/box.jpg"
        )]
        self.assertIsNone(dispatch._pack_verify_dispatch_hold("AWB-1"))
