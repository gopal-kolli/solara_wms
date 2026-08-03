import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from solara_wms.wms import inventory_accuracy


class WMSCycleCount(Document):
    """Frozen, bin-level blind count controlled through scanner APIs."""

    def validate(self):
        self.total_items = len(self.items or [])
        self.items_with_variance = sum(
            1 for row in (self.items or [])
            if row.row_status in ("Confirmed Variance", "Counter Disagreement")
        )
        self.total_variance_value = sum(
            flt(row.variance_value) for row in (self.items or [])
        )

    @frappe.whitelist()
    def populate_items_from_warehouse(self):
        """Preview WMS balances while Draft; start_count takes the final snapshot."""
        if self.status != "Draft":
            frappe.throw(_("Count scope cannot change after counting starts"))
        if not self.warehouse or not self.bin:
            frappe.throw(_("Select a warehouse and physical bin first"))
        balances = frappe.get_all(
            "WMS Bin Balance",
            filters={"warehouse": self.warehouse, "bin": self.bin},
            fields=["name", "item_code", "physical_qty", "last_movement"],
            order_by="item_code asc",
        )
        if not balances:
            frappe.throw(_("No WMS balances were found in this physical bin"))
        self.items = []
        for balance in balances:
            item = frappe.db.get_value(
                "Item", balance.item_code, ["item_name", "stock_uom"], as_dict=True
            )
            valuation_rate = frappe.db.get_value(
                "Bin",
                {"item_code": balance.item_code, "warehouse": self.warehouse},
                "valuation_rate",
            )
            self.append(
                "items",
                {
                    "item_code": balance.item_code,
                    "item_name": item.item_name if item else "",
                    "uom": item.stock_uom if item else "",
                    "bin": self.bin,
                    "snapshot_balance": balance.name,
                    "snapshot_movement": balance.last_movement,
                    "book_qty": flt(balance.physical_qty),
                    "valuation_rate": flt(valuation_rate),
                    "row_status": "Pending",
                },
            )
        self.save()
        return {"items": len(self.items)}

    @frappe.whitelist()
    def fetch_book_quantities(self):
        """Refresh the Draft preview only; active snapshots are immutable."""
        if self.status != "Draft":
            frappe.throw(_("Reference quantities cannot change after counting starts"))
        return self.populate_items_from_warehouse()

    @frappe.whitelist()
    def start_count(self):
        return inventory_accuracy.start_blind_count(self.name)

    @frappe.whitelist()
    def complete_count(self):
        return inventory_accuracy.finalize_blind_count(self.name)

    @frappe.whitelist()
    def cancel_count(self):
        if self.status == "Completed":
            frappe.throw(_("Completed counts cannot be cancelled"))
        self.status = "Cancelled"
        self.save()
        return {"status": self.status}
