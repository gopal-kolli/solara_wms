import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class WMSWavePick(Document):
    """
    WMS Wave Pick - Consolidated batch picking.
    Groups multiple Sales Orders into a single picking wave for efficiency.

    Workflow: Draft -> Released -> Picking -> Completed

    On release: validates all orders
    On create_pick_task: creates a WMS Task (Pick) with consolidated items
    """

    def validate(self):
        self.update_totals()

    def update_totals(self):
        """Update summary counts."""
        self.total_orders = len(self.orders or [])
        self.total_items = len(self.items or [])
        self.total_qty = sum(flt(r.total_qty) for r in (self.items or []))

    @frappe.whitelist()
    def consolidate_items(self):
        """
        Read items from all linked Sales Orders, group by item_code,
        and populate the items child table with consolidated quantities.
        """
        if not self.orders:
            frappe.throw(_("Add Sales Orders before consolidating items"))

        item_map = {}  # item_code -> {total_qty, item_name, uom}

        for order_row in self.orders:
            so = frappe.get_doc("Sales Order", order_row.sales_order)
            for so_item in so.items:
                key = so_item.item_code
                if key in item_map:
                    item_map[key]["total_qty"] += flt(so_item.qty)
                else:
                    item_map[key] = {
                        "item_code": so_item.item_code,
                        "item_name": so_item.item_name,
                        "total_qty": flt(so_item.qty),
                        "uom": so_item.uom or so_item.stock_uom,
                    }

        # Clear existing items and repopulate
        self.items = []
        for item_data in item_map.values():
            self.append("items", {
                "item_code": item_data["item_code"],
                "item_name": item_data["item_name"],
                "total_qty": item_data["total_qty"],
                "uom": item_data["uom"],
                "picked_qty": 0,
            })

        self.update_totals()
        self.save()

        frappe.msgprint(
            _("Consolidated {0} items from {1} orders").format(
                len(self.items), len(self.orders)
            ),
            indicator="green"
        )

    # ─── STATUS TRANSITIONS ──────────────────────────────────

    @frappe.whitelist()
    def release_wave(self):
        """Draft -> Released"""
        if self.status != "Draft":
            frappe.throw(_("Only Draft waves can be released"))
        if not self.orders:
            frappe.throw(_("Add Sales Orders before releasing the wave"))
        if not self.items:
            frappe.throw(_("Consolidate items before releasing the wave"))

        self.status = "Released"
        self.save()
        frappe.msgprint(_("Wave released"), indicator="blue")

    @frappe.whitelist()
    def create_pick_task(self):
        """Released -> Picking. Creates a WMS Task (Pick)."""
        if self.status != "Released":
            frappe.throw(_("Only Released waves can create pick tasks"))

        error_log = []

        try:
            task = frappe.new_doc("WMS Task")
            task.task_type = "Pick"
            task.source_warehouse = self.warehouse
            task.assigned_to = self.assigned_to or ""
            task.priority = self.priority
            task.reference_doctype = "WMS Wave Pick"
            task.reference_name = self.name

            for row in self.items:
                task.append("items", {
                    "item_code": row.item_code,
                    "qty": flt(row.total_qty),
                    "source_bin": row.source_bin or "",
                    "batch_no": row.batch_no or "",
                })

            if not task.items:
                error_log.append("No items to create pick task for.")
            else:
                task.insert()
                self.wms_task = task.name

        except Exception as e:
            error_log.append(f"Pick task creation failed: {str(e)}")

        self.status = "Picking"
        self.error_log = "\n".join(error_log) if error_log else ""
        self.save()

        if error_log:
            frappe.msgprint(
                _("Pick task created with warnings. Check Error Log."),
                indicator="orange"
            )
        else:
            frappe.msgprint(
                _("Pick task {0} created").format(
                    f'<a href="/app/wms-task/{self.wms_task}">{self.wms_task}</a>'
                ),
                indicator="green"
            )

    @frappe.whitelist()
    def complete_wave(self):
        """Picking -> Completed"""
        if self.status != "Picking":
            frappe.throw(_("Only waves in Picking status can be completed"))

        # Mark all order rows based on pick results
        for order_row in self.orders or []:
            order_row.row_status = "Picked"

        self.completed_at = now_datetime()
        self.status = "Completed"
        self.save()
        frappe.msgprint(_("Wave completed"), indicator="green")

    @frappe.whitelist()
    def cancel_wave(self):
        """Cancel the wave (any status except Completed)."""
        if self.status == "Completed":
            frappe.throw(_("Completed waves cannot be cancelled"))

        self.status = "Cancelled"
        self.save()
        frappe.msgprint(_("Wave cancelled"), indicator="red")
