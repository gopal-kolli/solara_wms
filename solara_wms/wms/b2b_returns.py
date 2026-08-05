"""Scanner-first B2B / quick-commerce return intake.

The floor records a platform RTV as one physical return lot, counts every
carton and performs item-level QC.  Finalizing the lot never posts inventory or
revenue.  It prepares one of two review documents:

* consignment return -> draft Material Transfer from the platform warehouse;
* outright return -> draft Return Intake against the original Sales Invoice.

HQ retains the existing approval/submission boundary.  Investigation quantities
are evidence only and never enter a stock document automatically.
"""
import json

import frappe
from frappe.utils import cint, flt, now_datetime, today

from solara_wms.wms.doctype.return_intake.return_intake import (
    target_warehouse_for_condition,
)


CHANNELS = {"Blinkit", "Swiggy", "Zepto", "Amazon VC", "Flipkart VC", "Offline", "Other"}
TREATMENTS = {"Consignment Return", "Outright Return", "Investigation"}
FINDINGS = {
    "No fault found", "Transit damage", "Used / customer damage",
    "Missing accessories", "Wrong product", "Short quantity",
    "Excess quantity", "Packaging damage only", "Other",
}
BUCKETS = (
    ("good_qty", "Good"),
    ("repairable_qty", "Repairable"),
    ("scrap_qty", "Scrap"),
)


def _parse(value, expected_type, default):
    if isinstance(value, expected_type):
        return value
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, expected_type) else default


def _company():
    return (frappe.defaults.get_user_default("company") or
            "Win The Buy Box Private Limited")


def _ean(item_code):
    row = frappe.get_all("Item Barcode", filters={"parent": item_code},
                         fields=["barcode"], order_by="idx asc", limit_page_length=1)
    return row[0].barcode if row else None


def _payload(doc):
    return {
        "lot": doc.name,
        "lot_status": doc.status,
        "channel": doc.channel,
        "inventory_treatment": doc.inventory_treatment,
        "platform_return_id": doc.platform_return_id,
        "platform_document_no": doc.platform_document_no,
        "purchase_order": doc.purchase_order,
        "sales_order": doc.sales_order,
        "delivery_note": doc.delivery_note,
        "sales_invoice": doc.sales_invoice,
        "source_warehouse": doc.source_warehouse,
        "expected_cartons": cint(doc.expected_cartons),
        "received_cartons": cint(doc.received_cartons),
        "inventory_status": doc.inventory_status,
        "draft_stock_entry": doc.draft_stock_entry,
        "return_intake": doc.return_intake,
        "exception": cint(doc.exception),
        "exception_reason": doc.exception_reason,
        "cartons": [{
            "carton_code": row.carton_code,
            "platform_carton_code": row.platform_carton_code,
            "condition": row.condition,
            "notes": row.notes,
        } for row in (doc.cartons or [])],
        "items": [{
            "item_code": row.item_code,
            "item_name": row.item_name,
            "ean": row.ean,
            "expected_qty": flt(row.expected_qty),
            "received_qty": flt(row.received_qty),
            "good_qty": flt(row.good_qty),
            "repairable_qty": flt(row.repairable_qty),
            "scrap_qty": flt(row.scrap_qty),
            "investigation_qty": flt(row.investigation_qty),
            "warehouse_finding": row.warehouse_finding,
            "accessories_complete": cint(row.accessories_complete),
            "visual_pass": cint(row.visual_pass),
            "power_test": row.power_test,
            "function_test": row.function_test,
            "notes": row.notes,
        } for row in (doc.items or [])],
    }


def _existing(platform_return_id):
    rows = frappe.get_all("B2B Return Lot",
                          filters={"platform_return_id": platform_return_id},
                          fields=["name"], limit_page_length=1)
    return frappe.get_doc("B2B Return Lot", rows[0].name) if rows else None


@frappe.whitelist()
def b2b_return_lookup(platform_return_id):
    """Resume/read an existing platform return lot by its unique RTV ID."""
    return_id = (platform_return_id or "").strip()
    if not return_id:
        return {"status": "error", "message": "Enter the platform RTV / return ID."}
    doc = _existing(return_id)
    if not doc:
        return {"status": "not_found", "platform_return_id": return_id,
                "message": "No return lot exists yet for this RTV ID."}
    out = _payload(doc)
    out["status"] = "resume" if doc.status == "QC In Progress" else "already"
    return out


@frappe.whitelist()
def b2b_return_start(channel, inventory_treatment, platform_return_id,
                     expected_items, expected_cartons=0, platform_document_no=None,
                     purchase_order=None, sales_order=None, delivery_note=None,
                     sales_invoice=None, source_warehouse=None, customer=None,
                     vehicle_number=None, lr_number=None):
    """Create or resume one RTV/return lot. No stock document is created."""
    channel = (channel or "").strip()
    treatment = (inventory_treatment or "").strip()
    return_id = (platform_return_id or "").strip()
    if channel not in CHANNELS:
        frappe.throw("Choose a valid B2B return channel.")
    if treatment not in TREATMENTS:
        frappe.throw("Choose a valid inventory treatment.")
    if not return_id:
        frappe.throw("Enter the platform RTV / return ID.")
    prior = _existing(return_id)
    if prior:
        out = _payload(prior)
        out["status"] = "resume" if prior.status == "QC In Progress" else "already"
        return out

    items = _parse(expected_items, list, [])
    if not items:
        frappe.throw("Add at least one expected SKU from the platform return document.")
    item_rows = []
    for raw in items:
        code = (raw.get("item_code") or "").strip()
        qty = flt(raw.get("expected_qty"))
        if not code or qty <= 0:
            frappe.throw("Every expected line needs an Item Code and quantity above zero.")
        if not frappe.db.exists("Item", code):
            frappe.throw("Item {0} does not exist in Atlas.".format(code))
        item_rows.append({
            "item_code": code,
            "item_name": frappe.db.get_value("Item", code, "item_name") or code,
            "ean": (raw.get("ean") or _ean(code) or "").strip(),
            "expected_qty": qty,
            "received_qty": 0,
        })

    if treatment == "Consignment Return" and not source_warehouse:
        frappe.throw("Select the platform consignment warehouse.")
    if treatment == "Consignment Return" and not frappe.db.exists("Warehouse", source_warehouse):
        frappe.throw("Warehouse {0} does not exist in Atlas.".format(source_warehouse))
    if treatment == "Outright Return" and not sales_invoice:
        frappe.throw("Link the original Sales Invoice for an outright return.")

    doc = frappe.get_doc({
        "doctype": "B2B Return Lot",
        "status": "QC In Progress",
        "channel": channel,
        "inventory_treatment": treatment,
        "platform_return_id": return_id,
        "platform_document_no": (platform_document_no or "").strip(),
        "company": _company(),
        "customer": customer,
        "purchase_order": (purchase_order or "").strip(),
        "sales_order": sales_order,
        "delivery_note": delivery_note,
        "sales_invoice": sales_invoice,
        "source_warehouse": source_warehouse,
        "vehicle_number": (vehicle_number or "").strip(),
        "lr_number": (lr_number or "").strip(),
        "expected_cartons": max(0, cint(expected_cartons)),
        "received_cartons": 0,
        "inventory_status": "Pending QC",
        "received_at": now_datetime(),
        "received_by": frappe.session.user,
        "items": item_rows,
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    out = _payload(doc)
    out["status"] = "started"
    return out


@frappe.whitelist()
def b2b_return_add_carton(lot, platform_carton_code=None, condition="Unknown", notes=None):
    """Add one physical carton, idempotent by platform code when supplied."""
    doc = frappe.get_doc("B2B Return Lot", lot)
    if doc.status != "QC In Progress":
        return {"status": "error", "message": "This return lot is no longer open for QC."}
    platform_code = (platform_carton_code or "").strip()
    if platform_code:
        for row in doc.cartons or []:
            if row.platform_carton_code == platform_code:
                return {"status": "duplicate", "lot": doc.name,
                        "carton_code": row.carton_code,
                        "message": "This carton is already recorded in the return lot."}
    if condition not in ("Sealed / Intact", "Damaged", "Wet", "Open / Repacked", "Unknown"):
        frappe.throw("Choose a valid outer-carton condition.")
    seq = len(doc.cartons or []) + 1
    carton_code = "B2BRT-{0}-{1:04d}".format(doc.name, seq)
    doc.append("cartons", {
        "carton_code": carton_code,
        "platform_carton_code": platform_code,
        "condition": condition,
        "notes": (notes or "").strip(),
    })
    doc.received_cartons = seq
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    return {"status": "ok", "lot": doc.name, "carton_code": carton_code,
            "received_cartons": seq, "expected_cartons": cint(doc.expected_cartons)}


def _validate_results(doc, results):
    by_code = {row.get("item_code"): row for row in results if row.get("item_code")}
    exception_reasons = []
    draft_rows = []
    for item in doc.items:
        raw = by_code.get(item.item_code)
        if not raw:
            frappe.throw("Complete QC for {0}.".format(item.item_code))
        received = flt(raw.get("received_qty"))
        good = flt(raw.get("good_qty"))
        repairable = flt(raw.get("repairable_qty"))
        scrap = flt(raw.get("scrap_qty"))
        investigation = flt(raw.get("investigation_qty"))
        values = (received, good, repairable, scrap, investigation)
        if any(value < 0 for value in values):
            frappe.throw("{0}: quantities cannot be negative.".format(item.item_code))
        if abs((good + repairable + scrap + investigation) - received) > 0.001:
            frappe.throw("{0}: Good + Repairable + Scrap + Investigation must equal Received."
                         .format(item.item_code))
        if good + repairable + scrap > flt(item.expected_qty) + 0.001:
            frappe.throw("{0}: quantities above the expected return must stay in Investigation."
                         .format(item.item_code))
        finding = (raw.get("warehouse_finding") or "").strip()
        if finding and finding not in FINDINGS:
            frappe.throw("{0}: choose a valid warehouse finding.".format(item.item_code))
        if good > 0:
            if not cint(raw.get("accessories_complete")) or not cint(raw.get("visual_pass")):
                frappe.throw("{0}: Good stock requires complete accessories and a passed visual check."
                             .format(item.item_code))
            if raw.get("power_test") not in ("Pass", "Not Applicable"):
                frappe.throw("{0}: Good stock requires a passed/not-applicable power test."
                             .format(item.item_code))
            if raw.get("function_test") not in ("Pass", "Not Applicable"):
                frappe.throw("{0}: Good stock requires a passed/not-applicable function test."
                             .format(item.item_code))
        if (repairable > 0 or scrap > 0) and finding in ("", "No fault found"):
            frappe.throw("{0}: record the defect for Repairable/Scrap quantity."
                         .format(item.item_code))

        item.received_qty = received
        item.good_qty = good
        item.repairable_qty = repairable
        item.scrap_qty = scrap
        item.investigation_qty = investigation
        item.warehouse_finding = finding
        item.accessories_complete = cint(raw.get("accessories_complete"))
        item.visual_pass = cint(raw.get("visual_pass"))
        item.power_test = raw.get("power_test") or "Not Applicable"
        item.function_test = raw.get("function_test") or "Not Applicable"
        item.notes = (raw.get("notes") or "").strip()

        if received < flt(item.expected_qty) - 0.001:
            exception_reasons.append("{0}: short {1}".format(
                item.item_code, flt(item.expected_qty) - received))
        if received > flt(item.expected_qty) + 0.001:
            exception_reasons.append("{0}: excess {1}".format(
                item.item_code, received - flt(item.expected_qty)))
        if investigation > 0:
            exception_reasons.append("{0}: {1} in investigation".format(
                item.item_code, investigation))
        for field, condition in BUCKETS:
            qty = flt(raw.get(field))
            if qty > 0:
                draft_rows.append({"item_code": item.item_code, "qty": qty,
                                   "condition": condition})
    return draft_rows, exception_reasons


def _prepare_consignment_transfer(doc, rows):
    if not rows:
        return None
    abbr = frappe.get_cached_value("Company", doc.company, "abbr")
    se = frappe.new_doc("Stock Entry")
    se.stock_entry_type = "Material Transfer"
    se.company = doc.company
    se.posting_date = today()
    se.remarks = ("B2B return lot {0} · {1} · RTV {2} · draft for HQ review"
                  .format(doc.name, doc.channel, doc.platform_return_id))[:140]
    for row in rows:
        se.append("items", {
            "item_code": row["item_code"],
            "qty": row["qty"],
            "s_warehouse": doc.source_warehouse,
            "t_warehouse": target_warehouse_for_condition(abbr, row["condition"]),
        })
    se.flags.ignore_permissions = True
    se.insert(ignore_permissions=True)
    return se.name


def _prepare_outright_intake(doc, rows, evidence):
    if not rows:
        return None
    si = frappe.get_doc("Sales Invoice", doc.sales_invoice)
    intake = frappe.get_doc({
        "doctype": "Return Intake",
        "workflow_state": "Draft",
        "sales_invoice": si.name,
        "customer": si.customer,
        "customer_name": si.customer_name,
        "company": si.company,
        "channel": doc.channel,
        "posting_date": today(),
        "items": [{"item_code": row["item_code"], "return_qty": row["qty"],
                   "condition": row["condition"]} for row in rows],
        "qc_videos": [
            {"video": evidence["platform_document_url"], "note": "Platform RTV document"},
            {"video": evidence["arrival_photo_url"], "note": "Arrival / sealed cartons"},
            {"video": evidence["open_photo_url"], "note": "Opened cartons"},
            {"video": evidence["qc_evidence_url"], "note": "QC / damage evidence"},
        ],
        "remarks": ("B2B return lot {0} · {1} · RTV {2}"
                    .format(doc.name, doc.channel, doc.platform_return_id))[:140],
    })
    intake.flags.ignore_permissions = True
    intake.insert(ignore_permissions=True)
    frappe.db.set_value("Return Intake", intake.name, "workflow_state", "Pending HQ Review")
    intake.workflow_state = "Pending HQ Review"
    return intake.name


@frappe.whitelist()
def b2b_return_finalize(lot, item_results, evidence, notes=None):
    """Freeze QC and prepare draft inventory paperwork. Never submit/post."""
    doc = frappe.get_doc("B2B Return Lot", lot)
    if doc.status != "QC In Progress":
        return {"status": "already", **_payload(doc),
                "message": "This B2B return lot has already been finalized."}
    results = _parse(item_results, list, [])
    evidence = _parse(evidence, dict, {})
    labels = {
        "platform_document_url": "platform RTV / return document",
        "arrival_photo_url": "arrival / sealed-cartons photo",
        "open_photo_url": "opened-cartons photo",
        "qc_evidence_url": "QC / damage evidence",
    }
    for field, label in labels.items():
        if not (evidence.get(field) or "").strip():
            frappe.throw("Attach the required {0}.".format(label))
    if not doc.cartons:
        frappe.throw("Record at least one physical carton before finalizing QC.")

    rows, exceptions = _validate_results(doc, results)
    if cint(doc.expected_cartons) and cint(doc.received_cartons) != cint(doc.expected_cartons):
        exceptions.append("Cartons expected {0}, received {1}".format(
            cint(doc.expected_cartons), cint(doc.received_cartons)))
    doc.platform_document_url = evidence["platform_document_url"]
    doc.arrival_photo_url = evidence["arrival_photo_url"]
    doc.open_photo_url = evidence["open_photo_url"]
    doc.qc_evidence_url = evidence["qc_evidence_url"]
    doc.notes = (notes or "").strip()
    doc.completed_at = now_datetime()
    doc.exception = cint(bool(exceptions))
    doc.exception_reason = "\n".join(exceptions)

    if doc.inventory_treatment == "Consignment Return":
        doc.draft_stock_entry = _prepare_consignment_transfer(doc, rows)
        doc.inventory_status = ("Draft Transfer Prepared" if doc.draft_stock_entry
                                else "Exception")
    elif doc.inventory_treatment == "Outright Return":
        doc.return_intake = _prepare_outright_intake(doc, rows, evidence)
        doc.inventory_status = ("Draft Return Intake Prepared" if doc.return_intake
                                else "Exception")
    else:
        doc.inventory_status = "Exception"
        exceptions.append("Inventory treatment requires investigation")
        doc.exception = 1
        doc.exception_reason = "\n".join(exceptions)

    doc.status = "Pending HQ Review" if rows else "Exception"
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    out = _payload(doc)
    out["status"] = "pending_review" if rows else "exception"
    out["message"] = ("QC saved. Inventory remains unchanged; HQ must review the draft document."
                      if rows else "QC saved as an exception; no inventory draft was prepared.")
    return out


@frappe.whitelist()
def b2b_return_queue(status=None, limit=250):
    """Read-only floor/HQ queue for platform return lots."""
    filters = {"status": status} if status else {}
    names = frappe.get_all("B2B Return Lot", filters=filters, pluck="name",
                           order_by="creation desc",
                           limit_page_length=min(max(cint(limit) or 250, 1), 1000))
    rows = [_payload(frappe.get_doc("B2B Return Lot", name)) for name in names]
    return {"rows": rows, "count": len(rows)}
