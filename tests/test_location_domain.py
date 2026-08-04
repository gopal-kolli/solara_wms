import pytest

from solara_wms.wms.location_domain import (
    LocationMasterError,
    location_id_from_scan,
    qr_payload,
    validate_location_row,
    validate_location_rows,
)


def base_row(**values):
    row = {
        "location_id": "HYD-L0001",
        "display_code": "HYD-FW-CW-A01",
        "hall_code": "FW",
        "zone_code": "CW",
        "zone_type": "Stocking",
        "bay_module": "L-10X12",
        "route_sequence": 100,
    }
    row.update(values)
    return row


def test_location_identity_and_qr_are_canonical():
    assert qr_payload("hyd-l0001") == "SOLARA:LOC:HYD-L0001"
    assert location_id_from_scan("SOLARA:LOC:hyd-l0001") == "HYD-L0001"


def test_standard_module_calculates_floor_area():
    row = validate_location_row(base_row())
    assert row["length_ft"] == 10
    assert row["width_ft"] == 12
    assert row["floor_area_sq_ft"] == 120
    assert row["qr_payload"] == "SOLARA:LOC:HYD-L0001"


def test_display_hall_must_match_hall_field():
    with pytest.raises(LocationMasterError, match="hall does not match"):
        validate_location_row(base_row(hall_code="BW"))


def test_custom_module_requires_positive_dimensions():
    with pytest.raises(LocationMasterError, match="greater than zero"):
        validate_location_row(base_row(bay_module="CUSTOM", length_ft=0, width_ft=8))


def test_import_rejects_duplicate_identity_and_display_code():
    rows, errors = validate_location_rows([base_row(), base_row()])
    assert len(rows) == 1
    assert errors == [{"row": 2, "error": "Duplicate Location ID in import"}]
