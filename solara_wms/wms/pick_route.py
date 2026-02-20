import re

import frappe
from frappe import _
from frappe.utils import flt

from solara_wms.wms.utils import get_available_qty


# ─── HELPERS ──────────────────────────────────────────────────────

def _natural_sort_key(s):
    """
    Sort key for mixed alpha-numeric strings (A2 < A10, not A10 < A2).
    Splits string into list of (str, int) tuples for comparison.
    """
    if not s:
        return []
    parts = re.split(r"(\d+)", str(s))
    return [int(p) if p.isdigit() else p.lower() for p in parts if p]


def _invert_sort_key(key_tuple):
    """
    Invert a sort key for descending order within serpentine routing.
    Numbers become negated; strings get reversed ordinals.
    """
    inverted = []
    for part in key_tuple:
        if isinstance(part, int):
            inverted.append(-part)
        elif isinstance(part, str):
            # Invert each char so 'Z' < 'A' in normal sort
            inverted.append([-ord(c) for c in part])
        else:
            inverted.append(part)
    return inverted


# ─── ZONE PRIORITY ────────────────────────────────────────────────

ZONE_PRIORITY = {
    "Picking": 0,
    "Stocking": 1,
    "Staging": 2,
    "Receiving": 3,
    "Return": 4,
    "Defective": 5,
}


def _zone_sort_value(zone_type):
    """Return numeric priority for a zone type. Lower = pick first."""
    return ZONE_PRIORITY.get(zone_type, 99)


# ─── BIN ALLOCATION ──────────────────────────────────────────────

def allocate_bins_for_items(items, warehouse):
    """
    Auto-assign source bins for items that don't have one.

    Strategy:
    - Query active Warehouse Bins in the warehouse
    - Prefer Picking zone, then Stocking, etc.
    - Within same zone, prefer older bins (FIFO by creation date)
    - Check ERPNext Bin for available stock at warehouse level
    - Items that already have source_bin pass through unchanged

    Args:
        items: list of dicts with item_code, qty, source_bin, etc.
        warehouse: source warehouse name

    Returns:
        list of items with source_bin populated where possible
    """
    if not warehouse:
        return items

    # Get all active bins in this warehouse with location data
    bins = frappe.get_all(
        "Warehouse Bin",
        filters={"warehouse": warehouse, "is_active": 1},
        fields=[
            "name", "bin_code", "zone_type",
            "aisle", "rack", "shelf", "level",
            "creation",
        ],
        order_by="creation asc",
    )

    if not bins:
        return items

    # Sort bins by zone priority (Picking first) then by creation (FIFO)
    bins.sort(key=lambda b: (_zone_sort_value(b.zone_type), b.creation))

    for item in items:
        # Skip items that already have a source bin assigned
        if item.get("source_bin"):
            continue

        # Check if item has available stock at the warehouse level
        stock_info = get_available_qty(item["item_code"], warehouse)
        if flt(stock_info.get("available_qty", 0)) <= 0:
            item["error_message"] = _(
                "No available stock for {0} in {1}"
            ).format(item["item_code"], warehouse)
            continue

        # Assign the first suitable bin (zone-priority, FIFO)
        # Since ERPNext tracks stock at warehouse level (not per-bin),
        # we assign the best bin based on zone priority for routing
        if bins:
            best_bin = bins[0]  # Already sorted by zone priority + creation
            item["source_bin"] = best_bin.name

            # Prefer Picking-zone bins specifically
            for b in bins:
                if b.zone_type == "Picking":
                    item["source_bin"] = b.name
                    break

    return items


# ─── SERPENTINE SORT ──────────────────────────────────────────────

def sort_items_serpentine(items):
    """
    Sort items in serpentine (snake) order for efficient warehouse traversal.

    Route logic:
    - Sort by: zone_type priority -> aisle (natural sort) -> rack (alternating) -> shelf -> level
    - Serpentine: odd-index aisles go rack ascending, even-index aisles go rack descending
    - This minimizes backtracking in the warehouse

    Args:
        items: list of dicts with source_bin populated

    Returns:
        list of items sorted in serpentine order with pick_sequence assigned
    """
    if not items:
        return items

    # Fetch bin location data for all source bins
    bin_names = [i.get("source_bin") for i in items if i.get("source_bin")]
    if not bin_names:
        # No bins assigned — just number them sequentially
        for seq, item in enumerate(items, 1):
            item["pick_sequence"] = seq
        return items

    bin_data = {}
    for bn in set(bin_names):
        data = frappe.db.get_value(
            "Warehouse Bin",
            bn,
            ["zone_type", "aisle", "rack", "shelf", "level"],
            as_dict=True,
        )
        if data:
            bin_data[bn] = data

    # Determine aisle ordering for serpentine direction
    unique_aisles = sorted(
        set(d.get("aisle", "") for d in bin_data.values() if d.get("aisle")),
        key=_natural_sort_key,
    )
    aisle_index = {aisle: idx for idx, aisle in enumerate(unique_aisles)}

    def _sort_key(item):
        bn = item.get("source_bin", "")
        data = bin_data.get(bn, {})

        zone = _zone_sort_value(data.get("zone_type", ""))
        aisle = data.get("aisle", "")
        rack = data.get("rack", "")
        shelf = data.get("shelf", "")
        level = data.get("level", "")

        aisle_key = _natural_sort_key(aisle)
        rack_key = _natural_sort_key(rack)
        shelf_key = _natural_sort_key(shelf)
        level_key = _natural_sort_key(level)

        # Serpentine: alternate rack direction per aisle
        aisle_idx = aisle_index.get(aisle, 0)
        if aisle_idx % 2 == 1:
            # Even aisles (0-indexed odd) — reverse rack direction
            rack_key = _invert_sort_key(rack_key)

        # Items without bins go to the end
        if not bn:
            return (999, [], [], [], [])

        return (zone, aisle_key, rack_key, shelf_key, level_key)

    items.sort(key=_sort_key)

    # Assign pick_sequence 1..N
    for seq, item in enumerate(items, 1):
        item["pick_sequence"] = seq

    return items


# ─── WHITELISTED APIs ─────────────────────────────────────────────

@frappe.whitelist()
def get_optimized_pick_route(task_name):
    """
    Preview API: Returns optimized pick sequence without saving.
    Shows what the route would look like after optimization.
    """
    task = frappe.get_doc("WMS Task", task_name)

    if task.task_type != "Pick":
        frappe.throw(_("Route optimization is only available for Pick tasks"))

    if task.status in ("Completed", "Cancelled"):
        frappe.throw(_("Cannot optimize a {0} task").format(task.status))

    # Build items list from child table
    items = []
    for row in task.items:
        items.append({
            "item_code": row.item_code,
            "item_name": row.item_name,
            "qty": flt(row.qty),
            "source_bin": row.source_bin or "",
            "batch_no": row.batch_no or "",
            "serial_no": row.serial_no or "",
            "error_message": "",
        })

    # Step 1: Allocate bins for items without source_bin
    items = allocate_bins_for_items(items, task.source_warehouse)

    # Step 2: Sort in serpentine order
    items = sort_items_serpentine(items)

    # Step 3: Enrich with location data for preview
    route = []
    for item in items:
        bin_info = {}
        if item.get("source_bin"):
            bin_info = frappe.db.get_value(
                "Warehouse Bin",
                item["source_bin"],
                ["bin_code", "aisle", "rack", "shelf", "level", "zone_type"],
                as_dict=True,
            ) or {}

        route.append({
            "pick_sequence": item.get("pick_sequence", 0),
            "item_code": item.get("item_code"),
            "item_name": item.get("item_name"),
            "qty": item.get("qty"),
            "source_bin": item.get("source_bin", ""),
            "bin_code": bin_info.get("bin_code", ""),
            "aisle": bin_info.get("aisle", ""),
            "rack": bin_info.get("rack", ""),
            "shelf": bin_info.get("shelf", ""),
            "level": bin_info.get("level", ""),
            "zone_type": bin_info.get("zone_type", ""),
            "error_message": item.get("error_message", ""),
        })

    return route


@frappe.whitelist()
def apply_optimized_route(task_name):
    """
    Persist API: Applies optimized pick route to the task.
    Reorders child table items and sets pick_sequence.
    """
    task = frappe.get_doc("WMS Task", task_name)

    if task.task_type != "Pick":
        frappe.throw(_("Route optimization is only available for Pick tasks"))

    if task.status in ("Completed", "Cancelled"):
        frappe.throw(_("Cannot optimize a {0} task").format(task.status))

    # Build items list from child table
    items = []
    for row in task.items:
        items.append({
            "item_code": row.item_code,
            "item_name": row.item_name,
            "qty": flt(row.qty),
            "actual_qty": flt(row.actual_qty),
            "uom": row.uom or "",
            "source_bin": row.source_bin or "",
            "target_bin": row.target_bin or "",
            "batch_no": row.batch_no or "",
            "serial_no": row.serial_no or "",
            "row_status": row.row_status or "Pending",
            "difference_qty": flt(row.difference_qty),
            "error_message": row.error_message or "",
        })

    # Step 1: Allocate bins
    items = allocate_bins_for_items(items, task.source_warehouse)

    # Step 2: Sort serpentine
    items = sort_items_serpentine(items)

    # Step 3: Rebuild child table in optimized order
    task.items = []
    for item in items:
        task.append("items", {
            "item_code": item["item_code"],
            "item_name": item.get("item_name", ""),
            "qty": item["qty"],
            "actual_qty": item.get("actual_qty", 0),
            "uom": item.get("uom", ""),
            "source_bin": item.get("source_bin", ""),
            "target_bin": item.get("target_bin", ""),
            "batch_no": item.get("batch_no", ""),
            "serial_no": item.get("serial_no", ""),
            "row_status": item.get("row_status", "Pending"),
            "pick_sequence": item.get("pick_sequence", 0),
            "difference_qty": item.get("difference_qty", 0),
            "error_message": item.get("error_message", ""),
        })

    task.save()

    frappe.msgprint(
        _("Pick route optimized: {0} items sequenced").format(len(task.items)),
        indicator="green",
    )

    return {"success": True, "items_count": len(task.items)}
