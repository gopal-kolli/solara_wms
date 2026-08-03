# Copyright (c) 2026, SOLARA and contributors
# For license information, please see license.txt
"""Appliance Express classification and carton-EAN verification.

Express is deliberately narrow: one physical appliance carton, or one of the
explicitly pre-kitted AFO bundles whose accessories are already sealed inside
that carton.  Loose accessories and multi-box orders stay on the normal lines.
"""
import json

import frappe
from frappe.utils import cint, flt


DEFAULT_APPLIANCE_SKUS = ("SOL-AF-501", "SOL-AF-124", "SOL-JUC-121")
DEFAULT_PREKIT_BUNDLES = (
    "SOL-AF-501-SIL-BAS-P6-SPY-101",
    "SOL-AF-501-SIL-BASKET-P6-SPY-101",
    "SOL-AF-124-SIL-BAS-P6-SPY-101",
    "SOL-AF-124-SIL-BASKET-P6-SPY-101",
)
STATION = "Appliance Express"


def _value(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _codes(value, defaults):
    if value is None or not str(value).strip():
        return set(defaults)
    return {part.strip().upper() for part in str(value).replace(",", "\n").splitlines()
            if part.strip()}


def express_config(settings=None):
    settings = settings or frappe.get_single("D2C Fulfillment Settings")
    raw_enabled = settings.get("appliance_express_enabled")
    return {
        "enabled": bool(cint(1 if raw_enabled in (None, "") else raw_enabled)),
        "appliance_skus": _codes(settings.get("appliance_express_skus"),
                                  DEFAULT_APPLIANCE_SKUS),
        "prekit_bundles": _codes(settings.get("appliance_express_bundles"),
                                  DEFAULT_PREKIT_BUNDLES),
    }


def classify_lines(lines, box_count=1, appliance_skus=None, prekit_bundles=None):
    """Pure eligibility decision shared by wave rendering and the scan API."""
    appliance_skus = set(appliance_skus or DEFAULT_APPLIANCE_SKUS)
    prekit_bundles = set(prekit_bundles or DEFAULT_PREKIT_BUNDLES)
    if cint(box_count or 1) != 1:
        return {"eligible": False, "reason": "Multi-box orders use normal packing."}
    lines = [row for row in (lines or []) if flt(_value(row, "qty")) > 0]
    primary = [row for row in lines
               if str(_value(row, "item_code") or "").upper() in appliance_skus]
    if len(primary) != 1 or abs(flt(_value(primary[0], "qty")) - 1) > 0.001:
        return {"eligible": False,
                "reason": "Express requires exactly one approved appliance carton."}
    bundles = {str(_value(row, "bundle") or "").upper() for row in lines
               if _value(row, "bundle")}
    loose = [row for row in lines if not _value(row, "bundle")]
    if len(lines) == 1 and len(loose) == 1:
        kind = "single_appliance"
        bundle = None
    elif len(bundles) == 1 and not loose and next(iter(bundles)) in prekit_bundles:
        kind = "pre_kitted_combo"
        bundle = next(iter(bundles))
    else:
        return {"eligible": False,
                "reason": "Loose or non-approved combo contents use normal packing."}
    return {
        "eligible": True,
        "kind": kind,
        "bundle": bundle,
        "carton_item": str(_value(primary[0], "item_code") or "").upper(),
        "reason": ("Approved pre-kitted appliance combo" if bundle
                   else "Approved single appliance carton"),
    }


def classify_dn(dn, config=None):
    config = config or express_config()
    if not config.get("enabled"):
        return {"eligible": False, "reason": "Appliance Express is switched off."}
    return classify_lines(
        _value(dn, "_lines") or [],
        box_count=(len(_value(dn, "_awb_pairs") or []) or
                   cint(_value(dn, "custom_box_count")) or 1),
        appliance_skus=config["appliance_skus"],
        prekit_bundles=config["prekit_bundles"])


def _barcodes(item_code):
    rows = frappe.get_all("Item Barcode", filters={"parent": item_code},
                          fields=["barcode"], limit_page_length=0)
    return sorted({str(_value(row, "barcode") or "").strip() for row in rows
                   if str(_value(row, "barcode") or "").strip()})


@frappe.whitelist()
def appliance_express_state():
    cfg = express_config()
    return {"status": "ok", "enabled": cfg["enabled"],
            "station": STATION, "appliance_skus": sorted(cfg["appliance_skus"]),
            "prekit_bundles": sorted(cfg["prekit_bundles"])}


@frappe.whitelist()
def appliance_express_set_enabled(enabled=1, actor=None, reason=None):
    doc = frappe.get_single("D2C Fulfillment Settings")
    doc.appliance_express_enabled = 1 if cint(enabled) else 0
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    out = appliance_express_state()
    out["changed_by"] = actor or frappe.session.user
    out["reason"] = (reason or ("Express resumed" if out["enabled"]
                                else "Express workforce unavailable"))[:500]
    out["message"] = ("Appliance Express is ON for new waves." if out["enabled"] else
                      "Appliance Express is OFF; new waves use the normal lines.")
    return out


@frappe.whitelist()
def appliance_express_get(code):
    from solara_wms.wms.d2c_pack_verify import pack_verify_get
    out = pack_verify_get(code, station=STATION)
    if out.get("status") not in ("ok", "already"):
        return out
    cfg = express_config()
    decision = classify_lines(out.get("pieces") or [], out.get("box_count") or 1,
                              cfg["appliance_skus"], cfg["prekit_bundles"])
    if not cfg["enabled"]:
        decision = {"eligible": False, "reason": "Appliance Express is switched off."}
    if not decision.get("eligible"):
        return {"status": "not_express", "message": decision["reason"],
                "order": out.get("order"), "awb": out.get("awb")}
    barcodes = _barcodes(decision["carton_item"])
    if not barcodes:
        return {"status": "error", "message": "No EAN is configured for {0}. Use normal packing and tell the lead.".format(decision["carton_item"])}
    out.update({"express": decision, "carton_barcodes": barcodes,
                "station": STATION,
                "condition_checks": ["Correct factory carton", "Carton and seal undamaged"] +
                (["Pre-kit / Combo Ready marking present"] if decision.get("bundle") else [])})
    return out


@frappe.whitelist()
def appliance_express_submit(code, carton_ean=None, conditions=None,
                             photo_url=None, duration_sec=None, operator=None):
    from solara_wms.wms.d2c_pack_qc import qc_stage
    from solara_wms.wms.d2c_pack_verify import pack_verify_submit
    out = appliance_express_get(code)
    if out.get("status") != "ok":
        return out
    if str(carton_ean or "").strip() not in set(out.get("carton_barcodes") or []):
        return {"status": "error", "message": "Wrong appliance EAN. Do not apply the label."}
    try:
        checks = json.loads(conditions or "[]") if isinstance(conditions, str) else (conditions or [])
    except (TypeError, ValueError):
        checks = []
    required = out.get("condition_checks") or []
    if set(required) - set(checks):
        return {"status": "error", "message": "Complete every carton and seal check."}
    if not (photo_url or "").strip():
        return {"status": "error", "message": "A carton photo is mandatory."}
    if out.get("qc_required"):
        return qc_stage(out["awb"], station=STATION,
                        pieces_confirmed=out.get("total_pieces"),
                        duration_sec=duration_sec, packer=operator)
    notes = "APPLIANCE EXPRESS · carton EAN {0} · {1}".format(
        str(carton_ean).strip(), "; ".join(required))
    return pack_verify_submit(out["awb"], pieces_confirmed=out.get("total_pieces"),
                              station=STATION, photo_url=photo_url, notes=notes,
                              duration_sec=duration_sec)
