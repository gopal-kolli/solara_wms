import hashlib

import frappe
from frappe import _
from frappe.model.document import Document


class WMSPackHandoff(Document):
    """Append-only evidence that completed picks entered one parcel pack."""

    def autoname(self):
        digest = hashlib.sha256((self.idempotency_key or "").encode("utf-8")).hexdigest()
        self.name = "WMS-PACK-" + digest[:20].upper()

    def validate(self):
        if not self.is_new():
            frappe.throw(_("WMS Pack Handoff is append-only and cannot be edited"))

    def on_trash(self):
        if not getattr(self.flags, "allow_wms_pack_handoff_delete", False):
            frappe.throw(_("WMS Pack Handoff is append-only and cannot be deleted"))
