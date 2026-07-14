"""
Generate Warehouse Bin records for SOLARA new warehouse layout.

Two sheds:
  - Back Shed (166x200 ft): Receiving, Pallet Storage, Heavy Shelving, Overstock, Kitting
  - Front Shed (187x199 ft): Pick Zones A/B/C, Small Bins, Pack, Returns, Defective, Dispatch

Run: bench execute solara_wms.wms.generate_bins.create_all_bins
"""

import frappe


# ── Zone definitions ────────────────────────────────────────────────────────

ZONES = [
    # Back Shed - Receiving
    {"prefix": "RCV", "zone_type": "Receiving", "aisles": 2, "racks": 3, "levels": 1,
     "length": 120, "width": 80, "height": 100, "max_weight": 500,
     "desc": "Receiving dock areas"},

    # Back Shed - Pallet Racking (Appliances)
    {"prefix": "P", "zone_type": "Stocking", "aisles": 5, "racks": 6, "levels": 4,
     "length": 120, "width": 100, "height": 150, "max_weight": 1000,
     "desc": "Pallet racking - Appliances bulk storage"},

    # Back Shed - Heavy Duty Shelving (Cast Iron)
    {"prefix": "H", "zone_type": "Stocking", "aisles": 3, "racks": 8, "levels": 4,
     "length": 120, "width": 75, "height": 50, "max_weight": 500,
     "desc": "Heavy shelving - Cast Iron & Metal cookware"},

    # Back Shed - Overstock Reserve
    {"prefix": "OV", "zone_type": "Stocking", "aisles": 2, "racks": 10, "levels": 4,
     "length": 120, "width": 100, "height": 150, "max_weight": 1000,
     "desc": "Overstock pallet reserve"},

    # Back Shed - Kitting Staging
    {"prefix": "K", "zone_type": "Staging", "aisles": 3, "racks": 2, "levels": 3,
     "length": 100, "width": 60, "height": 40, "max_weight": 200,
     "desc": "Kitting line staging bins"},

    # Connection - Staging
    {"prefix": "STG", "zone_type": "Staging", "aisles": 2, "racks": 5, "levels": 2,
     "length": 120, "width": 100, "height": 100, "max_weight": 500,
     "desc": "Transfer staging between sheds"},

    # Front Shed - Zone A (High velocity flow racks)
    {"prefix": "A", "zone_type": "Picking", "aisles": 8, "racks": 7, "levels": 4,
     "length": 120, "width": 120, "height": 45, "max_weight": 100,
     "desc": "Zone A - High velocity pick (flow racks)"},

    # Front Shed - Zone B (Medium velocity shelving)
    {"prefix": "B", "zone_type": "Picking", "aisles": 6, "racks": 7, "levels": 5,
     "length": 120, "width": 60, "height": 40, "max_weight": 80,
     "desc": "Zone B - Medium velocity pick (shelving)"},

    # Front Shed - Zone C (Low velocity)
    {"prefix": "C", "zone_type": "Picking", "aisles": 2, "racks": 6, "levels": 5,
     "length": 120, "width": 60, "height": 40, "max_weight": 80,
     "desc": "Zone C - Low velocity pick"},

    # Front Shed - Small Bins
    {"prefix": "S", "zone_type": "Picking", "aisles": 2, "racks": 5, "levels": 6,
     "length": 45, "width": 30, "height": 25, "max_weight": 20,
     "desc": "Small bin shelving - Accessories"},

    # Front Shed - Returns
    {"prefix": "RET", "zone_type": "Return", "aisles": 1, "racks": 6, "levels": 5,
     "length": 120, "width": 60, "height": 40, "max_weight": 100,
     "desc": "Returns processing area"},

    # Front Shed - Defective
    {"prefix": "DEF", "zone_type": "Defective", "aisles": 1, "racks": 4, "levels": 5,
     "length": 120, "width": 60, "height": 40, "max_weight": 100,
     "desc": "Defective/quarantine area"},

    # Front Shed - Dispatch staging
    {"prefix": "DSP", "zone_type": "Staging", "aisles": 3, "racks": 2, "levels": 2,
     "length": 150, "width": 120, "height": 120, "max_weight": 500,
     "desc": "Dispatch dock staging"},
]


def generate_bin_code(prefix, aisle, rack, level):
    """Generate bin code: PREFIX-AA-RR-L"""
    return f"{prefix}-{aisle:02d}-{rack:02d}-{level}"


def create_all_bins(warehouse=None):
    """
    Create all warehouse bin records for the new warehouse layout.

    Args:
        warehouse: ERPNext Warehouse name. If None, uses first leaf warehouse.
    """
    if not warehouse:
        # Try to find a suitable warehouse
        warehouses = frappe.get_all(
            "Warehouse",
            filters={"is_group": 0, "disabled": 0},
            pluck="name",
            limit=1
        )
        if not warehouses:
            frappe.throw("No active warehouse found. Please create a warehouse first.")
        warehouse = warehouses[0]

    total_created = 0
    total_skipped = 0

    for zone in ZONES:
        prefix = zone["prefix"]
        zone_type = zone["zone_type"]
        created = 0

        for aisle in range(1, zone["aisles"] + 1):
            for rack in range(1, zone["racks"] + 1):
                for level in range(1, zone["levels"] + 1):
                    bin_code = generate_bin_code(prefix, aisle, rack, level)

                    # Skip if already exists
                    if frappe.db.exists("Warehouse Bin", {"bin_code": bin_code}):
                        total_skipped += 1
                        continue

                    doc = frappe.get_doc({
                        "doctype": "Warehouse Bin",
                        "warehouse": warehouse,
                        "bin_code": bin_code,
                        "zone_type": zone_type,
                        "status": "Active",
                        "is_active": 1,
                        "aisle": f"{prefix}-{aisle:02d}",
                        "rack": f"{rack:02d}",
                        "shelf": "01",
                        "level": str(level),
                        "bin_length": zone["length"],
                        "bin_width": zone["width"],
                        "bin_height": zone["height"],
                        "max_weight": zone["max_weight"],
                        "notes": zone["desc"],
                    })
                    doc.insert(ignore_permissions=True)
                    created += 1

        total_created += created
        print(f"  {prefix}: Created {created} bins ({zone_type})")

    frappe.db.commit()
    print(f"\nTotal: {total_created} bins created, {total_skipped} skipped (already exist)")
    print(f"Warehouse: {warehouse}")
    return total_created


def get_bin_summary():
    """Print summary of all bins by zone."""
    bins = frappe.get_all(
        "Warehouse Bin",
        fields=["zone_type", "count(name) as cnt"],
        group_by="zone_type",
        order_by="zone_type"
    )
    print("\n=== Warehouse Bin Summary ===")
    total = 0
    for b in bins:
        print(f"  {b.zone_type}: {b.cnt} bins")
        total += b.cnt
    print(f"  TOTAL: {total} bins")


def delete_all_bins(confirm=False):
    """Delete all warehouse bins. Use with caution."""
    if not confirm:
        print("Pass confirm=True to actually delete bins.")
        return

    count = frappe.db.count("Warehouse Bin")
    frappe.db.delete("Warehouse Bin")
    frappe.db.commit()
    print(f"Deleted {count} bins.")
