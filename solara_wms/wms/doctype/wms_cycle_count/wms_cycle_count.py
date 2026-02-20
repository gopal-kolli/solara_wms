import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class WMSCycleCount(Document):
    """
    WMS Cycle Count - Scheduled inventory counting.
    Enhanced version of ModernWMS StockTakingEntity.

    Features:
      - ABC classification support
      - Fetches book qty from ERPNext Bin
      - Auto-calculates variances (qty, %, value)
      - Creates Stock Reconciliation for discrepancies

    Workflow: Draft -> In Progress -> Completed
    """

    def validate(self):
        self.update_totals()

    def update_totals(self):
        """Update summary counts."""
        self.total_items = len(self.items or [])
        self.items_with_variance = sum(
            1 for r in (self.items or [])
            if r.row_status == "Variance"
        )
        self.total_variance_value = sum(
            flt(r.variance_value) for r in (self.items or [])
        )

    @frappe.whitelist()
    def populate_items_from_warehouse(self):
        """
        Fetch all items with stock in the selected warehouse from ERPNext Bin.
        Populates the items child table.
        """
        if not self.warehouse:
            frappe.throw(_("Select a warehouse first"))

        filters = {"warehouse": self.warehouse, "actual_qty": [">", 0]}

        bins = frappe.get_all(
            "Bin",
            filters=filters,
            fields=["item_code", "actual_qty", "valuation_rate"],
            order_by="item_code asc",
        )

        if not bins:
            frappe.msgprint(_("No items with stock found in this warehouse"))
            return

        self.items = []
        for bin_data in bins:
            item_name = frappe.db.get_value("Item", bin_data.item_code, "item_name")
            self.append("items", {
                "item_code": bin_data.item_code,
                "item_name": item_name or "",
                "book_qty": flt(bin_data.actual_qty),
                "valuation_rate": flt(bin_data.valuation_rate),
            })

        self.update_totals()
        self.save()

        frappe.msgprint(
            _("Populated {0} items from warehouse {1}").format(
                len(self.items), self.warehouse
            ),
            indicator="green"
        )

    @frappe.whitelist()
    def fetch_book_quantities(self):
        """
        Refresh book quantities from ERPNext Bin for all items.
        Call this right before counting to get latest figures.
        """
        if not self.items:
            frappe.throw(_("No items to fetch quantities for"))

        for row in self.items:
            bin_data = frappe.db.get_value(
                "Bin",
                {"item_code": row.item_code, "warehouse": self.warehouse},
                ["actual_qty", "valuation_rate"],
                as_dict=True,
            )
            if bin_data:
                row.book_qty = flt(bin_data.actual_qty)
                row.valuation_rate = flt(bin_data.valuation_rate)
            else:
                row.book_qty = 0
                row.valuation_rate = 0

        self.save()
        frappe.msgprint(_("Book quantities refreshed"), indicator="green")

    # ─── STATUS TRANSITIONS ──────────────────────────────────

    @frappe.whitelist()
    def start_count(self):
        """Draft -> In Progress."""
        if self.status != "Draft":
            frappe.throw(_("Only Draft counts can be started"))
        if not self.items:
            frappe.throw(_("Add items before starting the count"))

        self.status = "In Progress"
        self.save()
        frappe.msgprint(_("Cycle count started"), indicator="blue")

    @frappe.whitelist()
    def complete_count(self):
        """
        In Progress -> Completed.
        Calculates variances and creates Stock Reconciliation if needed.
        """
        if self.status != "In Progress":
            frappe.throw(_("Only In Progress counts can be completed"))

        error_log = []

        # Calculate variances
        for row in self.items:
            if row.counted_qty is not None and row.counted_qty != "":
                row.variance_qty = flt(row.counted_qty) - flt(row.book_qty)

                if flt(row.book_qty) != 0:
                    row.variance_pct = (flt(row.variance_qty) / flt(row.book_qty)) * 100
                else:
                    row.variance_pct = 100 if flt(row.variance_qty) != 0 else 0

                row.variance_value = flt(row.variance_qty) * flt(row.valuation_rate)

                if flt(row.variance_qty) == 0:
                    row.row_status = "Matched"
                else:
                    row.row_status = "Variance"
            else:
                # Not counted - assume matched
                row.counted_qty = row.book_qty
                row.variance_qty = 0
                row.variance_pct = 0
                row.variance_value = 0
                row.row_status = "Counted"

        self.update_totals()

        # Create Stock Reconciliation if variances exist
        items_with_diff = [
            row for row in self.items
            if row.row_status == "Variance" and flt(row.variance_qty) != 0
        ]

        if items_with_diff:
            try:
                sr = frappe.new_doc("Stock Reconciliation")
                sr.company = (
                    frappe.defaults.get_user_default("company")
                    or "Win The Buy Box Private Limited"
                )
                sr.purpose = "Stock Reconciliation"

                for row in items_with_diff:
                    sr.append("items", {
                        "item_code": row.item_code,
                        "warehouse": self.warehouse,
                        "qty": flt(row.counted_qty),
                        "batch_no": row.batch_no or "",
                    })

                sr.insert()
                sr.submit()
                self.stock_reconciliation = sr.name

            except Exception as e:
                error_log.append(f"Stock Reconciliation creation failed: {str(e)}")

        self.counted_at = now_datetime()
        self.last_count_date = now_datetime().date()
        self.status = "Completed"
        self.error_log = "\n".join(error_log) if error_log else ""
        self.save()

        if error_log:
            frappe.msgprint(
                _("Count completed with warnings. Check Error Log."),
                indicator="orange"
            )
        else:
            msg = _("Cycle count completed.")
            if self.stock_reconciliation:
                msg += _(" Stock Reconciliation: {0}").format(
                    f'<a href="/app/stock-reconciliation/{self.stock_reconciliation}">'
                    f'{self.stock_reconciliation}</a>'
                )
            elif not items_with_diff:
                msg += _(" No variances found - all items match.")
            frappe.msgprint(msg, indicator="green")

        return {
            "status": self.status,
            "items_with_variance": self.items_with_variance,
            "total_variance_value": self.total_variance_value,
            "stock_reconciliation": self.stock_reconciliation,
            "errors": error_log,
        }

    @frappe.whitelist()
    def cancel_count(self):
        """Cancel the count (any status except Completed)."""
        if self.status == "Completed":
            frappe.throw(_("Completed counts cannot be cancelled"))

        self.status = "Cancelled"
        self.save()
        frappe.msgprint(_("Cycle count cancelled"), indicator="red")
