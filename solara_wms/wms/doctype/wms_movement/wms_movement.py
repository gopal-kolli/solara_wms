import hashlib

import frappe
from frappe import _
from frappe.model.document import Document


class WMSMovement(Document):
    """Append-only physical movement evidence."""

    def autoname(self):
        digest = hashlib.sha256((self.idempotency_key or "").encode("utf-8")).hexdigest()
        self.name = "WMS-MOVE-" + digest[:20].upper()

    def validate(self):
        if not self.is_new():
            frappe.throw(_("WMS Movement is append-only and cannot be edited"))
        if self.source_bin and self.target_bin and self.source_bin == self.target_bin:
            frappe.throw(_("Source and Target Bin must be different"))

    def on_trash(self):
        if not getattr(self.flags, "allow_wms_movement_delete", False):
            frappe.throw(_("WMS Movement is append-only and cannot be deleted"))
