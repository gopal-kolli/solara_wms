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
from frappe.utils import add_days, flt, cint


SI_SERIES = "SHPSI27-.#####"
D2C_INCOME_ACCOUNT = "Sales - WTBBPL"


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
                si_name = _create_si_from_dn(row.delivery_note)
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


def _create_si_from_dn(dn_name):
    """Create + submit the SHPSI27 for a dispatched D2C Delivery Note.
    Faithful mirror of the LIVE 'Auto Create SI on Shopify DN Submit' script."""
    from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

    doc = frappe.get_doc("Delivery Note", dn_name)

    if (doc.get("custom_order_type") or "") == "B2B2C":
        return None

    so_name = ""
    for item in doc.items:
        if item.get("against_sales_order"):
            so_name = item.against_sales_order
            break
    if not so_name or not so_name.startswith("SHP"):
        return None

    per_billed = flt(frappe.db.get_value("Sales Order", so_name, "per_billed"))
    if per_billed > 0:
        return None

    si = make_sales_invoice(source_name=doc.name)
    si.naming_series = SI_SERIES
    si.posting_date = doc.posting_date
    si.set_posting_time = 1

    so_order_type = frappe.db.get_value("Sales Order", so_name, "custom_order_type") or ""
    if so_order_type:
        si.custom_payment_method = so_order_type

    so_prepaid = flt(frappe.db.get_value("Sales Order", so_name, "custom_prepaid_amount"))
    if so_prepaid > 0:
        si.custom_ppcod_prepaid_amount = so_prepaid

    # Re-anchor payment_schedule due dates off the DN posting date.
    for ps in si.payment_schedule:
        cd = cint(ps.credit_days)
        ps.due_date = add_days(doc.posting_date, cd)
    if si.payment_schedule:
        si.due_date = max(str(ps.due_date) for ps in si.payment_schedule)
    else:
        si.due_date = doc.posting_date

    # Tax-inclusive pricing (matches the Shopify customer-paying total).
    for t in si.taxes:
        t.included_in_print_rate = 1

    # Force Shopify revenue to Sales - WTBBPL (override channel-specific defaults).
    for it in si.items:
        it.income_account = D2C_INCOME_ACCOUNT

    si.flags.ignore_permissions = True
    si.insert()
    si.submit()
    return si.name
