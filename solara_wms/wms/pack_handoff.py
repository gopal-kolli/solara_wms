"""Opt-in completed-pick handoff to the existing D2C pack/dispatch flow."""

import hashlib

import frappe
from frappe import _
from frappe.utils import now_datetime

from solara_wms.wms.inventory_domain import (
    InventoryInvariantError,
    canonical_qty,
    match_pack_handoff,
    request_hash,
)


HANDOFF_DOCTYPE = "WMS Pack Handoff"


def pick_handoff_required():
    mode = frappe.db.get_single_value("WMS Settings", "operating_mode") or "Disabled"
    enabled = frappe.db.get_single_value(
        "WMS Settings", "require_pick_handoff_for_pack"
    )
    return mode in ("Shadow", "Draft Handoff") and bool(int(enabled or 0))


def _delivery_warehouse(dn):
    warehouses = {
        row.get("warehouse")
        for row in list(dn.get("items") or []) + list(dn.get("packed_items") or [])
        if row.get("warehouse")
    }
    if not warehouses and dn.get("set_warehouse"):
        warehouses.add(dn.get("set_warehouse"))
    if len(warehouses) != 1:
        raise InventoryInvariantError(
            "D2C parcel must resolve exactly one source warehouse for WMS handoff"
        )
    return next(iter(warehouses))


def _completed_pick_rows(delivery_note, awb, warehouse, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    return frappe.db.sql(
        f"""
        SELECT w.name AS work, l.item_code, l.executed_qty
          FROM `tabWMS Work` w
          JOIN `tabWMS Work Line` l
            ON l.parent = w.name AND l.parenttype = 'WMS Work'
         WHERE w.work_type = 'Pick'
           AND w.status = 'Completed'
           AND w.warehouse = %s
           AND w.reference_doctype = 'Delivery Note'
           AND w.reference_name = %s
           AND w.parcel_awb = %s
           AND (w.pack_handoff IS NULL OR w.pack_handoff = '')
         ORDER BY w.name
         {suffix}
        """,
        (warehouse, delivery_note, awb),
        as_dict=True,
    )


def _match(lines, rows):
    return match_pack_handoff(
        [{"item_code": row["item_code"], "qty": row["qty"]} for row in lines],
        [
            {"item_code": row.item_code, "executed_qty": row.executed_qty}
            for row in rows
        ],
    )


def pack_handoff_status(dn, awb, lines):
    """Read-only readiness information used by the pack bench."""
    if not pick_handoff_required():
        return {"pick_handoff_required": False, "pick_handoff_ready": True}
    existing = frappe.db.get_value(
        HANDOFF_DOCTYPE, {"awb": awb}, ["name", "pack_verify"], as_dict=True
    )
    if existing:
        return {
            "pick_handoff_required": True,
            "pick_handoff_ready": True,
            "pick_handoff": existing.name,
            "pick_handoff_consumed": True,
        }
    try:
        warehouse = _delivery_warehouse(dn)
        pilot = frappe.db.get_single_value("WMS Settings", "pilot_warehouse")
        if pilot and warehouse != pilot:
            raise InventoryInvariantError(
                f"D2C parcel warehouse {warehouse} is outside pilot warehouse {pilot}"
            )
        rows = _completed_pick_rows(dn.name, awb, warehouse)
        matched = _match(lines, rows)
        return {
            "pick_handoff_required": True,
            "pick_handoff_ready": True,
            "pick_handoff_warehouse": warehouse,
            "pick_handoff_work": [row.work for row in rows],
            "pick_handoff_items": {
                item: canonical_qty(qty) for item, qty in matched.items()
            },
        }
    except InventoryInvariantError as exc:
        return {
            "pick_handoff_required": True,
            "pick_handoff_ready": False,
            "pick_handoff_error": str(exc),
        }


def consume_pack_handoff(dn, awb, lines, pack_verify):
    """Atomically consume exact completed picks for one Pack Verify record."""
    if not pick_handoff_required():
        return None
    existing = frappe.db.get_value(
        HANDOFF_DOCTYPE, {"awb": awb}, ["name", "pack_verify"], as_dict=True
    )
    if existing:
        if existing.pack_verify != pack_verify:
            frappe.throw(_("Parcel pick work was already handed to another pack record"))
        return existing.name

    try:
        warehouse = _delivery_warehouse(dn)
        pilot = frappe.db.get_single_value("WMS Settings", "pilot_warehouse")
        if pilot and warehouse != pilot:
            raise InventoryInvariantError(
                f"D2C parcel warehouse {warehouse} is outside pilot warehouse {pilot}"
            )
        rows = _completed_pick_rows(dn.name, awb, warehouse, lock=True)
        matched = _match(lines, rows)
    except InventoryInvariantError as exc:
        frappe.throw(_(str(exc)))

    key = "pack-handoff:" + hashlib.sha256(
        (dn.name + "|" + awb).encode("utf-8")
    ).hexdigest()
    payload = {
        "command": "Pack Handoff",
        "idempotency_key": key,
        "delivery_note": dn.name,
        "awb": awb,
        "warehouse": warehouse,
        "pack_verify": pack_verify,
        "works": [row.work for row in rows],
        "items": {item: canonical_qty(qty) for item, qty in matched.items()},
    }
    handoff = frappe.get_doc(
        {
            "doctype": HANDOFF_DOCTYPE,
            "awb": awb,
            "delivery_note": dn.name,
            "warehouse": warehouse,
            "pack_verify": pack_verify,
            "idempotency_key": key,
            "request_hash": request_hash(payload),
            "posted_at": now_datetime(),
            "posted_by": frappe.session.user,
            "lines": [
                {
                    "work": row.work,
                    "item_code": row.item_code,
                    "qty": row.executed_qty,
                }
                for row in rows
            ],
        }
    )
    handoff.insert(ignore_permissions=True)
    now = now_datetime()
    for row in rows:
        frappe.db.sql(
            """
            UPDATE `tabWMS Work`
               SET pack_handoff = %s, packed_at = %s,
                   modified = %s, modified_by = %s
             WHERE name = %s AND status = 'Completed'
               AND (pack_handoff IS NULL OR pack_handoff = '')
            """,
            (handoff.name, now, now, frappe.session.user, row.work),
        )
        if frappe.db.sql("SELECT ROW_COUNT()")[0][0] != 1:
            frappe.throw(_("Completed pick work changed concurrently; rescan the parcel"))
    return handoff.name


def dispatch_pack_handoff_status(awb):
    """Fail-closed dispatch gate when the opt-in WMS pack pilot is active."""
    if not pick_handoff_required():
        return {"allowed": True, "required": False}
    handoff = frappe.db.get_value(
        HANDOFF_DOCTYPE, {"awb": awb}, ["name", "pack_verify"], as_dict=True
    )
    if not handoff:
        return {
            "allowed": False,
            "required": True,
            "message": "PACK HOLD — completed pick work has not been handed to this parcel.",
        }
    return {
        "allowed": True,
        "required": True,
        "pick_handoff": handoff.name,
        "pack_verify": handoff.pack_verify,
    }
