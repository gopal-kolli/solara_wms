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


def _candidate_sos(settings, limit):
    """SHP Sales Orders that could be released, before the per-order gates."""
    lookback = cint(settings.get("lookback_days")) or 3
    filters = {
        "name": ["like", _prefix(settings) + "%"],
        "docstatus": 1,
        "status": ["in", RELEASABLE_STATUSES],
        "per_delivered": 0,
        "skip_delivery_note": 0,
        "transaction_date": [">=", add_days(nowdate(), -lookback)],
    }
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


def _run_release(settings, dry_run=False):
    limit = cint(settings.get("release_batch_size")) or 200
    max_orders = cint(settings.get("max_orders_per_run")) or limit
    warehouse = _source_warehouse(settings)
    box_map = _box_config(settings)
    require_stock = cint(settings.get("require_stock"))

    candidates = _candidate_sos(settings, limit)
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
        if cint(so.get("custom_shopify_hold")):
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


def _make_and_submit_dn(so_name, warehouse, res, box_count=1, parcel_plan=None):
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
        # Defer invoicing: this DN is submitted EARLY (before physical dispatch)
        # only to create the AWB + label for packing. Flag it so the LIVE
        # "Auto Create SI on Shopify DN Submit" script skips it — the SHPSI27 is
        # raised later, after dispatch, via the evening D2C Invoice Run. (Manual
        # sheet-flow DNs are unflagged and keep invoicing at submit as before.)
        dn.custom_d2c_defer_si = 1
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
              "custom_shopify_fulfilled"]
    meta = frappe.get_meta("Delivery Note")
    for f in ("custom_awb_2", "custom_courier_2", "custom_box_count",
              "custom_awb_shortfall", "custom_awb_list"):
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
    for dn in dns:
        if time.monotonic() > deadline:
            _log("D2C Label Fetch", "time budget hit — processed part of batch, rest next run")
            break
        # Each DN isolated: a per-row error must never abort the whole */15 batch.
        try:
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
            if do_fulfill and not cint(dn.get("custom_shopify_fulfilled")):
                outcome = _try_fulfill(dn)
                if outcome in ("created", "updated", "in_sync"):
                    frappe.db.set_value("Delivery Note", dn.name,
                                        "custom_shopify_fulfilled", 1)
                    frappe.db.commit()
                    fulfilled += 1
                elif outcome == "failed":
                    ful_failed += 1

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
        awbs.append(str(dn["custom_awb_2"]).strip())
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


def push_shopify_fulfillment(dn):
    """Create the Shopify fulfillment for a DN, so the order shows fulfilled + the
    customer gets the AWB tracking. Idempotent + non-clobbering: returns in_sync if a
    success fulfillment already carries all our AWBs, and never overwrites a
    fulfillment created by another path. Returns created | in_sync | no_open_fo |
    skipped | failed. App-code replacement for the sandbox-broken 'Auto Sync AWB to
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
    for f in fulfillments:
        if f.get("status") == "success":
            covered |= set(f.get("tracking_numbers") or (
                [f["tracking_number"]] if f.get("tracking_number") else []))
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
        # No open FO: either fulfilled elsewhere (covered != our awbs) or cancelled.
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
    if not so_name or not so_name.startswith("SHP"):
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

    # Tax-inclusive pricing (matches the Shopify customer-paying total).
    for t in si.taxes:
        t.included_in_print_rate = 1

    # Force Shopify revenue to Sales - WTBBPL (override channel-specific defaults).
    for it in si.items:
        it.income_account = D2C_INCOME_ACCOUNT

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
        "shopify_order_id": ["is", "set"],
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
    dns = frappe.get_all(
        "Delivery Note",
        filters=filters,
        fields=["name", "awb_number", "courier_partner", "customer",
                "customer_name", "shopify_order_id", "shopify_order_number",
                "shipping_label", "custom_box_count"],
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
    return dn.get("shopify_order_number") or dn.get("shopify_order_id") or dn["name"]


@frappe.whitelist()
def prepare_todays_shipments(on_date=None, run_type="Ad-hoc", wave_tag=None):
    """Warehouse action. Build (1) a pick-list PDF and (2) a combined-labels PDF
    for the day's NOT-YET-PREPARED D2C Delivery Notes, and record them as a
    D2C Prepare Batch. Batch-aware: clicking again only emits orders that were
    not part of an earlier batch — re-clicks can never re-print already-packed
    orders. Creates no stock/accounting documents; reprints via reprint_batch.
    run_type/wave_tag: "Wave" runs come from the scheduled 9/12/15 waves
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
    result = _render_batch_files(candidates, on_date, batch_no, stamp)
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
        "missing_labels": json.dumps(result["missing_labels"]),
        "delivery_notes": [
            {"delivery_note": d["name"],
             "shopify_order_id": d.get("shopify_order_number") or d.get("shopify_order_id"),
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
        for url in (summary.get("pick_list_url"), summary.get("labels_pdf_url")):
            if not url:
                continue
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
        body = (
            "<p><b>D2C dispatch batch {batch}</b> — {orders} orders / {units:g} units "
            "({run_type}, {date}, stamp {stamp})</p>"
            "{courier_note}"
            "<ul><li>Pick list: <a href='{dash}/reconciliation/label-status/batch/{batch}/picklist.pdf'>{pl_name}</a></li>"
            "<li>Labels PDF: <a href='{dash}/reconciliation/label-status/batch/{batch}/labels.pdf'>{lb_name}</a></li></ul>"
            "{attach_note}{missing_note}"
            "<p>Ship these from the Atlas batch and SKIP them on the manual sheet.</p>"
        ).format(
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
    lines = [
        ":package: *D2C {0} wave — labels ready*".format(summary.get("run_type") or "Ad-hoc"),
        "Batch *{0}* · *{1} orders* / {2:g} units · stamp {3}".format(
            batch_name, summary.get("orders"), summary.get("units") or 0,
            summary.get("batch_stamp")),
        "• <{0}/reconciliation/label-status/batch/{1}/labels.pdf|:label: Labels PDF>".format(dash, batch_name),
        "• <{0}/reconciliation/label-status/batch/{1}/picklist.pdf|:clipboard: Pick list>".format(dash, batch_name),
        "Ship from the Atlas batch and *skip them on the manual sheet*.",
    ]
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
    for url in (result.get("pick_list_url"), result.get("labels_pdf_url")):
        if not url:
            continue
        for f in frappe.get_all("File", filters={"file_url": url,
                                                 "attached_to_name": ["is", "not set"]},
                                fields=["name"]):
            frappe.db.set_value("File", f.name,
                                {"attached_to_doctype": "D2C Prepare Batch",
                                 "attached_to_name": batch_name})


def run_prepare_waves():
    """Scheduler entry (*/15). Fires Prepare as a scheduled wave at the hours in
    D2C Fulfillment Settings.prepare_wave_hours (site timezone, e.g. 9,12,15).
    Idempotent per hour via wave_tag: a wave that already produced a batch never
    re-fires; an EMPTY wave (no unbatched orders yet) creates no batch and simply
    re-checks on the next */15 tick within the same hour, then lapses — orders
    arriving later roll into the next wave or an ad-hoc run. Wrapped so a defect
    can never wedge the shared scheduler (same contract as the other D2C jobs)."""
    try:
        settings = _settings()
        if not cint(settings.get("prepare_waves_enabled")):
            return
        raw = (settings.get("prepare_wave_hours") or "9,12,15")
        try:
            hours = sorted({int(h.strip()) for h in raw.split(",") if h.strip()})
        except Exception:
            _log("D2C Prepare Wave", "prepare_wave_hours is not a comma list of hours — using 9,12,15")
            hours = [9, 12, 15]
        now = now_datetime()
        if now.hour not in hours:
            return
        tag = "{0}-{1:02d}".format(now.strftime("%Y-%m-%d"), now.hour)
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
    result = _render_batch_files(dns, batch.date, batch.batch_no, stamp)
    return {"batch": batch.name, "batch_stamp": stamp,
            "orders": result["labelled"], "requested_orders": len(dns), **result}


def _render_batch_files(dns, on_date, batch_no, stamp):
    # Labels are the admission control for a batch. Build them first, then make
    # the pick list from the exact same successfully-labelled subset so the two
    # PDFs can never disagree and an unlabelled DN is never marked prepared.
    labels_url, missing = _build_combined_labels_pdf(dns, on_date, batch_no, stamp)
    missing_set = set(missing)
    printable = [d for d in dns if _label_identity(d) not in missing_set]
    pick_url = (_build_pick_list_pdf(printable, on_date, batch_no, stamp)
                if printable else None)
    return {
        "pick_list_url": pick_url,
        "labels_pdf_url": labels_url,
        "missing_labels": missing,
        "labelled": len(printable),
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


def _sku_summary(dns):
    # Aggregates dn['_lines'] (physical pieces): bundles appear as the component
    # SKUs the picker actually pulls from a bin; service rows excluded.
    # _enrich_physical_lines must have run.
    agg = {}
    for d in dns:
        for it in d["_lines"]:
            row = agg.setdefault(it["item_code"], {"item_name": it["item_name"], "qty": 0})
            row["qty"] += flt(it["qty"])
    return sorted(
        ({"item_code": k, **v} for k, v in agg.items()),
        key=lambda r: r["item_code"],
    )


def _build_pick_list_pdf(dns, on_date, batch_no, stamp):
    from frappe.utils.pdf import get_pdf

    _enrich_physical_lines(dns)
    sku_rows = _sku_summary(dns)
    total_pieces = sum(r["qty"] for r in sku_rows)

    def esc(s):
        return frappe.utils.escape_html(str(s or ""))

    # Drawn checkbox (span, not a unicode glyph — guaranteed to render in wkhtmltopdf)
    chk = ("<span style='display:inline-block;width:8px;height:8px;"
           "border:1px solid #222;margin-right:5px'></span>")

    pick_rows = "".join(
        "<tr><td>{0}</td><td>{1}</td><td style='text-align:right'>{2:g}</td></tr>".format(
            esc(r["item_code"]), esc(r["item_name"]), r["qty"])
        for r in sku_rows
    )

    def content_cell(d):
        parts = []
        for ln in d["_lines"]:
            of = (" <span style='color:#999'>(of {0})</span>".format(esc(ln["bundle"]))
                  if ln["bundle"] else "")
            parts.append("{0}{1:g}× {2}{3}".format(chk, ln["qty"], esc(ln["item_code"]), of))
        for it in d["_service"]:
            parts.append(
                "<span style='color:#999;font-style:italic'>{0}×{1:g} — service, not packed</span>"
                .format(esc(it.item_code), flt(it.qty)))
        return "<br>".join(parts)

    pack_rows = "".join(
        ("<tr{shade}><td>{n}</td><td>{order}</td><td>{awb}</td><td>{courier}</td>"
         "<td>{contents}</td>"
         "<td style='text-align:center;font-weight:bold'>{pieces:g}</td>"
         "<td></td><td></td></tr>").format(
            # Shaded row = multi-piece order → 100% second-person QC count (SOP-PACK-QC)
            shade=" style='background:#e8e8e8'"
                  if sum(flt(l["qty"]) for l in d["_lines"]) > 1 else "",
            n=i + 1,
            order=esc(d.get("shopify_order_number") or d.get("shopify_order_id") or d["name"]),
            awb=esc(d.get("awb_number")),
            courier=esc(d.get("courier_partner")),
            contents=content_cell(d),
            pieces=sum(flt(l["qty"]) for l in d["_lines"]))
        for i, d in enumerate(dns)
    )

    html = """
    <div style="font-family:Arial,sans-serif;font-size:11px">
      <h2 style="margin:0">D2C Pick List — {date} — Batch {batch_no} ({stamp})</h2>
      <p style="margin:2px 0 10px">Orders: <b>{orders}</b> &nbsp; Pieces: <b>{units:g}</b>
         &nbsp; SKUs: <b>{skus}</b> &nbsp;
         <span style="color:#888">generated {gen}</span></p>

      <h3 style="margin:12px 0 4px">A. PICK (by SKU — bundles exploded to components)</h3>
      <table border="1" cellspacing="0" cellpadding="4" width="100%"
             style="border-collapse:collapse">
        <thead><tr style="background:#f0f0f0">
          <th align="left">SKU</th><th align="left">Item</th><th align="right">Qty</th>
        </tr></thead>
        <tbody>{pick_rows}
          <tr style="background:#fafafa;font-weight:bold">
            <td colspan="2" align="right">Total pieces</td>
            <td align="right">{units:g}</td></tr>
        </tbody>
      </table>

      <h3 style="margin:16px 0 4px">B. PACK + QC (by order — matches label sequence)</h3>
      <p style="margin:0 0 4px;color:#555">Tick every box line as it goes in.
         <b>Shaded row = multi-piece order → second person counts before sealing
         (SOP-PACK-QC), both initial.</b> Pieces = physical units that must be in
         the parcel(s).</p>
      <table border="1" cellspacing="0" cellpadding="4" width="100%"
             style="border-collapse:collapse;table-layout:fixed;word-wrap:break-word">
        <colgroup>
          <col style="width:4%"><col style="width:13%"><col style="width:17%">
          <col style="width:9%"><col style="width:39%"><col style="width:6%">
          <col style="width:6%"><col style="width:6%">
        </colgroup>
        <thead><tr style="background:#f0f0f0">
          <th>#</th><th align="left">Order</th><th align="left">AWB</th>
          <th align="left">Courier</th><th align="left">Contents</th>
          <th>Pcs</th>
          <th>Packed</th><th>QC</th>
        </tr></thead>
        <tbody>{pack_rows}</tbody>
      </table>
    </div>
    """.format(date=on_date, batch_no=batch_no, stamp=stamp, orders=len(dns),
               units=total_pieces, skus=len(sku_rows),
               gen=now_datetime().strftime("%Y-%m-%d %H:%M"),
               pick_rows=pick_rows, pack_rows=pack_rows)

    pdf_bytes = get_pdf(html)
    return _save_output_file("d2c-pick-list-{0}.pdf".format(stamp), pdf_bytes)


def _build_combined_labels_pdf(dns, on_date, batch_no, stamp):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    missing = []
    first_err = None
    for d in dns:
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
    url = _save_output_file("d2c-labels-{0}.pdf".format(stamp), buf.getvalue())
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
