"""
D2C Fulfillment Automation — Phase 1 (single-AWB orders)
========================================================
Replaces the manual Google-sheet flow for Shopify D2C order fulfillment with an
Atlas-native pipeline modelled on the proven Amazon DF pattern.

Pipeline (all gated by the `D2C Fulfillment Settings` single doctype):

    Shopify order → Atlas SHP Sales Order (existing v12 connector; reserves stock)
          │
      release_d2c_shipments()   scheduler */15   ← THIS FILE
        eligible = submitted SHP SO, not On Hold, per_delivered=0,
                   skip_delivery_note=0, all items in stock,
                   NO multi-box SKU (config), not already DN'd
        → make_delivery_note(so) → submit
        → existing LIVE scripts fire on DN submit:
             "Create Clickpost Shipment"  → AWB + shipping_label
             "Auto Sync AWB to Shopify"   → fulfillment/tracking to Shopify
             "Auto Create SI on DN Submit"→ SHPSI27 invoice
          │
      fetch_d2c_labels()        scheduler */15   ← THIS FILE
        for submitted SHP DNs with a shipping_label URL and no attached PDF yet:
        download the (short-lived, presigned S3) label PDF → attach to the DN as a
        permanent private File, so the combined-PDF step never races URL expiry.
          │
      prepare_todays_shipments()  warehouse button ← THIS FILE
        BATCH-AWARE: each click covers only DNs not successfully printed in a
        prior D2C Prepare Batch; DNs still awaiting labels remain eligible for
        the next wave. Every printed batch is recorded and reprintable exactly.
          ├─ Pick List PDF  (SKU-summary section + per-order pack section)
          └─ Combined Labels PDF (attached label PDFs merged in pack sequence)

Phase 1 handles single-parcel orders only: the order's box count must equal
exactly 1. Box count combines two stacking rules (see _order_box_count):
  - per-SKU: Item.custom_boxes_per_unit (0 = nestable accessory riding inside a
    parent, 1 = own box [default], 2+ = multi-box combo e.g. airfryer+juicer);
    settings sku_box_config is a max-only safety net.
  - category-collapse: items of a COMBINABLE category (Cookware/Drinkware) STACK
    into one carton together (up to combine_piece_cap pieces). Appliances do NOT
    collapse — an airfryer + a cookware piece = 2 boxes.
Multi-box orders (count >= 2) stay on the manual sheet until Phase 2.

Reversibility is already handled by the installed base: "Cancel Clickpost
Shipment" voids the AWB on DN cancel. This module never cancels or deletes.
"""

import hashlib
import io
import json
import os

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime, nowdate, add_days, add_to_date, getdate

from solara_wms.wms.utils import get_available_qty


SETTINGS_DOCTYPE = "D2C Fulfillment Settings"
DEFAULT_WAREHOUSE = "Main Warehouse - WTBBPL"
DEFAULT_PREFIX = "SHP"
# Deferred-invoice SI (raised after the label is fetched, not at DN submit).
SI_SERIES = "SHPSI27-.#####"
D2C_INCOME_ACCOUNT = "Sales - WTBBPL"
OPD_REPLACEMENT_PREFIX = "REP-"
REPLACEMENT_COST_ACCOUNT = "Replacement/Warranty Cost - WTBBPL"
OPD_APPROVAL_STATUSES = {"approved", "captain_approved"}

# Batch pick-list/labels PDFs are private Files attached to a D2C Prepare Batch,
# readable in Atlas only by System Manager / Returns Manager. Non-admin wave-email
# recipients (dispatch/ops leads) therefore 403 on the raw /private/files link on
# links-only (>9 MB) batches. The P&L dashboard's label-status route proxies those
# PDFs through the Atlas token, so any @solara.in Google login can open them with no
# Atlas file permission. Wave emails link here instead of the raw Atlas file URL.
LABEL_DASHBOARD_BASE = "https://shopify-pl-dashboard-916807701528.asia-south1.run.app"

# ClickPost label fetch-by-AWB — for couriers (e.g. Shadowfax) that assign an AWB
# but do NOT write the presigned label URL back onto the DN. cp_id is ClickPost's
# per-courier id; seed the common ones, discover + cache the rest. API key lives
# in D2C Fulfillment Settings.clickpost_api_key (not in code).
CLICKPOST_LABEL_API = "https://www.clickpost.in/api/v1/fetch/shippinglabel/"
DEFAULT_CPID_SEED = {"shadowfax": 9, "delhivery": 4, "bluedart": 5, "elasticrun": 1}

# Items that belong to the whole order, not one box — they appear on EVERY
# parcel's label (packer applies them to each box) but are charged once.
PER_ORDER_LABEL_ITEMS = {"SOL-GIFWRAP", "SOL-INS-PERSONALISATION"}

# Shopify fulfillment: push AWB + tracking to Shopify so the order shows fulfilled
# and the customer gets tracking. The LIVE "Auto Sync AWB to Shopify" server script
# does this on DN re-save, but it calls frappe.make_get/post_request which are None
# in the safe_exec sandbox (throws 'NoneType' object is not callable — 133 fails/24h
# as of 2026-07-15), and the connector's own fulfillment sync is off
# (Shopify Setting.sync_delivery_note=0). So we do it here in app code (real HTTP).
SHOPIFY_CARRIER_MAP = {
    "Delhivery": "Delhivery", "Bluedart": "Bluedart", "Blue Dart": "Bluedart",
    "DTDC": "DTDC Express", "Xpressbees": "XpressBees", "Ecom Express": "Ecom Express",
    "Shadowfax": "Shadowfax",
}
# Releasable Sales Order statuses: goods still to go out, nothing delivered yet.
RELEASABLE_STATUSES = ("To Deliver and Bill", "To Deliver")

# Categories whose items STACK into one carton when bought together (validated on
# 10-day LIVE shipping history 2026-07-12: same-category multi-piece cookware
# orders shipped single-AWB 346:1). Appliances are deliberately NOT here — each
# needs its own box (airfryer + a cookware piece = 2 boxes, per ops).
DEFAULT_COMBINABLE_CATEGORIES = ("Cookware", "Drinkware")
DEFAULT_COMBINE_PIECE_CAP = 6  # pieces of one combinable category per carton

# Leak-prevention monitors (Layer 1 wave aging note + Layer 3 completeness report).
GOLIVE_DATE = "2026-07-16"      # D2C automation go-live; never reconcile before this
COMPLETENESS_WINDOW_DAYS = 4    # reconcile Shopify orders from the last N days
COMPLETENESS_GRACE_HOURS = 12   # skip orders newer than this (still in normal flow)
AGING_UNSHIPPED_HOURS = 12      # wave note flags orders released > this and still unshipped


# ─── SETTINGS HELPERS ─────────────────────────────────────────────

def _settings():
    return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def _box_config(settings):
    """Safety-net override map from the settings sku_box_config JSON. The Item
    master field `custom_boxes_per_unit` is the primary source of truth; this
    JSON can only bump a SKU's box count UP (never below the Item field) — a
    code-side backstop so a known multi-box combo can't slip through as a single
    AWB if its Item field wasn't set."""
    try:
        raw = json.loads(settings.get("sku_box_config") or "{}")
        return {str(k).strip().upper(): cint(v) for k, v in raw.items()}
    except Exception:
        _log("D2C Config", "sku_box_config is not valid JSON — treating as empty")
        return {}


def _item_boxes(item_code, box_map):
    """Physical boxes one unit of this SKU ships in, for AWB planning.
    Primary source: Item.custom_boxes_per_unit (0 = nestable accessory that packs
    inside a parent's box, 1 = own box [default], 2+ = multi-box combo). The
    settings JSON (box_map) is a max-only safety net for known combos."""
    if not item_code:
        return 1
    try:
        field_val = frappe.get_cached_value("Item", item_code, "custom_boxes_per_unit")
    except Exception:
        field_val = None
    field_val = cint(field_val) if field_val is not None else 1
    return max(field_val, cint(box_map.get(item_code.upper(), 0)))


_CATEGORY_MAP = None


def _category_map():
    """SKU -> combinable category ('Cookware'/'Drinkware'), loaded once from the
    app data file (curated from mis_category_map.json). Items not in the map are
    treated as non-combinable (each carries its own per-SKU box count)."""
    global _CATEGORY_MAP
    if _CATEGORY_MAP is None:
        try:
            path = os.path.join(os.path.dirname(__file__), "data", "combinable_categories.json")
            with open(path) as fh:
                raw = json.load(fh)
            _CATEGORY_MAP = {str(k).strip().upper(): v for k, v in raw.items()}
        except Exception:
            _log("D2C Config", "combinable_categories.json unreadable — category-collapse OFF")
            _CATEGORY_MAP = {}
    return _CATEGORY_MAP


def _item_category(item_code):
    if not item_code:
        return None
    return _category_map().get(item_code.upper())


def _combinable_categories(settings):
    raw = (settings.get("combine_categories") or "").strip()
    if not raw:
        return set(DEFAULT_COMBINABLE_CATEGORIES)
    return {c.strip() for c in raw.replace(",", "\n").splitlines() if c.strip()}


def _combine_piece_cap(settings):
    return cint(settings.get("combine_piece_cap")) or DEFAULT_COMBINE_PIECE_CAP


def _split_combos(settings):
    """Parent SKU -> ordered list of child SKUs (one physical parcel each).
    Mirrors the LIVE ClickPost script's SPLIT_COMBOS; settings-overridable via
    `split_combos` (JSON) so the app's parcel plan and the CP minter stay in sync.
    This is the ONLY place a per-parcel item breakdown exists for a bundle SKU."""
    default = {"SOL-AFO-501-JUC-121": ["SOL-AF-501", "SOL-JUC-121"]}
    raw = (settings.get("split_combos") or "").strip()
    if raw:
        try:
            return {str(k).strip().upper(): list(v) for k, v in json.loads(raw).items()}
        except Exception:
            _log("D2C Config", "split_combos is not valid JSON — using default")
    return {k.upper(): v for k, v in default.items()}


def _bin_pack_category(cat, pieces, cap):
    """Pack one combinable category's full-box pieces into cap-sized cartons,
    KEEPING item identity. Produces ceil(total_qty / cap) cartons — identical to
    the box-count math — with the specific items assigned to each carton."""
    cap = cap if cap > 0 else 1
    cartons, cur, cur_n = [], [], 0
    for p in pieces:
        remaining, code = cint(p["qty"]), p["item_code"]
        while remaining > 0:
            take = min(cap - cur_n, remaining)
            cur.append({"item_code": code, "qty": take})
            cur_n += take
            remaining -= take
            if cur_n >= cap:
                cartons.append({"items": cur, "kind": "category:" + cat})
                cur, cur_n = [], 0
    if cur:
        cartons.append({"items": cur, "kind": "category:" + cat})
    return cartons


def _order_parcels(so, box_map, settings):
    """Assign this order's items to physical parcels using the SAME validated
    rules as the box count, but KEEPING item identity so the AWB split knows what
    goes in each box. Returns a list of parcels:
        [{"items": [{"item_code":.., "qty":..}], "kind": ".."}]

    Rules (mirror _order_box_count):
      - non-combinable single-box item (appliance): each unit -> its own parcel
      - combinable-category item (Cookware/Drinkware), boxes==1: stacks with its
        peers, bin-packed into combine_piece_cap-sized cartons
      - multi-box SKU (boxes>=2): child SKUs from the combo map, one parcel each;
        if no combo map is known, `boxes` parcels of the SKU per unit
      - nestable accessory (boxes==0): rides inside the first parcel, never its
        own box, never dropped
    Floored at 1 parcel. Does NOT apply the jumbo guard — the caller gates that.

    NOTE (bug fix): a boxes>=2 line is routed to the multi-box path REGARDLESS of
    category. Previously a combinable-category SKU set to 2 boxes was mis-counted
    as a single stackable piece (silent under-ship); now it correctly multi-boxes."""
    combinable = _combinable_categories(settings)
    cap = _combine_piece_cap(settings)
    combos = _split_combos(settings)

    parcels = []       # own-box / combo / multibox-sku parcels
    cat_pieces = {}    # category -> [{item_code, qty}] full-box pieces to stack
    nestables = []     # boxes==0 items, ride inside parcel 1

    for it in so.items:
        code = it.get("item_code")
        qty = cint(it.get("qty"))
        if not code or qty <= 0:
            continue
        boxes = _item_boxes(code, box_map)
        cat = _item_category(code)
        if boxes == 0:
            nestables.append({"item_code": code, "qty": qty})
        elif boxes >= 2:
            children = combos.get(code.upper())
            for _u in range(qty):
                if children:
                    for child in children:
                        parcels.append({"items": [{"item_code": child, "qty": 1}], "kind": "combo"})
                else:
                    for _b in range(boxes):
                        parcels.append({"items": [{"item_code": code, "qty": 1}], "kind": "multibox_sku"})
        elif cat in combinable:
            cat_pieces.setdefault(cat, []).append({"item_code": code, "qty": qty})
        else:
            for _u in range(qty):
                parcels.append({"items": [{"item_code": code, "qty": 1}], "kind": "own_box"})

    for cat, pieces in cat_pieces.items():
        parcels.extend(_bin_pack_category(cat, pieces, cap))

    if not parcels:
        parcels.append({"items": [], "kind": "single"})
    per_order = [n for n in nestables if n["item_code"].upper() in PER_ORDER_LABEL_ITEMS]
    riders = [n for n in nestables if n["item_code"].upper() not in PER_ORDER_LABEL_ITEMS]
    for n in riders:
        # Ride-along nestables go in the LAST parcel — warehouse convention
        # (17-Jul): "AF-501 -> box 1, JUC-121 + everything else -> box 2".
        parcels[-1]["items"].append(n)
    for n in per_order:
        # Per-order items (gift wrap) must be on EVERY box's label so the packer
        # wraps each parcel (Suresh, 17-Jul). Value is counted once — see
        # _parcel_plan_for_dn (distinct item_code valued in its first parcel).
        for p in parcels:
            p["items"].append(dict(n))
    return parcels


def _order_box_count(so, box_map, settings):
    """Number of physical boxes this order ships in = number of BOX-BEARING
    parcels the split would produce (see _order_parcels), floored at 1. == 1 means
    single-AWB (Phase 1); >= 2 is multi-box.

    Complexity is measured by box-bearing PARCELS, not distinct line count: 0-box
    nestable accessories and virtual (warranty/gift) lines ride inside a parent
    parcel and never inflate this number — so an order like AFO+juicer combo + 5
    warranty/accessory lines is still just its 2 real boxes. The old line-count
    'jumbo guard' mis-fired on exactly these orders (many 0-box riders), routing
    2-4 box orders to the manual sheet. The release gate now caps on
    max_release_parcels instead; anything beyond that stays on the sheet, and the
    AWB-shortfall guard backstops dispatch, so a mis-estimate is HELD, never
    under-shipped."""
    return len(_order_parcels(so, box_map, settings))


def _fully_covered_by_known_combos(so, box_map, settings):
    """PHASE-1 multi-box release gate: True only when the order's entire >=2-box
    nature comes from a SINGLE known-splittable combo that the LIVE ClickPost
    script (SPLIT_COMBOS) already 2-parcels correctly — so releasing it can NEVER
    under-ship. Guarantees:
      1. exactly ONE line is a combo SKU, qty == 1 (CP splits on the first match
         and mints n=len(children) parcels regardless of qty, so >1 combo line or
         qty>1 would under-ship);
      2. that combo has exactly 2 children (matches the DN's 2-AWB storage cap —
         3+ children would be minted-and-orphaned);
      3. every OTHER line contributes 0 boxes (nestable ride-along only — a second
         appliance / cookware stack would need AWBs the CP combo branch won't mint).
    When all hold, box_count == 2 and CP produces exactly those 2 AWBs; the AWB
    guard still backstops it before dispatch. Line count is irrelevant here — any
    number of 0-box nestable riders leaves the order at its single combo's 2 boxes."""
    combos = _split_combos(settings)
    live = [it for it in so.items if cint(it.get("qty")) > 0]
    combo_lines = [it for it in live if (it.get("item_code") or "").upper() in combos]
    if len(combo_lines) != 1:
        return False
    cl = combo_lines[0]
    if cint(cl.get("qty")) != 1:
        return False
    if len(combos[(cl.get("item_code") or "").upper()]) != 2:
        return False
    for it in live:
        if it is cl:
            continue
        if _item_boxes(it.get("item_code"), box_map) != 0:
            return False  # a real second box the combo split won't cover
    return True


def _parcel_plan_for_dn(so, parcels):
    """Serialisable per-parcel plan for the ClickPost script: items + rupee value
    per parcel. Values come from the SO's own line rates (what the customer paid,
    incl. tax) so the COD split matches the order economics; a line missing a
    rate falls back to 1 so weights can never zero out. Only used for 2-parcel
    releases (Phase 2a)."""
    rate_of = {}
    for it in so.items:
        if it.item_code:
            rate_of[it.item_code] = flt(it.rate) or 1.0

    def price_of(code):
        # SO line rate when the item is an order line; combo CHILDREN are not SO
        # lines, so fall back to Item Price (MRP -> Standard Selling) ->
        # valuation_rate — same ladder the ClickPost script uses for weighting.
        if code in rate_of:
            return rate_of[code]
        val = frappe.db.get_value("Item Price", {"item_code": code, "price_list": "MRP"},
                                  "price_list_rate")
        if not val:
            val = frappe.db.get_value("Item Price",
                                      {"item_code": code, "price_list": "Standard Selling"},
                                      "price_list_rate")
        if not val:
            val = frappe.db.get_value("Item", code, "valuation_rate")
        return flt(val) or 1.0

    plan = []
    valued = set()  # a distinct item_code contributes value only once (per-order
    # items like gift wrap appear on every parcel's label but are charged once)
    for p in parcels:
        items = [{"item_code": i["item_code"], "qty": cint(i["qty"])}
                 for i in p.get("items", []) if i.get("item_code")]
        value = 0.0
        for i in items:
            if i["item_code"] in valued:
                continue
            valued.add(i["item_code"])
            value += price_of(i["item_code"]) * i["qty"]
        plan.append({"items": items, "value": round(value, 2), "kind": p.get("kind")})
    return plan


def _source_warehouse(settings):
    return settings.get("source_warehouse") or DEFAULT_WAREHOUSE


def _prefix(settings):
    return (settings.get("so_series_prefix") or DEFAULT_PREFIX).strip()


def _log(title, message):
    """Short, greppable Error Log entry (Error Log is the app-wide audit trail)."""
    frappe.log_error(message=message, title=title)


def _opd_replacement_prefix(settings):
    return (settings.get("opd_replacement_so_prefix") or OPD_REPLACEMENT_PREFIX).strip()


def _canonical_snapshot_hash(snapshot):
    """Hash the exact canonical snapshot written by OPD (UTF-8 SHA-256)."""
    return hashlib.sha256((snapshot or "").encode("utf-8")).hexdigest()


def _opd_snapshot_lines(snapshot):
    return sorted(
        (str(row.get("item_code") or ""), flt(row.get("qty")), flt(row.get("rate")))
        for row in snapshot.get("items", [])
    )


def _so_snapshot_lines(so):
    return sorted(
        (str(row.get("item_code") or ""), flt(row.get("qty")), flt(row.get("rate")))
        for row in so.items
    )


def _validate_opd_replacement(so, warehouse, require_stock=True):
    """Fail-closed validation of OPD's captain-approved free replacement.

    OPD supplies evidence; this internal worker re-verifies it immediately before
    submission.  Any commercial value, address override, changed line, or tampered
    snapshot routes the order back to manual review.
    """
    errors = []
    resolution_id = str(so.get("custom_opd_resolution_id") or "").strip()
    snapshot_raw = so.get("custom_opd_approval_snapshot") or ""
    stored_hash = str(so.get("custom_opd_approval_hash") or "").strip().lower()

    if not resolution_id or str(so.get("po_no") or "").strip() != resolution_id:
        errors.append("resolution_id_or_po_no_mismatch")
    if not so.get("custom_opd_approved_by") or not so.get("custom_opd_approved_at"):
        errors.append("approval_actor_or_time_missing")
    if not snapshot_raw or len(stored_hash) != 64:
        errors.append("approval_snapshot_or_hash_missing")
        snapshot = {}
    else:
        if _canonical_snapshot_hash(snapshot_raw) != stored_hash:
            errors.append("approval_snapshot_hash_mismatch")
        try:
            snapshot = json.loads(snapshot_raw)
        except Exception:
            snapshot = {}
            errors.append("approval_snapshot_invalid_json")

    if snapshot:
        if str(snapshot.get("resolution_id") or "") != resolution_id:
            errors.append("snapshot_resolution_mismatch")
        if str(snapshot.get("approval_status") or "").lower() not in OPD_APPROVAL_STATUSES:
            errors.append("snapshot_not_approved")
        if str(snapshot.get("approved_by") or "") != str(so.get("custom_opd_approved_by") or ""):
            errors.append("snapshot_approver_mismatch")
        snapshot_approved_at = str(snapshot.get("approved_at") or "").replace("T", " ")
        header_approved_at = str(so.get("custom_opd_approved_at") or "").replace("T", " ")
        if snapshot_approved_at[:19] != header_approved_at[:19]:
            errors.append("snapshot_approval_time_mismatch")
        if snapshot.get("alternate_address"):
            errors.append("alternate_address_requires_manual_review")
        if str(snapshot.get("customer") or "") != str(so.get("customer") or ""):
            errors.append("snapshot_customer_mismatch")
        if str(snapshot.get("shipping_address_name") or "") != str(so.get("shipping_address_name") or ""):
            errors.append("snapshot_address_mismatch")
        if _opd_snapshot_lines(snapshot) != _so_snapshot_lines(so):
            errors.append("snapshot_items_mismatch")

    if not so.get("shipping_address_name"):
        errors.append("shipping_address_missing")
    if abs(flt(so.get("net_total"))) > 0.01 or abs(flt(so.get("grand_total"))) > 0.01:
        errors.append("replacement_not_zero_value")
    if so.get("taxes_and_charges") or any(
        abs(flt(row.get("tax_amount"))) > 0.01 for row in (so.get("taxes") or [])
    ):
        errors.append("replacement_tax_template_or_amount_present")
    for row in so.items:
        if not row.get("item_code") or flt(row.get("qty")) <= 0:
            errors.append("invalid_item_or_qty")
        if abs(flt(row.get("rate"))) > 0.01:
            errors.append("non_zero_item_rate")
        if (row.get("gst_treatment") or "") != "Nil-Rated":
            errors.append("item_not_nil_rated")
        if require_stock and row.get("item_code"):
            available = get_available_qty(row.item_code, warehouse)
            if flt(available.get("actual_qty")) < flt(row.qty):
                errors.append("insufficient_stock:{0}".format(row.item_code))
    return sorted(set(errors))


def release_opd_replacements():
    """*/15 internal promoter: approved REP draft -> submitted SO -> submitted DN.

    The feature is dormant until its dedicated setting is enabled.  Every order
    is isolated so one rejected/tampered replacement cannot block the queue.
    """
    try:
        return _release_opd_replacements()
    except Exception:
        frappe.db.rollback()
        _log("OPD Replacement Release", "FATAL (swallowed): " + frappe.get_traceback())
        return None


def _release_opd_replacements():
    settings = _settings()
    if not cint(settings.get("opd_replacement_release_enabled")):
        return None

    limit = cint(settings.get("opd_replacement_batch_size")) or 25
    dry_run = cint(settings.get("opd_replacement_dry_run"))
    warehouse = _source_warehouse(settings)
    require_stock = cint(settings.get("require_stock"))
    rows = frappe.get_all(
        "Sales Order",
        filters={
            "name": ["like", _opd_replacement_prefix(settings) + "%"],
            "custom_opd_auto_fulfill": 1,
            "docstatus": ["in", [0, 1]],
        },
        fields=["name"], order_by="creation asc", limit_page_length=limit,
    )
    existing = _sos_with_existing_dn([r.name for r in rows])
    result = {"eligible": 0, "released": 0, "resumed": 0, "rejected": 0, "failed": 0,
              "delivery_notes": [], "exceptions": []}

    for row in rows:
        so_name = row.name
        if so_name in existing:
            result["resumed"] += 1
            continue
        savepoint = "opdrep_" + so_name.replace("-", "_")[:40]
        try:
            frappe.db.savepoint(savepoint)
            # Serialize scheduler/manual overlap for one resolution. Re-check the
            # DN link after acquiring the lock because the initial bulk lookup may
            # be stale by the time this worker reaches the row.
            frappe.db.get_value("Sales Order", so_name, "name", for_update=True)
            if frappe.db.exists(
                "Delivery Note Item",
                {"against_sales_order": so_name, "docstatus": ["<", 2]},
            ):
                result["resumed"] += 1
                frappe.db.rollback(save_point=savepoint)
                continue
            so = frappe.get_doc("Sales Order", so_name)
            errors = _validate_opd_replacement(so, warehouse, require_stock=require_stock)
            if errors:
                result["rejected"] += 1
                result["exceptions"].append({"so": so_name, "errors": errors})
                frappe.db.rollback(save_point=savepoint)
                continue
            if dry_run:
                result["eligible"] += 1
                frappe.db.rollback(save_point=savepoint)
                continue
            if so.docstatus == 0:
                so.flags.ignore_permissions = True
                so.submit()
            release_result = {"failed": 0, "failures": []}
            dn_name = _make_and_submit_dn(
                so_name, warehouse, release_result, box_count=1,
                is_replacement=True,
                opd_resolution_id=so.get("custom_opd_resolution_id"),
                opd_approval_hash=so.get("custom_opd_approval_hash"),
            )
            if not dn_name:
                raise RuntimeError((release_result.get("failures") or [{"err": "DN creation failed"}])[-1]["err"])
            result["released"] += 1
            result["delivery_notes"].append({"so": so_name, "dn": dn_name})
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            result["failed"] += 1
            result["exceptions"].append({"so": so_name, "errors": [str(exc)[:300]]})

    if result["eligible"] or result["released"] or result["rejected"] or result["failed"]:
        _log("OPD Replacement Release", json.dumps(result)[:8000])
    return result


# ─── RELEASE JOB: eligible SHP SO → Delivery Note ─────────────────

def release_d2c_shipments(force=False):
    """Scheduler entry (*/15). Auto-create + submit Delivery Notes for eligible
    single-AWB Shopify Sales Orders. Idempotent, gated, per-order isolated.

    force=True is the MANUAL pull (Run Release Now button): it bypasses the
    release_enabled pause + the cutoff-hour gate so the warehouse can release a
    batch on demand while auto-release is paused. Every per-order gate and Dry Run
    still apply, and it still releases oldest-first up to Max Orders Per Run.

    The whole body is wrapped so a defect here can NEVER propagate into the
    shared scheduler runner (protects every other app's jobs, incl. Amazon DF)."""
    try:
        return _release_d2c_shipments(force=force)
    except Exception:
        frappe.db.rollback()
        _log("D2C Release", "FATAL (swallowed): " + frappe.get_traceback())
        return None


def _release_d2c_shipments(force=False):
    settings = _settings()
    # force = explicit human pull (Run Release Now); bypasses the auto-release
    # pause so the warehouse can release a batch on demand.
    if not force and not cint(settings.get("release_enabled")):
        return

    # Optional hard stop after the daily cutoff hour (default: ship immediately,
    # so this is off unless enforce_cutoff_on_release is set). Manual pulls ignore it.
    if not force and cint(settings.get("enforce_cutoff_on_release")):
        cutoff = cint(settings.get("cutoff_hour")) or 16
        if get_datetime(now_datetime()).hour >= cutoff:
            return

    result = _run_release(settings, dry_run=cint(settings.get("dry_run")))
    if (
        result["created"] or result["failed"]
        or result["skipped_bad_data"] or result["skipped_broken_ppcod"]
    ):
        _log(
            "D2C Release",
            "created={0} skipped_multibox={1} skipped_nostock={2} "
            "skipped_dn_exists={3} skipped_bad_data={4} skipped_on_hold={5} "
            "skipped_ppcod={6} skipped_broken_ppcod={7} failed={8} dry_run={9}\n"
            "bad_data_sos={10}\nbroken_ppcod_sos={11}\nfailures={12}".format(
                result["created"], result["skipped_multibox"],
                result["skipped_nostock"], result["skipped_dn_exists"],
                result["skipped_bad_data"], result["skipped_on_hold"],
                result["skipped_ppcod"], result["skipped_broken_ppcod"],
                result["failed"], cint(settings.get("dry_run")),
                json.dumps(result["bad_data_sos"][:20]),
                json.dumps(result["broken_ppcod_sos"][:20]),
                json.dumps(result["failures"][:20]),
            ),
        )
    return result


def enqueue_release_range(from_date, to_date):
    """Queue a background release of EVERY order with transaction_date in
    [from_date, to_date] (manual date-range pull). Returns immediately; the job
    releases oldest-first through the whole range and posts a Slack summary when
    done. Bypasses the release_enabled pause; respects Dry Run + all per-order gates."""
    from_date, to_date = str(getdate(from_date)), str(getdate(to_date))
    frappe.enqueue(
        "solara_wms.wms.d2c_fulfillment._release_range_job",
        queue="long", timeout=3600, job_name="d2c_release_range",
        from_date=from_date, to_date=to_date, user=frappe.session.user)
    return {"queued": True, "from_date": from_date, "to_date": to_date}


def _release_range_job(from_date, to_date, user=None):
    """Background worker: drain every releasable order in the date range (looping
    the release job max_orders at a time until the range is empty), then post a
    summary to the wave Slack channel. Runs as Administrator (matches the cron)."""
    frappe.set_user("Administrator")
    settings = _settings()
    dry = cint(settings.get("dry_run"))
    keys = ("created", "skipped_multibox", "skipped_nostock", "skipped_dn_exists",
            "skipped_bad_data", "skipped_on_hold", "skipped_ppcod",
            "skipped_broken_ppcod", "failed")
    agg = {k: 0 for k in keys}
    for _i in range(400):  # safety ceiling (400 * max_orders_per_run)
        res = _run_release(settings, dry_run=dry, from_date=from_date, to_date=to_date)
        for k in keys:
            agg[k] += cint(res.get(k))
        frappe.db.commit()
        if not res["created"]:
            break  # only gated / no-more-releasable orders remain in the range
    held = (agg["skipped_on_hold"] + agg["skipped_ppcod"] + agg["skipped_multibox"]
            + agg["skipped_nostock"])
    lines = [
        ":package: *D2C range release done* — {0} → {1}{2}".format(
            from_date, to_date, " (dry-run)" if dry else ""),
        "Released *{0}* · held {1} (on-hold {2} · PPCOD {3} · multibox {4} · no-stock {5}) · "
        "bad-data {6} · failed {7}".format(
            agg["created"], held, agg["skipped_on_hold"], agg["skipped_ppcod"],
            agg["skipped_multibox"], agg["skipped_nostock"], agg["skipped_bad_data"],
            agg["failed"]),
        "Now run a wave / *Prepare* to print the pick list + labels for the released batch.",
    ]
    _post_slack(settings, "\n".join(lines), tag="D2C Range Release")
    _log("D2C Range Release",
         "range {0}..{1} by {2}: {3}".format(from_date, to_date, user, json.dumps(agg)))


def _candidate_sos(settings, limit, from_date=None, to_date=None):
    """SHP Sales Orders that could be released, before the per-order gates.
    from_date/to_date (manual date-range pull) scope by transaction_date instead of
    the rolling lookback window — 'process everything ordered on Jul 18-19'."""
    lookback = cint(settings.get("lookback_days")) or 3
    filters = {
        "name": ["like", _prefix(settings) + "%"],
        "docstatus": 1,
        "status": ["in", RELEASABLE_STATUSES],
        "per_delivered": 0,
        "skip_delivery_note": 0,
    }
    if from_date and to_date:
        filters["transaction_date"] = ["between", [str(from_date), str(to_date)]]
    else:
        filters["transaction_date"] = [">=", add_days(nowdate(), -lookback)]
    return frappe.get_all(
        "Sales Order",
        filters=filters,
        fields=["name"],
        order_by="transaction_date asc, creation asc",
        limit_page_length=limit,
    )


def _sos_with_existing_dn(so_names):
    """Idempotency: SO names that already have a draft/submitted Delivery Note."""
    if not so_names:
        return set()
    rows = frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_order": ["in", list(so_names)], "docstatus": ["<", 2]},
        fields=["against_sales_order"],
        limit_page_length=0,
    )
    return {r.against_sales_order for r in rows}


def _run_release(settings, dry_run=False, from_date=None, to_date=None):
    limit = cint(settings.get("release_batch_size")) or 200
    max_orders = cint(settings.get("max_orders_per_run")) or limit
    warehouse = _source_warehouse(settings)
    box_map = _box_config(settings)
    require_stock = cint(settings.get("require_stock"))

    candidates = _candidate_sos(settings, limit, from_date=from_date, to_date=to_date)
    already = _sos_with_existing_dn([c.name for c in candidates])

    res = {
        "created": 0, "failed": 0,
        "skipped_multibox": 0, "skipped_nostock": 0, "skipped_dn_exists": 0,
        "skipped_bad_data": 0, "bad_data_sos": [],
        "skipped_on_hold": 0, "skipped_ppcod": 0,
        "skipped_broken_ppcod": 0, "broken_ppcod_sos": [],
        "created_dns": [], "failures": [],
    }

    for cand in candidates:
        if res["created"] >= max_orders:
            break
        so_name = cand.name
        if so_name in already:
            res["skipped_dn_exists"] += 1
            continue

        try:
            so = frappe.get_doc("Sales Order", so_name)
        except Exception as e:
            res["failed"] += 1
            res["failures"].append({"so": so_name, "err": "load: " + str(e)})
            continue

        # Gate 0: malformed data — a line without item_code makes make_delivery_note
        # throw on EVERY retry. Skip and surface so ops fix or cancel the SO.
        if any(not it.item_code for it in so.items):
            res["skipped_bad_data"] += 1
            res["bad_data_sos"].append(so_name)
            continue

        # Gate 0.5: Shopify hold. The DN COD Guard hard-blocks held orders at
        # submit anyway — skipping upstream avoids burning a failed submit every
        # run. Self-healing: the next Shopify sync clears custom_shopify_hold
        # and the order releases on the following run.
        if (
            cint(so.get("custom_shopify_hold"))
            or cint(so.get("custom_shopify_cancellation_hold"))
        ):
            res["skipped_on_hold"] += 1
            continue

        # Gate 0.6: PPCOD exclusion (Phase-1 scope decision, 2026-07-12).
        # Default OFF: PPCOD orders stay on the manual sheet flow so the first
        # automated cohort is prepaid-only (no collect-at-door amount to get
        # wrong). Flip release_ppcod ON in settings to widen — no deploy needed.
        if (
            not cint(settings.get("release_ppcod"))
            and (so.get("custom_order_type") or "") == "PPCOD"
        ):
            res["skipped_ppcod"] += 1
            continue

        # Gate 0.7: broken PPCOD — classified partial-prepaid but COD amount 0.
        # The DN COD Guard hard-blocks these (the courier would be told to
        # collect ₹0). Healthy PPCOD releases normally: the guard auto-syncs
        # COD/prepaid amounts onto the DN. Surfaced so ops fix the SO
        # classification (v11 backfill) instead of it failing every run.
        if (
            (so.get("custom_order_type") or "") == "PPCOD"
            and flt(so.get("custom_cod_amount")) < 0.5
            and flt(so.get("grand_total")) > 0.5
        ):
            res["skipped_broken_ppcod"] += 1
            res["broken_ppcod_sos"].append(so_name)
            continue

        # Gate 1: single-parcel orders only. Nestable accessories count 0 boxes
        # (they ride inside the parent's box); same-category cookware/drinkware
        # stacks into one box; true combos + appliance-with-cookware count 2+.
        # Multi-box orders are Phase 2 territory — they stay on the manual sheet.
        # box_count is also stamped on the DN below and enforced by the AWB guard
        # (a DN can never ship with fewer AWBs than boxes).
        box_count = _order_box_count(so, box_map, settings)
        parcel_plan = None
        if box_count != 1:
            # A multi-box order releases if EITHER:
            #  (a) it is fully covered by a known-splittable combo the ClickPost
            #      script 2-parcels natively (release_known_combos), or
            #  (b) PHASE 2a/2b: its parcel plan is 2..max_release_parcels parcels and
            #      release_multibox_2p is ON — the plan is stamped on the DN and the
            #      ClickPost script executes it (one AWB per box-bearing parcel,
            #      value-weighted COD). Orders needing MORE parcels than the cap stay
            #      on the manual sheet. Complexity is bounded by box-bearing parcels,
            #      not line count, so 0-box riders never force an order off the path.
            covered_by_combo = (cint(settings.get("release_known_combos"))
                                and _fully_covered_by_known_combos(so, box_map, settings))
            max_parcels = cint(settings.get("max_release_parcels")) or 2
            if covered_by_combo or cint(settings.get("release_multibox_2p")):
                parcels = _order_parcels(so, box_map, settings)
                # Up to max_release_parcels boxes auto-release: one AWB per box-bearing
                # parcel; box_count == parcel count (never under-ship) and the plan is
                # stamped so labels list every parcel's full contents.
                if 2 <= len(parcels) <= max_parcels and box_count == len(parcels):
                    parcel_plan = _parcel_plan_for_dn(so, parcels)
            if not covered_by_combo and not parcel_plan:
                res["skipped_multibox"] += 1
                continue

        # Gate 2: physical stock present for every line (actual_qty, not available:
        # this SO's own reservation would otherwise net it out).
        if require_stock:
            short = False
            for it in so.items:
                if flt(it.delivered_qty) >= flt(it.qty):
                    continue
                avail = get_available_qty(it.item_code, warehouse)
                if flt(avail["actual_qty"]) < flt(it.qty):
                    short = True
                    break
            if short:
                res["skipped_nostock"] += 1
                continue

        if dry_run:
            res["created"] += 1
            res["created_dns"].append({"so": so_name, "dn": "(dry_run)"})
            continue

        dn_name = _make_and_submit_dn(so_name, warehouse, res, box_count, parcel_plan)
        if dn_name:
            res["created"] += 1
            res["created_dns"].append({"so": so_name, "dn": dn_name})

    return res


def _make_and_submit_dn(so_name, warehouse, res, box_count=1, parcel_plan=None,
                        is_replacement=False, opd_resolution_id=None,
                        opd_approval_hash=None):
    """One savepoint-isolated SO→DN release. Returns DN name or None."""
    from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

    savepoint = "d2c_" + so_name.replace("-", "_")[:40]
    try:
        frappe.db.savepoint(savepoint)
        dn = make_delivery_note(so_name)
        # Pin warehouse (mapper usually carries SO line warehouse; be explicit).
        dn.set_warehouse = warehouse
        for row in dn.items:
            if not row.warehouse:
                row.warehouse = warehouse
            if is_replacement:
                row.expense_account = REPLACEMENT_COST_ACCOUNT
        # Defer invoicing: this DN is submitted EARLY (before physical dispatch)
        # only to create the AWB + label for packing. Flag it so the LIVE
        # "Auto Create SI on Shopify DN Submit" script skips it — the SHPSI27 is
        # raised later, after dispatch, via the evening D2C Invoice Run. (Manual
        # sheet-flow DNs are unflagged and keep invoicing at submit as before.)
        dn.custom_d2c_defer_si = 1
        if is_replacement:
            dn.is_replacement = 1
            if dn.meta.has_field("custom_opd_resolution_id"):
                dn.custom_opd_resolution_id = opd_resolution_id
            if dn.meta.has_field("custom_opd_approval_hash"):
                dn.custom_opd_approval_hash = opd_approval_hash
        # Expected physical boxes — the AWB guard blocks label/invoice/dispatch if
        # the courier mints fewer AWBs than this, so a multi-box order can never
        # ship a parcel short.
        if dn.meta.has_field("custom_box_count"):
            dn.custom_box_count = box_count
        # Phase 2a: the executable parcel plan for the ClickPost script (2-parcel
        # multibox). Absent for single-box and known-combo releases.
        if parcel_plan and dn.meta.has_field("custom_parcel_plan"):
            dn.custom_parcel_plan = json.dumps(parcel_plan)
        dn.flags.ignore_permissions = True
        dn.insert(ignore_permissions=True)
        dn.submit()  # ← triggers CP AWB + Shopify sync (SI is deferred, see flag)
        frappe.db.commit()
        return dn.name
    except Exception as e:
        frappe.db.rollback(save_point=savepoint)
        res["failed"] += 1
        res["failures"].append({"so": so_name, "err": str(e)[:300]})
        return None


# ─── LABEL FETCH: attach the (expiring) CP label PDF to the DN ─────

def fetch_d2c_labels():
    """Scheduler entry (*/15). Download shipping_label PDFs for recently-submitted
    SHP DNs and attach them as permanent private Files, so prepare_todays_shipments
    never races the presigned-URL expiry. Wrapped so a defect can never wedge the
    shared scheduler runner (protects Amazon DF's jobs on the same bench)."""
    try:
        return _fetch_d2c_labels()
    except Exception:
        frappe.db.rollback()
        _log("D2C Label Fetch", "FATAL (swallowed): " + frappe.get_traceback())
        return None


def _fetch_d2c_labels():
    settings = _settings()
    if not cint(settings.get("label_fetch_enabled")):
        return

    lookback = cint(settings.get("lookback_days")) or 3
    # Auto-invoice a deferred DN the moment its label is on hand (label fetched +
    # DN posted). Ops decision 2026-07-15 — replaces the manual evening run as the
    # default trigger; the D2C Invoice Run screen stays as the manual fallback for
    # any DN whose auto-invoice was off/failed. Toggle: auto_invoice_on_label.
    auto_invoice = cint(settings.get("auto_invoice_on_label"))
    # Push Shopify fulfillment here too (the 'Auto Sync AWB to Shopify' server
    # script is dead in the safe_exec sandbox; connector sync_delivery_note=0).
    # Independent of the label — fulfillment only needs the AWB. Toggle set per-site.
    do_fulfill = cint(settings.get("auto_fulfill_shopify"))
    cp_key = _cp_key(settings)
    cpid_map = _cpid_map(settings)

    fields = ["name", "shipping_label", "awb_number", "courier_partner",
              "custom_d2c_defer_si", "per_billed", "shopify_order_id",
              "custom_shopify_fulfilled", "custom_dispatched", "is_replacement"]
    meta = frappe.get_meta("Delivery Note")
    for f in ("custom_awb_2", "custom_courier_2", "custom_box_count",
              "custom_awb_shortfall", "custom_awb_list",
              "custom_shopify_cancellation_hold"):
        if meta.has_field(f):
            fields.append(f)
    has_shortfall_field = meta.has_field("custom_awb_shortfall")

    # Scope to the automation's OWN deferred DNs that have an AWB (not the manual
    # sheet's DNs). We resolve the label two ways: the presigned URL on the DN if
    # a courier wrote it, else the ClickPost fetch-by-AWB API (Shadowfax etc. only
    # serve the label by AWB — they never write it to the DN). Oldest-first so a
    # backlog drains in order (and a set_value modified-bump can't starve old DNs);
    # a small dedicated cap keeps each */15 run inside the scheduler time budget
    # (each DN can do several Shopify + ClickPost HTTP calls + an SI submit).
    limit = cint(settings.get("label_batch_size")) or 40
    # per_billed < 100 keeps fully-processed DNs out of the candidate set —
    # invoicing is the LAST step of the chain, so anything fully billed is done.
    # Without this, completed old DNs occupy the oldest-first batch slots for the
    # whole lookback window and starve newer DNs (bit us on go-live day: 16-Jul's
    # 100 DNs got no labels because 15-Jul's 59 completed DNs filled batch=25).
    # A billed-but-unfulfilled DN can only arise from a manual out-of-band invoice;
    # the fetch_labels_now button / D2C Invoice Run screen remain the fallback there.
    dns = frappe.get_all(
        "Delivery Note",
        filters={
            "custom_d2c_defer_si": 1,
            "docstatus": 1,
            "posting_date": [">=", add_days(nowdate(), -lookback)],
            "awb_number": ["is", "set"],
            "per_billed": ["<", 100],
        },
        fields=fields,
        order_by="posting_date asc, creation asc",
        limit_page_length=limit,
    )

    import time
    deadline = time.monotonic() + (cint(settings.get("label_time_budget_sec")) or 210)

    fetched = pending = invoiced = inv_failed = fulfilled = ful_failed = errors = 0
    shortfall = 0
    ful_no_fo = 0
    for dn in dns:
        if time.monotonic() > deadline:
            _log("D2C Label Fetch", "time budget hit — processed part of batch, rest next run")
            break
        # Each DN isolated: a per-row error must never abort the whole */15 batch.
        try:
            if cint(dn.get("custom_shopify_cancellation_hold")):
                continue
            # AWB GUARD: a multi-box DN must carry at least box_count AWBs before we
            # do ANYTHING (fulfill/label/invoice). If the courier minted fewer AWBs
            # than boxes, the order is a parcel short — hold it visibly, never ship.
            box_count = cint(dn.get("custom_box_count")) or 1
            if box_count > 1 and len(_awb_courier_pairs(dn)) < box_count:
                shortfall += 1
                if has_shortfall_field and not cint(dn.get("custom_awb_shortfall")):
                    frappe.db.set_value("Delivery Note", dn.name,
                                        "custom_awb_shortfall", 1)
                    frappe.db.commit()
                    _log("D2C AWB Guard", "SHORTFALL {0}: {1} AWB(s) < {2} boxes — held".format(
                        dn.name, len(_awb_courier_pairs(dn)), box_count))
                continue
            # Fulfillment first — needs only the AWB, so it never waits on the label.
            # Never fulfill merely because a label/AWB exists. Shopify
            # fulfillment triggers the customer "shipped" notification, so the
            # dispatch gate is mandatory: manual all-parcel scan or ClickPost
            # first-movement stamp must prove the parcel left the warehouse.
            if (do_fulfill and cint(dn.get("custom_dispatched"))
                    and dn.get("shopify_order_id")
                    and not cint(dn.get("is_replacement"))
                    and not cint(dn.get("custom_shopify_fulfilled"))):
                outcome = _try_fulfill(dn)
                if outcome in ("created", "updated", "repaired", "in_sync"):
                    frappe.db.set_value("Delivery Note", dn.name,
                                        "custom_shopify_fulfilled", 1)
                    frappe.db.commit()
                    fulfilled += 1
                elif outcome == "failed":
                    ful_failed += 1
                elif outcome == "no_open_fo":
                    # Counted + logged: this used to be a SILENT dead end. A DN
                    # landing here has an incomplete Shopify fulfillment we do not
                    # own, so no code path will ever heal it — it needs eyes.
                    ful_no_fo += 1

            has_label = True
            if not _has_attached_label(dn.name):
                has_label = _attach_label_for_dn(dn, cp_key, cpid_map, settings)
                if has_label:
                    fetched += 1
                else:
                    pending += 1
            # Auto-invoice once the label is on hand: deferred, not-yet-billed DNs.
            if (
                auto_invoice and has_label
                and cint(dn.get("custom_d2c_defer_si"))
                and flt(dn.get("per_billed")) < 0.01
            ):
                outcome = _auto_invoice_deferred_dn(dn.name)
                if outcome == "created":
                    invoiced += 1
                elif outcome == "failed":
                    inv_failed += 1
        except Exception as e:
            frappe.db.rollback()
            errors += 1
            _log("D2C Label Fetch", "DN {0}: {1}".format(dn.get("name"), str(e)[:250]))

    if (fetched or pending or invoiced or inv_failed or fulfilled or ful_failed
            or errors or shortfall):
        _log(
            "D2C Label Fetch",
            "attached={0} pending={1} invoiced={2} inv_failed={3} "
            "fulfilled={4} ful_failed={5} errors={6} awb_shortfall={7}".format(
                fetched, pending, invoiced, inv_failed, fulfilled, ful_failed,
                errors, shortfall),
        )
    return {"attached": fetched, "pending": pending,
            "invoiced": invoiced, "inv_failed": inv_failed,
            "fulfilled": fulfilled, "ful_failed": ful_failed,
            "ful_no_fo": ful_no_fo,
            "errors": errors, "awb_shortfall": shortfall}


def _cp_key(settings):
    return (settings.get("clickpost_api_key") or "").strip()


def _cpid_map(settings):
    """courier(lowercase) -> ClickPost cp_id. Seed defaults + settings overrides."""
    m = dict(DEFAULT_CPID_SEED)
    raw = (settings.get("courier_cpid_map") or "").strip()
    if raw:
        try:
            m.update({str(k).strip().lower(): int(v)
                      for k, v in json.loads(raw).items()})
        except Exception:
            pass
    return m


def _awb_courier_pairs(dn):
    """(awb, courier) pairs for a DN. Single parcel -> one pair; 2-box combos ->
    awb_number + custom_awb_2; N-box (Phase 2b) -> the full custom_awb_list JSON,
    which is authoritative when present."""
    raw = dn.get("custom_awb_list")
    if raw:
        try:
            lst = json.loads(raw)
            pairs = [(str(x.get("awb")).strip(), str(x.get("courier") or "").strip())
                     for x in lst if x.get("awb")]
            if pairs:
                seen, out = set(), []
                for a, c in pairs:
                    if a and a not in seen:
                        seen.add(a)
                        out.append((a, c))
                return out
        except Exception:
            _log("D2C AWB", "{0}: custom_awb_list bad JSON — falling back".format(dn.get("name")))
    awbs = [a.strip() for a in str(dn.get("awb_number") or "").split(",") if a.strip()]
    cours = [c.strip() for c in str(dn.get("courier_partner") or "").split(",") if c.strip()]
    if dn.get("custom_awb_2"):
        # Frappe Document supports ``get`` but is not guaranteed to implement
        # mapping subscription.  Keep this helper valid for both live
        # DeliveryNote documents and the plain dicts used by batch builders.
        awbs.append(str(dn.get("custom_awb_2")).strip())
        cours.append(str(dn.get("custom_courier_2") or dn.get("courier_partner") or "").strip())
    seen, pairs = set(), []
    for i, a in enumerate(awbs):
        if a in seen:
            continue
        seen.add(a)
        pairs.append((a, cours[i] if i < len(cours) else (cours[0] if cours else "")))
    return pairs


def _fetch_cp_label_url(awb, cp_id, cp_key):
    """ClickPost label-fetch by AWB (read-only: regenerate=false). Returns the
    presigned label PDF URL, or None."""
    if not (awb and cp_id and cp_key):
        return None
    try:
        import requests
        r = requests.get(CLICKPOST_LABEL_API, params={
            "key": cp_key, "waybill": awb, "cp_id": cp_id, "regenerate": "false",
        }, timeout=25)
        if r.status_code == 200:
            j = r.json()
            if j.get("meta", {}).get("success"):
                return (j.get("result") or {}).get("shipping_label")
    except Exception:
        pass
    return None


def _label_bytes_for_awb(awb, courier, cp_key, cpid_map):
    """One parcel's label PDF bytes via ClickPost fetch-by-AWB: the courier's mapped
    cp_id first, then every known cp_id (covers a blank/unmapped courier). A courier
    ClickPost serves that isn't in the map just needs its cp_id added to
    `courier_cpid_map` in settings — no code change, no API-hammering probes."""
    if not (awb and cp_key):
        return None
    tried = set()
    cp_id = cpid_map.get((courier or "").strip().lower())
    order = ([cp_id] if cp_id else []) + sorted(set(cpid_map.values()) - {cp_id})
    for cid in order:
        tried.add(cid)
        url = _fetch_cp_label_url(awb, cid, cp_key)
        if url:
            b = _download_pdf_bytes(url)
            if b:
                return b
    return None


def _attach_label_for_dn(dn, cp_key, cpid_map, settings):
    """Resolve + attach the label PDF(s) for one DN. Multi-parcel aware: a combo DN
    has 2 AWBs (awb_number + custom_awb_2), so we fetch each parcel's label and
    MERGE them into the single d2c-label-<DN>.pdf File. For a single parcel we fall
    back to the DN's presigned shipping_label URL if the by-AWB fetch found nothing.
    Returns True once a label is attached."""
    import io

    pairs = _awb_courier_pairs(dn)
    contents = []
    for awb, courier in pairs:
        b = _label_bytes_for_awb(awb, courier, cp_key, cpid_map)
        if b:
            contents.append(b)
    # single-parcel fallback: the DN's own presigned URL (couriers that write it back)
    if not contents and dn.get("shipping_label"):
        b = _download_pdf_bytes(dn.shipping_label)
        if b:
            contents.append(b)

    if not contents:
        return False
    # Multi-parcel: attach ONLY when EVERY parcel's label is on hand — otherwise we'd
    # lock the DN (label attached) with one parcel unlabelled, shipping it blind.
    # Wait and retry next run instead.
    if len(pairs) > 1 and len(contents) < len(pairs):
        return False
    if len(contents) == 1:
        return _attach_label_bytes(dn.name, contents[0])

    # merge multiple parcel labels into one PDF
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    for b in contents:
        try:
            writer.append(PdfReader(io.BytesIO(_as_pdf_bytes(b) or b)))
        except Exception:
            pass
    if not writer.pages:
        return _attach_label_bytes(dn.name, contents[0])  # merge failed -> first as-is
    buf = io.BytesIO()
    writer.write(buf)
    return _attach_label_bytes(dn.name, buf.getvalue())


def _try_fulfill(dn):
    """Push fulfillment for one DN, never raising into the job loop."""
    try:
        return push_shopify_fulfillment(dn)
    except Exception as e:
        _log("D2C Fulfill", "{0}: {1}".format(dn.get("name"), str(e)[:250]))
        return "failed"


def fulfill_dispatched_dn(dn_name):
    """Push one physically-dispatched DN to Shopify, idempotently.

    This is the only normal gate for customer-facing Shopify fulfillment.
    Creating an AWB or fetching a label is not dispatch evidence.  Success is
    latched only after ``push_shopify_fulfillment`` confirms the complete
    authoritative AWB set on Shopify.
    """
    settings = _settings()
    if not cint(settings.get("auto_fulfill_shopify")):
        return "disabled"

    dn = frappe.get_doc("Delivery Note", dn_name)
    from solara_wms.wms.shopify_cancellations import delivery_note_cancellation_hold
    if (dn.docstatus != 1
            or not cint(dn.get("custom_d2c_defer_si"))
            or not cint(dn.get("custom_dispatched"))
            or cint(dn.get("custom_shopify_fulfilled"))
            or cint(dn.get("is_replacement"))
            or not dn.get("shopify_order_id")
            or not _awb_courier_pairs(dn)):
        return "skipped"
    if delivery_note_cancellation_hold(dn):
        return "cancellation_hold"

    outcome = _try_fulfill(dn)
    if outcome in ("created", "updated", "repaired", "in_sync"):
        frappe.db.set_value(
            "Delivery Note", dn.name, "custom_shopify_fulfilled", 1,
            update_modified=False,
        )
        frappe.db.commit()
    return outcome


@frappe.whitelist()
def sync_dispatched_shopify_fulfillments(days=14, limit=40):
    """Catch up dispatched DNs that have not yet reached Shopify.

    Runs every five minutes and deliberately includes fully billed DNs; the
    label-fetch job excludes ``per_billed=100`` and therefore cannot recover a
    historical fulfillment backlog.  Bounded oldest-first processing keeps the
    Shopify/API load predictable. Failures remain unlatched for the next tick.
    """
    try:
        settings = _settings()
        if not cint(settings.get("auto_fulfill_shopify")):
            return {"skipped": "auto_fulfill_shopify off"}

        start = add_days(nowdate(), -max(cint(days), 1))
        batch_limit = min(max(cint(limit), 1), 100)
        rows = frappe.get_all(
            "Delivery Note",
            filters={
                "docstatus": 1,
                "custom_d2c_defer_si": 1,
                "custom_dispatched": 1,
                "custom_shopify_fulfilled": 0,
                "shopify_order_id": ["is", "set"],
                "awb_number": ["is", "set"],
                "posting_date": [">=", start],
            },
            fields=["name"],
            order_by="posting_date asc, creation asc",
            limit_page_length=batch_limit,
        )

        counts = {"selected": len(rows), "synced": 0, "failed": 0,
                  "no_open_fo": 0, "skipped": 0}
        for row in rows:
            outcome = fulfill_dispatched_dn(row.name)
            if outcome in ("created", "updated", "repaired", "in_sync"):
                counts["synced"] += 1
            elif outcome == "no_open_fo":
                counts["no_open_fo"] += 1
            elif outcome == "failed":
                counts["failed"] += 1
            else:
                counts["skipped"] += 1

        if rows:
            _log("D2C Dispatch Shopify Sync", json.dumps(counts, sort_keys=True))
        return counts
    except Exception:
        frappe.db.rollback()
        _log("D2C Dispatch Shopify Sync", "FATAL: " + frappe.get_traceback())
        return {"error": True}


def _shopify_json(r):
    """Parsed body ONLY if the call truly succeeded — HTTP 200 with no top-level
    GraphQL `errors`. Shopify returns 200 + {"errors":[{...THROTTLED}], "data":null}
    on cost-based rate limiting; without this a throttled call looks like an empty
    userErrors list and would be mistaken for success. None => treat as failure."""
    try:
        if r.status_code != 200:
            return None
        j = r.json()
        if j.get("errors"):
            return None
        return j
    except Exception:
        return None


def _repair_tracking(dn, headers, gql, fulfillment_id, awbs, urls, carrier):
    """Heal an EXISTING fulfillment of ours that is missing tracking numbers.

    ALWAYS sends the FULL AWB set, never a delta: fulfillmentTrackingInfoUpdateV2
    REPLACES the whole trackingInfo array rather than merging into it. That exact
    behaviour is what let the legacy 2-AWB repush cron destroy parcels 3..N on
    every 3+ box order (133 orders / 150 tracking numbers, 17-Jul..29-Jul-2026,
    surfaced by Shanu on SOL1241948). Success is claimed only when every AWB is
    confirmed present in the response."""
    import requests

    mut = ("mutation($f: ID!, $t: FulfillmentTrackingInput!, $n: Boolean) { "
           "fulfillmentTrackingInfoUpdateV2(fulfillmentId: $f, trackingInfoInput: $t, "
           "notifyCustomer: $n) { fulfillment { id trackingInfo { number } } "
           "userErrors { message } } }")
    payload = {"query": mut, "variables": {
        "f": "gid://shopify/Fulfillment/" + str(fulfillment_id),
        "t": {"numbers": awbs, "urls": urls, "company": carrier},
        "n": True}}
    r = requests.post(gql, data=json.dumps(payload), headers=headers, timeout=30)
    j = _shopify_json(r)
    node = ((j or {}).get("data") or {}).get("fulfillmentTrackingInfoUpdateV2") or {}
    errs = node.get("userErrors", [])
    have = set()
    for t in ((node.get("fulfillment") or {}).get("trackingInfo") or []):
        if t.get("number"):
            have.add(t["number"])
    if j is None or errs or not set(awbs).issubset(have):
        _log("D2C Fulfill", "repair {0} AWB {1}: {2}".format(
            dn.get("name"), ",".join(awbs), errs or "throttled/HTTP/incomplete"))
        return "failed"
    _log("D2C Fulfill", "REPAIRED {0}: tracking now {1}".format(
        dn.get("name"), ",".join(awbs)))
    return "repaired"


def push_shopify_fulfillment(dn):
    """Create the Shopify fulfillment for a DN, so the order shows fulfilled + the
    customer gets the AWB tracking. Idempotent + non-clobbering: returns in_sync if a
    success fulfillment already carries all our AWBs, and never overwrites a
    fulfillment created by another path. Returns created | repaired | in_sync |
    no_open_fo | skipped | failed. App-code replacement for the sandbox-broken 'Auto Sync AWB to
    Shopify' server script (frappe.make_*_request is None in safe_exec). Every
    Shopify call is validated via _shopify_json (rejects throttle/HTTP/errors) so a
    rate-limited response is NEVER mistaken for a successful fulfillment."""
    import requests

    oid = str(dn.get("shopify_order_id") or "")
    # Multi-parcel aware: a combo DN carries 2 AWBs (awb_number + custom_awb_2).
    pairs = _awb_courier_pairs(dn)
    awbs = [a for a, _ in pairs]
    if not (oid and awbs):
        return "skipped"

    # Read shop_url + token from the Shopify Setting doc ATTRIBUTE — exactly like the
    # proven-working blinkit_edi inventory sync. NOTE: get_decrypted_password() reads
    # the __Auth table which holds a stale/different value here and 401s; the live
    # Admin-API token is the doc's `password` field attribute.
    shop = frappe.get_doc("Shopify Setting")
    shop_url = shop.shopify_url
    token = shop.password
    if not (shop_url and token):
        return "skipped"

    carrier_raw = (pairs[0][1] or dn.get("courier_partner") or "Delhivery").strip()
    carrier = SHOPIFY_CARRIER_MAP.get(carrier_raw, carrier_raw)
    # Customer tracking URL = the ClickPost-hosted branded page, per courier
    # (Suresh, 17-Jul): solara.clickpost.ai/?cp_id=<cpid>&waybill=<awb>&security_key=<key>.
    _s = _settings()
    cpid_map = _cpid_map(_s)
    trk_base = (_s.get("clickpost_tracking_base") or "https://solara.clickpost.ai/").rstrip("?")
    trk_key = _s.get("clickpost_tracking_key") or "1d387d97-3d1d-434d-b714-f5b4df706219"
    def _trk(awb_val, courier_val):
        cid = cpid_map.get((courier_val or carrier_raw or "").strip().lower())
        base = trk_base + ("" if trk_base.endswith("?") else "?")
        u = base + "waybill=" + str(awb_val) + "&security_key=" + trk_key
        if cid:
            u = base + "cp_id=" + str(cid) + "&waybill=" + str(awb_val) + "&security_key=" + trk_key
        return u
    urls = [_trk(a, c) for a, c in pairs]
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    gql = "https://" + shop_url + "/admin/api/2024-01/graphql.json"
    # Shopify takes a single number or a numbers[] list on one fulfillment.
    if len(awbs) == 1:
        track_info = {"number": awbs[0], "url": urls[0], "company": carrier}
    else:
        track_info = {"numbers": awbs, "urls": urls, "company": carrier}
    awb = ",".join(awbs)  # for log lines

    # Existing fulfillments. Scan ALL of them (do not break on the first): if any
    # success fulfillment already carries all our AWBs the order is in sync; if the
    # order is fulfilled by some OTHER path whose tracking we don't own, we must NOT
    # clobber it or duplicate — we detect that and stop (no open fulfillment order
    # will exist to create against anyway).
    fr = requests.get(
        "https://" + shop_url + "/admin/api/2024-01/orders/" + oid + "/fulfillments.json",
        headers=headers, timeout=30)
    if fr.status_code != 200:
        _log("D2C Fulfill", "list {0}: HTTP {1}".format(dn.get("name"), fr.status_code))
        return "failed"
    fulfillments = (fr.json() or {}).get("fulfillments", [])
    covered = set()
    repairable = None
    for f in fulfillments:
        if f.get("status") == "success":
            nums = set(f.get("tracking_numbers") or (
                [f["tracking_number"]] if f.get("tracking_number") else []))
            covered |= nums
            # A success fulfillment whose tracking is a SUBSET of ours is one of
            # OURS that came up short (truncated by the legacy 2-AWB repush cron,
            # or created before the last parcel's AWB landed) — safe to repair in
            # place. One carrying tracking we don't own belongs to another path;
            # never touch it. An empty set is a subset: filling in tracking on an
            # untracked fulfillment can't clobber anything.
            if repairable is None and nums.issubset(set(awbs)):
                repairable = f.get("id")
    if awbs and set(awbs).issubset(covered):
        return "in_sync"

    # Create against the order's OPEN fulfillment orders. An already-fulfilled order
    # (by CS/manual/connector) has NO open fulfillment order, so we return without
    # touching the foreign fulfillment.
    foq = ('{ order(id: "gid://shopify/Order/' + oid + '") { fulfillmentOrders(first: 10) '
           '{ edges { node { id status } } } } }')
    r = requests.post(gql, data=json.dumps({"query": foq}), headers=headers, timeout=30)
    jf = _shopify_json(r)
    if jf is None:
        _log("D2C Fulfill", "fo-query {0} AWB {1}: throttled/HTTP".format(dn.get("name"), awb))
        return "failed"
    edges = (((jf.get("data") or {}).get("order") or {}).get(
        "fulfillmentOrders") or {}).get("edges", [])
    open_fos = [(e.get("node") or {}).get("id") for e in edges
                if (e.get("node") or {}).get("status") == "OPEN"]
    if not open_fos:
        # No open FO: the order is already fulfilled (or cancelled). If one of OUR
        # fulfillments is carrying an incomplete tracking set, repair it in place —
        # without this, a short fulfillment could never be healed and the customer
        # never saw parcels 3..N. Anything we don't own is left strictly alone.
        if repairable:
            return _repair_tracking(dn, headers, gql, repairable, awbs, urls, carrier)
        _log("D2C Fulfill", "no open FO for {0} AWB {1} — not ours to repair".format(
            dn.get("name"), awb))
        return "no_open_fo"

    cmut = ("mutation($f: FulfillmentV2Input!) { fulfillmentCreateV2(fulfillment: $f) "
            "{ fulfillment { id status } userErrors { message } } }")
    cpayload = {"query": cmut, "variables": {"f": {
        "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fid} for fid in open_fos],
        "trackingInfo": track_info, "notifyCustomer": True}}}
    r = requests.post(gql, data=json.dumps(cpayload), headers=headers, timeout=30)
    jc = _shopify_json(r)
    node = ((jc or {}).get("data") or {}).get("fulfillmentCreateV2") or {}
    errs = node.get("userErrors", [])
    # Success ONLY if we got a real fulfillment id back (guards throttle/HTTP/errors).
    if jc is None or errs or not (node.get("fulfillment") or {}).get("id"):
        _log("D2C Fulfill", "create {0} AWB {1}: {2}".format(
            dn.get("name"), awb, errs or "throttled/HTTP/no-id"))
        return "failed"
    return "created"


def _auto_invoice_deferred_dn(dn_name):
    """Create + submit the deferred SHPSI27 for one labelled DN, savepoint-isolated
    so one invoice failure never aborts the label-fetch pass. Returns
    'created' | 'skipped' | 'failed'."""
    sp = "d2cinv_" + dn_name.replace("-", "_")[:40]
    try:
        frappe.db.savepoint(sp)
        si_name = create_si_from_deferred_dn(dn_name)
        frappe.db.commit()
        return "created" if si_name else "skipped"
    except Exception as e:
        frappe.db.rollback(save_point=sp)
        _log("D2C Auto Invoice", "{0}: {1}".format(dn_name, str(e)[:300]))
        return "failed"


def create_si_from_deferred_dn(dn_name):
    """Create + submit the SHPSI27 for a dispatched/labelled D2C Delivery Note.
    Single source of truth for deferred D2C invoicing — used by both the
    auto-invoice-on-label hook and the manual D2C Invoice Run screen. Mirrors the
    LIVE 'Auto Create SI on Shopify DN Submit' server script (SHPSI27 series,
    tax-inclusive print rate, Sales - WTBBPL income, payment-schedule re-anchor,
    PPCOD prepaid, submit -> IRN via India Compliance). Returns SI name, or None
    when the DN should not be invoiced (B2B2C, no SHP SO, or already billed)."""
    from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

    doc = frappe.get_doc("Delivery Note", dn_name)

    if (doc.get("custom_order_type") or "") == "B2B2C":
        return None

    so_name = ""
    for item in doc.items:
        if item.get("against_sales_order"):
            so_name = item.against_sales_order
            break
    is_replacement = bool(so_name and so_name.startswith(OPD_REPLACEMENT_PREFIX))
    if not so_name or not (so_name.startswith("SHP") or is_replacement):
        return None

    # Serialize double-invoicing: lock the SO row (FOR UPDATE) before the per_billed
    # check so a concurrent auto-invoice-on-label tick + a manual D2C Invoice Run
    # can't both pass the guard and mint two SHPSI27 / two IRNs for the same DN. The
    # SI submit bumps SO.per_billed inside this txn; the 2nd caller blocks on the
    # lock, then reads per_billed > 0 and bails.
    if flt(frappe.db.get_value("Sales Order", so_name, "per_billed", for_update=True)) > 0:
        return None

    si = make_sales_invoice(source_name=doc.name)
    si.naming_series = SI_SERIES
    si.posting_date = doc.posting_date
    si.set_posting_time = 1

    so_order_type = frappe.db.get_value("Sales Order", so_name, "custom_order_type") or ""
    if so_order_type:
        si.custom_payment_method = so_order_type

    so_prepaid = flt(frappe.db.get_value("Sales Order", so_name, "custom_prepaid_amount"))
    if so_prepaid > 0:
        si.custom_ppcod_prepaid_amount = so_prepaid

    # Re-anchor payment_schedule due dates off the DN posting date.
    for ps in si.payment_schedule:
        ps.due_date = add_days(doc.posting_date, cint(ps.credit_days))
    if si.payment_schedule:
        si.due_date = max(str(ps.due_date) for ps in si.payment_schedule)
    else:
        si.due_date = doc.posting_date

    if is_replacement:
        si.set("taxes", [])
        if si.meta.has_field("custom_opd_resolution_id"):
            si.custom_opd_resolution_id = doc.get("custom_opd_resolution_id")
        if si.meta.has_field("custom_opd_approval_hash"):
            si.custom_opd_approval_hash = doc.get("custom_opd_approval_hash")
    else:
        # Tax-inclusive pricing (matches the Shopify customer-paying total).
        for t in si.taxes:
            t.included_in_print_rate = 1

    # Force Shopify revenue to Sales - WTBBPL (override channel-specific defaults).
    for it in si.items:
        it.income_account = D2C_INCOME_ACCOUNT
        if is_replacement:
            it.rate = 0
            it.gst_treatment = "Nil-Rated"

    si.flags.ignore_permissions = True
    si.insert()
    si.submit()
    return si.name


def _label_file_name(dn_name):
    return "d2c-label-{0}.pdf".format(dn_name)


def _has_attached_label(dn_name):
    return bool(frappe.db.exists(
        "File",
        {"attached_to_doctype": "Delivery Note",
         "attached_to_name": dn_name,
         "file_name": _label_file_name(dn_name)},
    ))


def _download_pdf_bytes(url):
    """Download a label PDF URL -> bytes (HTTP 200, non-empty). None on failure."""
    if not url:
        return None
    try:
        import requests
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200 and resp.content:
            return resp.content
        _log("D2C Label Fetch", "download HTTP {0}".format(resp.status_code))
    except Exception as e:
        _log("D2C Label Fetch", "download: {0}".format(str(e)[:200]))
    return None


def _attach_label_bytes(dn_name, content):
    """Attach label PDF bytes privately to the DN as d2c-label-<DN>.pdf."""
    if not content:
        return False
    try:
        f = frappe.get_doc({
            "doctype": "File",
            "file_name": _label_file_name(dn_name),
            "attached_to_doctype": "Delivery Note",
            "attached_to_name": dn_name,
            "is_private": 1,
            "content": content,
        })
        f.flags.ignore_permissions = True
        f.insert(ignore_permissions=True)
        frappe.db.commit()
        return True
    except Exception as e:
        _log("D2C Label Fetch", "attach {0}: {1}".format(dn_name, str(e)[:300]))
        return False


def _download_and_attach_label(dn_name, url):
    """Download a single label PDF URL and attach it. Best-effort."""
    return _attach_label_bytes(dn_name, _download_pdf_bytes(url))


def _as_pdf_bytes(data):
    """Coerce File content to raw bytes (get_content can return str on some
    storage backends, which breaks PdfReader(BytesIO(...)))."""
    if data is None:
        return None
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("latin-1", "ignore")
    return None


def _read_attached_label(dn_name):
    """Read the attached label File's bytes, robust across storage backends:
    get_content(), then a direct read of the file's full path."""
    fname = frappe.db.get_value(
        "File",
        {"attached_to_doctype": "Delivery Note", "attached_to_name": dn_name,
         "file_name": _label_file_name(dn_name)},
        "name",
    )
    if not fname:
        return None
    fdoc = frappe.get_doc("File", fname)
    try:
        data = _as_pdf_bytes(fdoc.get_content())
        if data:
            return data
    except Exception:
        pass
    try:
        with open(fdoc.get_full_path(), "rb") as fh:
            return fh.read()
    except Exception as e:
        _log("D2C Prepare", "label read {0}: {1}".format(dn_name, str(e)[:200]))
    return None


def _label_pdf_bytes(dn_name, shipping_label_url):
    """Return label PDF bytes: prefer the attached File, fall back to a live download."""
    data = _read_attached_label(dn_name)
    if data:
        return data
    if shipping_label_url:
        try:
            import requests
            resp = requests.get(shipping_label_url, timeout=30)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception:
            pass
    return None


# ─── PREPARE TODAY'S SHIPMENTS: pick list + combined labels ───────

def _prepare_lookback(settings):
    # 0 is a legitimate value (today only); None/unset falls back to 1 so
    # overnight releases (posted yesterday, after the last wave) roll into the
    # morning wave instead of stranding unprinted.
    val = settings.get("prepare_lookback_days")
    return cint(val) if val is not None else 1


def _todays_d2c_dns(settings, on_date):
    """ALL submitted D2C DNs from (on_date - prepare_lookback) through on_date
    (labelled or not). Prepare renders labels first and records only successfully
    rendered DNs in the batch; unlabelled DNs remain unbatched for the next wave.
    Stable pack sequence: batch identical single-SKU orders together, then by AWB."""
    start = add_days(on_date, -_prepare_lookback(settings))
    filters = {
        "docstatus": 1,
        "posting_date": ["between", [start, on_date]],
    }
    # Only the automation's OWN DNs. While the manual sheet coexists, its DNs
    # (already shipped by the floor) must never enter a pick batch / wave email;
    # after the sheet retires every D2C DN carries this flag anyway.
    if frappe.get_meta("Delivery Note").has_field("custom_d2c_defer_si"):
        filters["custom_d2c_defer_si"] = 1
    # AWB guard at the pack choke point: a DN held for an AWB shortfall (fewer AWBs
    # than boxes) is EXCLUDED from the pick batch, so it can never be picked/shipped
    # a parcel short.
    if frappe.get_meta("Delivery Note").has_field("custom_awb_shortfall"):
        filters["custom_awb_shortfall"] = 0
    if frappe.get_meta("Delivery Note").has_field("custom_shopify_cancellation_hold"):
        filters["custom_shopify_cancellation_hold"] = 0
    fields = ["name", "awb_number", "courier_partner", "customer",
              "customer_name", "shopify_order_id", "shopify_order_number",
              "shipping_label", "custom_box_count", "is_replacement"]
    or_filters = {"shopify_order_id": ["is", "set"], "is_replacement": 1}
    # Multi-parcel AWBs. WITHOUT these the pick list can only print awb_number and
    # silently under-reports every 2-4 box order (the labels PDF, which uses
    # _awb_courier_pairs, shows them all) — the sheet then looks like it has
    # "extra duplicate labels" and the floor de-dups real parcels away.
    meta = frappe.get_meta("Delivery Note")
    for f in ("custom_awb_2", "custom_courier_2", "custom_awb_list",
              "custom_opd_resolution_id"):
        if meta.has_field(f):
            fields.append(f)
    dns = frappe.get_all(
        "Delivery Note",
        filters=filters,
        or_filters=or_filters,
        fields=fields,
        limit_page_length=0,
    )
    for dn in dns:
        dn["items"] = frappe.get_all(
            "Delivery Note Item",
            filters={"parent": dn.name},
            fields=["item_code", "item_name", "qty"],
            order_by="idx",
        )
        codes = sorted({i.item_code for i in dn["items"]})
        # Sort key: single-SKU orders grouped by SKU (huge pick/pack win at qty-1),
        # multi-SKU orders after, then AWB for determinism.
        dn["_sortkey"] = (0 if len(codes) == 1 else 1, "|".join(codes), dn.awb_number or "")
    dns.sort(key=lambda d: d["_sortkey"])
    return dns


def _batched_dn_names(on_date, lookback=1):
    """DNs successfully printed in a prior batch within the rolling window.
    A historical child row with label_found=0 did not appear in that batch's
    labels PDF and must not suppress recovery in a later wave."""
    start = add_days(on_date, -lookback)
    batches = frappe.get_all("D2C Prepare Batch",
                             filters={"date": ["between", [start, on_date]]},
                             fields=["name"], limit_page_length=0)
    if not batches:
        return set()
    rows = frappe.get_all(
        "D2C Prepare Batch DN",
        filters={"parent": ["in", [b.name for b in batches]],
                 "parenttype": "D2C Prepare Batch",
                 "label_found": 1},
        fields=["delivery_note"],
        limit_page_length=0,
    )
    return {r.delivery_note for r in rows}


def _label_identity(dn):
    """Stable identifier used by label rendering and missing-label filtering."""
    return (dn.get("shopify_order_number") or dn.get("shopify_order_id")
            or dn.get("custom_opd_resolution_id") or dn["name"])


@frappe.whitelist()
def prepare_todays_shipments(on_date=None, run_type="Ad-hoc", wave_tag=None):
    """Warehouse action. Build (1) a pick-list PDF and (2) a combined-labels PDF
    for the day's NOT-YET-PREPARED D2C Delivery Notes, and record them as a
    D2C Prepare Batch. Batch-aware: clicking again only emits orders that were
    not part of an earlier batch — re-clicks can never re-print already-packed
    orders. Creates no stock/accounting documents; reprints via reprint_batch.
    run_type/wave_tag: "Wave" runs come from the configured scheduled waves
    (run_prepare_waves); the tag makes each wave fire at most once."""
    settings = _settings()
    on_date = getdate(on_date) if on_date else getdate(nowdate())

    all_dns = _todays_d2c_dns(settings, on_date)
    already = _batched_dn_names(on_date, _prepare_lookback(settings))
    candidates = [d for d in all_dns if d["name"] not in already]

    batch_no = len(frappe.get_all("D2C Prepare Batch", filters={"date": on_date},
                                  limit_page_length=0)) + 1
    summary = {
        "date": str(on_date),
        "batch_no": batch_no,
        "run_type": run_type if run_type in ("Ad-hoc", "Wave") else "Ad-hoc",
        "orders": 0,
        "already_prepared": len(already),
        "units": 0,
        "missing_labels": [],
        "pick_list_url": None,
        "labels_pdf_url": None,
        "appliance_pick_list_url": None,
        "appliance_labels_pdf_url": None,
    }
    if not candidates:
        summary["message"] = (
            "No NEW D2C Delivery Notes for {0} — {1} order(s) already covered by "
            "earlier batches. Use reprint_batch to regenerate a past batch."
            .format(on_date, len(already)))
        return summary

    # Generation stamp MMDDHHMM (e.g. 07120900) — names the files and traces the
    # batch to the moment it was made. Only successfully merged labels are
    # recorded in the child table, so each printed AWB is blocked from later
    # batches while an unresolved label remains eligible.
    stamp = now_datetime().strftime("%m%d%H%M")
    summary["batch_stamp"] = stamp

    # Render labels first. Only DNs whose complete label PDF was successfully
    # read and merged are allowed into the pick list or batch child table. This
    # is the hard boundary that makes concurrent fetch/prepare scheduler jobs
    # safe: a label that arrives seconds late remains unbatched and is retried
    # in the next wave instead of being permanently marked as prepared.
    from solara_wms.wms.d2c_appliance_express import express_config
    result = _render_batch_files(candidates, on_date, batch_no, stamp,
                                 pack_lines=cint(settings.get("pack_lines")),
                                 appliance_express=express_config(settings))
    missing = set(result["missing_labels"])
    dns = [d for d in candidates if _label_identity(d) not in missing]
    summary.update(result)
    summary["orders"] = len(dns)
    summary["units"] = sum(flt(i.qty) for d in dns for i in d["items"])

    if not dns:
        summary["message"] = (
            "No label-ready D2C Delivery Notes for {0} — {1} order(s) are still "
            "awaiting complete labels and remain eligible for the next wave."
            .format(on_date, len(result["missing_labels"])))
        return summary

    # Courier split (parcels = labels per carrier) for the email/Slack handover note
    csplit = {}
    for d in dns:
        ck = (d.get("courier_partner") or "—").strip() or "—"
        ce = csplit.setdefault(ck, {"orders": 0, "parcels": 0})
        ce["orders"] += 1
        ce["parcels"] += cint(d.get("custom_box_count")) or 1
    summary["by_courier"] = sorted(
        ({"courier": k, **v} for k, v in csplit.items()), key=lambda x: -x["parcels"])

    batch = frappe.get_doc({
        "doctype": "D2C Prepare Batch",
        "date": on_date,
        "batch_no": batch_no,
        "batch_stamp": stamp,
        "run_type": run_type if run_type in ("Ad-hoc", "Wave") else "Ad-hoc",
        "wave_tag": wave_tag,
        "orders": len(dns),
        "units": summary["units"],
        "pick_list_url": result["pick_list_url"],
        "labels_pdf_url": result["labels_pdf_url"],
        "appliance_pick_list_url": result.get("appliance_pick_list_url"),
        "appliance_labels_pdf_url": result.get("appliance_labels_pdf_url"),
        "output_parts": json.dumps(result["parts"]) if result.get("parts") else None,
        "missing_labels": json.dumps(result["missing_labels"]),
        "delivery_notes": [
            {"delivery_note": d["name"],
             "shopify_order_id": _label_identity(d),
             "awb_number": d.get("awb_number"),
             "label_found": 1}
            for d in dns
        ],
    })
    batch.flags.ignore_permissions = True
    batch.insert(ignore_permissions=True)
    _attach_outputs_to_batch(batch.name, result)
    # Traceability: point each printed DN back to this batch (custom_prepare_batch)
    # so a delivery note traces straight to the wave / pick-list / labels it went
    # out on. Read-only stamp; never bumps the DN's modified time.
    for d in dns:
        try:
            frappe.db.set_value("Delivery Note", d["name"], "custom_prepare_batch",
                                batch.name, update_modified=False)
        except Exception:
            pass
    frappe.db.commit()
    summary["batch"] = batch.name
    # Layer 1: aging tripwire — orders released >12h ago still not printed/dispatched.
    try:
        summary["aging"] = _aging_unshipped(now_datetime())
    except Exception:
        summary["aging"] = []
        _log("D2C Aging", "aging check failed (batch/notify unaffected)\n{0}".format(
            frappe.get_traceback()))
    _email_batch(batch.name, summary, settings)
    _slack_batch(batch.name, summary, settings)
    return summary


def _file_bytes(file_url):
    """Robust read of a just-generated output File (get_content → coerce →
    disk-path fallback; same contract as the label reader from PR #4)."""
    try:
        f = frappe.get_doc("File", {"file_url": file_url})
        content = f.get_content()
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("latin-1", "ignore")
    except Exception:
        pass
    try:
        f = frappe.get_doc("File", {"file_url": file_url})
        with open(f.get_full_path(), "rb") as fh:
            return fh.read()
    except Exception:
        return None


def _email_batch(batch_name, summary, settings):
    """Email the batch's pick-list + labels PDFs to wave_email_recipients.
    Fires on EVERY batch-creating Prepare run (wave or ad-hoc). Best-effort:
    an email failure must never fail the batch itself. Oversized attachments
    (>9 MB total) fall back to a links-only email."""
    raw = (settings.get("wave_email_recipients") or "").strip()
    if not raw:
        return
    recipients = [r.strip() for r in raw.replace(",", "\n").splitlines() if r.strip()]
    if not recipients:
        return
    try:
        attachments = []
        total = 0
        seen_urls = set()
        for url in (summary.get("pick_list_url"), summary.get("labels_pdf_url"),
                    summary.get("appliance_pick_list_url"),
                    summary.get("appliance_labels_pdf_url")):
            if not url:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            content = _file_bytes(url)
            if content:
                total += len(content)
                attachments.append({"fname": url.rsplit("/", 1)[-1], "fcontent": content})
        if total > 9 * 1024 * 1024:
            attachments = []  # links-only; SES bounces oversized mail silently
        missing = summary.get("missing_labels") or []
        by_c = summary.get("by_courier") or []
        courier_note = ("<p><b>By courier:</b> " + " &nbsp;·&nbsp; ".join(
            "{0} <b>{1}</b>".format(c["courier"], c["parcels"]) for c in by_c) + "</p>") if by_c else ""
        parts = summary.get("parts") or []
        express_part = next((p for p in parts if p.get("kind") == "express"), None)
        normal_parts = [p for p in parts if p.get("kind") == "packing_line"]
        parts_note = ""
        if parts:
            rows = "".join(
                "<li><b>{station}</b>: orders {a}&ndash;{b} ({o} orders / {pc:g} pcs)</li>"
                .format(station=pt.get("name") or "Line " + str(pt["part"]),
                        a=pt["order_range"][0], b=pt["order_range"][1],
                        o=pt["orders"], pc=pt.get("pieces") or 0)
                for pt in parts)
            parts_note = ("<p><b>Print packages:</b> Appliance Express is a separate "
                          "pick-list/labels pair. Lines 1&ndash;6 remain together in the "
                          "Normal Packing pair, with black divider pages between lines.</p>"
                          "<ul>{0}</ul>".format(rows))
        package_links = []
        if normal_parts or not express_part:
            package_links.append(
                "<li><b>Normal Packing</b>: <a href='{0}/reconciliation/label-status/"
                "batch/{1}/picklist.pdf'>pick list</a> &middot; <a href='{0}/"
                "reconciliation/label-status/batch/{1}/labels.pdf'>labels</a></li>"
                .format(LABEL_DASHBOARD_BASE, batch_name))
        if express_part:
            package_links.append(
                "<li><b>Appliance Express</b>: <a href='{0}/reconciliation/label-status/"
                "batch/{1}/picklist-express.pdf'>pick list</a> &middot; <a href='{0}/"
                "reconciliation/label-status/batch/{1}/labels-express.pdf'>labels</a></li>"
                .format(LABEL_DASHBOARD_BASE, batch_name))
        package_links = "<ul>{0}</ul>".format("".join(package_links))
        body = (
            "<p><b>D2C dispatch batch {batch}</b> — {orders} orders / {units:g} units "
            "({run_type}, {date}, stamp {stamp})</p>"
            "{courier_note}"
            "{package_links}"
            "{parts_note}"
            "{attach_note}{missing_note}"
            "<p>Ship these from the Atlas batch and SKIP them on the manual sheet.</p>"
        ).format(
            parts_note=parts_note,
            package_links=package_links,
            courier_note=courier_note,
            batch=batch_name, orders=summary.get("orders"), units=summary.get("units") or 0,
            run_type=summary.get("run_type") or "", date=summary.get("date"),
            stamp=summary.get("batch_stamp"), dash=LABEL_DASHBOARD_BASE,
            pl_name=(summary.get("pick_list_url") or "").rsplit("/", 1)[-1],
            lb_name=(summary.get("labels_pdf_url") or "").rsplit("/", 1)[-1],
            attach_note="<p>Both PDFs attached (dashboard links below work too).</p>" if attachments else
                        "<p>PDFs too large to attach — open via the dashboard links above "
                        "(sign in with your @solara.in Google account).</p>",
            missing_note="<p>⚠ {0} order(s) missing a label at prepare time — they will "
                         "surface in the next batch once labelled.</p>".format(len(missing)) if missing else "",
        )
        frappe.sendmail(
            recipients=recipients,
            subject="D2C batch {0} — {1} orders ({2})".format(
                batch_name, summary.get("orders"), summary.get("run_type") or "Ad-hoc"),
            message=body,
            attachments=attachments or None,
        )
        _log("D2C Prepare Email", "batch {0} emailed to {1} ({2} attachment(s), {3} KB)".format(
            batch_name, len(recipients), len(attachments), total // 1024))
    except Exception:
        _log("D2C Prepare Email", "batch {0}: send failed (batch itself is fine)\n{1}".format(
            batch_name, frappe.get_traceback()))


def _slack_batch(batch_name, summary, settings):
    """Post the wave 'labels ready' notification to Slack (Incoming Webhook).
    Runs off the site's email relay, so the team is notified even when email is
    failing (the email_delivery_service relay 504'd every wave on 19-Jul).
    Best-effort — a Slack failure must never fail the batch or block the email."""
    try:
        webhook = (settings.get_password("wave_slack_webhook", raise_exception=False) or "").strip()
    except Exception:
        webhook = ""
    if not webhook:
        return
    import requests
    dash = LABEL_DASHBOARD_BASE
    missing = summary.get("missing_labels") or []
    parts = summary.get("parts") or []
    express_part = next((p for p in parts if p.get("kind") == "express"), None)
    normal_parts = [p for p in parts if p.get("kind") == "packing_line"]
    lines = [
        ":package: *D2C {0} wave — labels ready*".format(summary.get("run_type") or "Ad-hoc"),
        "Batch *{0}* · *{1} orders* / {2:g} units · stamp {3}".format(
            batch_name, summary.get("orders"), summary.get("units") or 0,
            summary.get("batch_stamp")),
    ]
    if normal_parts or not express_part:
        lines.extend([
            "• *Normal Packing* · <{0}/reconciliation/label-status/batch/{1}/"
            "picklist.pdf|:clipboard: Pick list> · <{0}/reconciliation/label-status/"
            "batch/{1}/labels.pdf|:label: Labels>".format(dash, batch_name),
        ])
    if express_part:
        lines.extend([
            "• *Appliance Express* · <{0}/reconciliation/label-status/batch/{1}/"
            "picklist-express.pdf|:clipboard: Pick list> · <{0}/reconciliation/"
            "label-status/batch/{1}/labels-express.pdf|:label: Labels>"
            .format(dash, batch_name),
        ])
    lines.append("Ship from the Atlas batch and *skip them on the manual sheet*.")
    if parts:
        lines.append("*Separate print packages:* Appliance Express is independent; "
                     "Normal Packing keeps divider pages between Lines 1–6. "
                     + "  ·  ".join(
                         "{station} #{a}–{b} ({pc:g} pcs)".format(
                             station=pt.get("name") or "Line " + str(pt["part"]),
                             a=pt["order_range"][0],
                             b=pt["order_range"][1], pc=pt.get("pieces") or 0)
                         for pt in parts))
    by_c = summary.get("by_courier") or []
    if by_c:
        lines.insert(2, "*By courier:* " + "  ·  ".join(
            "{0} *{1}*".format(c["courier"], c["parcels"]) for c in by_c))
    if missing:
        lines.append(":warning: {0} order(s) awaiting a label at prepare time — "
                     "they appear in the next wave once labelled.".format(len(missing)))
    # Layer 1 aging tripwire — orders released >12h ago and STILL not shipped. Unlike
    # 'awaiting a label' (normal same-wave in-flight), these are at risk of slipping;
    # surface them so the floor manually checks (the completeness monitor + recovery
    # net are the automated backstops).
    aging = summary.get("aging") or []
    if aging:
        shown = ", ".join(aging[:10]) + (" …" if len(aging) > 10 else "")
        lines.append(":rotating_light: {0} order(s) released >{1}h ago still NOT shipped — "
                     "please check manually: {2}".format(len(aging), AGING_UNSHIPPED_HOURS, shown))
    try:
        resp = requests.post(webhook, json={"text": "\n".join(lines)}, timeout=15)
        _log("D2C Prepare Slack", "batch {0}: slack {1} ({2})".format(
            batch_name, "posted" if resp.status_code == 200 else "FAILED", resp.status_code))
    except Exception:
        _log("D2C Prepare Slack", "batch {0}: slack post failed (batch/email unaffected)\n{1}".format(
            batch_name, frappe.get_traceback()))


def _attach_outputs_to_batch(batch_name, result):
    """Link the generated pick-list/labels File docs to the batch record so they
    show in its attachments sidebar and can't be swept by the same-name overwrite
    in _save_output_file (which skips attached files)."""
    urls = [result.get("pick_list_url"), result.get("labels_pdf_url"),
            result.get("appliance_pick_list_url"),
            result.get("appliance_labels_pdf_url")]
    for p in result.get("parts") or []:
        urls += [p.get("pick_list_url"), p.get("labels_pdf_url")]
    for url in urls:
        if not url:
            continue
        for f in frappe.get_all("File", filters={"file_url": url,
                                                 "attached_to_name": ["is", "not set"]},
                                fields=["name"]):
            frappe.db.set_value("File", f.name,
                                {"attached_to_doctype": "D2C Prepare Batch",
                                 "attached_to_name": batch_name})


def _prepare_wave_slots(raw):
    """Parse comma-separated site-time wave slots.

    Plain hours remain compatible (``9,12,15``); ``HH:MM`` adds quarter-hour
    starts such as ``8:30``. A slot remains active for the rest of its hour so
    the */15 scheduler can retry an empty or briefly delayed first tick.
    """
    slots = set()
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            hour, minute = (int(part.strip()) for part in token.split(":", 1))
        else:
            hour, minute = int(token), 0
        if hour not in range(24) or minute not in (0, 15, 30, 45):
            raise ValueError("wave slots must be an hour or a 15-minute boundary")
        slots.add((hour, minute))
    if not slots:
        raise ValueError("at least one wave slot is required")
    return sorted(slots)


def run_prepare_waves():
    """Scheduler entry (*/15). Fires Prepare at configured site-time slots
    (e.g. 8:30,12,15). Idempotent per slot via wave_tag: a wave that already
    produced a batch never re-fires; an EMPTY wave creates no batch and re-checks
    on later */15 ticks in that slot's hour, then lapses. Wrapped so a defect can
    never wedge the shared scheduler (same contract as the other D2C jobs)."""
    try:
        settings = _settings()
        if not cint(settings.get("prepare_waves_enabled")):
            return
        raw = (settings.get("prepare_wave_hours") or "9,12,15")
        try:
            slots = _prepare_wave_slots(raw)
        except Exception:
            _log("D2C Prepare Wave", "prepare_wave_hours is invalid — using 9,12,15")
            slots = [(9, 0), (12, 0), (15, 0)]
        now = now_datetime()
        active = [(hour, minute) for hour, minute in slots
                  if now.hour == hour and now.minute >= minute]
        if not active:
            return
        hour, minute = active[-1]
        tag = "{0}-{1:02d}{2:02d}".format(
            now.strftime("%Y-%m-%d"), hour, minute)
        if frappe.get_all("D2C Prepare Batch", filters={"wave_tag": tag}, limit_page_length=1):
            return  # this wave already produced its batch
        summary = prepare_todays_shipments(run_type="Wave", wave_tag=tag)
        if summary.get("batch"):
            _log("D2C Prepare Wave", "wave {0} → batch {1}: {2} orders, labels={3}".format(
                tag, summary["batch"], summary.get("orders"),
                "missing " + str(len(summary.get("missing_labels") or [])) if summary.get("missing_labels") else "complete"))
    except Exception:
        frappe.db.rollback()
        _log("D2C Prepare Wave", "FATAL (swallowed): " + frappe.get_traceback())


@frappe.whitelist()
def reprint_batch(batch_name):
    """Regenerate the pick list + labels PDF for an existing batch, exactly as
    originally scoped (same DN set). No new batch is created."""
    batch = frappe.get_doc("D2C Prepare Batch", batch_name)
    settings = _settings()
    dns = _todays_d2c_dns(settings, batch.date)
    wanted = {r.delivery_note for r in batch.delivery_notes}
    dns = [d for d in dns if d["name"] in wanted]
    # Reuse the original stamp so a reprint regenerates the SAME filenames.
    stamp = batch.get("batch_stamp") or now_datetime().strftime("%m%d%H%M")
    from solara_wms.wms.d2c_appliance_express import express_config
    result = _render_batch_files(dns, batch.date, batch.batch_no, stamp,
                                 pack_lines=cint(settings.get("pack_lines")),
                                 appliance_express=express_config(settings))
    # A reprint used to leave its output ORPHANED, which broke the batch it was
    # meant to repair (2026-07-28):
    #   * _save_output_file only sweeps prior same-name files that are UNATTACHED,
    #     so the original stays and the reprint lands as a SECOND File doc.
    #   * unchanged content (e.g. the labels PDF) reuses the same file_url, so the
    #     new unattached doc SHADOWS the attached one — Frappe then resolves the
    #     path to a file attached to nothing and permission falls back to
    #     owner-only, i.e. 403 for the warehouse dashboard.
    #   * changed content (e.g. a re-rendered pick list) gets a suffixed url that
    #     nothing pointed at, so the dashboard kept serving the STALE pdf.
    # Attaching both, then re-pointing the batch, makes a reprint a true drop-in
    # replacement: the existing Slack/email/dashboard links resolve to the new file.
    _attach_outputs_to_batch(batch.name, result)
    updates = {}
    if result.get("pick_list_url"):
        updates["pick_list_url"] = result["pick_list_url"]
    if result.get("labels_pdf_url"):
        updates["labels_pdf_url"] = result["labels_pdf_url"]
    # These two must also be cleared when a supervisor reprints after Express
    # has been switched off; otherwise the batch would retain stale package links.
    updates["appliance_pick_list_url"] = result.get("appliance_pick_list_url")
    updates["appliance_labels_pdf_url"] = result.get("appliance_labels_pdf_url")
    if result.get("parts"):
        updates["output_parts"] = json.dumps(result["parts"])
    if updates:
        frappe.db.set_value("D2C Prepare Batch", batch.name, updates,
                            update_modified=False)
        frappe.db.commit()
    return {"batch": batch.name, "batch_stamp": stamp,
            "orders": result["labelled"], "requested_orders": len(dns), **result}


def _dn_pieces(d):
    """Physical piece count of one DN (post _enrich_physical_lines)."""
    return sum(flt(l["qty"]) for l in d.get("_lines") or [])


def _partition_for_pack_lines(dns, n):
    """Split an already-sorted DN list into n CONTIGUOUS chunks for the packing
    lines, balanced by physical pieces (a combo is slower to pack than three
    qty-1 orders of the same count of rows). Contiguity is load-bearing: it
    preserves the single-SKU assembly-line runs the sort key builds and keeps a
    multi-box order's labels inside one line's stack. After the greedy cut, the
    boundary slides forward up to 3 orders to finish the current SKU run
    (_sortkey[1]) so a run isn't split across two lines. Every chunk is
    non-empty while orders remain; n > len(dns) degenerates gracefully."""
    n = cint(n)
    if n <= 1 or len(dns) <= 1:
        return [list(dns)] if dns else []
    n = min(n, len(dns))
    total = sum(_dn_pieces(d) for d in dns) or len(dns)
    chunks, start, used = [], 0, 0.0
    for part in range(n):
        left = n - part
        if part == n - 1:
            chunks.append(dns[start:])
            break
        fair = (total - used) / left
        acc, i = 0.0, start
        while i < len(dns) - left + 1 and acc < fair:
            acc += _dn_pieces(dns[i]) or 1
            i += 1
        # finish the current SKU run if its boundary is within 3 more orders
        def _run(d):
            return d.get("_sortkey", ("", "", ""))[1] if d.get("_sortkey") else None
        slid = 0
        while (i < len(dns) - left + 1 and slid < 3
               and _run(dns[i]) is not None and _run(dns[i]) == _run(dns[i - 1])):
            acc += _dn_pieces(dns[i]) or 1
            i += 1
            slid += 1
        chunks.append(dns[start:i])
        used += acc
        start = i
    return [c for c in chunks if c]


def _render_batch_files(dns, on_date, batch_no, stamp, pack_lines=0,
                        appliance_express=None):
    # Labels are the admission control for a batch. Build them first, then make
    # the pick list from the exact same successfully-labelled subset so the two
    # PDFs can never disagree and an unlabelled DN is never marked prepared.
    labels_url, missing = _build_combined_labels_pdf(dns, on_date, batch_no, stamp)
    missing_set = set(missing)
    printable = [d for d in dns if _label_identity(d) not in missing_set]
    # Appliance Express is a genuinely separate physical print package. Normal
    # Lines 1..N retain one pick list + one label stack with divider pages. This
    # prevents an Express label from being handed to a normal table while still
    # avoiding six separate printer jobs for the normal floor.
    parts, normal_parts = [], []
    express_dns, normal_dns = [], printable
    express_pick_url = express_labels_url = None
    express_on = bool(appliance_express and appliance_express.get("enabled"))
    if printable and (cint(pack_lines) > 1 or express_on):
        _enrich_physical_lines(printable)   # piece counts for balancing (idempotent)
        if express_on:
            from solara_wms.wms.d2c_appliance_express import classify_dn
            express_dns, normal_dns = [], []
            for d in printable:
                d["_awb_pairs"] = _awb_courier_pairs(d)
                (express_dns if classify_dn(d, appliance_express).get("eligible")
                 else normal_dns).append(d)

        if express_dns:
            # When the whole wave is Express, reuse the admission label file as
            # the Express file and legacy batch link. Mixed waves get a distinct
            # filename so the two printer packages can never be confused.
            if normal_dns:
                express_labels_url, _ = _build_combined_labels_pdf(
                    express_dns, on_date, batch_no, stamp,
                    file_suffix="-appliance-express")
            else:
                express_labels_url = labels_url
            express_pick_url = _build_pick_list_pdf(
                express_dns, on_date, batch_no, stamp,
                part_label="Appliance Express",
                file_suffix="-appliance-express" if normal_dns else "",
                prekit_bundles=appliance_express.get("prekit_bundles"))
            parts.append({
                "part": 0, "name": "Appliance Express", "kind": "express",
                "orders": len(express_dns),
                "pieces": sum(_pick_list_piece_count(
                    d, appliance_express.get("prekit_bundles"))
                    for d in express_dns),
                "order_range": [1, len(express_dns)],
                "pick_list_url": express_pick_url,
                "labels_pdf_url": express_labels_url,
                "_names": [d["name"] for d in express_dns],
            })

        pos = 1
        for i, chunk in enumerate(_partition_for_pack_lines(normal_dns, pack_lines), 1):
            normal_parts.append({
                "part": i, "name": "Line " + str(i), "kind": "packing_line",
                "orders": len(chunk),
                "pieces": sum(_dn_pieces(d) for d in chunk),
                "order_range": [pos, pos + len(chunk) - 1],
                "_names": [d["name"] for d in chunk],
            })
            pos += len(chunk)
        parts.extend(normal_parts)

        if normal_dns:
            # Re-merge only the normal floor labels, with Line 1..N divider
            # pages. Express labels are in their own PDF above.
            labels_url, _ = _build_combined_labels_pdf(
                normal_dns, on_date, batch_no, stamp,
                parts=normal_parts or None)
            pick_url = _build_pick_list_pdf(
                normal_dns, on_date, batch_no, stamp,
                parts=normal_parts or None)
        else:
            # Preserve the established batch links for an Express-only wave.
            labels_url, pick_url = express_labels_url, express_pick_url
    else:
        pick_url = (_build_pick_list_pdf(printable, on_date, batch_no, stamp)
                    if printable else None)
    # output_parts is persisted for attribution (CS ticket → order # → line);
    # strip the internal DN name list from what gets stored/notified.
    public_parts = [{k: v for k, v in p.items() if k != "_names"} for p in parts]
    return {
        "pick_list_url": pick_url,
        "labels_pdf_url": labels_url,
        "appliance_pick_list_url": express_pick_url,
        "appliance_labels_pdf_url": express_labels_url,
        "missing_labels": missing,
        "labelled": len(express_dns) + len(normal_dns),
        "parts": public_parts,
    }


def _enrich_physical_lines(dns):
    """Attach dn['_lines'] = the PHYSICAL pieces to pick/pack (Product Bundle DN
    rows exploded into their Packed Item components, each tagged with its bundle
    code) and dn['_service'] = nothing-to-pack rows (non-stock lines such as
    extended warranty). DN item rows themselves are untouched — pack sequence,
    labels and batch records are unaffected. Pick-list rendering only."""
    names = [d["name"] for d in dns]
    if not names:
        return
    packed = frappe.get_all(
        "Packed Item",
        filters={"parent": ["in", names], "parenttype": "Delivery Note"},
        fields=["parent", "parent_item", "item_code", "item_name", "qty"],
        order_by="idx",
        limit_page_length=0,
    )
    by_dn = {}
    for p in packed:
        by_dn.setdefault(p.parent, {}).setdefault(p.parent_item, []).append(p)
    codes = sorted({i.item_code for d in dns for i in d["items"]})
    stock = {}
    if codes:
        for r in frappe.get_all("Item", filters={"item_code": ["in", codes]},
                                fields=["item_code", "is_stock_item"],
                                limit_page_length=0):
            stock[r.item_code] = cint(r.is_stock_item)
    for d in dns:
        packs = by_dn.get(d["name"], {})
        lines, service = [], []
        for it in d["items"]:
            comps = packs.get(it.item_code)
            if comps:
                for c in comps:
                    lines.append({"item_code": c.item_code, "item_name": c.item_name,
                                  "qty": flt(c.qty), "bundle": it.item_code})
            elif stock.get(it.item_code, 1):
                lines.append({"item_code": it.item_code, "item_name": it.item_name,
                              "qty": flt(it.qty), "bundle": None})
            else:
                service.append(it)
        d["_lines"] = lines
        d["_service"] = service


def _pick_list_lines(dn, prekit_bundles=None):
    """Return the lines the floor must physically pick for one order.

    Normal Product Bundles stay exploded to components. For Appliance Express,
    the explicitly approved pre-kitted bundle is already sealed inside one
    appliance carton. Showing its Packed Item components as loose pick lines
    would tell the floor to pick the same accessories twice. Collapse only that
    approved bundle to its parent SKU for document rendering; the DN, Packed
    Items, inventory accounting, labels and scan-time checks remain untouched.
    """
    lines = list(dn.get("_lines") or [])
    approved = {str(code).strip().upper() for code in (prekit_bundles or [])
                if str(code).strip()}
    bundles = {str(line.get("bundle") or "").strip().upper() for line in lines
               if line.get("bundle")}
    if (len(bundles) != 1 or next(iter(bundles)) not in approved
            or any(not line.get("bundle") for line in lines)):
        return lines

    bundle = next(iter(bundles))
    parent = next((it for it in dn.get("items") or []
                   if str(it.get("item_code") or "").strip().upper() == bundle), None)
    qty = flt(parent.get("qty")) if parent else 1
    return [{
        "item_code": bundle,
        "item_name": ((parent.get("item_name") if parent else None) or bundle),
        "qty": qty or 1,
        "bundle": None,
        "pre_kitted": True,
    }]


def _pick_list_piece_count(dn, prekit_bundles=None):
    return sum(flt(line["qty"])
               for line in _pick_list_lines(dn, prekit_bundles))


def _sku_summary(dns, prekit_bundles=None):
    # Aggregates the floor-facing pick lines. Normal bundles appear as physical
    # components; approved Express pre-kits remain one parent carton. Service
    # rows are excluded. _enrich_physical_lines must have run.
    agg = {}
    for d in dns:
        for it in _pick_list_lines(d, prekit_bundles):
            row = agg.setdefault(it["item_code"], {"item_name": it["item_name"], "qty": 0})
            row["qty"] += flt(it["qty"])
    return sorted(
        ({"item_code": k, **v} for k, v in agg.items()),
        key=lambda r: r["item_code"],
    )


def _build_pick_list_pdf(dns, on_date, batch_no, stamp, part_label=None, parts=None,
                         file_suffix="", prekit_bundles=None):
    from frappe.utils.pdf import get_pdf

    _enrich_physical_lines(dns)
    sku_rows = _sku_summary(dns, prekit_bundles)
    total_pieces = sum(r["qty"] for r in sku_rows)

    def esc(s):
        return frappe.utils.escape_html(str(s or ""))

    # Drawn checkbox (span, not a unicode glyph — guaranteed to render in wkhtmltopdf)
    chk = ("<span style='display:inline-block;width:7px;height:7px;"
           "border:1px solid #222;margin-right:4px'></span>")

    # The four launch lines use one visual language from the SKU segregation
    # matrix through every line's pack section. Extra future lines fall back to
    # distinct accessible colours instead of silently reverting to monochrome.
    line_themes = [
        {"name": "BLUE", "dark": "#1559a6", "light": "#dceeff"},
        {"name": "GREEN", "dark": "#207a3d", "light": "#dff5e3"},
        {"name": "YELLOW", "dark": "#986b00", "light": "#fff4c7"},
        {"name": "RED", "dark": "#a32d2d", "light": "#ffe0e0"},
        {"name": "PURPLE", "dark": "#6941c6", "light": "#eee8ff"},
        {"name": "TEAL", "dark": "#087e8b", "light": "#d9f4f5"},
    ]

    def line_theme(part_index):
        return line_themes[(max(cint(part_index), 1) - 1) % len(line_themes)]

    def awb_cell(d):
        """EVERY parcel's AWB, one per line, numbered when there is more than one.
        Printing only awb_number here is what made multi-box orders look like they
        had duplicate labels (labels PDF renders one page per AWB)."""
        pairs = _awb_courier_pairs(d)
        if not pairs:
            return ""
        if len(pairs) == 1:
            return esc(pairs[0][0])
        return "<br>".join(
            "<b>{0}/{1}</b> {2}".format(i + 1, len(pairs), esc(a))
            for i, (a, _c) in enumerate(pairs)
        )

    if parts:
        by_name = {d["name"]: d for d in dns}
        matrix_parts = []
        matrix = {}
        for pt in parts:
            chunk = [by_name[n] for n in pt.get("_names") or [] if n in by_name]
            if not chunk:
                continue
            rows = _sku_summary(chunk, prekit_bundles)
            quantities = {row["item_code"]: row["qty"] for row in rows}
            matrix_parts.append((pt, quantities))
            for row in rows:
                matrix.setdefault(row["item_code"], row["item_name"])

        line_headers = "".join(
            "<th style='background:{dark};color:#fff;text-align:center'>"
            "{station}<br><span style='font-size:8px'>{colour}</span></th>".format(
                dark=line_theme(pt.get("part"))["dark"],
                station=esc(pt.get("name") or "Line " + str(pt.get("part"))),
                colour=line_theme(pt.get("part"))["name"])
            for pt, _quantities in matrix_parts
        )
        matrix_rows = []
        for sku in sorted(matrix):
            qty_cells = "".join(
                "<td style='text-align:center;background:{light};font-weight:bold'>{qty}</td>"
                .format(light=line_theme(pt.get("part"))["light"],
                        qty=("{0:g}".format(quantities.get(sku, 0))
                             if quantities.get(sku, 0) else "&ndash;"))
                for pt, quantities in matrix_parts
            )
            total = sum(quantities.get(sku, 0) for _pt, quantities in matrix_parts)
            matrix_rows.append(
                "<tr><td>{sku}</td><td>{item}</td>{qty_cells}"
                "<td style='text-align:center;font-weight:bold'>{total:g}</td><td></td></tr>"
                .format(sku=esc(sku), item=esc(matrix[sku]), qty_cells=qty_cells,
                        total=total))
        line_totals = "".join(
            "<td style='text-align:center;background:{light};font-weight:bold'>{qty:g}</td>"
            .format(light=line_theme(pt.get("part"))["light"],
                    qty=sum(quantities.values()))
            for pt, quantities in matrix_parts
        )
        pick_table = (
            "<table border='1' cellspacing='0' cellpadding='4' width='100%' "
            "style='border-collapse:collapse;font-size:9px'>"
            "<thead><tr style='background:#101828;color:#fff'>"
            "<th align='left'>SKU</th><th align='left'>Item</th>{line_headers}"
            "<th>Total</th><th>Picker<br>initial</th></tr></thead>"
            "<tbody>{rows}<tr style='background:#fafafa;font-weight:bold'>"
            "<td colspan='2' align='right'>Total pieces by line</td>{line_totals}"
            "<td style='text-align:center'>{units:g}</td><td></td></tr></tbody></table>"
        ).format(line_headers=line_headers, rows="".join(matrix_rows),
                 line_totals=line_totals, units=total_pieces)
    else:
        pick_rows = "".join(
            "<tr><td>{0}</td><td>{1}</td><td style='text-align:right'>{2:g}</td></tr>".format(
                esc(r["item_code"]), esc(r["item_name"]), r["qty"])
            for r in sku_rows
        )
        pick_table = (
            "<table border='1' cellspacing='0' cellpadding='4' width='100%' "
            "style='border-collapse:collapse'>"
            "<thead><tr style='background:#f0f0f0'>"
            "<th align='left'>SKU</th><th align='left'>Item</th>"
            "<th align='right'>Qty</th></tr></thead>"
            "<tbody>{rows}<tr style='background:#fafafa;font-weight:bold'>"
            "<td colspan='2' align='right'>Total pieces</td>"
            "<td align='right'>{units:g}</td></tr></tbody></table>"
        ).format(rows=pick_rows, units=total_pieces)

    def content_cell(d):
        parts = []
        for ln in _pick_list_lines(d, prekit_bundles):
            of = (" <span style='color:#999'>(of {0})</span>".format(esc(ln["bundle"]))
                  if ln["bundle"] else "")
            prekit = (" <b style='color:#166534'>(PRE-KITTED — do not pick accessories separately)</b>"
                      if ln.get("pre_kitted") else "")
            parts.append("{0}{1:g}× {2}{3}{4}".format(
                chk, ln["qty"], esc(ln["item_code"]), of, prekit))
        for it in d["_service"]:
            parts.append(
                "<span style='color:#999;font-style:italic'>{0}×{1:g} — service, not packed</span>"
                .format(esc(it.item_code), flt(it.qty)))
        return "<br>".join(parts)

    def pack_rows_html(seq, start_no, shade_color="#e8e8e8"):
        """Section B rows for a slice, numbered GLOBALLY (start_no..) so the #
        column always equals the position in the batch's label sequence."""
        return "".join(
            ("<tr{shade}><td>{n}</td><td>{order}</td><td>{awb}</td>"
             "<td style='text-align:center;font-weight:bold'>{boxes}</td>"
             "<td>{contents}</td>"
             "<td style='text-align:center;font-weight:bold'>{pieces:g}</td>"
             "<td></td><td></td></tr>").format(
                # Shaded row = multi-piece order → 100% second-person QC count (SOP-PACK-QC)
                shade=" style='background:{0}'".format(shade_color)
                      if _pick_list_piece_count(d, prekit_bundles) > 1 else "",
                n=start_no + i,
                order=esc(d.get("shopify_order_number") or d.get("shopify_order_id") or d["name"]),
                awb=awb_cell(d),
                # Cartons to hand the courier. Shown so a packer can never seal one box
                # for an order whose label sheet carries two.
                boxes=len(_awb_courier_pairs(d)) or cint(d.get("custom_box_count")) or 1,
                contents=content_cell(d),
                pieces=_pick_list_piece_count(d, prekit_bundles))
            for i, d in enumerate(seq)
        )

    html = """
    <div style="font-family:Arial,sans-serif;font-size:11px">
      <h2 style="margin:0">D2C Pick List — {date} — Batch {batch_no} ({stamp}){part_h}</h2>
      <p style="margin:2px 0 10px">Orders: <b>{orders}</b> &nbsp; Pieces: <b>{units:g}</b>
         &nbsp; SKUs: <b>{skus}</b> &nbsp;
         <span style="color:#888">generated {gen}</span></p>

      <h3 style="margin:12px 0 4px">{pick_heading}</h3>
      {pick_table}

      {section_b}
    </div>
    """

    B_TABLE = """
      <p style="margin:0 0 3px;color:#555">Tick every box line as it goes in.
         <b>Shaded row = multi-piece order → second person counts before sealing
         (SOP-PACK-QC), both initial.</b> Pieces = physical units that must be in
         the parcel(s). <b>Box = cartons to hand over; every AWB listed is a
         SEPARATE parcel — hand over ALL of them.</b></p>
      <table border="1" cellspacing="0" cellpadding="1" width="100%"
             style="border-collapse:collapse;table-layout:fixed;word-wrap:break-word;
                    font-size:8px;line-height:1.15">
        <colgroup>
          <col style="width:3%"><col style="width:12%"><col style="width:20%">
          <col style="width:4%"><col style="width:45%"><col style="width:5%">
          <col style="width:5.5%"><col style="width:5.5%">
        </colgroup>
        <thead><tr style="background:{header_bg};color:{header_text}">
          <th>#</th><th align="left">Order</th><th align="left">AWB(s)</th>
          <th>Box</th><th align="left">Contents</th>
          <th>Pcs</th>
          <th>Packed</th><th>QC</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>"""

    if parts:
        # One file, one line-section per part: a full-width black separator
        # heading on a fresh page marks where each line's work begins, mirroring
        # the divider sheets in the labels stack. Order #s stay GLOBAL so the
        # sheet always matches the label sequence 1:1.
        by_name = {d["name"]: d for d in dns}
        sections = []
        for pt in parts:
            chunk = [by_name[n] for n in pt.get("_names") or [] if n in by_name]
            if not chunk:
                continue
            station = pt.get("name") or "Line " + str(pt["part"])
            theme = line_theme(pt.get("part"))
            station_display = "{0} - {1}".format(station, theme["name"])
            sections.append(
                "<div style='page-break-before:always'></div>"
                "<h2 style='font-size:26px;background:{dark};color:#fff;padding:10px;"
                "text-align:center;margin:0 0 2px'>{station} &nbsp;·&nbsp; "
                "orders {a}&ndash;{b} &nbsp;·&nbsp; {o} orders / {pc:g} pcs</h2>"
                .format(dark=theme["dark"], station=esc(station_display).upper(),
                        a=pt["order_range"][0],
                        b=pt["order_range"][1], o=pt["orders"],
                        pc=pt.get("pieces") or 0)
                + "<h3 style='margin:6px 0 3px'>B. PACK + QC — {station} "
                  "(matches label sequence)</h3>".format(station=esc(station_display))
                + B_TABLE.format(
                    rows=pack_rows_html(chunk, pt["order_range"][0], theme["light"]),
                    header_bg=theme["dark"], header_text="#fff")
            )
        section_b = "".join(sections)
    else:
        section_b = ("<h3 style='margin:12px 0 3px'>B. PACK + QC (by order — "
                     "matches label sequence)</h3>"
                     + B_TABLE.format(rows=pack_rows_html(dns, 1),
                                      header_bg="#f0f0f0", header_text="#000"))

    html = html.format(
        date=on_date, batch_no=batch_no, stamp=stamp, orders=len(dns),
        part_h=(" — <span style='background:#222;color:#fff;padding:1px 6px'>"
                + esc(part_label) + "</span>") if part_label else "",
        units=total_pieces, skus=len(sku_rows),
        gen=now_datetime().strftime("%Y-%m-%d %H:%M"),
        pick_heading=(
            "A. PICK AND SEGREGATE BY SKU (place quantities directly into the colour-coded line zones)"
            if parts else
            "A. PICK (by carton SKU — approved pre-kits stay as one carton)"
            if prekit_bundles else
            "A. PICK (by SKU — bundles exploded to components)"),
        pick_table=pick_table, section_b=section_b)

    pdf_bytes = get_pdf(html)
    return _save_output_file(
        "d2c-pick-list-{0}{1}.pdf".format(stamp, file_suffix or ""), pdf_bytes)


def _line_separator_pdf_pages(part):
    """One big-type divider page for the thermal label stack: comes out of the
    printer as a physical sheet marking where the next line's labels begin."""
    from frappe.utils.pdf import get_pdf
    from pypdf import PdfReader
    html = (
        "<div style='font-family:Arial;text-align:center;margin-top:180px'>"
        "<div style='font-size:80px;font-weight:bold;background:#000;color:#fff;"
        "padding:30px 0'>{station}</div>"
        "<div style='font-size:34px;margin-top:24px'>orders {a}&ndash;{b}</div>"
        "<div style='font-size:24px;color:#555;margin-top:8px'>{o} orders &middot; "
        "{pc:g} pieces &middot; hand this stack to {station}</div></div>"
    ).format(station=frappe.utils.escape_html(
                 part.get("name") or "Line " + str(part["part"])).upper(),
             a=part["order_range"][0], b=part["order_range"][1],
             o=part["orders"], pc=part.get("pieces") or 0)
    return PdfReader(io.BytesIO(get_pdf(html))).pages


def _build_combined_labels_pdf(dns, on_date, batch_no, stamp, parts=None,
                               file_suffix=""):
    """parts: optional list of {'part','order_range','orders','pieces','_names'}
    (chunk metadata over the SAME dns sequence). When given, a divider page is
    inserted where each line's section begins, so ONE printed stack splits
    physically at the dividers instead of shipping N files."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    missing = []
    first_err = None
    # DN name -> the part that starts with it (divider goes before its labels)
    part_start = {}
    for pt in parts or []:
        names = pt.get("_names") or []
        if names:
            part_start[names[0]] = pt
    for d in dns:
        pt = part_start.get(d["name"])
        if pt:
            try:
                for page in _line_separator_pdf_pages(pt):
                    writer.add_page(page)
            except Exception:
                pass  # a missing divider must never block the labels
        # Prepare is intentionally network-free. The attached d2c-label File is
        # the fetch job's durable proof that every parcel label is present; using
        # a DN's live URL here can race expiry and can expose only parcel 1 of a
        # multi-box order.
        content = _read_attached_label(d["name"])
        if not content:
            missing.append(_label_identity(d))
            continue
        try:
            # add_page loop is version-agnostic (works where PdfWriter.append
            # may not); merges each label's page(s) in pack sequence.
            for page in PdfReader(io.BytesIO(content)).pages:
                writer.add_page(page)
        except Exception as e:
            if first_err is None:
                first_err = "{0}: {1}".format(d["name"], str(e)[:160])
            missing.append(_label_identity(d))

    if first_err:
        _log("D2C Prepare", "label merge first error — " + first_err)

    if len(writer.pages) == 0:
        return None, missing

    buf = io.BytesIO()
    writer.write(buf)
    url = _save_output_file(
        "d2c-labels-{0}{1}.pdf".format(stamp, file_suffix or ""), buf.getvalue())
    return url, missing


def _save_output_file(file_name, content):
    """Save a generated PDF as a private File and return its URL. Overwrites the
    prior same-name output so re-running Prepare doesn't pile up duplicates."""
    for old in frappe.get_all("File", filters={"file_name": file_name},
                              fields=["name", "attached_to_name"]):
        if old.attached_to_name:
            continue  # never touch files attached to documents
        try:
            frappe.delete_doc("File", old.name, ignore_permissions=True, force=True)
        except Exception:
            pass
    f = frappe.get_doc({
        "doctype": "File",
        "file_name": file_name,
        "is_private": 1,
        "content": content,
    })
    f.flags.ignore_permissions = True
    f.insert(ignore_permissions=True)
    frappe.db.commit()
    return f.file_url


# ─── LEAK-PREVENTION MONITORS (Layer 1 wave aging note + Layer 3 report) ───

def _chunk(seq, n=90):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _post_slack(settings, text, tag="D2C Slack"):
    """Post `text` to the wave Slack Incoming Webhook (get_password). Best-effort;
    returns True on HTTP 200. Shared by the wave notification and this monitor."""
    try:
        webhook = (settings.get_password("wave_slack_webhook", raise_exception=False) or "").strip()
    except Exception:
        webhook = ""
    if not webhook:
        return False
    try:
        import requests
        resp = requests.post(webhook, json={"text": text}, timeout=15)
        _log(tag, "slack {0} ({1})".format(
            "posted" if resp.status_code == 200 else "FAILED", resp.status_code))
        return resp.status_code == 200
    except Exception:
        _log(tag, "slack post failed\n{0}".format(frappe.get_traceback()))
        return False


def _aging_unshipped(on_datetime, hours=None):
    """LAYER 1 — fast wave-time tripwire. Defer DNs released > `hours` ago (within
    the completeness window) that are STILL not printed in a label_found=1 batch and
    not dispatch-scanned. These are at risk of slipping; the wave note surfaces the
    order numbers so the floor checks manually. Returns a list of order numbers."""
    hours = AGING_UNSHIPPED_HOURS if hours is None else hours
    on_date = getdate(on_datetime)
    cutoff = add_to_date(get_datetime(on_datetime), hours=-hours)
    start = add_days(on_date, -COMPLETENESS_WINDOW_DAYS)
    dns = frappe.get_all(
        "Delivery Note",
        filters={"custom_d2c_defer_si": 1, "docstatus": 1,
                 "creation": ["between", [str(start), cutoff]]},
        fields=["name", "shopify_order_number"], limit_page_length=0)
    if not dns:
        return []
    printed = _batched_dn_names(on_date, COMPLETENESS_WINDOW_DAYS)
    scanned = {r.delivery_note for r in frappe.get_all(
        "D2C Dispatch Scan", fields=["delivery_note"], limit_page_length=0)}
    return [d.shopify_order_number or d.name for d in dns
            if d.name not in printed and d.name not in scanned]


def _completeness_buckets(on_datetime):
    """LAYER 3 core — reconcile every Shopify order in the rolling window against
    its terminal dispatch state, categorized by WHERE it is stuck. Frappe-native
    mirror of scripts/d2c_completeness_monitor.py. Returns {category: [(order,
    value, note)]}. Stage-agnostic: catches a miss no matter which stage failed."""
    win_start = str(add_days(getdate(on_datetime), -COMPLETENESS_WINDOW_DAYS))
    win_end = add_to_date(get_datetime(on_datetime), hours=-COMPLETENESS_GRACE_HOURS)
    sos = frappe.get_all(
        "Sales Order",
        filters={"shopify_order_id": ["is", "set"], "creation": ["between", [win_start, win_end]]},
        fields=["shopify_order_id", "shopify_order_number", "creation", "transaction_date",
                "grand_total", "custom_shopify_hold", "custom_order_type"],
        limit_page_length=0)
    soids = [s.shopify_order_id for s in sos]

    dn_by_oid = {}
    for ch in _chunk(soids):
        for d in frappe.get_all(
                "Delivery Note", filters={"shopify_order_id": ["in", ch], "docstatus": 1},
                fields=["name", "shopify_order_id", "awb_number", "custom_awb_shortfall"],
                limit_page_length=0):
            dn_by_oid.setdefault(d.shopify_order_id, []).append(d)

    printed = set()
    for b in frappe.get_all("D2C Prepare Batch", filters={"date": [">=", GOLIVE_DATE]},
                            fields=["name"], limit_page_length=0):
        for r in frappe.get_all(
                "D2C Prepare Batch DN",
                filters={"parent": b.name, "parenttype": "D2C Prepare Batch", "label_found": 1},
                fields=["delivery_note"], limit_page_length=0):
            printed.add(r.delivery_note)
    scanned = {r.delivery_note for r in frappe.get_all(
        "D2C Dispatch Scan", fields=["delivery_note"], limit_page_length=0)}

    all_names = [d.name for lst in dn_by_oid.values() for d in lst]
    labelled = set()
    for ch in _chunk(all_names):
        for f in frappe.get_all(
                "File",
                filters={"attached_to_doctype": "Delivery Note", "attached_to_name": ["in", ch],
                         "file_name": ["like", "d2c-label%"]},
                fields=["attached_to_name"], limit_page_length=0):
            labelled.add(f.attached_to_name)

    buckets = {}

    def add(cat, s, note=""):
        buckets.setdefault(cat, []).append((s.shopify_order_number, flt(s.grand_total), note))

    for s in sos:
        dns = dn_by_oid.get(s.shopify_order_id, [])
        if dns:
            dn = dns[0]
            if dn.name in scanned:
                add("dispatched_ok", s)
            elif dn.custom_awb_shortfall:
                add("ALARM_awb_shortfall_held", s, dn.name)
            elif not dn.awb_number:
                add("ALARM_no_awb_cp_refused", s, dn.name)
            elif dn.name in printed:
                add("printed_awaiting_dispatch", s)
            elif dn.name in labelled:
                add("ALARM_stranded_labelled_unprinted", s, dn.name)
            else:
                add("pending_label", s)
            continue
        age_h = (get_datetime(on_datetime) - get_datetime(s.creation)).total_seconds() / 3600.0
        if cint(s.custom_shopify_hold):
            add("info_on_hold_gokwik" if age_h < 48 else "ALARM_hold_stuck_48h", s)
        elif str(s.transaction_date) < GOLIVE_DATE:
            add("ALARM_old_txn_wont_release", s, "txn " + str(s.transaction_date))
        elif (s.custom_order_type or "") == "PPCOD":
            add("info_ppcod_on_sheet", s)
        else:
            add("nodn_needs_release_check", s)
    return buckets


_COMPLETENESS_ALARM = [
    ("ALARM_stranded_labelled_unprinted", "Labelled, never printed (STRANDED)"),
    ("ALARM_no_awb_cp_refused", "Released, no AWB (CP refused: unserviceable/virtual)"),
    ("ALARM_awb_shortfall_held", "AWB shortfall — held (fewer AWBs than boxes)"),
    ("ALARM_old_txn_wont_release", "Old-dated re-sync — won't auto-release"),
    ("ALARM_hold_stuck_48h", "On-hold >48h — GoKwik never confirmed"),
]
_COMPLETENESS_INFO = [
    ("nodn_needs_release_check", "no-DN release check"),
    ("info_on_hold_gokwik", "on-hold (GoKwik)"),
    ("info_ppcod_on_sheet", "PPCOD on sheet"),
    ("pending_label", "awaiting label"),
    ("printed_awaiting_dispatch", "printed, awaiting scan"),
    ("dispatched_ok", "dispatched"),
]


def _render_completeness_slack(buckets, on_datetime):
    total = sum(len(v) for v in buckets.values())
    alarm_n = sum(len(buckets.get(k, [])) for k, _ in _COMPLETENESS_ALARM)
    head = ":rotating_light:" if alarm_n else ":white_check_mark:"
    lines = [
        "{0} *D2C Completeness Monitor* — {1} IST".format(
            head, get_datetime(on_datetime).strftime("%d %b %H:%M")),
        "Last {0}d up to −{1}h · {2} orders reconciled · *{3} need action*".format(
            COMPLETENESS_WINDOW_DAYS, COMPLETENESS_GRACE_HOURS, total, alarm_n),
        "",
        "*ALARM — leaks to clear:*",
    ]
    if not alarm_n:
        lines.append("  _none — every order dispatched or legitimately in-flight_ :tada:")
    for key, label in _COMPLETENESS_ALARM:
        rows = buckets.get(key, [])
        if not rows:
            continue
        val = sum(r[1] for r in rows)
        sample = ", ".join(r[0] for r in rows[:8]) + (" …" if len(rows) > 8 else "")
        lines.append("  • *{0}* · ₹{1:,.0f} — {2}".format(len(rows), val, label))
        lines.append("      {0}".format(sample))
    info = [(k, l) for k, l in _COMPLETENESS_INFO if buckets.get(k)]
    if info:
        lines.append("")
        lines.append("_Informational: " + " · ".join(
            "{0} {1}".format(len(buckets[k]), l) for k, l in info) + "_")
    return "\n".join(lines)


@frappe.whitelist()
def d2c_completeness_report():
    """LAYER 3 — scheduler (2×/day) + button. Reconcile the window and post the
    categorized leak report to the wave Slack webhook. Gated by
    completeness_report_enabled (default OFF). Best-effort; never raises into the
    scheduler. Returns a small summary dict for the Run-Now button."""
    settings = _settings()
    if not cint(settings.get("completeness_report_enabled")):
        return {"skipped": "completeness_report_enabled is off"}
    try:
        now = now_datetime()
        buckets = _completeness_buckets(now)
        text = _render_completeness_slack(buckets, now)
        posted = _post_slack(settings, text, tag="D2C Completeness")
        alarm = sum(len(buckets.get(k, [])) for k, _ in _COMPLETENESS_ALARM)
        return {"posted": posted, "alarm": alarm,
                "reconciled": sum(len(v) for v in buckets.values())}
    except Exception:
        _log("D2C Completeness", "report failed\n{0}".format(frappe.get_traceback()))
        return {"error": True}
