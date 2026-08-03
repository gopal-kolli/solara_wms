import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class WMSBinBalance(Document):
    """Compact physical-bin state; never an ERPNext valuation ledger."""

    def autoname(self):
        key = "\x1f".join((self.warehouse or "", self.bin or "", self.item_code or ""))
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20].upper()
        self.name = "WMS-BAL-" + digest

    def validate(self):
        bin_warehouse = frappe.db.get_value("Warehouse Bin", self.bin, "warehouse")
        if bin_warehouse != self.warehouse:
            frappe.throw(
                _("Bin {0} does not belong to warehouse {1}").format(
                    self.bin, self.warehouse
                )
            )
        for field, label in (
            ("physical_qty", "Physical Quantity"),
            ("allocated_qty", "Allocated Quantity"),
            ("hold_qty", "Hold Quantity"),
        ):
            if flt(self.get(field)) < 0:
                frappe.throw(_("{0} cannot be negative").format(label))
        self.available_qty = (
            flt(self.physical_qty) - flt(self.allocated_qty) - flt(self.hold_qty)
        )
        if flt(self.available_qty) < 0:
            frappe.throw(_("Allocated plus Hold Quantity cannot exceed Physical Quantity"))
