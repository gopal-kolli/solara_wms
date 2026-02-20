import frappe
from frappe import _
from frappe.utils import flt


def is_stock_frozen(item_code=None, warehouse=None, bin_code=None, batch_no=None):
    """
    Check if stock is frozen for given parameters.
    Returns True if ANY active WMS Stock Freeze matches the criteria.

    A freeze matches if ALL its non-null fields match the provided parameters.
    For example, a freeze on item_code="ITEM-001" matches any request
    involving that item, regardless of warehouse.
    """
    filters = {"status": "Active", "freeze_type": "Freeze"}

    freezes = frappe.get_all(
        "WMS Stock Freeze",
        filters=filters,
        fields=["item_code", "warehouse", "bin", "batch_no"],
    )

    for freeze in freezes:
        match = True

        if freeze.item_code and item_code and freeze.item_code != item_code:
            match = False
        elif freeze.item_code and not item_code:
            match = False

        if freeze.warehouse and warehouse and freeze.warehouse != warehouse:
            match = False
        elif freeze.warehouse and not warehouse:
            match = False

        if freeze.bin and bin_code and freeze.bin != bin_code:
            match = False
        elif freeze.bin and not bin_code:
            match = False

        if freeze.batch_no and batch_no and freeze.batch_no != batch_no:
            match = False
        elif freeze.batch_no and not batch_no:
            match = False

        if match:
            return True

    return False


def get_available_qty(item_code, warehouse):
    """
    Get available qty from ERPNext Bin, minus any frozen/allocated qty.
    Returns dict with actual_qty, reserved_qty, and available_qty.
    """
    bin_data = frappe.db.get_value(
        "Bin",
        {"item_code": item_code, "warehouse": warehouse},
        ["actual_qty", "reserved_qty", "ordered_qty", "projected_qty"],
        as_dict=True,
    )

    if not bin_data:
        return {
            "actual_qty": 0,
            "reserved_qty": 0,
            "available_qty": 0,
            "projected_qty": 0,
        }

    available = flt(bin_data.actual_qty) - flt(bin_data.reserved_qty)

    return {
        "actual_qty": flt(bin_data.actual_qty),
        "reserved_qty": flt(bin_data.reserved_qty),
        "available_qty": available,
        "projected_qty": flt(bin_data.projected_qty),
    }


def get_item_barcode(item_code):
    """
    Get barcode for an item from the Item Barcode child table.
    Returns the first barcode found, or None.
    """
    barcodes = frappe.get_all(
        "Item Barcode",
        filters={"parent": item_code, "parenttype": "Item"},
        fields=["barcode", "barcode_type"],
        order_by="idx asc",
        limit=1,
    )

    if barcodes:
        return barcodes[0].barcode

    return None


def get_book_qty(item_code, warehouse):
    """
    Get current book qty and valuation rate from ERPNext Bin DocType.
    Returns dict with actual_qty (book qty) and valuation_rate.
    """
    bin_data = frappe.db.get_value(
        "Bin",
        {"item_code": item_code, "warehouse": warehouse},
        ["actual_qty", "valuation_rate"],
        as_dict=True,
    )

    if not bin_data:
        return {"actual_qty": 0, "valuation_rate": 0}

    return {
        "actual_qty": flt(bin_data.actual_qty),
        "valuation_rate": flt(bin_data.valuation_rate),
    }


def check_stock_freeze_on_task(doc, method):
    """
    Hook: before_save on WMS Task.
    Prevents completing tasks on frozen stock.
    Called via doc_events in hooks.py.
    """
    if doc.status != "Completed":
        return

    # Check if any items are on frozen stock
    for row in doc.items or []:
        if is_stock_frozen(
            item_code=row.item_code,
            warehouse=doc.source_warehouse or doc.target_warehouse,
            bin_code=row.source_bin or row.target_bin,
            batch_no=row.batch_no,
        ):
            frappe.throw(
                _(
                    "Cannot complete task: Item {0} is on frozen stock. "
                    "Release the stock freeze before proceeding."
                ).format(row.item_code)
            )
