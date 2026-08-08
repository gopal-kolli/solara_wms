"""Fail-closed Shopify-to-Atlas shipping-address synchronisation.

The upstream connector creates an order-specific Address on initial order
sync, but does not receive ``orders/updated``.  This module is deliberately a
small, independent polling path: it reads recently updated Shopify orders and
updates only an unlabelled Shopify Sales Order with its own address record.

It never writes to Shopify and never changes a Delivery Note.  A changed state
can alter GST, and an existing DN can already have a courier label, so both are
put on a warehouse hold for an approved CS/Ops resolution instead of being
silently changed.
"""

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from solara_wms.wms.shopify_address_values import (
    address_values as _address_values,
    addresses_match as _addresses_match,
    state_changed as _state_changed,
)


SETTINGS_DOCTYPE = "D2C Fulfillment Settings"
SO_HOLD_FIELD = "custom_shopify_address_change_hold"
DN_HOLD_FIELD = "custom_shopify_address_change_hold"
HOLD_AT_FIELD = "custom_shopify_address_change_at"
HOLD_REASON_FIELD = "custom_shopify_address_change_reason"
def _has_field(doctype, fieldname):
    return frappe.get_meta(doctype).has_field(fieldname)


def _delivery_notes_for_sales_order(so_name):
    rows = frappe.get_all(
        "Delivery Note Item", filters={"against_sales_order": so_name},
        fields=["parent"], limit_page_length=0,
    )
    return sorted({row.parent for row in rows if row.parent})


def _set_address_change_hold(so_name, delivery_notes, reason):
    """Latch a non-destructive pack/dispatch hold for human resolution."""
    now = now_datetime()
    values = {
        SO_HOLD_FIELD: 1,
        HOLD_AT_FIELD: now,
        HOLD_REASON_FIELD: reason[:140],
    }
    if _has_field("Sales Order", SO_HOLD_FIELD):
        frappe.db.set_value("Sales Order", so_name, values, update_modified=False)
    if _has_field("Delivery Note", DN_HOLD_FIELD):
        for dn_name in delivery_notes:
            frappe.db.set_value("Delivery Note", dn_name, values, update_modified=False)
    return now


def delivery_note_address_change_hold(dn):
    """True if this DN or any source SO is awaiting address-change review."""
    if _has_field("Delivery Note", DN_HOLD_FIELD) and cint(dn.get(DN_HOLD_FIELD)):
        return True
    sales_orders = {row.against_sales_order for row in dn.items
                    if row.get("against_sales_order")}
    if not sales_orders or not _has_field("Sales Order", SO_HOLD_FIELD):
        return False
    return bool(frappe.get_all(
        "Sales Order", filters={"name": ["in", list(sales_orders)], SO_HOLD_FIELD: 1},
        fields=["name"], limit_page_length=1,
    ))


def address_hold_response(dn=None):
    return {
        "status": "address_change_hold",
        "dn": dn.name if dn else None,
        "order": dn.get("shopify_order_number") if dn else None,
        "message": "ADDRESS CHANGE HOLD — do not pack, label or dispatch. Send to CS/Ops review.",
    }


def _is_order_specific_address(address_name, order_number):
    suffix = "-" + str(order_number or "").strip().upper() + "-SHIPPING"
    return bool(address_name and suffix != "--SHIPPING"
                and str(address_name).upper().endswith(suffix))


def _sync_one(order):
    """Synchronise one Shopify payload.  Returns an auditable action code."""
    shopify_id = str(order.get("id") or "").strip()
    if not shopify_id:
        return "ignored"
    rows = frappe.get_all(
        "Sales Order", filters={"shopify_order_id": shopify_id, "docstatus": 1},
        fields=["name", "shopify_order_number", "shipping_address_name"],
        limit_page_length=2,
    )
    if len(rows) != 1:
        return "not_found" if not rows else "ambiguous"
    so = rows[0]
    if not _is_order_specific_address(so.shipping_address_name, so.shopify_order_number):
        _set_address_change_hold(so.name, _delivery_notes_for_sales_order(so.name),
                                 "Address record is not order-specific")
        return "held_unsafe_address"

    address = frappe.get_doc("Address", so.shipping_address_name)
    remote = _address_values(order.get("shipping_address"))
    if not remote["address_line1"] or not remote["pincode"]:
        _set_address_change_hold(so.name, _delivery_notes_for_sales_order(so.name),
                                 "Shopify address is incomplete")
        return "held_incomplete"
    if _addresses_match(address, remote):
        return "unchanged"

    delivery_notes = _delivery_notes_for_sales_order(so.name)
    if delivery_notes:
        _set_address_change_hold(so.name, delivery_notes,
                                 "Address changed after Delivery Note creation")
        return "held_delivery_note"
    if _state_changed(address, remote):
        _set_address_change_hold(so.name, [], "Inter-state address change requires GST review")
        return "held_gst_state"

    # The address is unique to this Shopify order and it has no DN/AWB yet.
    # Set only the shipping fields; preserve Address links/title and audit history.
    frappe.db.set_value("Address", address.name, remote, update_modified=True)
    return "updated"


def _recent_shopify_orders(minutes):
    """Fetch modified orders without registering an additional Shopify webhook."""
    import requests

    shop = frappe.get_doc("Shopify Setting")
    shop_url = (shop.get("shopify_url") or "").strip()
    token = shop.get("password")
    if not (shop_url and token):
        raise RuntimeError("Shopify Setting URL/token is unavailable")
    since = add_to_date(now_datetime(), minutes=-minutes, as_datetime=True)
    # Keep the poller on the connector's API version rather than pinning a
    # retired Shopify version in a second integration path.
    try:
        from ecommerce_integrations.shopify.constants import API_VERSION
    except Exception:
        API_VERSION = "2025-10"
    url = "https://{0}/admin/api/{1}/orders.json".format(shop_url, API_VERSION)
    response = requests.get(
        url,
        headers={"X-Shopify-Access-Token": token},
        params={
            "status": "any", "limit": 250,
            "updated_at_min": since.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "fields": "id,name,shipping_address",
        }, timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError("Shopify address poll HTTP {0}".format(response.status_code))
    return (response.json() or {}).get("orders", [])


def sync_shopify_address_changes():
    """Scheduled, opt-in address sync.  A failure never wedges the shared worker."""
    try:
        settings = frappe.get_cached_doc(SETTINGS_DOCTYPE)
        if not cint(settings.get("shopify_address_sync_enabled")):
            return {"skipped": "address sync off"}
        minutes = max(10, min(cint(settings.get("shopify_address_sync_lookback_minutes")) or 30, 180))
        counts = {}
        for order in _recent_shopify_orders(minutes):
            action = _sync_one(order)
            counts[action] = counts.get(action, 0) + 1
        frappe.db.commit()
        return counts
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Shopify Address Sync")
        return {"failed": 1}
