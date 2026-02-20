import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class WMSPackStation(Document):
    """
    WMS Pack Station - Scan-to-verify packing station.
    Operator scans items, assigns to packages, records weights.

    Workflow: Draft -> Packing -> Completed

    On completion:
      - Creates Delivery Note (Draft)
      - Creates Packing Slip (while DN is Draft)
      - Submits DN
    """

    def validate(self):
        self.update_totals()

    def update_totals(self):
        """Update summary counts."""
        self.total_to_pack = sum(flt(r.ordered_qty) for r in (self.items or []))
        self.total_packed = sum(flt(r.packed_qty) for r in (self.items or []))
        self.total_packages = len(self.packages or [])
        self.total_weight = sum(
            flt(r.gross_weight) for r in (self.packages or [])
        )

        # Update remaining_qty for each item
        for row in self.items or []:
            row.remaining_qty = flt(row.ordered_qty) - flt(row.packed_qty)

    @frappe.whitelist()
    def populate_from_sales_order(self):
        """Fetch items from Sales Order and populate items table."""
        if not self.sales_order:
            frappe.throw(_("Select a Sales Order first"))

        so = frappe.get_doc("Sales Order", self.sales_order)
        self.items = []

        for so_item in so.items:
            # Try to get barcode
            barcode = None
            barcodes = frappe.get_all(
                "Item Barcode",
                filters={"parent": so_item.item_code, "parenttype": "Item"},
                fields=["barcode"],
                limit=1,
            )
            if barcodes:
                barcode = barcodes[0].barcode

            self.append("items", {
                "item_code": so_item.item_code,
                "item_name": so_item.item_name,
                "ordered_qty": flt(so_item.qty),
                "packed_qty": 0,
                "remaining_qty": flt(so_item.qty),
                "uom": so_item.uom or so_item.stock_uom,
                "barcode": barcode or "",
            })

        self.update_totals()
        self.save()

        frappe.msgprint(
            _("Fetched {0} items from Sales Order").format(len(self.items)),
            indicator="green"
        )

    @frappe.whitelist()
    def scan_item(self, barcode=None):
        """
        Match barcode to item in items table.
        Increment packed_qty and assign to current open package.
        Returns scan result for UI feedback.
        """
        if not barcode:
            return {"success": False, "message": "No barcode provided"}

        if self.status != "Packing":
            return {"success": False, "message": "Pack station is not in Packing status"}

        # Find matching item
        matched_row = None
        for row in self.items or []:
            if row.barcode == barcode or row.item_code == barcode:
                if flt(row.remaining_qty) > 0:
                    matched_row = row
                    break

        if not matched_row:
            # Check if item exists but is fully packed
            for row in self.items or []:
                if row.barcode == barcode or row.item_code == barcode:
                    return {
                        "success": False,
                        "message": f"Item {row.item_code} is fully packed",
                    }
            return {"success": False, "message": f"Barcode {barcode} not found in items"}

        # Find current open package
        current_package = None
        for pkg in self.packages or []:
            if pkg.row_status == "Open":
                current_package = pkg
                break

        if not current_package:
            return {
                "success": False,
                "message": "No open package. Add a new package first.",
            }

        # Pack the item
        matched_row.packed_qty = flt(matched_row.packed_qty) + 1
        matched_row.remaining_qty = flt(matched_row.ordered_qty) - flt(matched_row.packed_qty)
        matched_row.package_no = current_package.package_no

        if flt(matched_row.remaining_qty) <= 0:
            matched_row.row_status = "Packed"

        self.update_totals()
        self.save()

        return {
            "success": True,
            "message": f"Packed 1x {matched_row.item_code} into Package {current_package.package_no}",
            "item_code": matched_row.item_code,
            "packed_qty": matched_row.packed_qty,
            "remaining_qty": matched_row.remaining_qty,
        }

    @frappe.whitelist()
    def add_package(self):
        """Create a new package row with next sequential package_no."""
        next_no = len(self.packages or []) + 1
        self.append("packages", {
            "package_no": next_no,
            "package_type": "Box",
            "row_status": "Open",
        })
        self.update_totals()
        self.save()

        frappe.msgprint(
            _("Package {0} added").format(next_no),
            indicator="green"
        )

    @frappe.whitelist()
    def seal_package(self, package_no=None):
        """Mark a package as Sealed."""
        if not package_no:
            frappe.throw(_("Specify package number to seal"))

        for pkg in self.packages or []:
            if pkg.package_no == int(package_no) and pkg.row_status == "Open":
                pkg.row_status = "Sealed"
                self.save()
                frappe.msgprint(
                    _("Package {0} sealed").format(package_no),
                    indicator="green"
                )
                return

        frappe.throw(_("Open package {0} not found").format(package_no))

    # ─── STATUS TRANSITIONS ──────────────────────────────────

    @frappe.whitelist()
    def start_packing(self):
        """Draft -> Packing."""
        if self.status != "Draft":
            frappe.throw(_("Only Draft pack stations can start packing"))
        if not self.items:
            frappe.throw(_("Add items before starting"))

        self.packer = self.packer or frappe.session.user
        self.started_at = now_datetime()
        self.status = "Packing"

        # Auto-create first package
        if not self.packages:
            self.append("packages", {
                "package_no": 1,
                "package_type": "Box",
                "row_status": "Open",
            })

        self.update_totals()
        self.save()
        frappe.msgprint(_("Packing started. Scan items to pack."), indicator="blue")

    @frappe.whitelist()
    def complete_packing(self):
        """
        Packing -> Completed.
        Creates Delivery Note and Packing Slip.
        IMPORTANT: DN must be Draft when creating Packing Slip.
        """
        if self.status != "Packing":
            frappe.throw(_("Only pack stations in Packing status can be completed"))

        error_log = []

        # Mark remaining items as Short
        for row in self.items or []:
            if flt(row.remaining_qty) > 0:
                row.row_status = "Short"
            elif flt(row.packed_qty) >= flt(row.ordered_qty):
                row.row_status = "Packed"

        # Seal any open packages
        for pkg in self.packages or []:
            if pkg.row_status == "Open":
                pkg.row_status = "Sealed"

        # Step 1: Create Delivery Note (Draft)
        dn = None
        try:
            dn = frappe.new_doc("Delivery Note")
            dn.customer = self.customer
            dn.company = (
                frappe.defaults.get_user_default("company")
                or "Win The Buy Box Private Limited"
            )

            for row in self.items or []:
                if flt(row.packed_qty) <= 0:
                    continue
                dn_item = {
                    "item_code": row.item_code,
                    "qty": flt(row.packed_qty),
                    "warehouse": self.warehouse or "",
                    "batch_no": row.batch_no or "",
                    "serial_no": row.serial_no or "",
                }
                if self.sales_order:
                    dn_item["against_sales_order"] = self.sales_order
                dn.append("items", dn_item)

            if dn.items:
                dn.insert()
                self.delivery_note = dn.name
            else:
                error_log.append("No packed items to create Delivery Note for.")

        except Exception as e:
            error_log.append(f"Delivery Note creation failed: {str(e)}")
            dn = None

        # Step 2: Create Packing Slip (while DN is Draft)
        if dn and not error_log:
            try:
                ps = frappe.new_doc("Packing Slip")
                ps.delivery_note = dn.name

                for row in self.items or []:
                    if flt(row.packed_qty) <= 0:
                        continue
                    ps.append("items", {
                        "item_code": row.item_code,
                        "qty": flt(row.packed_qty),
                    })

                ps.gross_weight_pkg = flt(self.total_weight)
                ps.insert()
                self.packing_slip = ps.name

            except Exception as e:
                error_log.append(f"Packing Slip creation failed: {str(e)}")

        # Step 3: Submit Delivery Note
        if dn and self.delivery_note:
            try:
                dn.reload()
                dn.submit()
            except Exception as e:
                error_log.append(f"Delivery Note submission failed: {str(e)}")

        self.completed_at = now_datetime()
        self.status = "Completed"
        self.update_totals()
        self.error_log = "\n".join(error_log) if error_log else ""
        self.save()

        if error_log:
            frappe.msgprint(
                _("Packing completed with warnings. Check Error Log."),
                indicator="orange"
            )
        else:
            frappe.msgprint(
                _("Packing completed. DN: {0}").format(
                    f'<a href="/app/delivery-note/{self.delivery_note}">'
                    f'{self.delivery_note}</a>'
                ),
                indicator="green"
            )

        return {
            "status": self.status,
            "delivery_note": self.delivery_note,
            "packing_slip": self.packing_slip,
            "errors": error_log,
        }

    @frappe.whitelist()
    def cancel_packing(self):
        """Cancel packing (any status except Completed)."""
        if self.status == "Completed":
            frappe.throw(_("Completed pack stations cannot be cancelled"))

        self.status = "Cancelled"
        self.save()
        frappe.msgprint(_("Packing cancelled"), indicator="red")
