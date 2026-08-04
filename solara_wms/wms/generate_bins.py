"""Deprecated legacy rack generator.

The warehouse is floor-loaded and now uses the controlled, preview-first
location master. This module is retained only to fail closed for old commands.
"""

import frappe


def create_all_bins(warehouse=None):
    frappe.throw(
        "Legacy rack-based bin generation is disabled. Use "
        "solara_wms.wms.location_master.preview_location_master followed by "
        "the controlled Draft import."
    )


def get_bin_summary():
    """Read-only summary retained for diagnostic compatibility."""
    return frappe.get_all(
        "Warehouse Bin",
        fields=["zone_type", "count(name) as count"],
        group_by="zone_type",
        order_by="zone_type",
    )


def delete_all_bins(confirm=False):
    frappe.throw("Bulk Warehouse Bin deletion is permanently disabled")
