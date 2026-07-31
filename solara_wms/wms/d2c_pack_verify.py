# Copyright (c) 2026, SOLARA and contributors
# For license information, please see license.txt
"""D2C pack-verify — scan the parcel at the pack bench, show the packer exactly
what goes in the box, make them confirm the piece count, and keep a photo of the
open box as evidence.

Why this exists: missing/wrong items is the #1 CS driver (Judge.me 1★ share
39%→63%, dominant theme "items missing"; combos ≈35% of sales). The paper QC
sheet (SOP-PACK-QC) deters but cannot block — a distracted packer can still seal
a short box.

Why a PHOTO rather than scanning every item's barcode: ~122 D2C-shipped SKUs
carry no EAN, and several combo components are nested inside the parent's carton
at goods-inward (AFO combo's oil sprayer + baskets) so they physically cannot be
scanned at the bench. A photo is evidence for EVERY order regardless of barcode
coverage, and it settles a CS dispute outright.

Contract mirrors d2c_dispatch: the floor never holds an Atlas credential — the
dashboard proxies these with its own token (see app/routes/warehouse.py).
"""
import json

import frappe
from frappe.utils import cint, flt, now_datetime

from solara_wms.wms.d2c_dispatch import _resolve
from solara_wms.wms.d2c_fulfillment import _awb_courier_pairs, _enrich_physical_lines


def _log(title, msg):
    try:
        frappe.log_error(message=msg, title=title)
    except Exception:
        pass


def _pieces_for_dn(dn_name):
    """The physical pieces that must go in the box, bundle-exploded — the same
    data Section B of the pick list prints, so screen and paper can never
    disagree. Returns (lines, service_lines)."""
    dn = frappe.get_doc("Delivery Note", dn_name)
    shim = {
        "name": dn.name,
        "items": dn.items,
        "shopify_order_number": dn.get("shopify_order_number"),
    }
    _enrich_physical_lines([shim])
    lines = [{
        "item_code": l["item_code"],
        "item_name": l["item_name"],
        "qty": flt(l["qty"]),
        "bundle": l.get("bundle"),
    } for l in shim.get("_lines") or []]
    service = [{"item_code": s.item_code, "item_name": s.item_name, "qty": flt(s.qty)}
               for s in shim.get("_service") or []]
    return lines, service


@frappe.whitelist()
def pack_verify_get(code):
    """Scan at the bench -> what to pack. Returns:
      status: ok | not_found | already | error
      order, dn, courier, box_index, box_count, awbs[]
      pieces[]  (item_code, item_name, qty, bundle)  -> the tick list
      services[] (warranty etc — nothing physical to pack)
      total_pieces
    Read-only: this call records nothing."""
    code = (code or "").strip()
    if not code:
        return {"status": "error", "message": "Empty scan"}

    dn_name, awb, box_index, box_count = _resolve(code)
    if not dn_name:
        return {"status": "not_found", "message": "No order found for: " + code}

    dn = frappe.get_doc("Delivery Note", dn_name)
    prior = frappe.get_all("D2C Pack Verify", filters={"delivery_note": dn_name},
                           fields=["name", "verified_at", "verified_by", "pieces_expected"],
                           limit_page_length=1)
    lines, service = _pieces_for_dn(dn_name)
    total = sum(l["qty"] for l in lines)
    pairs = _awb_courier_pairs(dn)
    out = {
        "status": "already" if prior else "ok",
        "dn": dn_name,
        "order": dn.get("shopify_order_number") or dn.get("shopify_order_id"),
        "courier": dn.get("courier_partner"),
        "box_index": box_index,
        "box_count": cint(dn.get("custom_box_count")) or len(pairs) or 1,
        "awbs": [a for a, _c in pairs],
        "pieces": lines,
        "services": service,
        "total_pieces": total,
        "printed_batch": dn.get("custom_prepare_batch"),
    }
    if prior:
        p = prior[0]
        out["message"] = ("ALREADY PACK-VERIFIED " + str(p.verified_at)[:16]
                          + " by " + (p.verified_by or "?"))
    return out


@frappe.whitelist()
def pack_verify_submit(code, pieces_confirmed=None, station=None,
                       photo_url=None, notes=None, duration_sec=None):
    """Record the pack verification. `pieces_confirmed` is what the packer
    actually counted into the box; a mismatch against the expected count is
    stored and flagged rather than silently accepted — the point is to catch the
    short-pack, so a mismatch must still be recorded, not thrown away.

    ADVISORY during the pilot: this never blocks a shipment. It creates the
    audit record and stamps the DN. Escalate to a hard gate only once adoption
    is proven (dispatch-scan died at 2 scans / 9,988 DNs because the hardware
    never reached the floor)."""
    code = (code or "").strip()
    if not code:
        return {"status": "error", "message": "Empty scan"}

    dn_name, awb, box_index, box_count = _resolve(code)
    if not dn_name:
        return {"status": "not_found", "message": "No order found for: " + code}

    dn = frappe.get_doc("Delivery Note", dn_name)
    lines, _service = _pieces_for_dn(dn_name)
    expected = sum(l["qty"] for l in lines)
    confirmed = flt(pieces_confirmed) if pieces_confirmed is not None else expected
    mismatch = 1 if abs(confirmed - expected) > 0.001 else 0

    existing = frappe.get_all("D2C Pack Verify", filters={"delivery_note": dn_name},
                              fields=["name"], limit_page_length=1)
    if existing:
        return {"status": "already", "dn": dn_name,
                "order": dn.get("shopify_order_number"),
                "message": "Already pack-verified — no second record created."}

    try:
        doc = frappe.get_doc({
            "doctype": "D2C Pack Verify",
            "delivery_note": dn_name,
            "shopify_order_number": dn.get("shopify_order_number"),
            "awb": awb,
            "courier": dn.get("courier_partner"),
            "box_count": cint(dn.get("custom_box_count")) or 1,
            "prepare_batch": dn.get("custom_prepare_batch"),
            "pieces_expected": expected,
            "pieces_confirmed": confirmed,
            "mismatch": mismatch,
            "contents": json.dumps(lines),
            "photo_url": photo_url,
            "station": station,
            "notes": notes,
            "duration_sec": cint(duration_sec),
            "verified_at": now_datetime(),
            "verified_by": frappe.session.user,
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        if frappe.get_meta("Delivery Note").has_field("custom_pack_verified"):
            frappe.db.set_value("Delivery Note", dn_name,
                                {"custom_pack_verified": 1,
                                 "custom_pack_verified_at": now_datetime()},
                                update_modified=False)
        frappe.db.commit()
    except Exception as e:
        frappe.db.rollback()
        _log("D2C Pack Verify", "submit {0}: {1}".format(dn_name, frappe.get_traceback()))
        return {"status": "error", "message": str(e)[:160]}

    return {
        "status": "mismatch" if mismatch else "ok",
        "dn": dn_name,
        "order": dn.get("shopify_order_number"),
        "record": doc.name,
        "pieces_expected": expected,
        "pieces_confirmed": confirmed,
        "message": ("COUNT MISMATCH — expected {0:g}, packer counted {1:g}. "
                    "Recorded for review.".format(expected, confirmed)) if mismatch
                   else "Pack verified — {0:g} piece(s).".format(expected),
    }


@frappe.whitelist()
def pack_verify_summary(on_date=None):
    """Today's tally for the pilot scoreboard: verifications, mismatches caught,
    median duration, split by station (= packing line)."""
    from frappe.utils import getdate, nowdate
    d = getdate(on_date) if on_date else getdate(nowdate())
    rows = frappe.get_all("D2C Pack Verify",
                          filters={"verified_at": ["between", [str(d) + " 00:00:00",
                                                               str(d) + " 23:59:59"]]},
                          fields=["station", "mismatch", "duration_sec"],
                          limit_page_length=0)
    by_station = {}
    for r in rows:
        s = by_station.setdefault(r.station or "—", {"verified": 0, "mismatches": 0})
        s["verified"] += 1
        s["mismatches"] += cint(r.mismatch)
    durs = sorted(cint(r.duration_sec) for r in rows if cint(r.duration_sec))
    return {
        "date": str(d),
        "verified": len(rows),
        "mismatches": sum(cint(r.mismatch) for r in rows),
        "median_sec": durs[len(durs) // 2] if durs else None,
        "by_station": by_station,
    }
