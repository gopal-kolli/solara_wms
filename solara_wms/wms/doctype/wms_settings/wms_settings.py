import frappe
from frappe import _
from frappe.model.document import Document


class WMSSettings(Document):
    def validate(self):
        if self.operating_mode != "Disabled" and not self.pilot_warehouse:
            frappe.throw(_("Pilot Warehouse is required before enabling WMS"))
        if self.pilot_warehouse:
            is_group = frappe.db.get_value("Warehouse", self.pilot_warehouse, "is_group")
            if is_group:
                frappe.throw(_("Pilot Warehouse must be a leaf Warehouse"))
