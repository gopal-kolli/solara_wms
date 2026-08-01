"""Move D2C Pack Verify uniqueness from Delivery Note to parcel AWB."""

import frappe


def execute():
    table = "tabD2C Pack Verify"
    if not frappe.db.table_exists("D2C Pack Verify"):
        return

    # Frappe adds unique indexes when a field becomes unique, but removal of a
    # historical unique flag is not a reliable cross-version index drop. Remove
    # every unique index whose indexed column is delivery_note explicitly.
    indexes = frappe.db.sql(
        "SHOW INDEX FROM `{0}` WHERE Column_name='delivery_note' AND Non_unique=0"
        .format(table), as_dict=True)
    for row in indexes:
        key = row.get("Key_name")
        if key and key != "PRIMARY":
            safe_key = key.replace("`", "``")
            frappe.db.sql(
                "ALTER TABLE `{0}` DROP INDEX `{1}`".format(table, safe_key))

    # Older pilot records should already hold the scanned AWB. Backfill the
    # exceptional blanks before the new AWB-required/unique contract is used.
    from solara_wms.wms.d2c_fulfillment import _awb_courier_pairs

    rows = frappe.get_all(
        "D2C Pack Verify", filters={"awb": ["in", [None, ""]]},
        fields=["name", "delivery_note"], limit_page_length=0)
    for row in rows:
        if not row.delivery_note:
            continue
        dn = frappe.get_doc("Delivery Note", row.delivery_note)
        pairs = _awb_courier_pairs(dn)
        if pairs and pairs[0][0]:
            frappe.db.set_value(
                "D2C Pack Verify", row.name, "awb", pairs[0][0],
                update_modified=False)

