import hashlib
import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from solara_wms.wms.location_domain import LocationMasterError, qr_payload


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
        if not self.location_id:
            self.location_id = "LEGACY-L" + digest[:8]

    def validate(self):
        self.validate_location_identity()
        self.validate_commissioning_state()
        self.calculate_volume()
        self.validate_warehouse()
        self.generate_bin_code_if_empty()
        self.validate_unique_bin_code()
        self.validate_route_sequence()

    def validate_commissioning_state(self):
        """Fail closed when the operational flags disagree with commissioning."""
        old_status = (
            frappe.db.get_value("Warehouse Bin", self.name, "commissioning_status")
            if self.name and not self.is_new()
            else None
        )
        if (
            old_status
            and old_status != self.commissioning_status
            and not self.flags.get("controlled_location_transition")
            and not self.location_id.startswith("LEGACY-")
        ):
            frappe.throw(_("Use the controlled location commissioning workflow to change status"))
        if self.commissioning_status == "Active":
            if not self.marking_evidence or not self.field_verified_by or not self.baseline_reference:
                frappe.throw(_("Active locations require marking, field verification and a signed baseline reference"))
            self.is_active = 1
            if self.status in ("Blocked", "Maintenance"):
                self.status = "Active"
        elif not self.location_id.startswith("LEGACY-"):
            self.is_active = 0
            self.status = "Blocked"

    def on_trash(self):
        references = (
            ("WMS Bin Balance", {"bin": self.name}),
            ("WMS Item Location", {"bin": self.name}),
            ("WMS Movement", {"source_bin": self.name}),
            ("WMS Movement", {"target_bin": self.name}),
            ("WMS Work Line", {"source_bin": self.name}),
        )
        for doctype, filters in references:
            if frappe.db.exists(doctype, filters):
                frappe.throw(
                    _("Location {0} has WMS history and cannot be deleted; retire it instead").format(
                        self.location_id
                    )
                )

    def validate_location_identity(self):
        self.location_id = (self.location_id or "").strip().upper()
        if not self.location_id:
            digest = hashlib.sha1(
                (self.name or self.bin_code or "").encode("utf-8")
            ).hexdigest()[:8].upper()
            self.location_id = "LEGACY-L" + digest
        if not re.fullmatch(
            r"(?:[A-Z0-9]{2,8}-L[0-9]{4,8}|LEGACY-L[A-F0-9]{8})",
            self.location_id,
        ):
            frappe.throw(_("Location ID must match HYD-L0001 style"))
        old = (
            frappe.db.get_value("Warehouse Bin", self.name, "location_id")
            if self.name
            else None
        )
        if old and old != self.location_id:
            frappe.throw(_("Location ID is immutable and cannot be changed"))
        duplicate = frappe.db.exists(
            "Warehouse Bin",
            {"location_id": self.location_id, "name": ["!=", self.name or ""]},
        )
        if duplicate:
            frappe.throw(_("Location ID {0} already exists").format(self.location_id))
        try:
            self.qr_payload = (
                qr_payload(self.location_id)
                if not self.location_id.startswith("LEGACY-")
                else "SOLARA:LOC:" + self.location_id
            )
        except LocationMasterError as exc:
            frappe.throw(_(str(exc)))
        self.barcode = self.qr_payload
        if self.bin_length and self.bin_width:
            self.floor_area_sq_ft = (
                flt(self.bin_length) * flt(self.bin_width) / (30.48 * 30.48)
            )

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
