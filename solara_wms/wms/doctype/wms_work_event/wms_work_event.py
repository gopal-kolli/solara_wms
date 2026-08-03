import hashlib

import frappe
from frappe import _
from frappe.model.document import Document


class WMSWorkEvent(Document):
    """Append-only command evidence for WMS work state changes."""

    def autoname(self):
        digest = hashlib.sha256((self.idempotency_key or "").encode("utf-8")).hexdigest()
        self.name = "WMS-WEVT-" + digest[:20].upper()

    def validate(self):
        if not self.is_new():
            frappe.throw(_("WMS Work Event is append-only and cannot be edited"))

    def on_trash(self):
        if not getattr(self.flags, "allow_wms_work_event_delete", False):
            frappe.throw(_("WMS Work Event is append-only and cannot be deleted"))
