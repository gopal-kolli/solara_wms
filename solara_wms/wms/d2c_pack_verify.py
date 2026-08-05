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
from frappe.utils import cint, flt, getdate, now_datetime, nowdate

from solara_wms.wms.d2c_dispatch import (
    _CP_ID,
    _cp_track,
    _resolve,
    _tracking_for_dns,
    _tracking_says_dispatched,
)
from solara_wms.wms.d2c_fulfillment import _awb_courier_pairs, _enrich_physical_lines


def _log(title, msg):
    try:
        frappe.log_error(message=msg, title=title)
    except Exception:
        pass


def _dispatch_hold(dn, awb):
    """Return a hard pack hold once the parcel has left the warehouse.

    ``custom_dispatched`` is stamped by both the security handover flow and the
    ClickPost tracking audit. The parcel-level scan lookup also covers a
    partially handed-over multi-box order before the DN-level flag is set.
    """
    scans = []
    if awb:
        scans = frappe.get_all(
            "D2C Dispatch Scan",
            filters={"awb": awb},
            fields=["scanned_at", "scanned_by"],
            limit_page_length=1,
        )
    live_status = ""
    if not cint(dn.get("custom_dispatched")) and not scans:
        # Historical DNs can predate PackVerify and have no warehouse dispatch
        # scan. For an older ClickPost-tracked label, ask the courier directly.
        # Today/tomorrow's normal packing work does not incur this network call,
        # and ClickPost failure returns no status so an outage cannot stop lines.
        posting_date = dn.get("posting_date")
        courier = next(
            (value for parcel_awb, value in _awb_courier_pairs(dn)
             if (parcel_awb or "").strip() == awb),
            None,
        ) or dn.get("courier_partner")
        courier_key = (courier or "").strip().lower()
        if (posting_date and courier_key and courier_key != "elasticrun"
                and getdate(posting_date) < getdate(nowdate())):
            live_status = _cp_track(
                [awb], _CP_ID.get(courier_key, 4)
            ).get(awb, "")
        if not _tracking_says_dispatched(live_status):
            return None

    when = None
    who = None
    if scans:
        when = scans[0].get("scanned_at")
        who = scans[0].get("scanned_by")
    if not when:
        when = dn.get("custom_dispatched_at")
    if not who:
        who = dn.get("custom_dispatched_by")
    if live_status:
        who = "ClickPost live check"

    detail = ""
    if when:
        detail += " " + str(when)[:16]
    if who:
        detail += " by " + str(who)
    if live_status:
        detail += " (" + live_status.split("|", 1)[-1] + ")"
    return {
        "status": "error",
        "dn": getattr(dn, "name", None),
        "awb": awb,
        "order": dn.get("shopify_order_number") or dn.get("shopify_order_id"),
        "message": "ALREADY DISPATCHED{0} — DO NOT PACK OR APPLY ANOTHER LABEL."
                   .format(detail),
    }


def _dispatched_sibling_hold(dn, awb):
    """Return a hard pack hold if a DIFFERENT Delivery Note for the SAME Shopify
    order has already dispatched (2026-07-26 incident: duplicate/orphan Sales
    Orders sharing one shopify_order_id where one already shipped + was re-released,
    minting a genuinely new AWB that per-AWB checks cannot catch).

    Exempt: ``is_replacement`` DNs (OPD captain-approved replacement/RTO reship)
    are EXPECTED to ship after an earlier DN for the same order.
    """
    if cint(dn.get("is_replacement")):
        return None

    order_id = str(dn.get("shopify_order_id") or "").strip()
    order_number = str(dn.get("shopify_order_number") or "").strip().upper()
    if not order_id and not order_number:
        return None

    or_filters = {}
    if order_id:
        or_filters["shopify_order_id"] = order_id
    if order_number:
        or_filters["shopify_order_number"] = order_number

    dn_name = getattr(dn, "name", None) or dn.get("name")
    fields = ["name", "custom_dispatched", "custom_dispatched_at",
              "custom_dispatched_by", "posting_date", "courier_partner"] + [
        f for f in ("awb_number", "custom_awb_2", "custom_awb_list")
        if frappe.get_meta("Delivery Note").has_field(f)
    ]
    siblings = [
        s for s in frappe.get_all(
            "Delivery Note", filters={"docstatus": 1}, or_filters=or_filters,
            fields=fields, limit_page_length=20,
        ) if s.name != dn_name
    ]
    if not siblings:
        return None

    flagged = next((s for s in siblings if cint(s.get("custom_dispatched"))), None)
    scan = None
    if not flagged:
        rows = frappe.get_all(
            "D2C Dispatch Scan",
            filters={"delivery_note": ["in", [s.name for s in siblings]]},
            fields=["delivery_note", "scanned_at", "scanned_by"],
            order_by="scanned_at asc", limit_page_length=1,
        )
        scan = rows[0] if rows else None

    # For old un-flagged, un-scanned siblings: check live ClickPost tracking
    # (reuse existing vetted bucket logic from d2c_dispatch).
    live_status, live_sib = "", None
    if not flagged and not scan:
        old = [s for s in siblings if s.get("posting_date")
               and getdate(s["posting_date"]) < getdate(nowdate())]
        if old:
            tracking = _tracking_for_dns(old)
            for s in old:
                for sib_awb, status in tracking.get(s.name, []):
                    courier = next((c for a, c in _awb_courier_pairs(frappe.get_doc("Delivery Note", s.name))
                                    if a == sib_awb), s.get("courier_partner"))
                    if (courier or "").strip().lower() == "elasticrun":
                        continue
                    if _tracking_says_dispatched(status):
                        live_status, live_sib = status, s.name
                        break
                if live_status:
                    break

    if not (flagged or scan or live_status):
        return None

    sib_name = (flagged.name if flagged else scan.delivery_note if scan else live_sib)
    when = (flagged.get("custom_dispatched_at") if flagged
            else scan.scanned_at if scan else None)
    who = (flagged.get("custom_dispatched_by") if flagged
           else scan.scanned_by if scan else "ClickPost live check")
    detail = ""
    if when:
        detail += " " + str(when)[:16]
    if who:
        detail += " by " + str(who)
    if live_status:
        detail += " (" + live_status.split("|", 1)[-1] + ")"

    return {
        "status": "error",
        "dn": dn_name,
        "awb": awb,
        "order": dn.get("shopify_order_number") or dn.get("shopify_order_id"),
        "message": ("ALREADY DISPATCHED under {0}{1} — this Shopify order "
                    "already shipped on a different Delivery Note. DO NOT PACK "
                    "OR APPLY ANOTHER LABEL.").format(sib_name, detail),
    }


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


def _pieces_for_parcel(dn, lines, box_index, box_count, service_lines=None):
    """Return only the physical pieces assigned to the scanned parcel.

    The parcel plan is the same source used to create the labels/AWBs. A
    multi-box verification must never fall back to the whole Delivery Note:
    doing so either asks the packer to duplicate products across boxes or
    records a false short-pack. Incomplete plans are therefore a hard stop.
    """
    if cint(box_count) <= 1:
        return lines, None
    if not box_index:
        return [], "Multi-box order — scan the AWB on the parcel in front of you."
    try:
        plan = json.loads(dn.get("custom_parcel_plan") or "[]")
    except (TypeError, ValueError):
        plan = []
    if not plan or cint(box_index) > len(plan):
        return [], ("Box {0} of {1} has no usable parcel plan. Do not seal it; "
                    "set it aside for the line lead.".format(box_index, box_count))

    wanted = {}
    planned_qty = {}
    for idx, parcel in enumerate(plan, 1):
        for item in parcel.get("items") or []:
            code = item.get("item_code")
            if not code:
                continue
            planned_qty[code] = planned_qty.get(code, 0) + flt(item.get("qty"))
            if idx == cint(box_index):
                wanted[code] = wanted.get(code, 0) + flt(item.get("qty"))

    # A physical DN line absent from every parcel plan is a release-data defect,
    # not something the packer should guess a box for. Parcel plans can name a
    # Product Bundle parent while `lines` contains its exploded physical
    # components, so either reference legitimately covers the physical line.
    unplanned = sorted({row["item_code"] for row in lines
                        if row["item_code"] not in planned_qty
                        and row.get("bundle") not in planned_qty})
    if unplanned:
        return [], ("Parcel plan is missing: {0}. Do not seal; tell the line lead."
                    .format(", ".join(unplanned)))

    remaining = dict(wanted)
    bundle_codes = {row.get("bundle") for row in lines if row.get("bundle")}
    resolved_bundles = set()
    kept = []
    for line in lines:
        code = line["item_code"]
        need = remaining.get(code, 0)
        bundle = line.get("bundle")
        qty = 0
        if need > 0:
            # Explicit component allocation takes precedence when a plan names
            # the physical SKU directly.
            qty = min(flt(line.get("qty")), need)
            remaining[code] = need - qty
        elif bundle and wanted.get(bundle, 0) > 0:
            # The physical line quantity represents the full DN. Allocate the
            # same fraction as this parcel's bundle-parent quantity, which also
            # supports multiple units of one combo split across boxes.
            total_bundle_qty = planned_qty.get(bundle, 0)
            if total_bundle_qty > 0:
                qty = flt(line.get("qty")) * wanted[bundle] / total_bundle_qty
                resolved_bundles.add(bundle)
        if qty > 0:
            row = dict(line)
            row["qty"] = qty
            kept.append(row)
    # Parcel plans deliberately repeat order-level service instructions (for
    # example SOL-INS-PERSONALISATION) on every label.  They are useful to the
    # operator, but _pieces_for_dn() correctly excludes them from the physical
    # checklist.  Do not turn that representation difference into a false
    # missing-item hold; only unresolved *physical* plan rows belong here.
    service_codes = {row.get("item_code") for row in (service_lines or [])
                     if row.get("item_code")}
    missing = sorted(code for code, qty in remaining.items()
                     if qty > 0.001
                     and code not in service_codes
                     and not (code in bundle_codes and code in resolved_bundles))
    if missing:
        return [], ("Parcel plan references unavailable contents: {0}. Do not seal; "
                    "tell the line lead.".format(", ".join(missing)))
    if not kept:
        return [], ("No physical contents were resolved for Box {0}. Do not seal; "
                    "tell the line lead.".format(box_index))
    return kept, None


def _sku_images(codes):
    """SKU -> a picture the packer can recognise at a glance.

    Shubham, 2026-07-31: "it should have image of what to pack" — a photo is read
    far faster than SOL-AF-SIL-BASKET-P6, and mis-picks between similar variants
    (blue vs black tumbler) are exactly what the SKU code fails to prevent.

    Atlas Item.image is EMPTY for all 737 stock items today, so the pictures come
    from Shopify. They are cached on Item.image the first time a SKU is seen, so
    the bench never waits on a Shopify round-trip twice.
    """
    if not codes:
        return {}
    out = {}
    missing = []
    for r in frappe.get_all("Item", filters={"name": ["in", list(codes)]},
                            fields=["name", "image"], limit_page_length=0):
        if r.image:
            out[r.name] = r.image
        else:
            missing.append(r.name)
    if not missing:
        return out
    try:
        shop = (frappe.db.get_single_value("Shopify Setting", "shopify_url") or "").strip()
        token = frappe.utils.password.get_decrypted_password(
            "Shopify Setting", "Shopify Setting", "password", raise_exception=False)
        if not (shop and token):
            return out
        import requests
        base = "https://{0}/admin/api/2024-01".format(shop.replace("https://", "").rstrip("/"))
        # one page-walk is enough: the catalogue is a few hundred SKUs
        page, url = None, base + "/products.json"
        for _ in range(6):
            params = {"limit": 250, "fields": "id,image,images,variants"}
            if page:
                params["page_info"] = page
            resp = requests.get(url, headers={"X-Shopify-Access-Token": token},
                                params=params, timeout=20)
            if resp.status_code != 200:
                break
            for p in resp.json().get("products") or []:
                imgs = {i.get("id"): i.get("src") for i in (p.get("images") or [])}
                main = (p.get("image") or {}).get("src")
                for v in p.get("variants") or []:
                    sku = v.get("sku")
                    if sku and sku in missing and sku not in out:
                        src = imgs.get(v.get("image_id")) or main
                        if src:
                            out[sku] = src
                            # cache so the next scan is instant and Shopify-free
                            try:
                                frappe.db.set_value("Item", sku, "image", src,
                                                    update_modified=False)
                            except Exception:
                                pass
            link = resp.headers.get("Link") or ""
            if 'rel="next"' not in link:
                break
            page = link.split("page_info=")[1].split(">")[0].split("&")[0]
        frappe.db.commit()
    except Exception:
        _log("D2C Pack Verify", "image lookup failed (non-fatal)\n" + frappe.get_traceback())
    return out


@frappe.whitelist()
def pack_verify_get(code, station=None):
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
    from solara_wms.wms.shopify_cancellations import (
        delivery_note_cancellation_hold,
        hold_response,
    )
    if delivery_note_cancellation_hold(dn):
        response = hold_response(dn)
        response["awb"] = awb
        return response
    if not awb:
        return {"status": "need_parcel",
                "message": "Multi-box order — scan each parcel's AWB barcode."}

    dispatched = _dispatch_hold(dn, awb)
    if dispatched:
        return dispatched
    sibling_hold = _dispatched_sibling_hold(dn, awb)
    if sibling_hold:
        return sibling_hold

    prior = frappe.get_all("D2C Pack Verify", filters={"awb": awb},
                           fields=["name", "verified_at", "verified_by", "pieces_expected"],
                           limit_page_length=1)
    lines, service = _pieces_for_dn(dn_name)
    lines, parcel_error = _pieces_for_parcel(
        dn, lines, box_index, box_count, service_lines=service)
    if parcel_error:
        return {"status": "error", "message": parcel_error, "dn": dn_name,
                "order": dn.get("shopify_order_number"), "awb": awb,
                "box_index": box_index, "box_count": box_count}
    imgs = _sku_images({l["item_code"] for l in lines})
    for l in lines:
        l["image"] = imgs.get(l["item_code"])
    total = sum(l["qty"] for l in lines)
    pairs = _awb_courier_pairs(dn)
    out = {
        "status": "already" if prior else "ok",
        "dn": dn_name,
        "awb": awb,
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
    from solara_wms.wms.pack_handoff import pack_handoff_status
    out.update(pack_handoff_status(dn, awb, lines))
    if not prior:
        try:
            from solara_wms.wms.d2c_pack_qc import qc_state
            out.update(qc_state(awb, lines, station=(station or "").strip()))
        except Exception:
            _log("D2C Pack QC", "selection failed (pack continues)\n" + frappe.get_traceback())
            out.update({"qc_required": False, "qc_staged": False})
    if prior:
        p = prior[0]
        out["message"] = ("ALREADY PACK-VERIFIED " + str(p.verified_at)[:16]
                          + " by " + (p.verified_by or "?"))
    return out


@frappe.whitelist()
def pack_verify_submit(code, pieces_confirmed=None, station=None,
                       photo_url=None, notes=None, duration_sec=None,
                       qc_record=None):
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
    photo_url = (photo_url or "").strip()
    if not photo_url:
        return {"status": "error",
                "message": "A photo of the open box is mandatory before PACKED."}

    dn_name, awb, box_index, box_count = _resolve(code)
    if not dn_name:
        return {"status": "not_found", "message": "No order found for: " + code}
    if not awb:
        return {"status": "need_parcel",
                "message": "Multi-box order — scan each parcel's AWB barcode."}

    dn = frappe.get_doc("Delivery Note", dn_name)
    from solara_wms.wms.shopify_cancellations import (
        delivery_note_cancellation_hold,
        hold_response,
    )
    if delivery_note_cancellation_hold(dn):
        response = hold_response(dn)
        response["awb"] = awb
        return response
    dispatched = _dispatch_hold(dn, awb)
    if dispatched:
        return dispatched
    sibling_hold = _dispatched_sibling_hold(dn, awb)
    if sibling_hold:
        return sibling_hold
    lines, service = _pieces_for_dn(dn_name)
    lines, parcel_error = _pieces_for_parcel(
        dn, lines, box_index, box_count, service_lines=service)
    if parcel_error:
        return {"status": "error", "message": parcel_error}
    expected = sum(l["qty"] for l in lines)
    confirmed = flt(pieces_confirmed) if pieces_confirmed is not None else expected
    mismatch = 1 if abs(confirmed - expected) > 0.001 else 0

    existing = frappe.get_all("D2C Pack Verify", filters={"awb": awb},
                              fields=["name"], limit_page_length=1)
    if existing:
        return {"status": "already", "dn": dn_name,
                "order": dn.get("shopify_order_number"),
                "message": "Already pack-verified — no second record created."}

    from solara_wms.wms.pack_handoff import (
        consume_pack_handoff,
        pack_handoff_status,
    )
    handoff_state = pack_handoff_status(dn, awb, lines)
    if handoff_state.get("pick_handoff_required") and mismatch:
        return {
            "status": "count_hold",
            "dn": dn_name,
            "awb": awb,
            "message": "COUNT HOLD — confirmed pieces must match completed pick work.",
        }
    if not handoff_state.get("pick_handoff_ready"):
        return {
            "status": "pick_hold",
            "dn": dn_name,
            "awb": awb,
            "message": "PICK HOLD — " + handoff_state.get(
                "pick_handoff_error", "completed pick evidence is not ready"
            ),
        }

    qc_rows = frappe.get_all("D2C Pack QC", filters={"awb": awb},
                             fields=["name", "status"], limit_page_length=1)
    if qc_rows:
        qc = qc_rows[0]
        authorised_release = (qc_record and qc_record == qc.name)
        if qc.status in ("Pending", "Failed") and not authorised_release:
            return {"status": "qc_hold",
                    "message": "QC HOLD — independent QC must pass before sealing."}

    try:
        doc = frappe.get_doc({
            "doctype": "D2C Pack Verify",
            "delivery_note": dn_name,
            "shopify_order_number": dn.get("shopify_order_number"),
            "awb": awb,
            "courier": dn.get("courier_partner"),
            "box_index": box_index or 1,
            "box_count": box_count or 1,
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
        wms_pack_handoff = consume_pack_handoff(dn, awb, lines, doc.name)
        if wms_pack_handoff:
            frappe.db.set_value(
                "D2C Pack Verify",
                doc.name,
                "wms_pack_handoff",
                wms_pack_handoff,
                update_modified=False,
            )
        pairs = _awb_courier_pairs(dn)
        required_awbs = [a for a, _courier in pairs if a] or [awb]
        verified_rows = frappe.get_all(
            "D2C Pack Verify",
            filters={"delivery_note": dn_name, "awb": ["in", required_awbs],
                     "mismatch": 0},
            fields=["awb"], limit_page_length=0)
        verified_awbs = {row.awb for row in verified_rows}
        fully_verified = set(required_awbs).issubset(verified_awbs)
        if frappe.get_meta("Delivery Note").has_field("custom_pack_verified"):
            frappe.db.set_value("Delivery Note", dn_name,
                                {"custom_pack_verified": 1 if fully_verified else 0,
                                 "custom_pack_verified_at": (now_datetime()
                                                             if fully_verified else None)},
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
        "wms_pack_handoff": wms_pack_handoff,
        "awb": awb,
        "box_index": box_index or 1,
        "box_count": box_count or 1,
        "fully_verified": fully_verified,
        "verified_boxes": len(verified_awbs),
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


def purge_pack_photos(days=90):
    """Scheduler (daily). Delete pack-verify box photos older than `days`.

    Retention matches SOP-PACK-QC's initialled-sheet rule (>=90 days) — long
    enough to settle any CS "item missing" dispute, short enough that the
    evidence never becomes a storage problem. At ~850 orders/day and ~80 KB a
    photo that is ~2 GB/month, so without this the 200 GB object-storage
    allowance disappears inside a few years and every site backup carries it.

    Only the FILE is removed; the D2C Pack Verify record (counts, mismatch,
    contents snapshot, who/when) is kept forever — that is the audit trail.
    """
    from frappe.utils import add_days, nowdate
    cutoff = add_days(nowdate(), -cint(days or 90))
    rows = frappe.get_all("File",
                          filters={"file_name": ["like", "packverify-%"],
                                   "creation": ["<", cutoff]},
                          fields=["name"], limit_page_length=500)
    removed = 0
    for r in rows:
        try:
            frappe.delete_doc("File", r.name, ignore_permissions=True, force=True)
            removed += 1
        except Exception:
            pass
    if removed:
        frappe.db.commit()
        _log("D2C Pack Photo Purge",
             "removed {0} pack-verify photo(s) older than {1} days".format(removed, days))
    return {"removed": removed, "cutoff": cutoff}
