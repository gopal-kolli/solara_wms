import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, time_diff_in_seconds


class WMSKittingOrder(Document):
    def validate(self):
        self.validate_qty()
        self.set_output_item()

    def before_submit(self):
        if self.status not in ("Draft", "In Progress"):
            frappe.throw("Only Draft or In Progress orders can be submitted.")

    def on_submit(self):
        self.db_set("status", "In Progress")
        if not self.started_at:
            self.db_set("started_at", now_datetime())

    def on_cancel(self):
        self.db_set("status", "Cancelled")

    def validate_qty(self):
        if self.qty_to_kit <= 0:
            frappe.throw("Qty to Kit must be greater than 0.")

    def set_output_item(self):
        if self.kitting_bom and not self.kit_item:
            self.kit_item = frappe.db.get_value(
                "WMS Kitting BOM", self.kitting_bom, "kit_item"
            )
        self.output_item = self.kit_item

    @frappe.whitelist()
    def populate_items_from_bom(self):
        """Pull component items from the linked Kitting BOM, scaled by qty_to_kit."""
        if not self.kitting_bom:
            frappe.throw("Please select a Kitting BOM first.")

        bom = frappe.get_doc("WMS Kitting BOM", self.kitting_bom)
        self.items = []
        for comp in bom.components:
            self.append("items", {
                "item_code": comp.item_code,
                "item_name": comp.item_name,
                "required_qty": comp.qty * self.qty_to_kit,
                "picked_qty": 0,
                "uom": comp.uom,
                "source_bin": comp.source_bin,
                "row_status": "Pending",
            })

        self.kit_item = bom.kit_item
        self.kit_item_name = bom.kit_item_name
        self.output_item = bom.kit_item
        if not self.output_warehouse:
            self.output_warehouse = self.warehouse

    @frappe.whitelist()
    def complete_kitting(self):
        """Mark order as completed and create a Repack stock entry."""
        if self.status != "In Progress":
            frappe.throw("Only In Progress orders can be completed.")

        # Check all items picked
        for row in self.items:
            if row.picked_qty < row.required_qty:
                frappe.throw(
                    f"Item {row.item_code} not fully picked: "
                    f"{row.picked_qty}/{row.required_qty}"
                )

        # Create Repack stock entry (consume components, produce kit)
        se = frappe.new_doc("Stock Entry")
        se.stock_entry_type = "Repack"
        se.purpose = "Repack"

        # Source items (components consumed)
        for row in self.items:
            se.append("items", {
                "item_code": row.item_code,
                "qty": row.required_qty,
                "s_warehouse": self.warehouse,
                "uom": row.uom or frappe.db.get_value("Item", row.item_code, "stock_uom"),
            })

        # Target item (assembled kit)
        se.append("items", {
            "item_code": self.kit_item,
            "qty": self.qty_to_kit,
            "t_warehouse": self.output_warehouse or self.warehouse,
            "uom": frappe.db.get_value("Item", self.kit_item, "stock_uom"),
            "is_finished_item": 1,
        })

        se.insert()
        se.submit()

        # Update kitting order
        completed_at = now_datetime()
        time_mins = 0
        if self.started_at:
            time_mins = round(
                time_diff_in_seconds(completed_at, self.started_at) / 60, 1
            )

        self.db_set({
            "status": "Completed",
            "qty_completed": self.qty_to_kit,
            "completed_at": completed_at,
            "time_taken_mins": time_mins,
            "stock_entry": se.name,
        })

        # Update child row statuses
        for row in self.items:
            frappe.db.set_value(
                "WMS Kitting Order Item", row.name, "row_status", "Picked"
            )

        frappe.msgprint(
            f"Kitting complete! Stock Entry {se.name} created.",
            indicator="green",
        )
        return se.name
