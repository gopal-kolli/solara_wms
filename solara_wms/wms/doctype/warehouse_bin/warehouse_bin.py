import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class WarehouseBin(Document):
    """
    Warehouse Bin - physical storage location within an ERPNext Warehouse.
    Maps to ModernWMS GoodsLocation + WarehouseArea concepts.

    ModernWMS field mapping:
      warehouse    -> warehouse_id (FK to Warehouse)
      bin_code     -> location_name
      zone_type    -> area_property (0=Picking,1=Stocking,2=Receiving,3=Return,4=Defective,5=Staging)
      aisle        -> roadway_number
      rack         -> shelf_number
      shelf        -> layer_number
      level        -> tag_number
      bin_length   -> location_length
      bin_width    -> location_width
      bin_height   -> location_heigth
      bin_volume   -> location_volume (auto-calculated)
      max_weight   -> location_load
      is_active    -> is_valid
    """

    def autoname(self):
        # Location codes such as FP-01 may be reused at another SOLARA site;
        # the document identity is the warehouse + code pair.
        self.generate_bin_code_if_empty()
        key = "\x1f".join((self.warehouse or "", self.bin_code or ""))
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16].upper()
        self.name = "WMS-BIN-" + digest

    def validate(self):
        self.calculate_volume()
        self.validate_warehouse()
        self.generate_bin_code_if_empty()
        self.validate_unique_bin_code()
        self.validate_route_sequence()

    def calculate_volume(self):
        """Auto-calculate volume from dimensions (ModernWMS: location_volume)"""
        if self.bin_length and self.bin_width and self.bin_height:
            self.bin_volume = flt(self.bin_length) * flt(self.bin_width) * flt(self.bin_height)
        elif not self.bin_volume:
            self.bin_volume = 0

    def validate_warehouse(self):
        """Ensure the linked warehouse is a leaf node (not a group)"""
        if self.warehouse:
            is_group = frappe.db.get_value("Warehouse", self.warehouse, "is_group")
            if is_group:
                frappe.throw(
                    _("Warehouse Bin must be linked to a leaf Warehouse, not a Warehouse Group. "
                      "'{0}' is a group.").format(self.warehouse)
                )

    def generate_bin_code_if_empty(self):
        """Auto-generate bin_code from aisle/rack/shelf/level if not manually set"""
        if not self.bin_code and self.aisle:
            parts = [self.aisle]
            if self.rack:
                parts.append(self.rack)
            if self.shelf:
                parts.append(self.shelf)
            if self.level:
                parts.append(self.level)
            self.bin_code = "-".join(parts)

    def validate_route_sequence(self):
        if (self.route_sequence or 0) < 0:
            frappe.throw(_("Pick Route Sequence cannot be negative"))

    def validate_unique_bin_code(self):
        existing = frappe.db.exists(
            "Warehouse Bin",
            {
                "warehouse": self.warehouse,
                "bin_code": self.bin_code,
                "name": ["!=", self.name or ""],
            },
        )
        if existing:
            frappe.throw(
                _("Bin Code {0} already exists in warehouse {1}").format(
                    self.bin_code, self.warehouse
                )
            )

    @frappe.whitelist()
    def set_status(self, new_status):
        """Change bin status (Active, Full, Blocked, Maintenance)"""
        valid_statuses = ["Active", "Full", "Blocked", "Maintenance"]
        if new_status not in valid_statuses:
            frappe.throw(_("Invalid status: {0}. Must be one of: {1}").format(
                new_status, ", ".join(valid_statuses)
            ))
        self.status = new_status
        self.save()
        frappe.msgprint(
            _("Bin {0} status changed to {1}").format(self.bin_code, new_status),
            indicator="green" if new_status == "Active" else "orange"
        )
