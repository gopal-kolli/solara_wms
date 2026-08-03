import hashlib

import frappe
from frappe import _
from frappe.model.document import Document


class WMSCountEntry(Document):
    """Append-only evidence for one blind count attempt."""

    def autoname(self):
        identity = f"{self.cycle_count_item}\x1f{self.attempt}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self.name = "WMS-COUNT-" + digest[:20].upper()

    def validate(self):
        if not self.is_new():
            frappe.throw(_("WMS Count Entry is append-only and cannot be edited"))

    def on_trash(self):
        if not getattr(self.flags, "allow_wms_count_entry_delete", False):
            frappe.throw(_("WMS Count Entry is append-only and cannot be deleted"))
