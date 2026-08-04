"""Give pre-1.7 Warehouse Bin rows a stable legacy QR identity."""

import hashlib

import frappe


def execute():
    for row in frappe.get_all(
        "Warehouse Bin", fields=["name", "warehouse", "bin_code", "location_id"]
    ):
        if row.location_id:
            continue
        key = "\x1f".join((row.warehouse or "", row.bin_code or row.name))
        location_id = "LEGACY-L" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8].upper()
        payload = "SOLARA:LOC:" + location_id
        frappe.db.set_value(
            "Warehouse Bin",
            row.name,
            {"location_id": location_id, "qr_payload": payload, "barcode": payload},
            update_modified=False,
        )
