"""D2C Invoice Run — evening batch invoicing for the D2C fulfillment automation.

The automation submits Delivery Notes EARLY (to produce the AWB + label for
packing) and flags them `custom_d2c_defer_si=1` so the "Auto Create SI on Shopify
DN Submit" script skips them. This screen lets an accounts user, after the day's
physical dispatch is done, load those dispatched-but-unbilled D2C DNs, tick/untick
each, and create the SHPSI27 invoices in one pass — so the invoice is raised AFTER
dispatch (invoice-after-dispatch), not at DN submit.

The SI-creation logic mirrors the LIVE "Auto Create SI on Shopify DN Submit"
server script exactly (SHPSI27 series, tax-inclusive print rate, Sales - WTBBPL
income, payment-schedule re-anchor, PPCOD prepaid, submit -> IRN via India
Compliance), so a deferred invoice is identical to one raised at submit.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import cint

from solara_wms.wms.d2c_fulfillment import create_si_from_deferred_dn


class D2CInvoiceRun(Document):
    @frappe.whitelist()
    def load_pending(self):
        """Populate `orders` with dispatched D2C DNs (deferred, unbilled) up to
        run_date. Idempotent: replaces the current list."""
        rows = frappe.get_all(
            "Delivery Note",
            filters={
                "custom_d2c_defer_si": 1,
                "docstatus": 1,
                "per_billed": ["<", 0.01],  # not yet invoiced
                "posting_date": ["<=", self.run_date],
                "is_return": 0,
            },
            fields=["name", "shopify_order_id", "customer", "customer_name",
                    "grand_total", "custom_order_type"],
            order_by="posting_date asc, creation asc",
            limit_page_length=0,
        )
        self.set("orders", [])
        for dn in rows:
            self.append("orders", {
                "include": 1,
                "delivery_note": dn.name,
                "shopify_order_id": dn.get("shopify_order_id"),
                "customer": dn.get("customer_name") or dn.get("customer"),
                "order_type": dn.get("custom_order_type"),
                "amount": dn.get("grand_total"),
            })
        self.status = "Loaded {0} pending order(s)".format(len(rows))
        self.summary = None
        self.save(ignore_permissions=True)
        return {"loaded": len(rows)}

    @frappe.whitelist()
    def create_invoices(self):
        """Create a SHPSI27 for every ticked row that has no invoice yet.
        Each invoice is created + submitted in its own savepoint, so one failure
        never rolls back the others."""
        created, failed, skipped = 0, 0, 0
        for row in self.orders:
            if not cint(row.include):
                skipped += 1
                continue
            if row.sales_invoice:
                skipped += 1
                continue
            sp = "d2cinv_" + (row.delivery_note or "").replace("-", "_")[:40]
            try:
                frappe.db.savepoint(sp)
                si_name = create_si_from_deferred_dn(row.delivery_note)
                frappe.db.commit()
                if si_name:
                    row.sales_invoice = si_name
                    row.result = "created"
                    created += 1
                else:
                    row.result = "skipped (already billed / no SO)"
                    skipped += 1
            except Exception as e:
                frappe.db.rollback(save_point=sp)
                row.result = "FAILED: " + str(e)[:200]
                failed += 1
        self.status = "Created {0} | failed {1} | skipped {2}".format(created, failed, skipped)
        self.summary = self.status
        self.save(ignore_permissions=True)
        return {"created": created, "failed": failed, "skipped": skipped}
