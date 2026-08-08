"""Atlas report for the daily Shopify address-change control queue."""

from solara_wms.wms.shopify_address_sync import address_change_exceptions


def execute(filters=None):
    columns = [
        {"fieldname": "shopify_order_number", "label": "Shopify Order", "fieldtype": "Data", "width": 150},
        {"fieldname": "name", "label": "Sales Order", "fieldtype": "Link", "options": "Sales Order", "width": 150},
        {"fieldname": "delivery_note", "label": "Delivery Note", "fieldtype": "Data", "width": 180},
        {"fieldname": "held_at", "label": "Held At", "fieldtype": "Datetime", "width": 160},
        {"fieldname": "reason", "label": "Reason", "fieldtype": "Data", "width": 300},
    ]
    return columns, address_change_exceptions()
