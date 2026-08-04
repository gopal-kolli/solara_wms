"""Controlled physical-location master import and QR resolution.

Imports create inactive Draft locations only. They never seed balances, create
ERPNext warehouses, post stock documents or commit the request transaction.
"""

import hashlib

import frappe
from frappe import _
from frappe.utils import now_datetime

from solara_wms.wms.location_domain import (
    LocationMasterError,
    location_id_from_scan,
    validate_location_rows,
)


def _rows(value):
    parsed = frappe.parse_json(value) if isinstance(value, str) else value
    if not isinstance(parsed, list):
        frappe.throw(_("Location rows must be a JSON array"))
    if not parsed or len(parsed) > 1000:
        frappe.throw(_("Location import must contain between 1 and 1000 rows"))
    return parsed


def _warehouse(warehouse):
    row = frappe.db.get_value("Warehouse", warehouse, ["name", "is_group", "disabled"], as_dict=True)
    if not row or row.is_group or row.disabled:
        frappe.throw(_("Select an active leaf Warehouse"))
    return row.name


def _preview(rows, warehouse):
    normalized, errors = validate_location_rows(_rows(rows))
    for row in normalized:
        existing_id = frappe.db.get_value(
            "Warehouse Bin", {"location_id": row["location_id"]},
            [
                "name", "warehouse", "bin_code", "hall_code", "zone_code",
                "zone_type", "bay_module", "route_sequence",
            ], as_dict=True,
        )
        existing_code = frappe.db.get_value(
            "Warehouse Bin", {"warehouse": warehouse, "bin_code": row["display_code"]},
            ["name", "location_id"], as_dict=True,
        )
        if existing_id:
            expected = (
                warehouse, row["display_code"], row["hall_code"], row["zone_code"],
                row["zone_type"], row["bay_module"], row["route_sequence"],
            )
            actual = (
                existing_id.warehouse, existing_id.bin_code, existing_id.hall_code,
                existing_id.zone_code, existing_id.zone_type, existing_id.bay_module,
                int(existing_id.route_sequence or 0),
            )
            if actual != expected:
                errors.append({"location_id": row["location_id"], "error": "Location ID already exists with different master data"})
        elif existing_code and existing_code.location_id != row["location_id"]:
            errors.append({"location_id": row["location_id"], "error": "Display Code already belongs to another Location ID"})
        row["action"] = "Skip" if existing_id else "Create Draft"
    return normalized, errors


@frappe.whitelist()
def preview_location_master(rows, warehouse):
    frappe.only_for("System Manager")
    warehouse = _warehouse(warehouse)
    normalized, errors = _preview(rows, warehouse)
    return {
        "warehouse": warehouse,
        "rows": normalized,
        "error_count": len(errors),
        "errors": errors,
        "confirmation_hash": hashlib.sha256(frappe.as_json(normalized).encode("utf-8")).hexdigest(),
        "writes": 0,
    }


@frappe.whitelist(methods=["POST"])
def import_location_master(rows, warehouse, confirmation):
    """Create inactive Draft locations after an exact preview confirmation."""
    frappe.only_for("System Manager")
    warehouse = _warehouse(warehouse)
    if frappe.db.get_single_value("WMS Settings", "operating_mode") != "Disabled":
        frappe.throw(_("Location master import requires WMS operating mode Disabled"))
    normalized, errors = _preview(rows, warehouse)
    if errors:
        frappe.throw(_("Location master contains {0} error(s); preview and correct them first").format(len(errors)))
    expected = hashlib.sha256(frappe.as_json(normalized).encode("utf-8")).hexdigest()
    if confirmation != expected:
        frappe.throw(_("Confirmation hash does not match the current location preview"))
    created = []
    skipped = []
    for row in normalized:
        existing = frappe.db.get_value("Warehouse Bin", {"location_id": row["location_id"]}, "name")
        if existing:
            skipped.append(existing)
            continue
        doc = frappe.get_doc({
            "doctype": "Warehouse Bin",
            "warehouse": warehouse,
            "location_id": row["location_id"],
            "bin_code": row["display_code"],
            "hall_code": row["hall_code"],
            "zone_code": row["zone_code"],
            "zone_type": row["zone_type"],
            "bay_module": row["bay_module"],
            "commissioning_status": "Draft",
            "status": "Blocked",
            "is_active": 0,
            "route_sequence": row["route_sequence"],
            "bin_length": row["length_ft"] * 30.48,
            "bin_width": row["width_ft"] * 30.48,
            "floor_area_sq_ft": row["floor_area_sq_ft"],
            "notes": row["notes"],
        })
        doc.insert(ignore_permissions=True)
        created.append(doc.name)
    return {"warehouse": warehouse, "created": created, "skipped": skipped, "commissioning_status": "Draft", "writes": len(created)}


@frappe.whitelist(methods=["POST"])
def commission_location(
    location_id,
    target_status,
    evidence_reference=None,
    baseline_reference=None,
):
    """Advance one location through Draft -> Marked -> Verified -> Active.

    This records control evidence only. It never seeds a WMS balance or adjusts
    Atlas stock; the baseline count/Stock Reconciliation remains a separately
    reviewed workflow.
    """
    frappe.only_for("System Manager")
    if frappe.db.get_single_value("WMS Settings", "operating_mode") != "Disabled":
        frappe.throw(_("Location commissioning requires WMS operating mode Disabled"))
    location_id = location_id_from_scan(location_id)
    name = frappe.db.get_value("Warehouse Bin", {"location_id": location_id}, "name")
    if not name:
        frappe.throw(_("Location {0} was not found").format(location_id))
    doc = frappe.get_doc("Warehouse Bin", name)
    target_status = str(target_status or "").strip().title()
    transitions = {"Draft": "Marked", "Marked": "Verified", "Verified": "Active"}
    expected = transitions.get(doc.commissioning_status)
    if target_status != expected:
        frappe.throw(
            _("Location {0} must move from {1} to {2}, not {3}").format(
                location_id, doc.commissioning_status, expected or "no further status", target_status
            )
        )
    evidence = str(evidence_reference or "").strip()
    baseline = str(baseline_reference or "").strip()
    if target_status == "Marked":
        if not evidence:
            frappe.throw(_("A floor-marking evidence reference is required"))
        doc.marking_evidence = evidence
    elif target_status == "Verified":
        if not evidence:
            frappe.throw(_("An independent field-verification reference is required"))
        doc.field_verified_by = frappe.session.user
        doc.field_verified_at = now_datetime()
        doc.notes = "\n".join(filter(None, [doc.notes, "Field verification: " + evidence]))
    elif target_status == "Active":
        if not baseline:
            frappe.throw(_("A signed baseline count reference is required before activation"))
        doc.baseline_reference = baseline
    doc.commissioning_status = target_status
    doc.flags.controlled_location_transition = True
    doc.save(ignore_permissions=True)
    return {
        "location_id": doc.location_id,
        "display_code": doc.bin_code,
        "commissioning_status": doc.commissioning_status,
        "is_active": bool(doc.is_active),
        "status": doc.status,
    }


@frappe.whitelist(methods=["POST"])
def retire_location(location_id, reason):
    """Retire an empty location without deleting its identity or history."""
    frappe.only_for("System Manager")
    if frappe.db.get_single_value("WMS Settings", "operating_mode") != "Disabled":
        frappe.throw(_("Location retirement requires WMS operating mode Disabled"))
    location_id = location_id_from_scan(location_id)
    name = frappe.db.get_value("Warehouse Bin", {"location_id": location_id}, "name")
    if not name:
        frappe.throw(_("Location {0} was not found").format(location_id))
    reason = str(reason or "").strip()
    if len(reason) < 8:
        frappe.throw(_("A retirement reason of at least 8 characters is required"))
    nonzero = frappe.db.sql(
        """SELECT name FROM `tabWMS Bin Balance`
            WHERE bin = %s
              AND (physical_qty != 0 OR allocated_qty != 0 OR hold_qty != 0)
            LIMIT 1 FOR UPDATE""",
        name,
    )
    if nonzero:
        frappe.throw(_("Location {0} has WMS quantity and cannot be retired").format(location_id))
    doc = frappe.get_doc("Warehouse Bin", name)
    if doc.commissioning_status == "Retired":
        return {"location_id": location_id, "commissioning_status": "Retired", "replayed": True}
    doc.commissioning_status = "Retired"
    doc.is_active = 0
    doc.status = "Blocked"
    doc.notes = "\n".join(filter(None, [doc.notes, "Retired: " + reason]))
    doc.flags.controlled_location_transition = True
    doc.save(ignore_permissions=True)
    return {"location_id": location_id, "commissioning_status": "Retired", "replayed": False}


def resolve_location_scan(warehouse, scanned_value, require_active=True):
    """Resolve immutable QR, Location ID, display code or legacy document name."""
    scanned = str(scanned_value or "").strip()
    if not scanned:
        frappe.throw(_("Scan a location QR"))
    try:
        filters = {"location_id": location_id_from_scan(scanned)}
    except LocationMasterError:
        if frappe.db.exists("Warehouse Bin", scanned):
            filters = {"name": scanned}
        else:
            filters = {"warehouse": warehouse, "bin_code": scanned.upper()}
    row = frappe.db.get_value(
        "Warehouse Bin", filters,
        ["name", "warehouse", "bin_code", "location_id", "status", "is_active"], as_dict=True,
    )
    if not row or row.warehouse != warehouse:
        frappe.throw(_("Scanned location does not belong to warehouse {0}").format(warehouse))
    if require_active and (not row.is_active or row.status in ("Blocked", "Maintenance")):
        frappe.throw(_("Location {0} is not active").format(row.bin_code or row.location_id))
    return row
