"""Pure validation for SOLARA physical location master rows."""

import re


class LocationMasterError(ValueError):
    pass


LOCATION_ID_RE = re.compile(r"^[A-Z0-9]{2,8}-L[0-9]{4,8}$")
DISPLAY_CODE_RE = re.compile(r"^[A-Z0-9]{2,8}-(FW|BW)-[A-Z0-9]+-[A-Z0-9]+$")
ZONE_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,19}$")

MODULES = {
    "S-5X6": (5.0, 6.0),
    "M-5X12": (5.0, 12.0),
    "M-10X6": (10.0, 6.0),
    "L-10X12": (10.0, 12.0),
    "DYNAMIC": (0.0, 0.0),
    "CUSTOM": None,
}
ZONE_TYPES = {"Picking", "Stocking", "Receiving", "Return", "Defective", "Staging"}


def normalize_location_id(value):
    location_id = str(value or "").strip().upper()
    if not LOCATION_ID_RE.fullmatch(location_id):
        raise LocationMasterError("Location ID must match HYD-L0001 style")
    return location_id


def qr_payload(location_id):
    return "SOLARA:LOC:" + normalize_location_id(location_id)


def location_id_from_scan(value):
    scanned = str(value or "").strip().upper()
    if scanned.startswith("SOLARA:LOC:"):
        scanned = scanned.split(":", 2)[2]
    return normalize_location_id(scanned)


def validate_location_row(row):
    if not isinstance(row, dict):
        raise LocationMasterError("Each location row must be an object")
    location_id = normalize_location_id(row.get("location_id"))
    display_code = str(row.get("display_code") or "").strip().upper()
    match = DISPLAY_CODE_RE.fullmatch(display_code)
    if not match:
        raise LocationMasterError("Display Code must match HYD-FW-DW-A01 style")
    hall_code = str(row.get("hall_code") or "").strip().upper()
    if hall_code not in {"FW", "BW"}:
        raise LocationMasterError("Hall Code must be FW or BW")
    if match.group(1) != hall_code:
        raise LocationMasterError("Display Code hall does not match Hall Code")
    zone_code = str(row.get("zone_code") or "").strip().upper()
    if not ZONE_CODE_RE.fullmatch(zone_code):
        raise LocationMasterError("Zone Code is invalid")
    zone_type = str(row.get("zone_type") or "").strip()
    if zone_type not in ZONE_TYPES:
        raise LocationMasterError("Zone Type is invalid")
    module = str(row.get("bay_module") or "").strip().upper()
    if module not in MODULES:
        raise LocationMasterError("Bay Module is invalid")
    if module == "CUSTOM":
        try:
            length_ft = float(row.get("length_ft") or 0)
            width_ft = float(row.get("width_ft") or 0)
        except (TypeError, ValueError) as exc:
            raise LocationMasterError("Custom bay dimensions must be numeric") from exc
        if length_ft <= 0 or width_ft <= 0:
            raise LocationMasterError("Custom bay dimensions must be greater than zero")
    else:
        length_ft, width_ft = MODULES[module]
    try:
        route_sequence = int(row.get("route_sequence") or 0)
    except (TypeError, ValueError) as exc:
        raise LocationMasterError("Route Sequence must be an integer") from exc
    if route_sequence < 0:
        raise LocationMasterError("Route Sequence cannot be negative")
    return {
        "location_id": location_id,
        "qr_payload": qr_payload(location_id),
        "display_code": display_code,
        "hall_code": hall_code,
        "zone_code": zone_code,
        "zone_type": zone_type,
        "bay_module": module,
        "length_ft": length_ft,
        "width_ft": width_ft,
        "floor_area_sq_ft": length_ft * width_ft,
        "route_sequence": route_sequence,
        "notes": str(row.get("notes") or "").strip(),
    }


def validate_location_rows(rows):
    normalized = []
    errors = []
    location_ids = set()
    display_codes = set()
    for index, row in enumerate(rows or [], 1):
        try:
            value = validate_location_row(row)
            if value["location_id"] in location_ids:
                raise LocationMasterError("Duplicate Location ID in import")
            if value["display_code"] in display_codes:
                raise LocationMasterError("Duplicate Display Code in import")
            location_ids.add(value["location_id"])
            display_codes.add(value["display_code"])
            normalized.append(value)
        except LocationMasterError as exc:
            errors.append({"row": index, "error": str(exc)})
    return normalized, errors
