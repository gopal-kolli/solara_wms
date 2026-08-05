"""Immediate, idempotent warehouse hold for cancelled Shopify orders.

This module deliberately does not cancel submitted ERPNext documents.  Its
job is narrower and time-critical: once Shopify says an order is cancelled,
prevent release, packing and dispatch while the cancellation control queue
decides whether the case is a clean pre-shipment reversal or an RTO/return.
"""

import json

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from solara_wms.wms.d2c_dispatch import (
    _CP_ID,
    _NOT_MOVED,
    _TRACK_BATCH,
    _cp_track,
)
from solara_wms.wms.d2c_fulfillment import _awb_courier_pairs


HOLD_FIELD = "custom_shopify_cancellation_hold"
HOLD_AT_FIELD = "custom_shopify_cancelled_at"
HOLD_REASON_FIELD = "custom_shopify_cancellation_reason"


def _has_field(doctype, fieldname):
    return frappe.get_meta(doctype).has_field(fieldname)


def _matching_sales_orders(shopify_order_id=None, order_number=None):
    names = set()
    candidates = {
        "shopify_order_id": str(shopify_order_id or "").strip(),
        "shopify_order_number": str(order_number or "").strip().upper(),
    }
    for fieldname, value in candidates.items():
        if not value or not _has_field("Sales Order", fieldname):
            continue
        for row in frappe.get_all(
            "Sales Order",
            filters={fieldname: value},
            fields=["name"],
            limit_page_length=0,
        ):
            names.add(row.name)
    return sorted(names)


def _delivery_notes_for_sales_orders(sales_orders):
    if not sales_orders:
        return []
    rows = frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_order": ["in", list(sales_orders)]},
        fields=["parent"],
        limit_page_length=0,
    )
    return sorted({row.parent for row in rows if row.parent})


def delivery_note_cancellation_hold(dn):
    """Return True when the DN or any source SO has a cancellation hold."""
    if _has_field("Delivery Note", HOLD_FIELD) and cint(dn.get(HOLD_FIELD)):
        return True
    sales_orders = {row.against_sales_order for row in dn.items
                    if row.get("against_sales_order")}
    if not sales_orders or not _has_field("Sales Order", HOLD_FIELD):
        return False
    return bool(frappe.get_all(
        "Sales Order",
        filters={"name": ["in", list(sales_orders)], HOLD_FIELD: 1},
        fields=["name"],
        limit_page_length=1,
    ))


def hold_response(dn=None):
    return {
        "status": "cancellation_hold",
        "dn": dn.name if dn else None,
        "order": (dn.get("shopify_order_number") if dn else None),
        "message": "CANCELLED ORDER HOLD — do not pack, label or dispatch this parcel.",
    }


def _tracking_bucket(status):
    """Normalize ClickPost's free-text status into a cancellation decision."""
    value = (status or "").strip().lower()
    if not value:
        return "NO_DATA"
    if "cancel" in value:
        return "CANCELLED"
    if value.startswith("bucket:"):
        try:
            return ("NOT_MOVED" if int(value.split("|", 1)[0].split(":", 1)[1]) <= 1
                    else "MOVED")
        except (TypeError, ValueError):
            return "NO_DATA"
    if any(marker in value for marker in _NOT_MOVED):
        return "NOT_MOVED"
    return "MOVED"


def _track_delivery_notes(delivery_notes):
    """Return parcel-level courier evidence without changing shipment state."""
    parcels = []
    pending = {}
    for dn in delivery_notes:
        for awb, courier in _awb_courier_pairs(dn):
            courier_key = (courier or "").strip().lower()
            cp_id = _CP_ID.get(courier_key, 4)
            parcel = {
                "delivery_note": dn.name,
                "awb": awb,
                "courier": courier,
                "cp_id": cp_id,
                "status": "",
                "movement": "NO_DATA",
            }
            parcels.append(parcel)
            pending.setdefault(cp_id, []).append(parcel)

    for cp_id, rows in pending.items():
        for index in range(0, len(rows), _TRACK_BATCH):
            chunk = rows[index:index + _TRACK_BATCH]
            statuses = _cp_track([row["awb"] for row in chunk], cp_id)
            for row in chunk:
                row["status"] = statuses.get(row["awb"], "")
                row["movement"] = _tracking_bucket(row["status"])
    return parcels


def _operational_classification(delivery_notes, parcels):
    submitted = [dn for dn in delivery_notes if cint(dn.docstatus) == 1]
    if not delivery_notes:
        return "NO_DELIVERY_NOTE", "No Atlas Delivery Note or AWB exists."
    if delivery_notes and not submitted:
        return "DN_NOT_SUBMITTED", "Only draft/cancelled Delivery Note evidence exists."
    if any(cint(dn.get("custom_dispatched")) for dn in submitted):
        return "MOVED_RTO", "Atlas dispatch evidence exists; route through return/RTO intake."
    if not parcels:
        return "NO_AWB", "A submitted Delivery Note exists but has no AWB."

    movement = {row["movement"] for row in parcels}
    if "MOVED" in movement:
        return "MOVED_RTO", "Courier tracking confirms movement; do not void as pre-shipment."
    if movement == {"CANCELLED"}:
        return "AWB_CANCELLED", "Every AWB is already cancelled with the courier."
    if movement <= {"NOT_MOVED", "CANCELLED"}:
        return "NOT_MOVED_CAN_VOID", "Every active AWB is still pre-pickup and may be voided after approval."
    return "CARRIER_REVIEW", "Courier movement could not be proven for every AWB; keep the warehouse hold."


@frappe.whitelist()
def cancellation_evidence(shopify_order_id=None, order_number=None):
    """Read live Atlas + courier evidence for one cancelled Shopify order.

    This is intentionally read-only.  It tells the control queue whether the
    order can follow the pre-shipment AWB-void path or must go through RTO /
    Return Intake.  Submitted document cancellation remains approval-gated.
    """
    if not (shopify_order_id or order_number):
        frappe.throw("shopify_order_id or order_number is required")
    sales_orders = _matching_sales_orders(shopify_order_id, order_number)
    delivery_note_names = _delivery_notes_for_sales_orders(sales_orders)
    delivery_notes = [frappe.get_doc("Delivery Note", name)
                      for name in delivery_note_names]
    parcels = _track_delivery_notes(delivery_notes)
    code, reason = _operational_classification(delivery_notes, parcels)
    return {
        "classification": code,
        "reason": reason,
        "sales_orders": sales_orders,
        "delivery_notes": [
            {
                "name": dn.name,
                "docstatus": cint(dn.docstatus),
                "status": dn.get("status"),
                "dispatched": cint(dn.get("custom_dispatched")),
                "held": cint(dn.get(HOLD_FIELD)),
            }
            for dn in delivery_notes
        ],
        "parcels": parcels,
    }


@frappe.whitelist()
def cancellation_evidence_batch(orders=None):
    """Batch form of :func:`cancellation_evidence` for the control queue.

    ClickPost calls are grouped by courier so a backlog refresh does not make
    one carrier request per Shopify order.  The endpoint is read-only and is
    capped to 100 cases per request.
    """
    if isinstance(orders, str):
        orders = json.loads(orders or "[]")
    orders = list(orders or [])
    if not orders:
        return {"results": {}}
    if len(orders) > 100:
        frappe.throw("A maximum of 100 cancellation cases may be checked at once")

    order_context = {}
    unique_dn_names = set()
    for order in orders:
        shopify_order_id = str(order.get("shopify_order_id") or "").strip()
        order_number = str(order.get("order_number") or "").strip()
        key = shopify_order_id or order_number
        if not key:
            continue
        sales_orders = _matching_sales_orders(shopify_order_id, order_number)
        dn_names = _delivery_notes_for_sales_orders(sales_orders)
        unique_dn_names.update(dn_names)
        order_context[key] = {
            "sales_orders": sales_orders,
            "delivery_note_names": dn_names,
        }

    docs_by_name = {
        name: frappe.get_doc("Delivery Note", name)
        for name in sorted(unique_dn_names)
    }
    all_parcels = _track_delivery_notes(list(docs_by_name.values()))
    parcels_by_dn = {}
    for parcel in all_parcels:
        parcels_by_dn.setdefault(parcel["delivery_note"], []).append(parcel)

    results = {}
    for key, context in order_context.items():
        delivery_notes = [docs_by_name[name]
                          for name in context["delivery_note_names"]]
        parcels = [parcel for name in context["delivery_note_names"]
                   for parcel in parcels_by_dn.get(name, [])]
        code, reason = _operational_classification(delivery_notes, parcels)
        results[key] = {
            "classification": code,
            "reason": reason,
            "sales_orders": context["sales_orders"],
            "delivery_notes": [
                {
                    "name": dn.name,
                    "docstatus": cint(dn.docstatus),
                    "status": dn.get("status"),
                    "dispatched": cint(dn.get("custom_dispatched")),
                    "held": cint(dn.get(HOLD_FIELD)),
                }
                for dn in delivery_notes
            ],
            "parcels": parcels,
        }
    return {"results": results}


@frappe.whitelist()
def apply_cancellation_hold(shopify_order_id=None, order_number=None,
                            cancelled_at=None, reason=None):
    """Persist a hard warehouse hold on every matching SO and linked DN.

    Safe to retry.  It changes only allow-on-submit custom control fields and
    never cancels an SO, DN, SI, AWB or payment document.
    """
    if not (shopify_order_id or order_number):
        frappe.throw("shopify_order_id or order_number is required")
    if not _has_field("Sales Order", HOLD_FIELD):
        frappe.throw("Shopify cancellation hold fields are not installed")

    held_at = get_datetime(cancelled_at) if cancelled_at else now_datetime()
    reason = (str(reason or "Shopify order cancelled").strip())[:140]
    sales_orders = _matching_sales_orders(shopify_order_id, order_number)
    delivery_notes = _delivery_notes_for_sales_orders(sales_orders)

    so_values = {HOLD_FIELD: 1, HOLD_AT_FIELD: held_at, HOLD_REASON_FIELD: reason}
    dn_values = dict(so_values)
    for name in sales_orders:
        frappe.db.set_value("Sales Order", name, so_values, update_modified=False)
    if _has_field("Delivery Note", HOLD_FIELD):
        for name in delivery_notes:
            frappe.db.set_value("Delivery Note", name, dn_values, update_modified=False)
    frappe.db.commit()

    return {
        "status": "held" if sales_orders else "atlas_order_not_found",
        "shopify_order_id": str(shopify_order_id or ""),
        "order_number": str(order_number or ""),
        "sales_orders": sales_orders,
        "delivery_notes": delivery_notes,
        "held_at": str(held_at),
    }
