import frappe
from collections import defaultdict
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class ReturnIntake(Document):
    """
    Return Intake — warehouse returns desk form.

    The warehouse Returns Manager records goods physically returned against a
    specific Atlas Sales Invoice. The form validates the SKUs/qtys against the
    original delivery, then (on HQ approval) reverses INVENTORY ONLY by creating
    a Return Delivery Note (is_return=1) — bringing stock back and reversing COGS
    at original cost, linked to the source sale. It never touches revenue
    (no Credit Note); revenue reversal stays with the existing settlement /
    connector flows.

    Good lines restock to Main Warehouse; Damaged/Used lines go to QC / Rejected
    (write-off stays in the monthly QC review, not here).

    Workflow: Draft -> Pending HQ Review -> Approved (auto-submits the Return DN)
    / Rejected.
    """

    # ─── VALIDATION ──────────────────────────────────────────

    def validate(self):
        si = self._get_source_invoice()
        delivered_total, source_ref, dn_list = self._build_delivered_map(si)
        already = self._already_returned_map(dn_list, si.name)
        self._validate_items(delivered_total, source_ref, already)

        if self.workflow_state == "Pending HQ Review":
            self._require_qc_evidence()

    def before_submit(self):
        # Submit (docstatus 0->1) happens on the Approve / Reject transition.
        if self.workflow_state == "Rejected":
            return
        self._require_qc_evidence()
        if self.return_dn_submitted:
            frappe.throw(_("Return documents have already been created for this intake."))

    def _get_source_invoice(self):
        if not self.sales_invoice:
            frappe.throw(_("Select the original Sales Invoice."))
        si = frappe.get_doc("Sales Invoice", self.sales_invoice)
        if si.docstatus != 1:
            frappe.throw(_("Sales Invoice {0} is not submitted — cannot process a return against it.").format(si.name))
        if si.is_return:
            frappe.throw(_("{0} is itself a return invoice. Pick the original sale.").format(si.name))
        return si

    def _build_delivered_map(self, si):
        """
        Returns (delivered_total, source_ref, dn_list).
        - delivered_total: {item_code: qty} total delivered for the cap.
        - source_ref: {item_code: {dn, dn_detail, against_sales_order, so_detail,
          against_sales_invoice}} — the source row to reverse against.
        - dn_list: distinct source Delivery Notes (for the linked-DN display + the
          already-returned lookup).
        """
        delivered_total = defaultdict(float)
        source_ref = {}
        dn_names = set()

        # DNs that reference this SI directly (DN created from SI).
        dn_items = frappe.get_all(
            "Delivery Note Item",
            filters={"against_sales_invoice": si.name, "docstatus": 1},
            fields=["parent", "name", "item_code", "qty",
                    "against_sales_order", "so_detail", "against_sales_invoice"],
        )
        # DNs referenced by this SI's items (SI created from DN).
        si_dn_names = {it.delivery_note for it in si.items if getattr(it, "delivery_note", None)}
        if si_dn_names:
            dn_items += frappe.get_all(
                "Delivery Note Item",
                filters={"parent": ["in", list(si_dn_names)], "docstatus": 1},
                fields=["parent", "name", "item_code", "qty",
                        "against_sales_order", "so_detail", "against_sales_invoice"],
            )

        if dn_items:
            seen_rows = set()
            for di in dn_items:
                if di.name in seen_rows:
                    continue
                seen_rows.add(di.name)
                dn_names.add(di.parent)
                delivered_total[di.item_code] += abs(flt(di.qty))
                cur = source_ref.get(di.item_code)
                if cur is None or abs(flt(di.qty)) > cur["_qty"]:
                    source_ref[di.item_code] = {
                        "_qty": abs(flt(di.qty)),
                        "dn": di.parent,
                        "dn_detail": di.name,
                        "against_sales_order": di.against_sales_order,
                        "so_detail": di.so_detail,
                        "against_sales_invoice": di.against_sales_invoice or si.name,
                    }
            self.update_stock_si = 0
        elif si.update_stock:
            # SI carried the stock (no separate DN) — reverse via Stock Entry.
            self.update_stock_si = 1
            for it in si.items:
                delivered_total[it.item_code] += abs(flt(it.qty))
                cur = source_ref.get(it.item_code)
                if cur is None or abs(flt(it.qty)) > cur["_qty"]:
                    source_ref[it.item_code] = {
                        "_qty": abs(flt(it.qty)),
                        "dn": None,
                        "dn_detail": None,
                        "against_sales_order": getattr(it, "sales_order", None),
                        "so_detail": getattr(it, "so_detail", None),
                        "against_sales_invoice": si.name,
                    }
        else:
            frappe.throw(_(
                "Sales Invoice {0} has no Delivery Note and did not carry stock "
                "(update_stock=0). Inventory cannot be reversed here — handle manually."
            ).format(si.name))

        self.linked_delivery_notes = ", ".join(sorted(dn_names)) if dn_names else ""
        return delivered_total, source_ref, sorted(dn_names)

    def _already_returned_map(self, dn_list, si_name):
        """Sum qty already reversed (idempotency / partial prior returns)."""
        already = defaultdict(float)
        if dn_list:
            ret_dns = frappe.get_all(
                "Delivery Note",
                filters={"is_return": 1, "return_against": ["in", dn_list], "docstatus": 1},
                pluck="name",
            )
            if ret_dns:
                for ri in frappe.get_all(
                    "Delivery Note Item",
                    filters={"parent": ["in", ret_dns]},
                    fields=["item_code", "qty"],
                ):
                    already[ri.item_code] += abs(flt(ri.qty))
        else:
            # update_stock SI path — prior reversals would be return SIs.
            ret_sis = frappe.get_all(
                "Sales Invoice",
                filters={"is_return": 1, "return_against": si_name, "docstatus": 1},
                pluck="name",
            )
            if ret_sis:
                for ri in frappe.get_all(
                    "Sales Invoice Item",
                    filters={"parent": ["in", ret_sis]},
                    fields=["item_code", "qty"],
                ):
                    already[ri.item_code] += abs(flt(ri.qty))
        return already

    def _validate_items(self, delivered_total, source_ref, already):
        if not self.items:
            frappe.throw(_("Add at least one returned item."))

        abbr = frappe.get_cached_value("Company", self.company, "abbr")
        main_wh = f"Main Warehouse - {abbr}"
        qc_wh = f"QC / Rejected - {abbr}"
        for wh in (main_wh, qc_wh):
            if not frappe.db.exists("Warehouse", wh):
                frappe.throw(_("Warehouse {0} does not exist on this site.").format(wh))

        # Aggregate requested qty per SKU (a SKU may appear as Good + Damaged rows).
        requested = defaultdict(float)
        for row in self.items:
            requested[row.item_code] += abs(flt(row.return_qty))

        for row in self.items:
            if flt(row.return_qty) <= 0:
                frappe.throw(_("Row {0}: Return Qty must be greater than zero.").format(row.idx))
            if row.item_code not in source_ref:
                frappe.throw(_(
                    "Row {0}: SKU {1} was not delivered on this invoice — it cannot be returned against it."
                ).format(row.idx, row.item_code))

            sref = source_ref[row.item_code]
            delivered_q = delivered_total[row.item_code]
            already_q = already.get(row.item_code, 0)
            max_returnable = delivered_q - already_q

            row.delivered_qty = delivered_q
            row.already_returned_qty = already_q
            row.max_returnable = max_returnable
            row.source_dn = sref["dn"]
            row.source_dn_detail = sref["dn_detail"]
            row.against_sales_order = sref["against_sales_order"]
            row.so_detail = sref["so_detail"]
            row.against_sales_invoice = sref["against_sales_invoice"]
            row.target_warehouse = main_wh if row.condition == "Good" else qc_wh

            if requested[row.item_code] > max_returnable + 0.001:
                frappe.throw(_(
                    "Over-return on SKU {0}: requested {1}, but max returnable is {2} "
                    "(delivered {3} − already returned {4})."
                ).format(row.item_code, requested[row.item_code], max_returnable,
                         delivered_q, already_q))

    def _require_qc_evidence(self):
        has_evidence = any(
            (r.video or r.drive_link) for r in (self.qc_videos or [])
        )
        if not has_evidence:
            frappe.throw(_("Attach at least one QC video (uploaded clip or Drive link) before submitting for review."))

    # ─── REVERSAL ON APPROVAL ────────────────────────────────

    def on_submit(self):
        # Submit fires for both Approved and Rejected (both docstatus 1).
        if self.workflow_state == "Rejected":
            return
        if self.return_dn_submitted:
            return

        error_log = []
        created = []
        try:
            if self.update_stock_si:
                created.append(self._create_stock_entry())
            else:
                created.extend(self._create_return_delivery_notes())
        except Exception as e:
            error_log.append(str(e))
            # Re-raise so the approval aborts and nothing is half-created.
            self.db_set("error_log", "\n".join(error_log))
            raise

        self.db_set("return_delivery_notes", ", ".join(created))
        self.db_set("return_dn_submitted", 1)
        self.db_set("error_log", "")

        links = ", ".join(
            f'<a href="/app/{"stock-entry" if self.update_stock_si else "delivery-note"}/{n}">{n}</a>'
            for n in created
        )
        frappe.msgprint(
            _("Inventory reversed. Created & submitted: {0}").format(links),
            indicator="green",
        )

    def _create_return_delivery_notes(self):
        """One Return Delivery Note per source DN, using ERPNext's own mapper so
        all reference fields (against_sales_order, so_detail, dn_detail, etc.) are
        preserved for correct return-qty tracking."""
        from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

        groups = defaultdict(list)
        for row in self.items:
            groups[row.source_dn].append(row)

        copy_keys = [
            "item_code", "item_name", "description", "uom", "stock_uom",
            "conversion_factor", "against_sales_order", "so_detail",
            "against_sales_invoice", "si_detail", "dn_detail",
            "expense_account", "cost_center", "item_group", "brand",
            "allow_zero_valuation_rate",
        ]

        created = []
        for dn_name, rows in groups.items():
            ret = make_sales_return(dn_name)
            templates = {r.item_code: r.as_dict() for r in ret.items}

            ret.set("items", [])
            for row in rows:
                t = templates.get(row.item_code)
                if not t:
                    frappe.throw(_(
                        "SKU {0} has nothing left to return on Delivery Note {1}."
                    ).format(row.item_code, dn_name))
                item = {k: t.get(k) for k in copy_keys if t.get(k) is not None}
                item["qty"] = -abs(flt(row.return_qty))
                item["warehouse"] = row.target_warehouse
                ret.append("items", item)

            ret.posting_date = self.posting_date
            ret.set_posting_time = 1
            ret.flags.ignore_permissions = True
            ret.insert(ignore_permissions=True)
            ret.submit()
            created.append(ret.name)

        return created

    def _create_stock_entry(self):
        """Fallback for SIs booked with update_stock=1 (no DN): bring stock back
        via a Material Receipt into the per-row target warehouse. Inventory only."""
        se = frappe.new_doc("Stock Entry")
        se.stock_entry_type = "Material Receipt"
        se.company = self.company
        se.posting_date = self.posting_date
        se.set_posting_time = 1
        for row in self.items:
            se.append("items", {
                "item_code": row.item_code,
                "qty": abs(flt(row.return_qty)),
                "t_warehouse": row.target_warehouse,
            })
        se.remarks = _("Return Intake {0} against {1}").format(self.name, self.sales_invoice)
        se.flags.ignore_permissions = True
        se.insert(ignore_permissions=True)
        se.submit()
        return se.name

    # ─── CANCELLATION ────────────────────────────────────────

    def on_cancel(self):
        """Cancel the system-created reversal docs so an undone intake reverses cleanly."""
        if not self.return_delivery_notes:
            return
        doctype = "Stock Entry" if self.update_stock_si else "Delivery Note"
        for name in [n.strip() for n in self.return_delivery_notes.split(",") if n.strip()]:
            if not frappe.db.exists(doctype, name):
                continue
            doc = frappe.get_doc(doctype, name)
            if doc.docstatus == 1:
                doc.flags.ignore_permissions = True
                doc.cancel()
        self.db_set("return_dn_submitted", 0)
