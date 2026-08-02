"""Scanner-first warehouse return intake.

One ``D2C Return Parcel`` represents one physical reverse AWB.  The station
records receipt and QC evidence, but it never posts stock or refunds money.  A
successful QC submission creates a draft ``Return Intake`` in Pending HQ Review;
the existing approval workflow is the only path that creates a Return Delivery
Note and moves inventory.
"""
import json
from collections import defaultdict

import frappe
from frappe.utils import cint, flt, now_datetime, today

from solara_wms.wms.d2c_dispatch import _resolve
from solara_wms.wms.d2c_fulfillment import _awb_courier_pairs
from solara_wms.wms.doctype.return_intake.return_intake import ReturnIntake


CUSTOMER_REASONS = {
    "Customer refused", "Customer unreachable", "Address / delivery failure",
    "Changed mind", "Customer reported defective", "Customer reported damaged",
    "Wrong / missing item reported", "Other",
}
CONDITIONS = {"Good", "Damaged", "Used", "Incomplete", "Wrong Item", "Missing / Empty"}
FINDINGS = {
    "No fault found", "Transit damage", "Used / customer damage",
    "Missing accessories", "Wrong product", "Empty parcel", "Serial mismatch",
    "Packaging damage only", "Other",
}


def _as_json(value, default):
    if isinstance(value, type(default)):
        return value
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _find_sales_invoice(dn):
    """Find the submitted original SI through every linkage shape used by Shopify."""
    candidates = []

    for row in frappe.get_all(
            "Delivery Note Item",
            filters={"parent": dn.name, "docstatus": 1,
                     "against_sales_invoice": ["is", "set"]},
            fields=["against_sales_invoice"], limit_page_length=0):
        candidates.append(row.against_sales_invoice)

    for row in frappe.get_all(
            "Sales Invoice Item",
            filters={"delivery_note": dn.name, "docstatus": 1},
            fields=["parent"], limit_page_length=0):
        candidates.append(row.parent)

    sales_orders = sorted({row.against_sales_order for row in dn.items
                           if row.get("against_sales_order")})
    if sales_orders:
        for row in frappe.get_all(
                "Sales Invoice Item",
                filters={"sales_order": ["in", sales_orders], "docstatus": 1},
                fields=["parent"], limit_page_length=0):
            candidates.append(row.parent)

    seen = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        si = frappe.get_doc("Sales Invoice", name)
        if si.docstatus == 1 and not si.is_return:
            return si
    return None


def _max_returnable(si):
    """Use the same source-link and prior-return rules as Return Intake itself."""
    probe = ReturnIntake({"doctype": "Return Intake", "sales_invoice": si.name,
                          "company": si.company})
    delivered, _source, dns = probe._build_delivered_map(si)
    already = probe._already_returned_map(dns, si.name)
    return {code: max(0, flt(qty) - flt(already.get(code)))
            for code, qty in delivered.items()}


def _expected_items(dn, si):
    """Return customer-facing order lines, excluding pure service/warranty lines."""
    bundle_parents = {row.parent_item for row in (dn.get("packed_items") or [])
                      if row.get("parent_item")}
    maxima = _max_returnable(si)
    grouped = {}
    for row in dn.items:
        qty = abs(flt(row.qty))
        if qty <= 0:
            continue
        meta = frappe.db.get_value(
            "Item", row.item_code, ["is_stock_item", "has_serial_no", "image"],
            as_dict=True) or {}
        if not cint(meta.get("is_stock_item")) and row.item_code not in bundle_parents:
            continue
        if row.item_code not in grouped:
            grouped[row.item_code] = {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "image": meta.get("image"),
                "expected_qty": 0,
                "max_returnable": flt(maxima.get(row.item_code)),
                "serial_required": cint(meta.get("has_serial_no")),
            }
        grouped[row.item_code]["expected_qty"] += qty
    return list(grouped.values())


def _channel(si):
    name = (si.name or "").upper()
    if name.startswith("SHP"):
        return "Shopify"
    if name.startswith("BLN"):
        return "Blinkit"
    if name.startswith("ZEP"):
        return "Zepto"
    if name.startswith(("REZ", "AMZ")):
        return "Amazon/Retailez"
    if name.startswith("FLK"):
        return "Flipkart"
    return "Other"


def _lookup(reverse_awb, order_code=None):
    reverse_awb = (reverse_awb or "").strip()
    order_code = (order_code or "").strip()
    if not reverse_awb:
        return {"status": "error", "message": "Scan the reverse AWB."}

    existing = frappe.get_all(
        "D2C Return Parcel", filters={"reverse_awb": reverse_awb},
        fields=["name"], limit_page_length=1)
    if existing:
        out = _parcel_payload(frappe.get_doc("D2C Return Parcel", existing[0].name))
        out["status"] = "resume" if out.get("parcel_status") == "QC In Progress" else "already"
        out["message"] = ("Resume the QC already started for this reverse AWB."
                          if out["status"] == "resume" else
                          "This reverse AWB has already been recorded.")
        return out

    lookup_code = order_code or reverse_awb
    dn_name, forward_awb, _box_index, _box_count = _resolve(lookup_code)
    if not dn_name:
        return {
            "status": "need_order",
            "reverse_awb": reverse_awb,
            "message": "Reverse AWB not mapped yet. Enter the Shopify order number or original AWB.",
        }

    dn = frappe.get_doc("Delivery Note", dn_name)
    si = _find_sales_invoice(dn)
    if not si:
        return {"status": "error", "reverse_awb": reverse_awb,
                "message": "The order was found, but no submitted original Sales Invoice is linked."}
    pairs = _awb_courier_pairs(dn)
    items = _expected_items(dn, si)
    if not items:
        return {"status": "error", "reverse_awb": reverse_awb,
                "message": "No returnable physical items were found on this order."}
    return {
        "status": "ok",
        "reverse_awb": reverse_awb,
        "lookup_code": lookup_code,
        "order": dn.get("shopify_order_number") or dn.get("shopify_order_id"),
        "customer_name": dn.get("customer_name") or si.get("customer_name"),
        "dn": dn.name,
        "sales_invoice": si.name,
        "forward_awb": forward_awb or (pairs[0][0] if pairs else None),
        "courier": next((courier for awb, courier in pairs if awb == forward_awb), None)
                   or dn.get("courier_partner"),
        "items": items,
    }


def _parcel_payload(doc):
    return {
        "parcel": doc.name,
        "parcel_status": doc.status,
        "reverse_awb": doc.reverse_awb,
        "lookup_code": doc.lookup_code,
        "order": doc.shopify_order_number,
        "customer_name": doc.customer_name,
        "dn": doc.delivery_note,
        "sales_invoice": doc.sales_invoice,
        "forward_awb": doc.forward_awb,
        "courier": doc.courier,
        "return_type": doc.return_type,
        "holding_bin": doc.holding_bin,
        "return_intake": doc.return_intake,
        "label_photo_url": doc.label_photo_url,
        "open_photo_url": doc.open_photo_url,
        "qc_evidence_url": doc.qc_evidence_url,
        "items": [{
            "item_code": row.item_code,
            "item_name": row.item_name,
            "image": row.image,
            "expected_qty": flt(row.expected_qty),
            "max_returnable": flt(row.max_returnable),
            "received_qty": flt(row.received_qty),
            "serial_required": cint(row.serial_required),
            "serial_number": row.serial_number,
            "serial_match": cint(row.serial_match),
            "condition": row.condition,
            "disposition": row.disposition,
            "warehouse_finding": row.warehouse_finding,
            "accessories_complete": cint(row.accessories_complete),
            "visual_pass": cint(row.visual_pass),
            "power_test": row.power_test,
            "function_test": row.function_test,
            "notes": row.notes,
        } for row in doc.items],
    }


@frappe.whitelist()
def return_lookup(reverse_awb, order_code=None):
    """Resolve a reverse AWB, with order/original-AWB fallback. Read-only."""
    return _lookup(reverse_awb, order_code)


@frappe.whitelist()
def return_start(reverse_awb, order_code=None, return_type="Unknown", station=None,
                 holding_bin=None):
    """Claim a physical reverse parcel for QC; idempotent by reverse AWB."""
    out = _lookup(reverse_awb, order_code)
    if out.get("status") in ("resume", "already"):
        return out
    if out.get("status") != "ok":
        return out
    if return_type not in ("Customer Return", "RTO", "Unknown"):
        return {"status": "error", "message": "Invalid return type."}

    doc = frappe.get_doc({
        "doctype": "D2C Return Parcel",
        "status": "QC In Progress",
        "reverse_awb": out["reverse_awb"],
        "courier": out.get("courier"),
        "return_type": return_type,
        "station": (station or "Returns Station").strip(),
        "holding_bin": (holding_bin or "").strip(),
        "shopify_order_number": out.get("order"),
        "customer_name": out.get("customer_name"),
        "delivery_note": out["dn"],
        "sales_invoice": out["sales_invoice"],
        "forward_awb": out.get("forward_awb"),
        "lookup_code": out.get("lookup_code"),
        "received_at": now_datetime(),
        "received_by": frappe.session.user,
        "items": [{**item, "received_qty": item["expected_qty"]}
                  for item in out["items"]],
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    payload = _parcel_payload(doc)
    payload["status"] = "started"
    return payload


def _validate_good(row, raw):
    if abs(flt(row.received_qty) - flt(row.expected_qty)) > 0.001:
        frappe.throw("{0}: Good quantity must equal the full expected quantity.".format(row.item_code))
    if not cint(row.accessories_complete):
        frappe.throw("{0}: Good requires every accessory/component to be present.".format(row.item_code))
    if not cint(row.visual_pass):
        frappe.throw("{0}: Good requires the visual inspection to pass.".format(row.item_code))
    if row.power_test not in ("Pass", "Not Applicable"):
        frappe.throw("{0}: Good requires a passed or not-applicable power test.".format(row.item_code))
    if row.function_test not in ("Pass", "Not Applicable"):
        frappe.throw("{0}: Good requires a passed or not-applicable functional test.".format(row.item_code))
    if cint(row.serial_required) and (not row.serial_number or not cint(row.serial_match)):
        frappe.throw("{0}: scan the serial number and confirm it matches before marking Good.".format(row.item_code))
    # Make sure false answers were sent deliberately, not inherited silently.
    for key in ("accessories_complete", "visual_pass", "power_test", "function_test"):
        if key not in raw:
            frappe.throw("{0}: complete every QC check.".format(row.item_code))


@frappe.whitelist()
def return_finalize(parcel, item_results=None, evidence=None, return_type=None,
                    customer_reason=None, warehouse_finding=None, notes=None,
                    claim_required=0):
    """Freeze QC evidence and create a Pending-HQ-Review Return Intake.

    This function deliberately never submits the Return Intake. Inventory only
    moves when an HQ Returns Reviewer approves the existing workflow.
    """
    doc = frappe.get_doc("D2C Return Parcel", parcel)
    if doc.return_intake:
        return {"status": "already", "parcel": doc.name,
                "return_intake": doc.return_intake,
                "message": "This return is already pending HQ review."}
    if doc.status != "QC In Progress":
        return {"status": "error", "message": "This return parcel is not open for QC."}

    results = _as_json(item_results, [])
    result_map = {row.get("item_code"): row for row in results if row.get("item_code")}
    evidence = _as_json(evidence, {})
    required_evidence = {
        "label_photo_url": "unopened parcel / label photo",
        "open_photo_url": "opened parcel photo",
        "qc_evidence_url": "QC / serial / damage photo",
    }
    for key, label in required_evidence.items():
        if not (evidence.get(key) or "").strip():
            frappe.throw("Take the required {0} before submitting.".format(label))

    if customer_reason not in CUSTOMER_REASONS:
        frappe.throw("Choose why the parcel was returned.")
    if warehouse_finding and warehouse_finding not in FINDINGS:
        frappe.throw("Choose a valid warehouse finding.")
    if return_type and return_type not in ("Customer Return", "RTO", "Unknown"):
        frappe.throw("Choose a valid return type.")

    intake_rows = []
    has_exception = False
    findings = []
    for row in doc.items:
        raw = result_map.get(row.item_code)
        if not raw:
            frappe.throw("Complete QC for {0}.".format(row.item_code))
        condition = (raw.get("condition") or "").strip()
        if condition not in CONDITIONS:
            frappe.throw("Choose the condition for {0}.".format(row.item_code))
        received = flt(raw.get("received_qty"))
        if received < 0 or received > flt(row.expected_qty) + 0.001:
            frappe.throw("{0}: received quantity must be between 0 and {1}."
                         .format(row.item_code, row.expected_qty))

        row.received_qty = received
        row.condition = condition
        row.serial_number = (raw.get("serial_number") or "").strip()
        row.serial_match = cint(raw.get("serial_match"))
        row.accessories_complete = cint(raw.get("accessories_complete"))
        row.visual_pass = cint(raw.get("visual_pass"))
        row.power_test = raw.get("power_test")
        row.function_test = raw.get("function_test")
        row.warehouse_finding = raw.get("warehouse_finding") or warehouse_finding
        row.notes = (raw.get("notes") or "").strip()

        if condition == "Good":
            _validate_good(row, raw)
            row.disposition = "Main Warehouse"
        elif condition in ("Damaged", "Used", "Incomplete"):
            if received <= 0:
                frappe.throw("{0}: received quantity is required for a physical {1} item."
                             .format(row.item_code, condition.lower()))
            row.disposition = "QC / Rejected"
        else:
            # Wrong/empty parcels are evidence and claims, not receipt of the SKU
            # that should have been in the box.
            if received != 0:
                frappe.throw("{0}: wrong/missing expected stock must have received quantity 0."
                             .format(row.item_code))
            row.disposition = "Investigation / No Receipt"
            has_exception = True

        if received < flt(row.expected_qty) - 0.001:
            has_exception = True
        if row.warehouse_finding:
            findings.append(row.warehouse_finding)

        if received > 0 and row.disposition != "Investigation / No Receipt":
            intake_rows.append({
                "item_code": row.item_code,
                "return_qty": received,
                "condition": ("Good" if condition == "Good"
                              else ("Used" if condition == "Used" else "Damaged")),
            })

    doc.return_type = return_type or doc.return_type
    doc.customer_reason = customer_reason
    doc.warehouse_finding = warehouse_finding or (findings[0] if findings else None)
    doc.claim_required = cint(claim_required) or cint(has_exception)
    doc.exception = cint(has_exception)
    doc.notes = (notes or "").strip()
    doc.label_photo_url = evidence["label_photo_url"]
    doc.open_photo_url = evidence["open_photo_url"]
    doc.qc_evidence_url = evidence["qc_evidence_url"]
    doc.evidence_urls = json.dumps(evidence, sort_keys=True)
    doc.completed_at = now_datetime()

    if not intake_rows:
        doc.status = "Exception"
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        return {"status": "exception", "parcel": doc.name,
                "message": "Recorded for investigation. No expected SKU was added to inventory."}

    si = frappe.get_doc("Sales Invoice", doc.sales_invoice)
    intake = frappe.get_doc({
        "doctype": "Return Intake",
        # Insert in the workflow's legal initial state. The station then moves
        # it to Pending HQ Review with a DB state update: applying the workflow
        # action here would depend on the dashboard API user's desk roles, while
        # the station endpoint already enforces the full QC/evidence contract.
        "workflow_state": "Draft",
        "sales_invoice": si.name,
        "customer": si.customer,
        "customer_name": si.customer_name,
        "company": si.company,
        "channel": _channel(si),
        "posting_date": today(),
        "return_parcel": doc.name,
        "items": intake_rows,
        "qc_videos": [
            {"video": evidence["label_photo_url"], "note": "Unopened parcel / reverse label"},
            {"video": evidence["open_photo_url"], "note": "Opened parcel contents"},
            {"video": evidence["qc_evidence_url"], "note": "QC / serial / damage evidence"},
        ],
        "remarks": ("Returns Station {0} · reverse AWB {1} · {2} · {3}"
                    .format(doc.station or "", doc.reverse_awb, customer_reason,
                            warehouse_finding or "finding recorded"))[:140],
    })
    intake.flags.ignore_permissions = True
    intake.insert(ignore_permissions=True)
    frappe.db.set_value("Return Intake", intake.name, "workflow_state", "Pending HQ Review")
    intake.workflow_state = "Pending HQ Review"

    doc.return_intake = intake.name
    doc.status = "Pending HQ Review"
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    return {
        "status": "pending_review",
        "parcel": doc.name,
        "return_intake": intake.name,
        "exception": cint(has_exception),
        "message": "QC saved. Inventory remains unchanged until HQ approves the Return Intake.",
    }
