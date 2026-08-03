import hashlib

import frappe
from frappe import _
from frappe.model.document import Document


class WMSWork(Document):
    """Bounded warehouse work created only by shadow-ledger services."""

    def autoname(self):
        digest = hashlib.sha256(
            (self.creation_idempotency_key or "").encode("utf-8")
        ).hexdigest()
        self.name = "WMS-WORK-" + digest[:20].upper()

    def validate(self):
        if not self.is_new():
            frappe.throw(_("WMS Work can only be changed through WMS commands"))
        if len(self.lines or []) != 1:
            frappe.throw(_("Phase 1 WMS Work must contain exactly one line"))
        line = self.lines[0]
        for bin_name in (line.source_bin, line.target_bin):
            if not bin_name:
                continue
            bin_warehouse = frappe.db.get_value("Warehouse Bin", bin_name, "warehouse")
            if bin_warehouse != self.warehouse:
                frappe.throw(
                    _("Bin {0} does not belong to warehouse {1}").format(
                        bin_name, self.warehouse
                    )
                )
