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
CONDITIONS = {
    "Good", "Repairable", "Scrap", "Damaged", "Used", "Incomplete",
    "Wrong Item", "Missing / Empty",
}
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
    """Return order lines plus the physical packed-component checklist.

    The accounting return must stay on the original Delivery Note parent line,
    but warehouse QC must prove every component packed behind that line.  A
    complete component checklist is therefore nested under each parent item.
    """
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
    packed = defaultdict(dict)
    for row in (dn.get("packed_items") or []):
        parent = row.get("parent_item")
        code = row.get("item_code")
        qty = abs(flt(row.get("qty")))
        if not parent or not code or qty <= 0:
            continue
        meta = frappe.db.get_value(
            "Item", code, ["item_name", "image"], as_dict=True) or {}
        component = packed[parent].setdefault(code, {
            "parent_item_code": parent,
            "item_code": code,
            "item_name": meta.get("item_name") or code,
            "image": meta.get("image"),
            "expected_qty": 0,
        })
        component["expected_qty"] += qty

    output = []
    for item in grouped.values():
        item["components"] = list(packed.get(item["item_code"], {}).values())
        output.append(item)
    return output


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


def _snapshot_rows(expected_items):
    item_rows = []
    component_rows = []
    for expected in expected_items:
        row = dict(expected)
        nested = row.pop("components", [])
        row["received_qty"] = row["expected_qty"]
        item_rows.append(row)
        component_rows.extend({**component, "received_qty": 0, "present": 0}
                              for component in nested)
    return item_rows, component_rows


def _resolved_lookup(reverse_awb, lookup_code, allow_additional=False,
                     exclude_parcel=None):
    """Resolve a known order/AWB without creating or mutating a parcel."""
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
    result = {
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
    if not allow_additional:
        open_parcels = frappe.get_all(
            "D2C Return Parcel",
            filters={
                "delivery_note": dn.name,
                "status": ["in", ["QC In Progress", "Pending HQ Review"]],
            },
            fields=["name", "reverse_awb", "status", "return_intake", "received_at"],
            order_by="creation desc",
            limit_page_length=20,
        )
        if exclude_parcel:
            open_parcels = [row for row in open_parcels if row.name != exclude_parcel]
        if open_parcels:
            result.update({
                "status": "possible_duplicate",
                "existing_parcels": open_parcels,
                "message": (
                    "Another open return parcel already exists for this order. "
                    "Resume it, or explicitly confirm this is a separate physical parcel."
                ),
            })
    return result


def _identify_pending(doc, order_code, allow_additional=False):
    """Attach an Identity Pending parcel to its exact source order."""
    out = _resolved_lookup(
        doc.reverse_awb, order_code, allow_additional=allow_additional,
        exclude_parcel=doc.name,
    )
    if out.get("status") != "ok":
        return out
    item_rows, component_rows = _snapshot_rows(out["items"])
    doc.shopify_order_number = out.get("order")
    doc.customer_name = out.get("customer_name")
    doc.delivery_note = out["dn"]
    doc.sales_invoice = out["sales_invoice"]
    doc.forward_awb = out.get("forward_awb")
    doc.lookup_code = order_code
    doc.courier = doc.courier or out.get("courier")
    doc.status = "QC In Progress"
    doc.inventory_status = "Pending QC"
    doc.finance_status = "Pending Inventory"
    doc.credit_note_status = "Pending"
    doc.refund_status = "Pending Verification"
    doc.exception = 0
    doc.identified_at = now_datetime()
    doc.identified_by = frappe.session.user
    doc.set("items", item_rows)
    doc.set("components", component_rows)
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    payload = _parcel_payload(doc)
    payload["status"] = "resolved"
    payload["message"] = "Order identified. Review the expected contents before opening and QC."
    return payload


def _lookup(reverse_awb, order_code=None, allow_additional=False,
            claim_identity=False):
    reverse_awb = (reverse_awb or "").strip()
    order_code = (order_code or "").strip()
    if not reverse_awb:
        return {"status": "error", "message": "Scan the reverse AWB."}

    existing = frappe.get_all(
        "D2C Return Parcel", filters={"reverse_awb": reverse_awb},
        fields=["name"], limit_page_length=1)
    if existing:
        doc = frappe.get_doc("D2C Return Parcel", existing[0].name)
        if doc.status == "Identity Pending":
            if order_code:
                if claim_identity:
                    return _identify_pending(doc, order_code, allow_additional)
                return _resolved_lookup(
                    doc.reverse_awb, order_code,
                    allow_additional=allow_additional,
                    exclude_parcel=doc.name,
                )
            out = _parcel_payload(doc)
            out["status"] = "identity_pending"
            out["message"] = (
                "This parcel is already in Identity Pending. Enter the exact Shopify "
                "order number or original AWB when it is found."
            )
            return out
        out = _parcel_payload(doc)
        out["status"] = "resume" if out.get("parcel_status") == "QC In Progress" else "already"
        out["message"] = ("Resume the QC already started for this reverse AWB."
                          if out["status"] == "resume" else
                          "This reverse AWB has already been recorded.")
        return out

    lookup_code = order_code or reverse_awb
    return _resolved_lookup(reverse_awb, lookup_code, allow_additional)


def _parcel_payload(doc):
    components = defaultdict(list)
    for row in (doc.get("components") or []):
        components[row.parent_item_code].append({
            "parent_item_code": row.parent_item_code,
            "item_code": row.item_code,
            "item_name": row.item_name,
            "image": row.image,
            "expected_qty": flt(row.expected_qty),
            "received_qty": flt(row.received_qty),
            "present": cint(row.present),
            "notes": row.notes,
        })
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
        "sender_name": doc.sender_name,
        "sender_phone": doc.sender_phone,
        "sender_pincode": doc.sender_pincode,
        "observed_item": doc.observed_item,
        "observed_serial_number": doc.observed_serial_number,
        "identity_notes": doc.identity_notes,
        "identified_at": doc.identified_at,
        "identified_by": doc.identified_by,
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
            "components": components.get(row.item_code, []),
        } for row in doc.items],
    }


@frappe.whitelist()
def return_lookup(reverse_awb, order_code=None):
    """Resolve a reverse AWB, with order/original-AWB fallback. Read-only."""
    return _lookup(reverse_awb, order_code)


def _clean_unidentified(reverse_awb, holding_bin, observed_item, **values):
    cleaned = {
        "reverse_awb": (reverse_awb or "").strip(),
        "holding_bin": (holding_bin or "").strip(),
        "observed_item": (observed_item or "").strip(),
    }
    if not cleaned["reverse_awb"]:
        frappe.throw("Scan the reverse AWB.")
    if not cleaned["holding_bin"]:
        frappe.throw("Assign a physical holding bin for the unidentified parcel.")
    if not cleaned["observed_item"]:
        frappe.throw("Record the product, SKU or physical description found inside.")
    for key in ("courier", "station", "sender_name", "sender_phone", "sender_pincode",
                "observed_serial_number", "identity_notes"):
        cleaned[key] = (values.get(key) or "").strip()
    return cleaned


@frappe.whitelist()
def return_unidentified_start(reverse_awb, holding_bin, observed_item, courier=None,
                              station=None, sender_name=None, sender_phone=None,
                              sender_pincode=None, observed_serial_number=None,
                              identity_notes=None):
    """Log a physical parcel before its original order is known.

    This is a memorandum/physical-control record only. It deliberately has no
    Sales Invoice, Delivery Note, item receipt, Credit Note or refund action.
    """
    data = _clean_unidentified(
        reverse_awb, holding_bin, observed_item, courier=courier, station=station,
        sender_name=sender_name, sender_phone=sender_phone,
        sender_pincode=sender_pincode,
        observed_serial_number=observed_serial_number,
        identity_notes=identity_notes,
    )
    existing = frappe.get_all(
        "D2C Return Parcel", filters={"reverse_awb": data["reverse_awb"]},
        fields=["name"], limit_page_length=1,
    )
    if existing:
        doc = frappe.get_doc("D2C Return Parcel", existing[0].name)
        payload = _parcel_payload(doc)
        payload["status"] = ("identity_pending" if doc.status == "Identity Pending"
                             else "already")
        payload["message"] = ("Unidentified parcel already logged; evidence can be retried."
                              if doc.status == "Identity Pending" else
                              "This reverse AWB has already been recorded.")
        return payload

    doc = frappe.get_doc({
        "doctype": "D2C Return Parcel",
        "status": "Identity Pending",
        "reverse_awb": data["reverse_awb"],
        "courier": data["courier"],
        "return_type": "Unknown",
        "station": data["station"] or "Returns Station",
        "holding_bin": data["holding_bin"],
        "sender_name": data["sender_name"],
        "sender_phone": data["sender_phone"],
        "sender_pincode": data["sender_pincode"],
        "observed_item": data["observed_item"],
        "observed_serial_number": data["observed_serial_number"],
        "identity_notes": data["identity_notes"],
        "exception": 1,
        "received_at": now_datetime(),
        "received_by": frappe.session.user,
        "inventory_status": "Exception",
        "finance_status": "Exception",
        "credit_note_status": "Pending",
        "refund_status": "Pending Verification",
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    payload = _parcel_payload(doc)
    payload["status"] = "identity_pending"
    payload["message"] = (
        "Parcel logged in Identity Pending. No stock, Credit Note or refund was created."
    )
    return payload


@frappe.whitelist()
def return_unidentified_evidence(parcel, label_photo_url, open_photo_url):
    """Attach the mandatory label and opened-contents evidence to an unknown parcel."""
    doc = frappe.get_doc("D2C Return Parcel", parcel)
    if doc.status != "Identity Pending":
        frappe.throw("Evidence can only be added through this path while identity is pending.")
    label_photo_url = (label_photo_url or "").strip()
    open_photo_url = (open_photo_url or "").strip()
    if not label_photo_url or not open_photo_url:
        frappe.throw("Both the label and opened-contents photos are required.")
    doc.label_photo_url = label_photo_url
    doc.open_photo_url = open_photo_url
    doc.evidence_urls = json.dumps({
        "label_photo_url": label_photo_url,
        "open_photo_url": open_photo_url,
    }, sort_keys=True)
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    return {
        "status": "identity_pending", "parcel": doc.name,
        "message": "Evidence saved. Keep the parcel in its holding bin until identified.",
    }


@frappe.whitelist()
def return_start(reverse_awb, order_code=None, return_type="Unknown", station=None,
                 holding_bin=None, additional_parcel=0):
    """Claim a physical reverse parcel for QC; idempotent by reverse AWB."""
    out = _lookup(
        reverse_awb, order_code,
        allow_additional=cint(additional_parcel), claim_identity=True,
    )
    if out.get("status") == "resolved":
        out["status"] = "started"
        return out
    if out.get("status") in ("resume", "already"):
        return out
    if out.get("status") != "ok":
        return out
    if return_type not in ("Customer Return", "RTO", "Unknown"):
        return {"status": "error", "message": "Invalid return type."}

    item_rows, component_rows = _snapshot_rows(out["items"])

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
        "inventory_status": "Pending QC",
        "finance_status": "Pending Inventory",
        "credit_note_status": "Pending",
        "refund_status": "Pending Verification",
        "items": item_rows,
        "components": component_rows,
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
def return_finalize(parcel, item_results=None, component_results=None, evidence=None, return_type=None,
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
    component_results = _as_json(component_results, [])
    component_map = {
        (row.get("parent_item_code"), row.get("item_code")): row
        for row in component_results
        if row.get("parent_item_code") and row.get("item_code")
    }
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

    component_complete = defaultdict(lambda: True)
    for component in (doc.get("components") or []):
        key = (component.parent_item_code, component.item_code)
        raw_component = component_map.get(key)
        if not raw_component:
            frappe.throw("Count packed component {0} for bundle {1}."
                         .format(component.item_code, component.parent_item_code))
        received = flt(raw_component.get("received_qty"))
        if received < 0 or received > flt(component.expected_qty) + 0.001:
            frappe.throw("{0}: component quantity must be between 0 and {1}."
                         .format(component.item_code, component.expected_qty))
        present = cint(raw_component.get("present"))
        component.received_qty = received
        component.present = present
        component.notes = (raw_component.get("notes") or "").strip()
        if not present or abs(received - flt(component.expected_qty)) > 0.001:
            component_complete[component.parent_item_code] = False

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

        finding = row.warehouse_finding
        if condition == "Good" and finding not in ("No fault found", "Packaging damage only"):
            frappe.throw("{0}: a Good item cannot have finding '{1}'."
                         .format(row.item_code, finding or "blank"))
        if condition in ("Repairable", "Scrap", "Damaged", "Used") and finding == "No fault found":
            frappe.throw("{0}: choose the actual defect before marking it {1}."
                         .format(row.item_code, condition))

        bundle_complete = component_complete[row.item_code]
        if not bundle_complete:
            row.disposition = "Investigation / No Receipt"
            row.condition = "Incomplete"
            has_exception = True
            if row.warehouse_finding in (None, "", "No fault found"):
                row.warehouse_finding = "Missing accessories"
            findings.append(row.warehouse_finding)
            continue

        if condition == "Good":
            _validate_good(row, raw)
            row.disposition = "Main Warehouse"
        elif condition == "Repairable":
            if received <= 0:
                frappe.throw("{0}: a repairable item must be physically received."
                             .format(row.item_code))
            row.disposition = "Repair / Refurbishment"
        elif condition == "Scrap":
            if received <= 0:
                frappe.throw("{0}: a scrap item must be physically received."
                             .format(row.item_code))
            row.disposition = "Scrap Hold"
        elif condition in ("Damaged", "Used"):
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
                "condition": condition,
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
        doc.inventory_status = "Exception"
        doc.finance_status = "Exception"
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
    doc.inventory_status = "Pending HQ Approval"
    doc.finance_status = "Pending Inventory"
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    return {
        "status": "pending_review",
        "parcel": doc.name,
        "return_intake": intake.name,
        "exception": cint(has_exception),
        "message": "QC saved. Inventory remains unchanged until HQ approves the Return Intake.",
    }


def _linked_credit_note(sales_invoice):
    rows = frappe.get_all(
        "Sales Invoice",
        filters={"is_return": 1, "return_against": sales_invoice,
                 "docstatus": ["in", [0, 1]]},
        fields=["name", "docstatus", "status", "grand_total", "posting_date"],
        order_by="docstatus desc, creation desc",
        limit_page_length=1,
    )
    return rows[0] if rows else None


def _refresh_finance_status(doc):
    """Derive the accounting close stage without ever creating or submitting it."""
    update = {}
    cn = _linked_credit_note(doc.sales_invoice) if doc.sales_invoice else None
    if cn:
        update["credit_note"] = cn.name
        update["credit_note_status"] = "Submitted" if cn.docstatus == 1 else "Draft"
    else:
        update["credit_note"] = None
        update["credit_note_status"] = "Pending"

    if doc.inventory_status != "Posted":
        finance_status = "Exception" if doc.inventory_status in ("Exception", "Rejected") else "Pending Inventory"
    elif not cn or cn.docstatus != 1:
        finance_status = "Pending Credit Note"
    elif doc.refund_status in ("Not Required", "Reconciled"):
        finance_status = "Closed"
    else:
        finance_status = "Pending Refund"
    update["finance_status"] = finance_status
    update["closed_at"] = now_datetime() if finance_status == "Closed" else None

    current = {key: doc.get(key) for key in update}
    if any(current[key] != value for key, value in update.items()):
        frappe.db.set_value("D2C Return Parcel", doc.name, update)
        for key, value in update.items():
            doc.set(key, value)
    return cn


@frappe.whitelist()
def return_finance_queue(finance_status=None, limit=250):
    """Read-only Finance/HQ queue spanning inventory, CN and refund completion."""
    limit = min(max(cint(limit) or 250, 1), 1000)
    filters = {}
    if finance_status:
        filters["finance_status"] = finance_status
    rows = frappe.get_all(
        "D2C Return Parcel",
        filters=filters,
        fields=[
            "name", "status", "reverse_awb", "shopify_order_number", "return_type",
            "sales_invoice", "delivery_note", "return_intake", "inventory_status",
            "credit_note", "credit_note_status", "refund_status", "finance_status",
            "customer_reason", "warehouse_finding", "claim_required", "exception",
            "received_at", "completed_at", "closed_at",
        ],
        order_by="creation desc",
        limit_page_length=limit,
    )
    output = []
    for row in rows:
        doc = frappe.get_doc("D2C Return Parcel", row.name)
        cn = _refresh_finance_status(doc)
        payload = _parcel_payload(doc)
        payload.update({
            "inventory_status": doc.inventory_status,
            "credit_note": doc.credit_note,
            "credit_note_status": doc.credit_note_status,
            "refund_status": doc.refund_status,
            "finance_status": doc.finance_status,
            "finance_notes": doc.finance_notes,
            "closed_at": doc.closed_at,
            "credit_note_amount": (cn.grand_total if cn else None),
        })
        output.append(payload)
    return {"rows": output, "count": len(output)}


@frappe.whitelist()
def return_mark_refund(parcel, refund_status, finance_notes=None):
    """Record Finance's refund verification; CN submission remains a separate review."""
    allowed_roles = {"System Manager", "Accounts Manager", "HQ Returns Reviewer"}
    if not allowed_roles.intersection(set(frappe.get_roles())):
        frappe.throw("Only Finance or an HQ Returns Reviewer can update refund status.",
                     frappe.PermissionError)
    if refund_status not in ("Not Required", "Pending Verification", "Refunded", "Reconciled"):
        frappe.throw("Choose a valid refund status.")
    doc = frappe.get_doc("D2C Return Parcel", parcel)
    doc.refund_status = refund_status
    if finance_notes is not None:
        doc.finance_notes = (finance_notes or "").strip()
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    _refresh_finance_status(doc)
    return {
        "status": "ok", "parcel": doc.name, "refund_status": doc.refund_status,
        "finance_status": doc.finance_status,
    }


@frappe.whitelist()
def return_exception_summary():
    """Daily control snapshot, including open duplicate-order warnings."""
    rows = frappe.get_all(
        "D2C Return Parcel",
        fields=["name", "status", "shopify_order_number", "reverse_awb", "return_intake",
                "inventory_status", "finance_status", "exception", "claim_required",
                "received_at", "completed_at"],
        order_by="creation desc",
        limit_page_length=1000,
    )
    by_order = defaultdict(list)
    counts = defaultdict(int)
    for row in rows:
        counts[row.status] += 1
        if row.shopify_order_number and row.status in ("QC In Progress", "Pending HQ Review"):
            by_order[row.shopify_order_number].append(row)
    duplicates = [
        {"order": order, "parcels": group}
        for order, group in sorted(by_order.items()) if len(group) > 1
    ]
    return {
        "total_parcels": len(rows),
        "unique_orders": len({row.shopify_order_number for row in rows if row.shopify_order_number}),
        "status_counts": dict(counts),
        "duplicate_open_orders": duplicates,
        "qc_in_progress": [row for row in rows if row.status == "QC In Progress"],
        "pending_hq_review": [row for row in rows if row.status == "Pending HQ Review"],
        "claims": [row for row in rows if cint(row.claim_required)],
        "identity_pending": [row for row in rows if row.status == "Identity Pending"],
        "exceptions": [row for row in rows if cint(row.exception) or row.finance_status == "Exception"],
    }
