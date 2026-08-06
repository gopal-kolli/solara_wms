"""Scanner-first B2B outbound carton control.

This module deliberately stops at physical evidence.  It does not submit a
Delivery Note, Sales Invoice, Stock Entry or any other accounting document.
Corporate releases an existing Atlas SO/DN/SI; the warehouse records which
physical units went into each SOLARA handling unit, and Security records which
closed handling units were loaded.
"""
import json
import math

import frappe
from frappe.utils import cint, flt, now_datetime

from solara_wms.wms.d2c_appliance_express import (
    DEFAULT_PREKIT_BUNDLES,
    express_config,
)


CHANNELS = (
    "Amazon VC", "Flipkart VC", "Blinkit", "Swiggy", "Zepto", "Offline", "Other",
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


def _normalise(value):
    return (value or "").strip()


def _infer_channel(reference, customer=""):
    text = "{0} {1}".format(reference or "", customer or "").upper()
    if text.startswith("REZ") or "RETAILEZ" in text or "AMAZON" in text:
        return "Amazon VC"
    if text.startswith("FLK") or "FLIPKART" in text:
        return "Flipkart VC"
    if text.startswith("BLN") or "BLINKIT" in text:
        return "Blinkit"
    if text.startswith("SWG") or "SWIGGY" in text:
        return "Swiggy"
    if text.startswith("ZPT") or "ZEPTO" in text:
        return "Zepto"
    if text.startswith("OFF"):
        return "Offline"
    return "Other"


def carton_data_status(qty_per_carton, carton_weight_kg=0, hsn_code=""):
    """Classify Atlas case-pack data without pretending a bad value is safe.

    HSN chapter 85 finished goods are normally a direct factory carton and one
    unit/carton is expected.  A heavy non-appliance master carton recorded as
    one unit is suspicious (the known drinkware master-data failure mode).
    """
    qty = cint(qty_per_carton)
    if qty <= 0:
        return "Missing"
    if qty == 1 and flt(carton_weight_kg) >= 5 and not str(hsn_code or "").startswith("85"):
        return "Suspicious"
    if qty == 1:
        return "Direct Carton"
    return "Atlas"


def projected_cartons(items):
    total = 0
    for row in items:
        case_qty = cint(row.get("qty_per_carton"))
        if case_qty <= 0 or row.get("carton_data_status") == "Suspicious":
            return 0
        total += int(math.ceil(flt(row.get("expected_qty")) / case_qty))
    return total


def bulk_scan_allowed(data_status, qty_per_carton, scanned_qty):
    amount = flt(scanned_qty)
    return (amount <= 1 or
            (data_status == "Atlas" and
             abs(amount - cint(qty_per_carton)) <= 0.001))


def plan_carton_rows(items, prekit_bundles=None):
    """Expand job items into one (item_code, qty) row per physical carton.

    Pre-registration refuses to plan on untrustworthy data: every item needs a
    scannable EAN and a verified case pack.  Approved pre-kits are one sealed
    carton per unit regardless of their (non-stock) Item master carton fields.
    """
    prekits = prekit_bundles if prekit_bundles is not None else _prekit_bundle_codes()
    rows = []
    for row in items:
        item_code = row.get("item_code")
        if not row.get("ean"):
            frappe.throw(
                "Cannot pre-register cartons: {0} has no EAN barcode in Atlas. "
                "Add an Item Barcode row first.".format(item_code)
            )
        if item_code in prekits:
            case_qty = 1
        else:
            case_qty = cint(row.get("qty_per_carton"))
            if case_qty <= 0 or row.get("carton_data_status") in ("Missing", "Suspicious"):
                frappe.throw(
                    "Cannot pre-register cartons: {0} has {1} carton data "
                    "(qty_per_carton={2}). Correct the Item master first.".format(
                        item_code, row.get("carton_data_status") or "unusable",
                        cint(row.get("qty_per_carton")))
                )
        expected = flt(row.get("expected_qty"))
        full = int(expected // case_qty)
        remainder = expected - full * case_qty
        rows.extend([(item_code, float(case_qty))] * full)
        if remainder > 0.001:
            rows.append((item_code, remainder))
    return rows


def _company():
    return (frappe.defaults.get_user_default("company") or
            "Win The Buy Box Private Limited")


def _ean(item_code):
    rows = frappe.get_all(
        "Item Barcode", filters={"parent": item_code}, fields=["barcode"],
        order_by="idx asc", limit_page_length=2,
    )
    return rows[0].barcode if rows else None


def _item_master(item_code):
    fields = [
        "item_name", "is_stock_item", "disabled", "qty_per_carton",
        "carton_length_cm", "carton_width_cm", "carton_height_cm",
        "carton_weight_kg", "gst_hsn_code",
    ]
    value = frappe.db.get_value("Item", item_code, fields, as_dict=True)
    if not value:
        frappe.throw("Item {0} does not exist in Atlas.".format(item_code))
    return value


def _merge_item(out, item_code, qty):
    if not item_code or flt(qty) <= 0:
        return
    out[item_code] = out.get(item_code, 0) + flt(qty)


def _prekit_bundle_codes():
    """Use the same approved sealed-carton list as Appliance Express.

    These bundle parents are non-stock sales SKUs in Atlas, but physically the
    components are already sealed into one factory/combination carton.  B2B
    must therefore scan the parent carton EAN once, not explode it into loose
    component picks.
    """
    try:
        return set(express_config().get("prekit_bundles") or DEFAULT_PREKIT_BUNDLES)
    except Exception:
        return set(DEFAULT_PREKIT_BUNDLES)


def _physical_items(source):
    """Return stock-bearing physical items, expanding product bundles safely."""
    merged = {}
    packed_parents = set()
    prekit_bundles = _prekit_bundle_codes()
    for row in getattr(source, "packed_items", None) or []:
        if getattr(row, "parent_item", None) in prekit_bundles:
            # Approved pre-kits are already one sealed physical carton.  Their
            # Packed Item rows are accounting components, not floor picks.
            continue
        _merge_item(merged, row.item_code, row.qty)
        if getattr(row, "parent_item", None):
            packed_parents.add(row.parent_item)

    for row in source.items or []:
        master = _item_master(row.item_code)
        if row.item_code in prekit_bundles:
            _merge_item(merged, row.item_code, row.qty)
            continue
        if cint(master.is_stock_item):
            _merge_item(merged, row.item_code, row.qty)
            continue
        if row.item_code in packed_parents:
            continue
        bundle = frappe.db.get_value(
            "Product Bundle", {"new_item_code": row.item_code, "disabled": 0}, "name",
        )
        if not bundle:
            frappe.throw(
                "{0} is not a stock item and has no active Product Bundle. "
                "Corporate must correct the Atlas source before packing.".format(row.item_code)
            )
        for child in frappe.get_all(
                "Product Bundle Item", filters={"parent": bundle},
                fields=["item_code", "qty"], order_by="idx asc"):
            _merge_item(merged, child.item_code, flt(row.qty) * flt(child.qty))

    rows = []
    for item_code, expected_qty in merged.items():
        master = _item_master(item_code)
        if cint(master.disabled):
            frappe.throw("Item {0} is disabled in Atlas.".format(item_code))
        status = carton_data_status(
            master.qty_per_carton, master.carton_weight_kg, master.gst_hsn_code,
        )
        rows.append({
            "item_code": item_code,
            "item_name": master.item_name or item_code,
            "ean": _ean(item_code) or "",
            "expected_qty": expected_qty,
            "packed_qty": 0,
            "qty_per_carton": cint(master.qty_per_carton),
            "carton_data_status": status,
            "carton_length_cm": flt(master.carton_length_cm),
            "carton_width_cm": flt(master.carton_width_cm),
            "carton_height_cm": flt(master.carton_height_cm),
            "carton_weight_kg": flt(master.carton_weight_kg),
        })
    return sorted(rows, key=lambda x: x["item_code"])


def _find_source(reference):
    code = _normalise(reference)
    if not code:
        frappe.throw("Scan an Atlas invoice, Delivery Note, Sales Order or platform PO.")
    for doctype in ("Delivery Note", "Sales Invoice", "Sales Order"):
        if frappe.db.exists(doctype, code):
            doc = frappe.get_doc(doctype, code)
            if cint(doc.docstatus) == 2:
                frappe.throw("{0} is cancelled in Atlas.".format(code))
            if cint(doc.docstatus) != 1:
                frappe.throw("{0} is still Draft. Corporate must submit/release it first."
                             .format(code))
            if cint(getattr(doc, "is_return", 0)):
                frappe.throw("{0} is a return document, not an outbound B2B job."
                             .format(code))
            return doc

    # Platform PO is stored on Sales Order.po_no.  Use a submitted/active SO
    # only, and fail if the same external PO points to more than one SO.
    matches = frappe.get_all(
        "Sales Order", filters={"po_no": code, "docstatus": 1},
        fields=["name"], limit_page_length=3,
    )
    if len(matches) == 1:
        return frappe.get_doc("Sales Order", matches[0].name)
    if len(matches) > 1:
        frappe.throw("Platform PO {0} matches multiple Sales Orders; scan the Atlas invoice/DN."
                     .format(code))
    frappe.throw("No Atlas B2B source found for {0}.".format(code))


def _existing_job(reference):
    code = _normalise(reference)
    if not code:
        return None
    if frappe.db.exists("B2B Outbound Job", code):
        return frappe.get_doc("B2B Outbound Job", code)
    # The same shipment may be scanned by its SI, DN, SO or platform PO at
    # different stations.  All aliases must resolve to one job or the warehouse
    # could produce two sets of carton IDs for the same goods.
    for field in ("source_document", "sales_invoice", "delivery_note",
                  "sales_order", "platform_po"):
        rows = frappe.get_all(
            "B2B Outbound Job", filters={field: code},
            fields=["name"], order_by="creation desc", limit_page_length=1,
        )
        if rows:
            return frappe.get_doc("B2B Outbound Job", rows[0].name)
    return None


def _unit_payload(unit):
    return {
        "handling_unit": unit.name,
        "job": unit.job,
        "sequence": cint(unit.sequence),
        "status": unit.status,
        "planned_item_code": getattr(unit, "planned_item_code", None),
        "planned_qty": flt(getattr(unit, "planned_qty", 0)),
        "platform_carton_code": unit.platform_carton_code,
        "photo_url": unit.photo_url,
        "closed_at": unit.closed_at,
        "loaded_at": unit.loaded_at,
        "loaded_by": unit.loaded_by,
        "items": [{
            "item_code": row.item_code,
            "item_name": row.item_name,
            "ean": row.ean,
            "qty": flt(row.qty),
        } for row in (unit.items or [])],
        "qr_payload": "SOLARA:B2B-HU:{0}".format(unit.name),
    }


def _job_payload(job, include_units=True):
    out = {
        "job": job.name,
        "job_status": job.job_status,
        "channel": job.channel,
        "company": job.company,
        "customer": job.customer,
        "source_doctype": job.source_doctype,
        "source_document": job.source_document,
        "sales_order": job.sales_order,
        "delivery_note": job.delivery_note,
        "sales_invoice": job.sales_invoice,
        "platform_po": job.platform_po,
        "appointment_at": job.appointment_at,
        "expected_units": flt(job.expected_units),
        "packed_units": flt(job.packed_units),
        "expected_cartons": cint(job.expected_cartons),
        "closed_cartons": cint(job.closed_cartons),
        "loaded_cartons": cint(job.loaded_cartons),
        "items": [{
            "item_code": row.item_code,
            "item_name": row.item_name,
            "ean": row.ean,
            "expected_qty": flt(row.expected_qty),
            "packed_qty": flt(row.packed_qty),
            "qty_per_carton": cint(row.qty_per_carton),
            "carton_data_status": row.carton_data_status,
            "carton_length_cm": flt(row.carton_length_cm),
            "carton_width_cm": flt(row.carton_width_cm),
            "carton_height_cm": flt(row.carton_height_cm),
            "carton_weight_kg": flt(row.carton_weight_kg),
        } for row in (job.items or [])],
    }
    if include_units:
        units = frappe.get_all(
            "B2B Handling Unit", filters={"job": job.name}, fields=["name"],
            order_by="sequence asc", limit_page_length=10000,
        )
        out["handling_units"] = [
            _unit_payload(frappe.get_doc("B2B Handling Unit", row.name)) for row in units
        ]
    return out


def _linked_names(source):
    names = {"sales_order": None, "delivery_note": None, "sales_invoice": None}
    if source.doctype == "Sales Order":
        names["sales_order"] = source.name
    elif source.doctype == "Delivery Note":
        names["delivery_note"] = source.name
        sales_orders = sorted({getattr(row, "against_sales_order", None)
                               for row in source.items or [] if getattr(row, "against_sales_order", None)})
        names["sales_order"] = sales_orders[0] if len(sales_orders) == 1 else None
    elif source.doctype == "Sales Invoice":
        names["sales_invoice"] = source.name
        delivery_notes = sorted({getattr(row, "delivery_note", None)
                                 for row in source.items or [] if getattr(row, "delivery_note", None)})
        sales_orders = sorted({getattr(row, "sales_order", None)
                               for row in source.items or [] if getattr(row, "sales_order", None)})
        names["delivery_note"] = delivery_notes[0] if len(delivery_notes) == 1 else None
        names["sales_order"] = sales_orders[0] if len(sales_orders) == 1 else None
    return names


@frappe.whitelist()
def b2b_job_lookup(reference):
    """Read/resume a job, or preview an Atlas source before creating it."""
    job = _existing_job(reference)
    if job:
        out = _job_payload(job)
        out["status"] = "resume" if job.job_status != "Loaded" else "already"
        return out
    source = _find_source(reference)
    items = _physical_items(source)
    names = _linked_names(source)
    out = {
        "status": "ready",
        "source_doctype": source.doctype,
        "source_document": source.name,
        "company": getattr(source, "company", None) or _company(),
        "customer": getattr(source, "customer", None),
        "channel": _infer_channel(source.name, getattr(source, "customer", "")),
        "platform_po": getattr(source, "po_no", None),
        "items": items,
        "expected_units": sum(flt(row["expected_qty"]) for row in items),
        "expected_cartons": projected_cartons(items),
    }
    out.update(names)
    return out


@frappe.whitelist()
def b2b_job_start(reference, channel=None, platform_po=None, appointment_at=None):
    prior = _existing_job(reference)
    if prior:
        out = _job_payload(prior)
        out["status"] = "already" if prior.job_status == "Loaded" else "resume"
        return out
    source = _find_source(reference)
    items = _physical_items(source)
    if not items:
        frappe.throw("The Atlas source has no physical stock items to pack.")
    names = _linked_names(source)
    selected_channel = _normalise(channel) or _infer_channel(
        source.name, getattr(source, "customer", ""),
    )
    if selected_channel not in CHANNELS:
        frappe.throw("Choose a valid B2B channel.")
    po = _normalise(platform_po) or _normalise(getattr(source, "po_no", None))
    job = frappe.get_doc({
        "doctype": "B2B Outbound Job",
        "job_status": "Released",
        "channel": selected_channel,
        "company": getattr(source, "company", None) or _company(),
        "customer": getattr(source, "customer", None),
        "source_doctype": source.doctype,
        "source_document": source.name,
        "platform_po": po,
        "appointment_at": appointment_at,
        "expected_units": sum(flt(row["expected_qty"]) for row in items),
        "packed_units": 0,
        "expected_cartons": projected_cartons(items),
        "closed_cartons": 0,
        "loaded_cartons": 0,
        "released_at": now_datetime(),
        "released_by": frappe.session.user,
        "items": items,
    })
    for field, value in names.items():
        job.set(field, value)
    job.flags.ignore_permissions = True
    job.insert(ignore_permissions=True)
    out = _job_payload(job)
    out["status"] = "started"
    return out


@frappe.whitelist()
def b2b_plan_cartons(reference):
    """Pre-register every carton of a shipment at SI time (label-first flow).

    Creates the job (or adopts a fresh one) plus one Planned handling unit per
    physical carton, so all SOLARA QR labels can be printed BEFORE floor work.
    Refuses when master data cannot support a trustworthy plan, or when the
    job has already produced cartons.
    """
    started = b2b_job_start(reference)
    job = frappe.get_doc("B2B Outbound Job", started["job"])
    if job.job_status != "Released":
        frappe.throw("Cartons can only be pre-registered before packing starts "
                     "(job is {0}).".format(job.job_status))
    if frappe.db.count("B2B Handling Unit", {"job": job.name}):
        frappe.throw("This job already has cartons. Pre-registration must run "
                     "before any carton is opened.")
    rows = plan_carton_rows([{
        "item_code": r.item_code, "ean": r.ean,
        "expected_qty": r.expected_qty, "qty_per_carton": r.qty_per_carton,
        "carton_data_status": r.carton_data_status,
    } for r in job.items or []])
    for sequence, (item_code, qty) in enumerate(rows, start=1):
        unit = frappe.get_doc({
            "doctype": "B2B Handling Unit",
            "job": job.name,
            "sequence": sequence,
            "status": "Planned",
            "planned_item_code": item_code,
            "planned_qty": qty,
        })
        unit.flags.ignore_permissions = True
        unit.insert(ignore_permissions=True)
    job.expected_cartons = len(rows)
    job.flags.ignore_permissions = True
    job.save(ignore_permissions=True)
    out = _job_payload(job)
    out.update({"status": "planned", "planned_cartons": len(rows)})
    return out


@frappe.whitelist()
def b2b_carton_select(code):
    """Floor step 1 of the label-first flow: scan a pre-printed carton QR.

    Marks the Planned unit Open so the next EAN scan verifies contents into
    exactly this carton.  Scanning an already-open unit resumes it.
    """
    unit = _handling_unit_from_code(code)
    if not unit:
        return {"status": "not_found", "message": "Unknown SOLARA B2B carton ID."}
    if unit.status == "Open":
        out = _unit_payload(unit)
        out["status"] = "resume"
        return out
    if unit.status != "Planned":
        return {"status": "blocked",
                "message": "STOP: carton is {0}; only a planned or open carton "
                           "can be selected.".format(unit.status)}
    job = frappe.get_doc("B2B Outbound Job", unit.job)
    if job.job_status not in ("Released", "Packing"):
        return {"status": "error", "message": "This job is not open for packing."}
    unit.status = "Open"
    unit.opened_at = now_datetime()
    unit.opened_by = frappe.session.user
    unit.flags.ignore_permissions = True
    unit.save(ignore_permissions=True)
    if job.job_status == "Released":
        job.job_status = "Packing"
        job.pack_started_at = job.pack_started_at or now_datetime()
        job.flags.ignore_permissions = True
        job.save(ignore_permissions=True)
    out = _unit_payload(unit)
    out["status"] = "selected"
    return out


@frappe.whitelist()
def b2b_carton_open(job, platform_carton_code=None):
    doc = frappe.get_doc("B2B Outbound Job", job)
    if doc.job_status not in ("Released", "Packing"):
        return {"status": "error", "message": "This job is not open for packing."}
    if frappe.db.count("B2B Handling Unit", {"job": doc.name, "status": "Planned"}):
        return {
            "status": "error",
            "message": "This job uses pre-printed carton labels. Scan the "
                       "carton's SOLARA QR label instead of opening a new carton.",
        }
    open_units = frappe.get_all(
        "B2B Handling Unit", filters={"job": doc.name, "status": "Open"},
        fields=["name"], limit_page_length=2,
    )
    if open_units:
        unit = frappe.get_doc("B2B Handling Unit", open_units[0].name)
        out = _unit_payload(unit)
        out["status"] = "resume"
        return out
    sequence = cint(frappe.db.count("B2B Handling Unit", {"job": doc.name})) + 1
    unit = frappe.get_doc({
        "doctype": "B2B Handling Unit",
        "job": doc.name,
        "sequence": sequence,
        "status": "Open",
        "platform_carton_code": _normalise(platform_carton_code),
        "opened_at": now_datetime(),
        "opened_by": frappe.session.user,
    })
    unit.flags.ignore_permissions = True
    unit.insert(ignore_permissions=True)
    if doc.job_status == "Released":
        doc.job_status = "Packing"
        doc.pack_started_at = doc.pack_started_at or now_datetime()
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    out = _unit_payload(unit)
    out["status"] = "opened"
    return out


def _match_job_item(job, code):
    value = _normalise(code)
    # Physical barcode evidence is the control.  Typing the expected Item Code
    # would let the exact Triply-vs-Cast-Iron failure through: an operator could
    # select Triply on screen while holding a Cast Iron carton.  Accept any
    # barcode mapped to the job item in Atlas, but never accept an unmapped SKU
    # string as proof of the physical product.
    barcode_items = frappe.get_all(
        "Item Barcode", filters={"barcode": value}, fields=["parent"],
        limit_page_length=5,
    )
    mapped = {row.parent for row in barcode_items}
    matches = [row for row in job.items or []
               if (row.ean and value == row.ean) or row.item_code in mapped]
    if not matches:
        frappe.throw(
            "WRONG PRODUCT / UNMAPPED BARCODE: {0} is not a physical barcode "
            "for this PO. Hold the carton; do not type the expected SKU."
            .format(value)
        )
    if len(matches) > 1:
        frappe.throw(
            "Barcode {0} maps to multiple Atlas items. Hold the carton and correct "
            "the Item Barcode mapping before packing.".format(value)
        )
    return matches[0]


@frappe.whitelist()
def b2b_carton_scan(job, handling_unit, code, qty=1):
    job_doc = frappe.get_doc("B2B Outbound Job", job)
    unit = frappe.get_doc("B2B Handling Unit", handling_unit)
    if unit.job != job_doc.name or unit.status != "Open":
        return {"status": "error", "message": "Open the correct carton before scanning."}
    amount = flt(qty)
    if amount <= 0:
        frappe.throw("Quantity must be above zero.")
    planned = _match_job_item(job_doc, code)
    planned_item = _normalise(getattr(unit, "planned_item_code", None))
    planned_qty = flt(getattr(unit, "planned_qty", 0))
    if planned_item and planned.item_code != planned_item:
        return {
            "status": "wrong_carton",
            "message": "STOP: this carton label is pre-registered for {0}, not {1}. "
                       "Hold both cartons and call the supervisor.".format(
                           planned_item, planned.item_code),
        }
    if unit.items and any(row.item_code != planned.item_code for row in unit.items):
        return {
            "status": "wrong_carton",
            "message": "STOP: one master carton may contain only one SKU. Close this carton first.",
        }
    if amount > 1:
        case_qty = cint(planned.qty_per_carton)
        plan_bulk_ok = planned_item and planned_qty and abs(amount - planned_qty) <= 0.001
        if not plan_bulk_ok and not bulk_scan_allowed(planned.carton_data_status, case_qty, amount):
            return {
                "status": "case_qty_blocked",
                "message": (
                    "STOP: bulk quantity is allowed only for a verified full master carton "
                    "of {0} units. Scan individual units or correct Atlas carton data."
                ).format(case_qty or 0),
            }
    if flt(planned.packed_qty) + amount > flt(planned.expected_qty) + 0.001:
        return {
            "status": "excess",
            "message": "STOP: {0} would exceed the job quantity ({1:g}/{2:g}).".format(
                planned.item_code, flt(planned.packed_qty), flt(planned.expected_qty)),
        }
    line = next((row for row in unit.items or [] if row.item_code == planned.item_code), None)
    if line:
        line.qty = flt(line.qty) + amount
    else:
        unit.append("items", {
            "item_code": planned.item_code,
            "item_name": planned.item_name,
            "ean": planned.ean,
            "qty": amount,
        })
    planned.packed_qty = flt(planned.packed_qty) + amount
    job_doc.packed_units = flt(job_doc.packed_units) + amount
    unit.flags.ignore_permissions = True
    unit.save(ignore_permissions=True)
    job_doc.flags.ignore_permissions = True
    job_doc.save(ignore_permissions=True)

    # Label-first cartons close themselves the moment their plan is fulfilled:
    # the label is already applied, so there is nothing left for the packer to do.
    auto_closed = False
    if planned_item and planned_qty:
        carton_total = sum(flt(row.qty) for row in unit.items or [])
        if carton_total + 0.001 >= planned_qty:
            job_doc = _finalise_close(unit)
            auto_closed = True

    return {
        "status": "ok",
        "item_code": planned.item_code,
        "item_name": planned.item_name,
        "scanned_qty": amount,
        "item_packed_qty": flt(planned.packed_qty),
        "item_expected_qty": flt(planned.expected_qty),
        "carton": _unit_payload(unit),
        "auto_closed": auto_closed,
        "job_status": job_doc.job_status,
        "job_packed_units": flt(job_doc.packed_units),
        "job_expected_units": flt(job_doc.expected_units),
    }


@frappe.whitelist()
def b2b_carton_remove(job, handling_unit, item_code, qty=1):
    """Undo an accidental scan while the carton is still open."""
    job_doc = frappe.get_doc("B2B Outbound Job", job)
    unit = frappe.get_doc("B2B Handling Unit", handling_unit)
    if unit.job != job_doc.name or unit.status != "Open":
        return {"status": "error", "message": "Only the open carton can be corrected."}
    amount = flt(qty)
    if amount <= 0:
        frappe.throw("Quantity must be above zero.")
    planned = next((row for row in job_doc.items or [] if row.item_code == item_code), None)
    line = next((row for row in unit.items or [] if row.item_code == item_code), None)
    if not planned or not line or flt(line.qty) + 0.001 < amount:
        return {"status": "error", "message": "That quantity is not in the open carton."}
    line.qty = flt(line.qty) - amount
    planned.packed_qty = max(0, flt(planned.packed_qty) - amount)
    job_doc.packed_units = max(0, flt(job_doc.packed_units) - amount)
    if flt(line.qty) <= 0.001:
        unit.remove(line)
    unit.flags.ignore_permissions = True
    unit.save(ignore_permissions=True)
    job_doc.flags.ignore_permissions = True
    job_doc.save(ignore_permissions=True)
    return {
        "status": "ok",
        "message": "Removed {0:g} x {1}.".format(amount, item_code),
        "carton": _unit_payload(unit),
        "job_packed_units": flt(job_doc.packed_units),
        "job_expected_units": flt(job_doc.expected_units),
    }


def _finalise_close(unit, platform_carton_code=None, photo_url=None):
    unit.platform_carton_code = _normalise(platform_carton_code) or unit.platform_carton_code
    unit.photo_url = _normalise(photo_url) or unit.photo_url
    unit.status = "Closed"
    unit.closed_at = now_datetime()
    unit.closed_by = frappe.session.user
    unit.flags.ignore_permissions = True
    unit.save(ignore_permissions=True)

    job = frappe.get_doc("B2B Outbound Job", unit.job)
    job.closed_cartons = frappe.db.count(
        "B2B Handling Unit", {"job": job.name, "status": ["in", ["Closed", "Loaded"]]},
    )
    complete = abs(flt(job.packed_units) - flt(job.expected_units)) <= 0.001
    if complete:
        job.job_status = "Pack Complete"
        job.pack_completed_at = now_datetime()
    job.flags.ignore_permissions = True
    job.save(ignore_permissions=True)
    return job


@frappe.whitelist()
def b2b_carton_close(handling_unit, platform_carton_code=None, photo_url=None):
    unit = frappe.get_doc("B2B Handling Unit", handling_unit)
    if unit.status == "Closed":
        out = _unit_payload(unit)
        out["status"] = "already"
        return out
    if unit.status != "Open":
        return {"status": "error", "message": "Only an open carton can be closed."}
    if not unit.items:
        return {"status": "error", "message": "Scan at least one item before closing."}
    prelabelled = bool(getattr(unit, "planned_item_code", None))
    job = _finalise_close(unit, platform_carton_code, photo_url)
    out = _unit_payload(unit)
    out.update({
        "status": "closed",
        "job_status": job.job_status,
        "closed_cartons": cint(job.closed_cartons),
        "message": ("Carton closed. Label was pre-applied." if prelabelled else
                    "Carton closed. Print and apply the SOLARA internal QR label."),
    })
    return out


def _handling_unit_from_code(code):
    value = _normalise(code)
    prefix = "SOLARA:B2B-HU:"
    if value.upper().startswith(prefix):
        value = value[len(prefix):]
    if not frappe.db.exists("B2B Handling Unit", value):
        return None
    return frappe.get_doc("B2B Handling Unit", value)


@frappe.whitelist()
def b2b_load_scan(code, vehicle_number=None, lr_number=None):
    unit = _handling_unit_from_code(code)
    if not unit:
        return {"status": "not_found", "message": "Unknown SOLARA B2B carton ID."}
    job = frappe.get_doc("B2B Outbound Job", unit.job)
    if unit.status == "Loaded":
        return {
            "status": "duplicate", "handling_unit": unit.name, "job": job.name,
            "message": "DUPLICATE: this carton was already loaded.",
        }
    if unit.status != "Closed":
        return {
            "status": "blocked", "handling_unit": unit.name, "job": job.name,
            "message": "STOP: carton is {0}, not closed by Packing.".format(unit.status),
        }
    if job.job_status not in ("Pack Complete", "Loading"):
        return {
            "status": "blocked", "handling_unit": unit.name, "job": job.name,
            "message": "STOP: PO/job packing is not complete.",
        }
    unit.status = "Loaded"
    unit.loaded_at = now_datetime()
    unit.loaded_by = frappe.session.user
    unit.vehicle_number = _normalise(vehicle_number)
    unit.lr_number = _normalise(lr_number)
    unit.flags.ignore_permissions = True
    unit.save(ignore_permissions=True)

    job.loaded_cartons = frappe.db.count(
        "B2B Handling Unit", {"job": job.name, "status": "Loaded"},
    )
    job.vehicle_number = _normalise(vehicle_number) or job.vehicle_number
    job.lr_number = _normalise(lr_number) or job.lr_number
    if cint(job.loaded_cartons) == cint(job.closed_cartons):
        job.job_status = "Loaded"
        job.load_completed_at = now_datetime()
    else:
        job.job_status = "Loading"
        job.load_started_at = job.load_started_at or now_datetime()
    job.flags.ignore_permissions = True
    job.save(ignore_permissions=True)
    return {
        "status": "loaded",
        "handling_unit": unit.name,
        "job": job.name,
        "channel": job.channel,
        "platform_po": job.platform_po,
        "loaded_cartons": cint(job.loaded_cartons),
        "closed_cartons": cint(job.closed_cartons),
        "job_status": job.job_status,
        "message": "Loaded {0} ({1}/{2} cartons).".format(
            unit.name, cint(job.loaded_cartons), cint(job.closed_cartons)),
    }


@frappe.whitelist()
def b2b_job_get(job):
    return _job_payload(frappe.get_doc("B2B Outbound Job", job))
