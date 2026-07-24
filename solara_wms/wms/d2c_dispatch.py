# Copyright (c) 2026, SOLARA and contributors
# For license information, please see license.txt
"""D2C dispatch-scan — scan each parcel's barcode at courier handover so a
duplicate can never be shipped (Amazon-DF-style dispatch confirmation).

The floor scans either barcode on the label:
  - the courier AWB (SF…/2904…/WB…), or
  - the Order-ID barcode (SOL12345-P1 → order + parcel index).

Each scan is recorded once (D2C Dispatch Scan, AWB unique = the DB-level
duplicate block). When every parcel of an order is scanned, the DN is stamped
custom_dispatched. Re-scanning a parcel returns a hard "already dispatched"
so it is never shipped twice.
"""
import json
import re
import time
import urllib.parse
import urllib.request

import frappe
from frappe.utils import cint, now_datetime, nowdate, add_days

from solara_wms.wms.d2c_fulfillment import _awb_courier_pairs


def _find_dn_by_awb(awb):
    for field in ("awb_number", "custom_awb_2"):
        rows = frappe.get_all("Delivery Note", filters={field: awb, "docstatus": 1},
                              fields=["name"], limit_page_length=1)
        if rows:
            return rows[0].name
    # N-box orders keep parcels 3+ only in custom_awb_list (JSON) — scan the
    # recent multibox DNs (bounded set) and match the AWB inside it.
    for d in frappe.get_all(
            "Delivery Note",
            filters={"custom_d2c_defer_si": 1, "docstatus": 1,
                     "custom_box_count": [">", 2],
                     "posting_date": [">=", add_days(nowdate(), -4)]},
            fields=["name", "custom_awb_list"], limit_page_length=0):
        if d.custom_awb_list and awb in d.custom_awb_list:
            return d.name
    return None


def _resolve(code):
    """code -> (dn_name, awb, box_index, box_count). Accepts an AWB or a
    SOL#####-P<n> order-id barcode or a bare SOL##### (single-parcel only)."""
    code = code.strip()
    up = code.upper()

    m = re.match(r"^(SOL\d+)[-_ ]?P(\d+)$", up)
    if m:
        ordno, pidx = m.group(1), int(m.group(2))
        rows = frappe.get_all("Delivery Note",
                              filters={"shopify_order_number": ordno, "docstatus": 1},
                              fields=["name"], limit_page_length=1)
        if not rows:
            return None, None, None, None
        dn = frappe.get_doc("Delivery Note", rows[0].name)
        pairs = _awb_courier_pairs(dn)
        awb = pairs[pidx - 1][0] if 0 < pidx <= len(pairs) else None
        return dn.name, awb, pidx, (cint(dn.get("custom_box_count")) or len(pairs) or 1)

    if re.match(r"^SOL\d+$", up):
        rows = frappe.get_all("Delivery Note",
                              filters={"shopify_order_number": up, "docstatus": 1},
                              fields=["name"], limit_page_length=1)
        if not rows:
            return None, None, None, None
        dn = frappe.get_doc("Delivery Note", rows[0].name)
        pairs = _awb_courier_pairs(dn)
        if len(pairs) > 1:
            # ambiguous — force an AWB/parcel scan so we don't dispatch a box short
            return dn.name, None, None, len(pairs)
        return dn.name, (pairs[0][0] if pairs else None), 1, 1

    # otherwise treat the code as the AWB itself
    dn_name = _find_dn_by_awb(code)
    if not dn_name:
        return None, None, None, None
    dn = frappe.get_doc("Delivery Note", dn_name)
    pairs = _awb_courier_pairs(dn)
    idx = next((i + 1 for i, (a, _) in enumerate(pairs) if a == code), None)
    return dn_name, code, idx, (cint(dn.get("custom_box_count")) or len(pairs) or 1)


@frappe.whitelist()
def scan_dispatch(code):
    """Record one parcel's dispatch scan. Returns a status the scan UI colours:
    ok (green) / duplicate (red) / not_found (amber) / need_parcel (amber)."""
    code = (code or "").strip()
    if not code:
        return {"status": "error", "message": "Empty scan"}

    dn_name, awb, box_index, box_count = _resolve(code)
    if not dn_name:
        return {"status": "not_found", "message": "No order found for: " + code}
    if not awb:
        return {"status": "need_parcel",
                "message": "Multi-box order — scan each parcel's AWB barcode (not the order barcode)."}

    dup = frappe.get_all("D2C Dispatch Scan", filters={"awb": awb},
                         fields=["scanned_at", "scanned_by", "shopify_order_number"],
                         limit_page_length=1)
    if dup:
        e = dup[0]
        return {"status": "duplicate", "awb": awb, "order": e.shopify_order_number,
                "message": "ALREADY DISPATCHED " + str(e.scanned_at)[:16] +
                           " by " + (e.scanned_by or "?") + " — DO NOT SHIP"}

    dn = frappe.get_doc("Delivery Note", dn_name)
    now = now_datetime()
    scan = frappe.get_doc({
        "doctype": "D2C Dispatch Scan", "awb": awb, "delivery_note": dn_name,
        "shopify_order_number": dn.get("shopify_order_number"),
        "courier": dn.get("courier_partner"), "box_index": box_index,
        "box_count": box_count, "scanned_at": now, "scanned_by": frappe.session.user,
    })
    scan.flags.ignore_permissions = True
    try:
        scan.insert(ignore_permissions=True)
    except frappe.exceptions.DuplicateEntryError:
        return {"status": "duplicate", "awb": awb, "order": dn.get("shopify_order_number"),
                "message": "ALREADY DISPATCHED (scanned a moment ago) — DO NOT SHIP"}

    scanned = frappe.db.count("D2C Dispatch Scan", {"delivery_note": dn_name})
    fully = scanned >= (box_count or 1)
    if fully and dn.meta.has_field("custom_dispatched") and not cint(dn.get("custom_dispatched")):
        frappe.db.set_value("Delivery Note", dn_name,
                            {"custom_dispatched": 1, "custom_dispatched_at": now,
                             "custom_dispatched_by": frappe.session.user})
    frappe.db.commit()

    today = frappe.db.count("D2C Dispatch Scan", {"scanned_at": [">=", nowdate() + " 00:00:00"]})
    return {"status": "ok", "awb": awb, "order": dn.get("shopify_order_number"),
            "courier": dn.get("courier_partner"), "box": box_index or scanned,
            "boxes": box_count or 1, "fully": fully, "today": today,
            "message": "DISPATCHED — box " + str(box_index or scanned) + " of " + str(box_count or 1) +
                       ("  (order complete)" if fully else "  (waiting for other boxes)")}


@frappe.whitelist()
def dispatch_summary():
    """Today's scan tally for the scan page header."""
    today0 = nowdate() + " 00:00:00"
    scans = frappe.db.count("D2C Dispatch Scan", {"scanned_at": [">=", today0]})
    orders = len(frappe.get_all("D2C Dispatch Scan",
                                filters={"scanned_at": [">=", today0]},
                                fields=["delivery_note"], distinct=True, limit_page_length=0))
    return {"scans_today": scans, "orders_today": orders}


# ─── AUTO DISPATCH STAMP from courier tracking ───────────────────────────
# Makes custom_dispatched a reliable "left our warehouse" flag WITHOUT needing the
# manual scan: poll ClickPost, and the first courier scan (movement past manifest)
# stamps the DN — the same field the manual scanner writes. ElasticRun has no
# ClickPost tracking, so those stay un-stamped and rely on the manual scan.

_CP_TRACK_KEY = "d3464616-bbd6-4874-919a-a7e8bd14d66f"   # = D2C Fulfillment Settings.clickpost_api_key
_CP_ID = {"delhivery": 4, "bluedart": 5, "shadowfax": 9, "elasticrun": 1}
_TRACK_BATCH = 15   # ClickPost track-order rejects >15 waybills/call (HTTP 400)
# statuses that mean the parcel has NOT yet left the warehouse (still at manifest)
_NOT_MOVED = ("order placed", "pickup pending", "manifest", "not picked",
              "awaiting pickup", "label generated", "pickup failed")


def _cp_track(awbs, cp_id):
    """One ClickPost track-order call for up to _TRACK_BATCH same-courier AWBs.
    Returns {awb: latest_status_text (lowercased)}. Failures/missing -> ''."""
    try:
        qs = urllib.parse.urlencode({"username": "solara", "key": _CP_TRACK_KEY,
                                     "waybill": ",".join(awbs), "cp_id": cp_id})
        req = urllib.request.Request("https://api.clickpost.in/api/v2/track-order/?" + qs,
                                     headers={"User-Agent": "solara-dispatch-stamp/1.0"})
        with urllib.request.urlopen(req, timeout=45) as r:
            res = (json.load(r).get("result") or {})
        out = {}
        for a in awbs:
            ls = (res.get(a) or {}).get("latest_status") or {}
            out[a] = (ls.get("clickpost_status_description") or ls.get("status") or "").lower()
        return out
    except Exception:
        return {a: "" for a in awbs}


def _tracking_says_dispatched(status):
    """True only if courier tracking shows movement PAST manifest (a first scan):
    delivered / in-transit / out-for-delivery / RTO / NDR. Empty ('' = NO_DATA,
    e.g. ElasticRun) and manifest/pickup-pending are NOT dispatched."""
    s = (status or "").strip().lower()
    if not s or "cancel" in s:
        return False
    if any(k in s for k in _NOT_MOVED):
        return False
    return True


@frappe.whitelist()
def stamp_dispatched(days=14):
    """Scheduled (gated by D2C Fulfillment Settings.dispatch_stamp_enabled, default
    OFF): poll ClickPost for open D2C DNs (submitted, defer, AWB set,
    custom_dispatched=0, posting_date within `days`) and stamp custom_dispatched=1
    + custom_dispatched_at on any whose courier tracking shows a first scan.
    Idempotent, best-effort, per-courier batched; never raises into the scheduler."""
    try:
        settings = frappe.get_single("D2C Fulfillment Settings")
        if not cint(settings.get("dispatch_stamp_enabled")):
            return {"skipped": "dispatch_stamp_enabled off"}
        start = add_days(nowdate(), -cint(days))
        dns = frappe.get_all(
            "Delivery Note",
            filters={"docstatus": 1, "custom_d2c_defer_si": 1, "custom_dispatched": 0,
                     "awb_number": ["is", "set"], "posting_date": [">=", start]},
            fields=["name", "awb_number", "courier_partner"], limit_page_length=0)
        by_courier = {}
        for d in dns:
            by_courier.setdefault((d.get("courier_partner") or "").lower(), []).append(d)
        now = now_datetime()
        stamped = 0
        for courier, rows in by_courier.items():
            cp_id = _CP_ID.get(courier, 4)
            for i in range(0, len(rows), _TRACK_BATCH):
                chunk = rows[i:i + _TRACK_BATCH]
                status = _cp_track([r["awb_number"] for r in chunk], cp_id)
                for r in chunk:
                    if _tracking_says_dispatched(status.get(r["awb_number"], "")):
                        frappe.db.set_value(
                            "Delivery Note", r["name"],
                            {"custom_dispatched": 1, "custom_dispatched_at": now,
                             "custom_dispatched_by": "clickpost-track"},
                            update_modified=False)
                        stamped += 1
                frappe.db.commit()
                time.sleep(0.3)
        return {"checked": len(dns), "stamped": stamped}
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "D2C Dispatch Stamp failed")
        return {"error": True}
