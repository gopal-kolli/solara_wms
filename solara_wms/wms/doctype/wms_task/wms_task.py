import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime, today


class WMSTask(Document):
    """
    WMS Task - directed warehouse operation.
    Unifies ModernWMS StockProcess, StockMove, and StockTaking concepts.

    Task types and their ModernWMS equivalents:
      Putaway  -> StockProcess (put goods into bin after receiving)
      Pick     -> StockProcess (pick from bin for order fulfillment)
      Pack     -> No direct equivalent (future: Packing Slip)
      Count    -> StockTaking (cycle count with book_qty vs counted_qty)
      Transfer -> StockMove (move between locations, move_status 0->1)

    ERPNext documents created on completion:
      Putaway/Transfer -> Stock Entry (Material Transfer)
      Pick             -> Stock Entry (Material Transfer)
      Count            -> Stock Reconciliation (only if differences)
      Pack             -> None in Phase 1
    """

    def validate(self):
        self.validate_locations()
        self.update_summary()
        self.calculate_differences()

    def validate_locations(self):
        """Ensure source/target warehouses and bins are consistent"""
        # For Transfer and Pick: source is required
        if self.task_type in ("Transfer", "Pick") and not self.source_warehouse:
            frappe.throw(_("Source Warehouse is required for {0} tasks").format(self.task_type))

        # For Putaway: target is required
        if self.task_type == "Putaway" and not self.target_warehouse:
            frappe.throw(_("Target Warehouse is required for Putaway tasks"))

        # For Transfer: both source and target required
        if self.task_type == "Transfer" and not self.target_warehouse:
            frappe.throw(_("Target Warehouse is required for Transfer tasks"))

        # Validate bin belongs to its warehouse
        if self.source_bin and self.source_warehouse:
            bin_wh = frappe.db.get_value("Warehouse Bin", self.source_bin, "warehouse")
            if bin_wh != self.source_warehouse:
                frappe.throw(
                    _("Source Bin {0} does not belong to Warehouse {1}").format(
                        self.source_bin, self.source_warehouse
                    )
                )

        if self.target_bin and self.target_warehouse:
            bin_wh = frappe.db.get_value("Warehouse Bin", self.target_bin, "warehouse")
            if bin_wh != self.target_warehouse:
                frappe.throw(
                    _("Target Bin {0} does not belong to Warehouse {1}").format(
                        self.target_bin, self.target_warehouse
                    )
                )

    def update_summary(self):
        """Update total_items and completed_items counts"""
        if self.items:
            self.total_items = len(self.items)
            self.completed_items = sum(
                1 for row in self.items if row.row_status == "Completed"
            )
        else:
            self.total_items = 0
            self.completed_items = 0

    def calculate_differences(self):
        """For Count tasks: calculate difference_qty per row (ModernWMS: difference_qty)"""
        if self.task_type == "Count" and self.items:
            for row in self.items:
                row.difference_qty = flt(row.actual_qty) - flt(row.qty)

    # ─── STATUS TRANSITIONS ──────────────────────────────────

    @frappe.whitelist()
    def assign_task(self, user=None):
        """Assign task to a user (Pending -> Assigned)"""
        if self.status != "Pending":
            frappe.throw(_("Only Pending tasks can be assigned"))

        if user:
            self.assigned_to = user
        elif not self.assigned_to:
            frappe.throw(_("Please specify a user to assign this task to"))

        self.status = "Assigned"
        self.save()
        frappe.msgprint(
            _("Task assigned to {0}").format(self.assigned_to),
            indicator="blue"
        )

    @frappe.whitelist()
    def start_task(self):
        """Start working on task (Assigned -> In Progress)"""
        if self.status not in ("Pending", "Assigned"):
            frappe.throw(_("Only Pending or Assigned tasks can be started"))

        if not self.assigned_to:
            self.assigned_to = frappe.session.user

        self.status = "In Progress"
        self.save()
        frappe.msgprint(_("Task started"), indicator="blue")

    @frappe.whitelist()
    def complete_task(self):
        """
        Complete the task and create ERPNext stock documents.
        Maps to ModernWMS:
          - StockProcess: process_status false->true + ConfirmAdjustment (is_update_stock)
          - StockMove: move_status 0->1 (confirmed)
          - StockTaking: job_status false->true (finished)
        """
        if self.status not in ("Assigned", "In Progress"):
            frappe.throw(_("Only Assigned or In Progress tasks can be completed"))

        if not self.items:
            frappe.throw(_("No items in this task to complete"))

        # Mark all pending rows as Completed (operator confirms all at once)
        for row in self.items:
            if row.row_status == "Pending":
                if not row.actual_qty:
                    row.actual_qty = row.qty  # Default: actual = expected
                row.row_status = "Completed"
                if self.task_type == "Count":
                    row.difference_qty = flt(row.actual_qty) - flt(row.qty)

        self.update_summary()

        # Create the appropriate ERPNext document based on task_type
        error_log = []
        try:
            if self.task_type in ("Putaway", "Transfer"):
                self.create_stock_entry_transfer(error_log)
            elif self.task_type == "Pick":
                self.create_stock_entry_pick(error_log)
            elif self.task_type == "Count":
                self.create_stock_reconciliation(error_log)
            # Pack: no stock document created in Phase 1

        except Exception as e:
            error_log.append(str(e))

        self.completed_at = now_datetime()
        self.status = "Completed"
        self.error_log = "\n".join(error_log) if error_log else ""
        self.save()

        if error_log:
            frappe.msgprint(
                _("Task completed with warnings. Check Error Log."),
                title=_("Task Completed"),
                indicator="orange"
            )
        else:
            result_link = ""
            if self.stock_entry:
                result_link = (
                    f' → <a href="/app/stock-entry/{self.stock_entry}">{self.stock_entry}</a>'
                )
            elif self.stock_reconciliation:
                result_link = (
                    f' → <a href="/app/stock-reconciliation/{self.stock_reconciliation}">'
                    f'{self.stock_reconciliation}</a>'
                )
            frappe.msgprint(
                _("Task completed successfully.{0}").format(result_link),
                title=_("Task Completed"),
                indicator="green"
            )

        return {
            "status": self.status,
            "stock_entry": self.stock_entry,
            "stock_reconciliation": self.stock_reconciliation,
            "errors": error_log,
        }

    @frappe.whitelist()
    def cancel_task(self):
        """Cancel the task (any status except Completed -> Cancelled)"""
        if self.status == "Completed":
            frappe.throw(_("Completed tasks cannot be cancelled"))

        self.status = "Cancelled"
        self.save()
        frappe.msgprint(_("Task cancelled"), indicator="red")

    # ─── STOCK DOCUMENT CREATION ─────────────────────────────

    def create_stock_entry_transfer(self, error_log):
        """
        Create Stock Entry (Material Transfer) for Putaway/Transfer tasks.
        Maps to ModernWMS:
          - StockMove: orig_goods_location_id -> dest_googs_location_id, move_status 0->1
          - StockProcess: ConfirmAdjustment subtracts source stock, adds target stock
        """
        se = frappe.new_doc("Stock Entry")
        se.stock_entry_type = "Material Transfer"
        se.company = frappe.defaults.get_user_default("company") or "Win The Buy Box Private Limited"

        for row in self.items:
            if row.row_status != "Completed":
                continue
            se.append("items", {
                "item_code": row.item_code,
                "qty": flt(row.actual_qty) or flt(row.qty),
                "s_warehouse": self.source_warehouse,
                "t_warehouse": self.target_warehouse,
                "batch_no": row.batch_no or "",
                "serial_no": row.serial_no or "",
            })

        if not se.items:
            error_log.append("No completed items to create Stock Entry for.")
            return

        try:
            se.insert()
            se.submit()
            self.stock_entry = se.name
        except Exception as e:
            error_log.append(f"Stock Entry creation failed: {str(e)}")

    def create_stock_entry_pick(self, error_log):
        """
        Create Stock Entry (Material Transfer) for Pick tasks.
        Moves items from source warehouse/bin to a staging/dispatch warehouse.
        Maps to ModernWMS Dispatch: pick_qty allocation -> stock decrement on delivery.
        """
        se = frappe.new_doc("Stock Entry")
        se.stock_entry_type = "Material Transfer"
        se.company = frappe.defaults.get_user_default("company") or "Win The Buy Box Private Limited"

        for row in self.items:
            if row.row_status != "Completed":
                continue
            se.append("items", {
                "item_code": row.item_code,
                "qty": flt(row.actual_qty) or flt(row.qty),
                "s_warehouse": self.source_warehouse,
                "t_warehouse": self.target_warehouse or self.source_warehouse,
                "batch_no": row.batch_no or "",
                "serial_no": row.serial_no or "",
            })

        if not se.items:
            error_log.append("No completed items to create Stock Entry for.")
            return

        try:
            se.insert()
            se.submit()
            self.stock_entry = se.name
        except Exception as e:
            error_log.append(f"Stock Entry creation failed: {str(e)}")

    def create_stock_reconciliation(self, error_log):
        """
        Create Stock Reconciliation for Count tasks with differences.
        Maps to ModernWMS StockTaking:
          - book_qty (our qty) vs counted_qty (our actual_qty)
          - difference_qty = counted_qty - book_qty
          - job_status false->true when completed
        Only creates Stock Reconciliation if there are actual discrepancies.
        """
        items_with_diff = [
            row for row in self.items
            if row.row_status == "Completed" and flt(row.difference_qty) != 0
        ]

        if not items_with_diff:
            # No differences found - count is clean, no reconciliation needed
            return

        sr = frappe.new_doc("Stock Reconciliation")
        sr.company = frappe.defaults.get_user_default("company") or "Win The Buy Box Private Limited"
        sr.purpose = "Stock Reconciliation"

        warehouse = self.source_warehouse or self.target_warehouse

        for row in items_with_diff:
            sr.append("items", {
                "item_code": row.item_code,
                "warehouse": warehouse,
                "qty": flt(row.actual_qty),
                "batch_no": row.batch_no or "",
                "serial_no": row.serial_no or "",
            })

        try:
            sr.insert()
            sr.submit()
            self.stock_reconciliation = sr.name
        except Exception as e:
            error_log.append(f"Stock Reconciliation creation failed: {str(e)}")
