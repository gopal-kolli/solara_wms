import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class WMSItemLocation(Document):
    """Warehouse-scoped SKU placement policy.

    This is operational master data only. It does not create an ERPNext
    warehouse or change Stock Ledger quantities.
    """

    def autoname(self):
        key = "\x1f".join(
            (self.item_code or "", self.warehouse or "", self.location_role or "")
        )
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16].upper()
        self.name = "WMS-LOC-" + digest

    def validate(self):
        self._validate_bin_scope()
        self._validate_quantities()
        self._validate_unique_role()

    def _validate_bin_scope(self):
        bin_warehouse = frappe.db.get_value("Warehouse Bin", self.bin, "warehouse")
        if bin_warehouse != self.warehouse:
            frappe.throw(
                _("Bin {0} does not belong to warehouse {1}").format(
                    self.bin, self.warehouse
                )
            )

    def _validate_quantities(self):
        if (self.priority or 0) < 0:
            frappe.throw(_("Allocation Priority cannot be negative"))
        for field, label in (
            ("minimum_qty", "Minimum Quantity"),
            ("maximum_qty", "Maximum Quantity"),
            ("replenish_qty", "Replenish Quantity"),
        ):
            if flt(self.get(field)) < 0:
                frappe.throw(_("{0} cannot be negative").format(label))
        if flt(self.maximum_qty) and flt(self.minimum_qty) > flt(self.maximum_qty):
            frappe.throw(_("Minimum Quantity cannot exceed Maximum Quantity"))

    def _validate_unique_role(self):
        existing = frappe.db.exists(
            "WMS Item Location",
            {
                "item_code": self.item_code,
                "warehouse": self.warehouse,
                "location_role": self.location_role,
                "name": ["!=", self.name or ""],
            },
        )
        if existing:
            frappe.throw(
                _("{0} already has a {1} location in {2}: {3}").format(
                    self.item_code, self.location_role, self.warehouse, existing
                )
            )
