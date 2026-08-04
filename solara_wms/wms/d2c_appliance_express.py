# Copyright (c) 2026, SOLARA and contributors
# For license information, please see license.txt
"""Appliance Express classification and carton-EAN verification.

Express is deliberately explicit: bare approved appliance cartons, approved
pre-kits, selected component-visible juicer bundles, and selected multi-box
appliance bundles.  Every multi-box parcel still gets its own AWB, carton-EAN,
condition and photo checks; unlisted loose accessories stay on normal lines.
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
    # Signature carton is also pre-kitted; the protective cover is already
    # included in the appliance-bay pack and must not send the order back to a
    # normal table.
    "SOL-AF-501-SIL-BASKET-P6-SPY-101-CVR-BAG",
)
DEFAULT_EXPRESS_COMBO_BUNDLES = (
    "SOL-JUC-121-COMBO-CVR-101",
    "SOL-JUC-121-GLSTUM-101",
    "SOL-JUC-121-INS-TUM-101",
)
DEFAULT_EXPRESS_MULTIBOX_BUNDLES = (
    "SOL-AFO-501-JUC-121",
    "SOL-AF-124-JUC-121",
)
# Loose physical add-ons explicitly approved for the two-carton AFO/AF124 +
# juicer Express family.  Their presence must create an on-screen checklist,
# not force the two factory cartons back through a normal packing table.
DEFAULT_EXPRESS_MULTIBOX_ACCESSORIES = (
    "SOL-AF-501-CVR-BAG",
    "SOL-AF-PP-101",
    "SOL-AF-SIL-BASKET-P6",
    "SOL-JUC-BAG-121",
    "SOL-SPY-101",
    "SOL-WB-105",
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
    if settings is None:
        settings = frappe.get_single("D2C Fulfillment Settings")
    raw_enabled = settings.get("appliance_express_enabled")
    raw_qc_enabled = settings.get("appliance_express_qc_enabled")
    return {
        "enabled": bool(cint(1 if raw_enabled in (None, "") else raw_enabled)),
        # This is deliberately independent from the daily global QC pause.
        # It stays OFF until management explicitly enables Appliance QC.
        "qc_enabled": bool(cint(0 if raw_qc_enabled in (None, "") else raw_qc_enabled)),
        "appliance_skus": _codes(settings.get("appliance_express_skus"),
                                  DEFAULT_APPLIANCE_SKUS),
        "prekit_bundles": _codes(settings.get("appliance_express_bundles"),
                                  DEFAULT_PREKIT_BUNDLES),
        "combo_bundles": _codes(settings.get("appliance_express_combo_bundles"),
                                 DEFAULT_EXPRESS_COMBO_BUNDLES),
        "multibox_bundles": _codes(
            settings.get("appliance_express_multibox_bundles"),
            DEFAULT_EXPRESS_MULTIBOX_BUNDLES),
        "multibox_accessories": set(DEFAULT_EXPRESS_MULTIBOX_ACCESSORIES),
    }


def classify_lines(lines, box_count=1, appliance_skus=None, prekit_bundles=None,
                   combo_bundles=None, multibox_bundles=None,
                   multibox_accessories=None):
    """Pure eligibility decision shared by wave rendering and the scan API."""
    appliance_skus = set(appliance_skus or DEFAULT_APPLIANCE_SKUS)
    prekit_bundles = set(prekit_bundles or DEFAULT_PREKIT_BUNDLES)
    combo_bundles = set(combo_bundles or DEFAULT_EXPRESS_COMBO_BUNDLES)
    multibox_bundles = set(multibox_bundles or DEFAULT_EXPRESS_MULTIBOX_BUNDLES)
    multibox_accessories = set(
        multibox_accessories or DEFAULT_EXPRESS_MULTIBOX_ACCESSORIES)
    box_count = cint(box_count or 1)
    lines = [row for row in (lines or []) if flt(_value(row, "qty")) > 0]
    primary = [row for row in lines
               if str(_value(row, "item_code") or "").upper() in appliance_skus]
    bundles = {str(_value(row, "bundle") or "").upper() for row in lines
               if _value(row, "bundle")}
    loose = [row for row in lines if not _value(row, "bundle")]

    # Explicit appliance+appliance bundles (currently AFO + slow juicer) ship
    # as separate factory cartons/AWBs but belong to the same appliance bay.
    # This branch works both on the whole DN during wave classification (two
    # primary cartons) and on one parcel during scan (one primary carton).
    if box_count > 1:
        approved_multibox = bundles & multibox_bundles
        bundle = next(iter(approved_multibox)) if len(approved_multibox) == 1 else None
        valid_primary = (primary and len(primary) <= box_count
                         and all(abs(flt(_value(row, "qty")) - 1) <= 0.001
                                 for row in primary))
        accessory_lines = [row for row in lines if row not in primary]
        valid_accessories = all(
            str(_value(row, "item_code") or "").upper() in multibox_accessories
            for row in accessory_lines)
        if bundle and valid_primary and valid_accessories:
            return {
                "eligible": True,
                "kind": "multi_box_appliance_combo",
                "bundle": bundle,
                "carton_item": str(_value(primary[0], "item_code") or "").upper(),
                "reason": "Approved multi-box appliance combo",
            }
        return {"eligible": False,
                "reason": "Multi-box order is not an approved Express appliance combo."}

    if len(primary) != 1 or abs(flt(_value(primary[0], "qty")) - 1) > 0.001:
        return {"eligible": False,
                "reason": "Express requires exactly one approved appliance carton."}
    if len(lines) == 1 and len(loose) == 1:
        kind = "single_appliance"
        bundle = None
    elif len(bundles) == 1 and not loose and next(iter(bundles)) in prekit_bundles:
        kind = "pre_kitted_combo"
        bundle = next(iter(bundles))
    elif len(bundles) == 1 and not loose and next(iter(bundles)) in combo_bundles:
        kind = "appliance_combo"
        bundle = next(iter(bundles))
    else:
        return {"eligible": False,
                "reason": "Loose or non-approved combo contents use normal packing."}
    return {
        "eligible": True,
        "kind": kind,
        "bundle": bundle,
        "carton_item": str(_value(primary[0], "item_code") or "").upper(),
        "reason": ("Approved pre-kitted appliance combo"
                   if kind == "pre_kitted_combo" else
                   "Approved appliance combo"
                   if kind == "appliance_combo" else
                   "Approved single appliance carton"),
    }


def classify_dn(dn, config=None):
    config = config or express_config()
    if not config.get("enabled"):
        return {"eligible": False, "reason": "Appliance Express is switched off."}
    return classify_lines(
        _value(dn, "_lines") or [],
        box_count=(len(_value(dn, "_awb_pairs") or []) or
                   cint(_value(dn, "custom_box_count")) or 1),
        appliance_skus=config.get("appliance_skus"),
        prekit_bundles=config.get("prekit_bundles"),
        combo_bundles=config.get("combo_bundles"),
        multibox_bundles=config.get("multibox_bundles"),
        multibox_accessories=config.get("multibox_accessories"))


def _barcodes(item_code):
    rows = frappe.get_all("Item Barcode", filters={"parent": item_code},
                          fields=["barcode"], limit_page_length=0)
    return sorted({str(_value(row, "barcode") or "").strip() for row in rows
                   if str(_value(row, "barcode") or "").strip()})


@frappe.whitelist()
def appliance_express_state():
    cfg = express_config()
    return {"status": "ok", "enabled": cfg["enabled"], "qc_enabled": cfg["qc_enabled"],
            "station": STATION, "appliance_skus": sorted(cfg["appliance_skus"]),
            "prekit_bundles": sorted(cfg["prekit_bundles"]),
            "combo_bundles": sorted(cfg["combo_bundles"]),
            "multibox_bundles": sorted(cfg["multibox_bundles"])}


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
def appliance_express_set_qc_enabled(enabled=0, actor=None, reason=None):
    """Persist the appliance-only QC switch until management changes it."""
    doc = frappe.get_single("D2C Fulfillment Settings")
    doc.appliance_express_qc_enabled = 1 if cint(enabled) else 0
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    out = appliance_express_state()
    out["changed_by"] = actor or frappe.session.user
    out["reason"] = (reason or ("Appliance QC enabled" if out["qc_enabled"]
                                else "Appliance QC disabled"))[:500]
    out["message"] = ("Independent QC is ON for Appliance Express." if out["qc_enabled"]
                      else "Independent QC is OFF for Appliance Express until re-enabled.")
    return out


@frappe.whitelist()
def appliance_express_get(code):
    from solara_wms.wms.d2c_pack_verify import pack_verify_get
    out = pack_verify_get(code, station=STATION)
    if out.get("status") not in ("ok", "already"):
        return out
    cfg = express_config()
    decision = classify_lines(out.get("pieces") or [], out.get("box_count") or 1,
                              cfg["appliance_skus"], cfg["prekit_bundles"],
                              cfg["combo_bundles"], cfg["multibox_bundles"])
    if not cfg["enabled"]:
        decision = {"eligible": False, "reason": "Appliance Express is switched off."}
    if not decision.get("eligible"):
        return {"status": "not_express", "message": decision["reason"],
                "order": out.get("order"), "awb": out.get("awb")}
    barcodes = _barcodes(decision["carton_item"])
    if not barcodes:
        return {"status": "error", "message": "No EAN is configured for {0}. Use normal packing and tell the lead.".format(decision["carton_item"])}
    combo_checks = []
    if decision.get("kind") == "pre_kitted_combo":
        combo_checks.append("Pre-kit / Combo Ready marking present")
    elif decision.get("kind") == "appliance_combo":
        combo_checks.append("All listed combo accessories present")
    out.update({"express": decision, "carton_barcodes": barcodes,
                "station": STATION,
                "condition_checks": ["Correct factory carton", "Carton and seal undamaged"] +
                combo_checks})
    if not cfg["qc_enabled"]:
        out.update({"qc_required": False, "qc_staged": False,
                    "qc_status": "Bypassed",
                    "qc_reason": "Independent QC disabled for Appliance Express"})
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
