"""Immediate, idempotent warehouse hold for cancelled Shopify orders.

This module deliberately does not cancel submitted ERPNext documents.  Its
job is narrower and time-critical: once Shopify says an order is cancelled,
prevent release, packing and dispatch while the cancellation control queue
decides whether the case is a clean pre-shipment reversal or an RTO/return.
"""

import frappe
from frappe.utils import cint, get_datetime, now_datetime


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
