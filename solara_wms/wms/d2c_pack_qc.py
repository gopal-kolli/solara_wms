# Copyright (c) 2026, SOLARA and contributors
# For license information, please see license.txt
"""Independent, capacity-safe QC between packing verification and sealing.

The selected parcel moves to the line's QC bay; the packing line immediately
starts the next AWB.  The roaming inspector works one oldest-first queue,
scans EANs (with an explicit manual path only for unbarcoded/bundle contents),
photographs the open box, and releases the parcel to Pack Verify on Pass.
"""
import hashlib
import json

import frappe
from frappe.utils import add_to_date, cint, flt, now_datetime, nowdate

from solara_wms.wms.d2c_dispatch import _resolve


def _value(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _conf_int(key, default):
    try:
        value = frappe.conf.get(key, default)
    except Exception:
        value = default
    return max(0, cint(value if value is not None else default))


def _bucket(awb, day=None):
    token = "{0}|{1}".format(day or nowdate(), (awb or "").strip())
    return int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16) % 100


def sampling_decision(awb, lines, single_rate=5, multi_rate=20, day=None):
    """Pure deterministic sampler: repeat lookups never change the answer."""
    pieces = sum(flt(_value(row, "qty")) for row in (lines or []))
    multi = pieces > 1.001 or len(lines or []) > 1
    rate = max(0, min(100, cint(multi_rate if multi else single_rate)))
    selected = _bucket(awb, day=day) < rate
    reason = ("Multi-piece sample ({0}%)" if multi else "Random sample ({0}%)").format(rate)
    return {"selected": selected, "reason": reason, "rate": rate,
            "pieces": pieces, "bucket": _bucket(awb, day=day)}


def _recent_line_failure(station):
    if not station:
        return False
    since = add_to_date(now_datetime(), minutes=-30)
    return bool(frappe.get_all(
        "D2C Pack QC", filters={"station": station, "status": "Failed",
                                 "audited_at": [">=", since]},
        fields=["name"], limit_page_length=1))


def qc_control_state():
    """Today's control defaults ON, so yesterday's pause never leaks forward."""
    rows = frappe.get_all(
        "D2C Pack QC Control", filters={"control_date": nowdate()},
        fields=["name", "enabled", "changed_at", "changed_by", "reason",
                "released_open_holds"], limit_page_length=1)
    if not rows:
        return {"enabled": True, "control_date": nowdate(), "changed_at": None,
                "changed_by": None, "reason": None, "released_open_holds": 0}
    row = rows[0]
    return {"enabled": bool(cint(_value(row, "enabled"))),
            "control_date": nowdate(), "changed_at": _value(row, "changed_at"),
            "changed_by": _value(row, "changed_by"), "reason": _value(row, "reason"),
            "released_open_holds": cint(_value(row, "released_open_holds"))}


@frappe.whitelist()
def qc_set_control(enabled=1, release_open=0, actor=None, reason=None):
    """Audited same-day pause/resume switch used by the QC supervisor PWA."""
    enabled = bool(cint(enabled))
    release_open = bool(cint(release_open)) and not enabled
    rows = frappe.get_all("D2C Pack QC Control", filters={"control_date": nowdate()},
                          fields=["name"], limit_page_length=1)
    doc = (frappe.get_doc("D2C Pack QC Control", _value(rows[0], "name")) if rows else
           frappe.get_doc({"doctype": "D2C Pack QC Control", "control_date": nowdate()}))
    released = 0
    if release_open:
        for row in frappe.get_all(
                "D2C Pack QC", filters={"status": ["in", ["Pending", "Failed"]]},
                fields=["name"], limit_page_length=0):
            hold = frappe.get_doc("D2C Pack QC", _value(row, "name"))
            hold.status = "Waived"
            hold.audited_at = now_datetime()
            hold.audited_by = (actor or frappe.session.user)
            hold.outcome_reason = "QC waived: " + ((reason or "QC workforce unavailable").strip()[:450])
            hold.flags.ignore_permissions = True
            hold.save(ignore_permissions=True)
            released += 1
    doc.enabled = 1 if enabled else 0
    doc.changed_at = now_datetime()
    doc.changed_by = (actor or frappe.session.user)
    doc.reason = (reason or ("QC resumed" if enabled else "QC workforce unavailable")).strip()[:500]
    doc.released_open_holds = released
    doc.flags.ignore_permissions = True
    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)
    frappe.db.commit()
    state = qc_control_state()
    state.update({"status": "ok", "released_now": released})
    return state


def qc_decision(awb, lines, station=None):
    if not qc_control_state()["enabled"]:
        return {"selected": False, "forced": False,
                "reason": "Independent QC paused for today", "rate": 0}
    if _recent_line_failure(station):
        return {"selected": True, "forced": True,
                "reason": "Escalation after recent QC failure", "rate": 100}
    out = sampling_decision(
        awb, lines,
        single_rate=_conf_int("pack_qc_single_rate", 5),
        multi_rate=_conf_int("pack_qc_multi_rate", 20))
    out["forced"] = False
    return out


def _open_count(station):
    return frappe.db.count("D2C Pack QC", {"station": station,
                                             "status": ["in", ["Pending", "Failed"]]})


def qc_state(awb, lines, station=None):
    rows = frappe.get_all(
        "D2C Pack QC", filters={"awb": awb},
        fields=["name", "status", "sample_reason", "station", "staged_at"],
        limit_page_length=1)
    if rows:
        row = rows[0]
        return {"qc_required": _value(row, "status") in ("Pending", "Failed"),
                "qc_staged": True, "qc_record": _value(row, "name"),
                "qc_status": _value(row, "status"),
                "qc_reason": _value(row, "sample_reason"),
                "qc_station": _value(row, "station")}
    decision = qc_decision(awb, lines, station=station)
    return {"qc_required": bool(decision["selected"]), "qc_staged": False,
            "qc_status": "Selected" if decision["selected"] else None,
            "qc_reason": decision["reason"] if decision["selected"] else None,
            "qc_forced": bool(decision.get("forced"))}


def _parcel_context(code):
    from solara_wms.wms.d2c_pack_verify import _pieces_for_dn, _pieces_for_parcel
    dn_name, awb, box_index, box_count = _resolve((code or "").strip())
    if not dn_name:
        return None, {"status": "not_found", "message": "No order found for: " + str(code)}
    if not awb:
        return None, {"status": "need_parcel",
                      "message": "Multi-box order — scan the parcel AWB."}
    dn = frappe.get_doc("Delivery Note", dn_name)
    lines, _service = _pieces_for_dn(dn_name)
    lines, error = _pieces_for_parcel(dn, lines, box_index, box_count)
    if error:
        return None, {"status": "error", "message": error}
    return {"dn": dn, "dn_name": dn_name, "awb": awb, "box_index": box_index,
            "box_count": box_count, "lines": lines}, None


@frappe.whitelist()
def qc_stage(code, station=None, pieces_confirmed=None, duration_sec=None, packer=None):
    """Move one selected open parcel into its line QC bay without blocking line."""
    station = (station or "").strip()
    if not (station.startswith("Line ") or station == "Appliance Express"):
        return {"status": "error", "message": "A packing line is required."}
    ctx, error = _parcel_context(code)
    if error:
        return error
    existing_pack = frappe.get_all("D2C Pack Verify", filters={"awb": ctx["awb"]},
                                   fields=["name"], limit_page_length=1)
    if existing_pack:
        return {"status": "already", "message": "Already pack-verified."}
    existing = frappe.get_all("D2C Pack QC", filters={"awb": ctx["awb"]},
                              fields=["name", "status", "station"], limit_page_length=1)
    if existing:
        row = existing[0]
        return {"status": "staged", "selected": True, "record": _value(row, "name"),
                "qc_status": _value(row, "status"), "station": _value(row, "station"),
                "message": "Parcel is already in the QC queue."}
    expected = sum(flt(_value(row, "qty")) for row in ctx["lines"])
    confirmed = flt(pieces_confirmed) if pieces_confirmed is not None else expected
    if abs(confirmed - expected) > 0.001:
        return {"status": "error", "message": "Resolve the packing count before QC."}
    decision = qc_decision(ctx["awb"], ctx["lines"], station=station)
    if not decision["selected"]:
        return {"status": "not_selected", "selected": False,
                "message": "Pack normally — this parcel was not selected for QC."}
    max_open = max(1, _conf_int("pack_qc_max_open_per_line", 2))
    if _open_count(station) >= max_open:
        return {"status": "capacity", "selected": False,
                "message": "QC bay is at capacity — pack this parcel normally."}
    doc = frappe.get_doc({
        "doctype": "D2C Pack QC", "delivery_note": ctx["dn_name"],
        "shopify_order_number": (ctx["dn"].get("shopify_order_number") or
                                  ctx["dn"].get("shopify_order_id")),
        "awb": ctx["awb"], "station": station, "status": "Pending",
        "sample_reason": decision["reason"], "pieces_expected": expected,
        "contents": json.dumps(ctx["lines"]), "staged_at": now_datetime(),
        "staged_by": (packer or frappe.session.user),
        "duration_sec": cint(duration_sec),
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "staged", "selected": True, "record": doc.name,
            "station": station, "awb": ctx["awb"],
            "message": "QC HOLD — place the open parcel in {0} QC bay.".format(station)}


def _barcode_requirements(lines):
    codes = sorted({_value(row, "item_code") for row in lines if _value(row, "item_code")})
    grouped = {code: [] for code in codes}
    if codes:
        for row in frappe.get_all("Item Barcode", filters={"parent": ["in", codes]},
                                  fields=["parent", "barcode"], limit_page_length=0):
            barcode = (_value(row, "barcode") or "").strip()
            if barcode:
                grouped.setdefault(_value(row, "parent"), []).append(barcode)
    return [{"item_code": _value(row, "item_code"),
             "item_name": _value(row, "item_name") or _value(row, "item_code"),
             "qty": max(1, cint(round(flt(_value(row, "qty"))))),
             "bundle": _value(row, "bundle"),
             "barcodes": sorted(set(grouped.get(_value(row, "item_code"), []))),
             "manual_allowed": bool(_value(row, "bundle") or
                                      not grouped.get(_value(row, "item_code")))}
            for row in lines]


def validate_scans(requirements, scans, manual_confirmed):
    """Pure exact-count validator shared by the API and unit tests."""
    remaining = {row["item_code"]: cint(row["qty"]) for row in requirements}
    barcode_map = {}
    for row in requirements:
        for barcode in row.get("barcodes") or []:
            barcode_map.setdefault(str(barcode).strip(), []).append(row["item_code"])
    matched = []
    unexpected = []
    for raw in scans or []:
        barcode = str(raw or "").strip()
        choices = barcode_map.get(barcode) or []
        code = next((item for item in choices if remaining.get(item, 0) > 0), None)
        if not code:
            unexpected.append(barcode)
            continue
        remaining[code] -= 1
        matched.append({"barcode": barcode, "item_code": code})
    manual_confirmed = manual_confirmed or {}
    manual_used = {}
    for row in requirements:
        code = row["item_code"]
        qty = max(0, cint(manual_confirmed.get(code)))
        if qty and not row.get("manual_allowed"):
            return {"ok": False, "message": code + " must be EAN-scanned."}
        used = min(qty, remaining.get(code, 0))
        remaining[code] = max(0, remaining.get(code, 0) - used)
        if used:
            manual_used[code] = used
    missing = {code: qty for code, qty in remaining.items() if qty > 0}
    if unexpected:
        return {"ok": False, "message": "Unexpected or duplicate barcode: " + unexpected[0],
                "unexpected": unexpected, "missing": missing}
    if missing:
        return {"ok": False, "message": "QC is incomplete — scan or confirm every piece.",
                "missing": missing}
    return {"ok": True, "matched": matched, "manual": manual_used}


@frappe.whitelist()
def qc_queue():
    rows = frappe.get_all(
        "D2C Pack QC", filters={"status": ["in", ["Pending", "Failed"]]},
        fields=["name", "awb", "shopify_order_number", "station", "status",
                "sample_reason", "pieces_expected", "staged_at", "audited_at"],
        order_by="staged_at asc", limit_page_length=100)
    now = now_datetime()
    out = []
    for row in rows:
        staged = _value(row, "staged_at")
        out.append({key: _value(row, key) for key in (
            "name", "awb", "shopify_order_number", "station", "status",
            "sample_reason", "pieces_expected", "staged_at", "audited_at")})
        out[-1]["wait_min"] = round(max(0, (now - staged).total_seconds()) / 60, 1) \
            if staged else None
    return {"status": "ok", "count": len(out), "parcels": out,
            "control": qc_control_state()}


@frappe.whitelist()
def qc_get(code):
    ctx, error = _parcel_context(code)
    if error:
        return error
    rows = frappe.get_all("D2C Pack QC", filters={"awb": ctx["awb"]},
                          fields=["name", "status", "station", "sample_reason",
                                  "staged_at", "recheck_count"], limit_page_length=1)
    if not rows:
        return {"status": "not_selected", "message": "This parcel is not in the QC queue."}
    qc = rows[0]
    if _value(qc, "status") == "Passed":
        return {"status": "already", "message": "QC already passed."}
    from solara_wms.wms.d2c_pack_verify import _sku_images
    requirements = _barcode_requirements(ctx["lines"])
    images = _sku_images({row["item_code"] for row in requirements})
    for row in requirements:
        row["image"] = images.get(row["item_code"])
    return {"status": "ok", "record": _value(qc, "name"), "awb": ctx["awb"],
            "dn": ctx["dn_name"], "order": (ctx["dn"].get("shopify_order_number") or
                                               ctx["dn"].get("shopify_order_id")),
            "station": _value(qc, "station"), "qc_status": _value(qc, "status"),
            "sample_reason": _value(qc, "sample_reason"),
            "recheck_count": cint(_value(qc, "recheck_count")),
            "total_pieces": sum(row["qty"] for row in requirements),
            "requirements": requirements}


@frappe.whitelist()
def qc_submit(code, scans=None, manual_confirmed=None, outcome="Pass",
              fail_reason=None, photo_url=None, duration_sec=None, inspector=None):
    ctx, error = _parcel_context(code)
    if error:
        return error
    rows = frappe.get_all("D2C Pack QC", filters={"awb": ctx["awb"]},
                          fields=["name"], limit_page_length=1)
    if not rows:
        return {"status": "not_selected", "message": "Parcel is not in the QC queue."}
    doc = frappe.get_doc("D2C Pack QC", _value(rows[0], "name"))
    if doc.status == "Passed":
        return {"status": "already", "message": "QC already passed."}
    photo_url = (photo_url or "").strip()
    if not photo_url:
        return {"status": "error", "message": "An open-box QC photo is mandatory."}
    try:
        scan_values = json.loads(scans or "[]") if isinstance(scans, str) else (scans or [])
        manual_values = (json.loads(manual_confirmed or "{}")
                         if isinstance(manual_confirmed, str) else (manual_confirmed or {}))
    except (TypeError, ValueError):
        return {"status": "error", "message": "Unreadable QC scan evidence."}
    audit_user = (inspector or frappe.session.user)
    if (outcome or "").strip().lower() == "fail":
        if not (fail_reason or "").strip():
            return {"status": "error", "message": "Choose why QC failed."}
        doc.status = "Failed"
        doc.outcome_reason = (fail_reason or "").strip()[:500]
        doc.barcode_scans = json.dumps({"scans": scan_values, "manual": manual_values})
        doc.photo_url = photo_url
        doc.duration_sec = cint(duration_sec)
        doc.audited_at = now_datetime()
        doc.audited_by = audit_user
        doc.recheck_count = cint(doc.recheck_count) + 1
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return {"status": "failed", "record": doc.name,
                "message": "QC FAILED — quarantine this parcel for correction."}
    requirements = _barcode_requirements(ctx["lines"])
    validation = validate_scans(requirements, scan_values, manual_values)
    if not validation["ok"]:
        return {"status": "incomplete", "message": validation["message"],
                "missing": validation.get("missing") or {}}
    from solara_wms.wms.d2c_pack_verify import pack_verify_submit
    packed = pack_verify_submit(
        ctx["awb"], pieces_confirmed=sum(row["qty"] for row in requirements),
        station=doc.station, photo_url=photo_url,
        notes="INDEPENDENT QC PASS {0} · inspector {1}".format(doc.name, audit_user),
        duration_sec=duration_sec, qc_record=doc.name)
    if packed.get("status") not in ("ok", "already"):
        return packed
    doc.status = "Passed"
    doc.outcome_reason = "Passed independent EAN/manual verification"
    doc.barcode_scans = json.dumps(validation)
    doc.photo_url = photo_url
    doc.duration_sec = cint(duration_sec)
    doc.audited_at = now_datetime()
    doc.audited_by = audit_user
    doc.pack_verify = packed.get("record")
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "passed", "record": doc.name,
            "pack_verify": packed.get("record"), "awb": ctx["awb"],
            "message": "QC PASSED — parcel may now be sealed."}
