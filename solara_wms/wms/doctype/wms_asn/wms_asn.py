import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class WMSASN(Document):
    """
    WMS ASN (Advanced Shipping Notice) - Inbound Receiving.
    Maps to ModernWMS AsnMaster + AsnEntity.

    Workflow: Draft -> Confirmed -> Arrived -> Unloading -> Sorted
              -> Putaway Created -> Completed

    On completion:
      - Creates Purchase Receipt from received quantities
      - Creates WMS Task (Putaway) for bin placement
    """

    def validate(self):
        self.update_totals()
        if not self.asn_no:
            self.asn_no = self.name or ""

    def update_totals(self):
        """Update summary totals from item rows."""
        self.total_expected = sum(flt(r.expected_qty) for r in (self.items or []))
        self.total_received = sum(flt(r.received_qty) for r in (self.items or []))
        self.total_shortage = sum(flt(r.shortage_qty) for r in (self.items or []))
        self.total_damage = sum(flt(r.damage_qty) for r in (self.items or []))

    # ─── STATUS TRANSITIONS ──────────────────────────────────

    @frappe.whitelist()
    def confirm_asn(self):
        """Draft -> Confirmed"""
        if self.status != "Draft":
            frappe.throw(_("Only Draft ASNs can be confirmed"))
        if not self.items:
            frappe.throw(_("Add items before confirming the ASN"))

        self.status = "Confirmed"
        self.save()
        frappe.msgprint(_("ASN confirmed"), indicator="blue")

    @frappe.whitelist()
    def mark_arrived(self):
        """Confirmed -> Arrived"""
        if self.status != "Confirmed":
            frappe.throw(_("Only Confirmed ASNs can be marked as arrived"))

        self.status = "Arrived"
        self.actual_arrival = now_datetime()
        self.save()
        frappe.msgprint(_("ASN marked as arrived"), indicator="blue")

    @frappe.whitelist()
    def start_unloading(self):
        """Arrived -> Unloading"""
        if self.status != "Arrived":
            frappe.throw(_("Only Arrived ASNs can start unloading"))

        self.status = "Unloading"
        self.save()
        frappe.msgprint(_("Unloading started"), indicator="blue")

    @frappe.whitelist()
    def complete_sorting(self):
        """Unloading -> Sorted. Operator fills received_qty, shortage, damage per row."""
        if self.status != "Unloading":
            frappe.throw(_("Only ASNs in Unloading status can be sorted"))

        for row in self.items or []:
            if flt(row.received_qty) > 0:
                if flt(row.received_qty) < flt(row.expected_qty):
                    row.shortage_qty = flt(row.expected_qty) - flt(row.received_qty)
                    row.row_status = "Short"
                elif flt(row.received_qty) > flt(row.expected_qty):
                    row.over_qty = flt(row.received_qty) - flt(row.expected_qty)
                    row.row_status = "Received"
                else:
                    row.row_status = "Received"
            elif flt(row.damage_qty) > 0:
                row.row_status = "Damaged"
            else:
                row.row_status = "Pending"

        self.update_totals()
        self.status = "Sorted"
        self.save()
        frappe.msgprint(_("Sorting completed"), indicator="green")

    @frappe.whitelist()
    def create_putaway(self):
        """Sorted -> Putaway Created. Creates a WMS Task (Putaway)."""
        if self.status != "Sorted":
            frappe.throw(_("Only Sorted ASNs can create putaway tasks"))

        error_log = []

        try:
            task = frappe.new_doc("WMS Task")
            task.task_type = "Putaway"
            task.target_warehouse = self.warehouse
            task.target_bin = self.receiving_bin or ""
            task.purchase_order = self.purchase_order or ""
            task.reference_doctype = "WMS ASN"
            task.reference_name = self.name

            for row in self.items or []:
                if flt(row.received_qty) <= 0:
                    continue
                task.append("items", {
                    "item_code": row.item_code,
                    "qty": flt(row.received_qty),
                    "target_bin": row.target_bin or "",
                    "batch_no": row.batch_no or "",
                    "serial_no": row.serial_no or "",
                })

            if not task.items:
                error_log.append("No received items to create putaway task for.")
            else:
                task.insert()
                self.putaway_task = task.name

        except Exception as e:
            error_log.append(f"Putaway task creation failed: {str(e)}")

        self.status = "Putaway Created"
        self.error_log = "\n".join(error_log) if error_log else ""
        self.save()

        if error_log:
            frappe.msgprint(
                _("Putaway created with warnings. Check Error Log."),
                indicator="orange"
            )
        else:
            frappe.msgprint(
                _("Putaway task {0} created").format(
                    f'<a href="/app/wms-task/{self.putaway_task}">{self.putaway_task}</a>'
                ),
                indicator="green"
            )

    @frappe.whitelist()
    def complete_asn(self):
        """Putaway Created -> Completed. Creates Purchase Receipt."""
        if self.status != "Putaway Created":
            frappe.throw(_("Only ASNs with Putaway Created status can be completed"))

        error_log = []

        try:
            pr = frappe.new_doc("Purchase Receipt")
            pr.supplier = self.supplier
            pr.company = (
                frappe.defaults.get_user_default("company")
                or "Win The Buy Box Private Limited"
            )
            pr.set_warehouse = self.warehouse

            for row in self.items or []:
                if flt(row.received_qty) <= 0:
                    continue
                pr.append("items", {
                    "item_code": row.item_code,
                    "qty": flt(row.received_qty),
                    "warehouse": self.warehouse,
                    "purchase_order": self.purchase_order or "",
                    "batch_no": row.batch_no or "",
                    "serial_no": row.serial_no or "",
                })

            if not pr.items:
                error_log.append("No received items to create Purchase Receipt for.")
            else:
                pr.insert()
                pr.submit()
                self.purchase_receipt = pr.name

        except Exception as e:
            error_log.append(f"Purchase Receipt creation failed: {str(e)}")

        self.status = "Completed"
        self.error_log = "\n".join(error_log) if error_log else ""
        self.save()

        if error_log:
            frappe.msgprint(
                _("ASN completed with warnings. Check Error Log."),
                indicator="orange"
            )
        else:
            frappe.msgprint(
                _("ASN completed. Purchase Receipt: {0}").format(
                    f'<a href="/app/purchase-receipt/{self.purchase_receipt}">'
                    f'{self.purchase_receipt}</a>'
                ),
                indicator="green"
            )

        return {
            "status": self.status,
            "purchase_receipt": self.purchase_receipt,
            "putaway_task": self.putaway_task,
            "errors": error_log,
        }

    @frappe.whitelist()
    def cancel_asn(self):
        """Cancel the ASN (any status except Completed)."""
        if self.status == "Completed":
            frappe.throw(_("Completed ASNs cannot be cancelled"))

        self.status = "Cancelled"
        self.save()
        frappe.msgprint(_("ASN cancelled"), indicator="red")
