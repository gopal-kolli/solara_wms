import frappe
from frappe.model.document import Document


class WMSKittingBOM(Document):
    def validate(self):
        self.validate_components()
        self.validate_no_circular_ref()

    def validate_components(self):
        if not self.components:
            frappe.throw("At least one component item is required.")

        seen = set()
        for row in self.components:
            if row.item_code in seen:
                frappe.throw(f"Duplicate component: {row.item_code}")
            seen.add(row.item_code)

            if row.qty <= 0:
                frappe.throw(f"Qty must be > 0 for {row.item_code}")

            # Component cannot be the kit itself
            if row.item_code == self.kit_item:
                frappe.throw("A kit cannot contain itself as a component.")

    def validate_no_circular_ref(self):
        """Ensure no circular BOM references (kit A -> kit B -> kit A)."""
        for row in self.components:
            child_bom = frappe.db.exists(
                "WMS Kitting BOM",
                {"kit_item": row.item_code, "is_active": 1}
            )
            if child_bom:
                child_doc = frappe.get_doc("WMS Kitting BOM", child_bom)
                for child_row in child_doc.components:
                    if child_row.item_code == self.kit_item:
                        frappe.throw(
                            f"Circular reference: {self.kit_item} -> {row.item_code} -> {self.kit_item}"
                        )

    @frappe.whitelist()
    def get_component_availability(self, warehouse=None):
        """Check stock availability for all components."""
        from solara_wms.wms.utils import get_available_qty

        if not warehouse:
            warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")

        result = []
        all_available = True
        for row in self.components:
            qty_data = get_available_qty(row.item_code, warehouse)
            available = qty_data.get("available_qty", 0)
            can_make = int(available / row.qty) if row.qty > 0 else 0
            if can_make < 1:
                all_available = False

            result.append({
                "item_code": row.item_code,
                "item_name": row.item_name,
                "required_per_kit": row.qty,
                "available_qty": available,
                "can_make": can_make,
            })

        return {
            "components": result,
            "all_available": all_available,
            "max_kits": min(r["can_make"] for r in result) if result else 0,
        }
