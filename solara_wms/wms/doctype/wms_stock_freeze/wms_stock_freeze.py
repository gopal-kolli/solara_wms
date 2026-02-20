import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class WMSStockFreeze(Document):
    """
    WMS Stock Freeze - Freeze/unfreeze inventory.
    Maps to ModernWMS StockFreezeEntity.

    Prevents stock movement for specific items/warehouses/bins/batches
    during audits, quality holds, or investigations.

    No child table - each freeze is a single scope record.
    """

    def validate(self):
        self.validate_scope()

    def validate_scope(self):
        """Ensure at least one scope field is specified."""
        if not any([self.item_code, self.warehouse, self.bin, self.batch_no]):
            frappe.throw(
                _("Specify at least one of: Item Code, Warehouse, Bin, or Batch No")
            )

    # ─── STATUS TRANSITIONS ──────────────────────────────────

    @frappe.whitelist()
    def activate_freeze(self):
        """Draft -> Active. Freezes the specified stock."""
        if self.status != "Draft":
            frappe.throw(_("Only Draft freezes can be activated"))

        self.frozen_by = frappe.session.user
        self.frozen_at = now_datetime()
        self.status = "Active"

        # If a specific bin is frozen, block it
        if self.bin:
            bin_doc = frappe.get_doc("Warehouse Bin", self.bin)
            bin_doc.set_status(new_status="Blocked")

        self.save()
        frappe.msgprint(
            _("Stock freeze activated"),
            indicator="orange"
        )

    @frappe.whitelist()
    def release_freeze(self):
        """Active -> Released. Unfreezes the specified stock."""
        if self.status != "Active":
            frappe.throw(_("Only Active freezes can be released"))

        self.released_by = frappe.session.user
        self.released_at = now_datetime()
        self.status = "Released"

        # If a specific bin was blocked, reactivate it
        if self.bin:
            try:
                bin_doc = frappe.get_doc("Warehouse Bin", self.bin)
                if bin_doc.status == "Blocked":
                    bin_doc.set_status(new_status="Active")
            except Exception:
                pass  # Bin may have been manually changed

        self.save()
        frappe.msgprint(
            _("Stock freeze released"),
            indicator="green"
        )

    @frappe.whitelist()
    def cancel_freeze(self):
        """Cancel the freeze."""
        if self.status == "Active" and self.bin:
            # Reactivate bin if it was blocked
            try:
                bin_doc = frappe.get_doc("Warehouse Bin", self.bin)
                if bin_doc.status == "Blocked":
                    bin_doc.set_status(new_status="Active")
            except Exception:
                pass

        self.status = "Cancelled"
        self.save()
        frappe.msgprint(_("Stock freeze cancelled"), indicator="red")
