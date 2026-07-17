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
        BATCH-AWARE: each click covers only DNs not in a prior D2C Prepare
        Batch for the date (re-clicks can't re-print packed orders); every
        batch is recorded with its DN list and is reprintable exactly.
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
from frappe.utils import cint, flt, get_datetime, now_datetime, nowdate, add_days, getdate

from solara_wms.wms.utils import get_available_qty


SETTINGS_DOCTYPE = "D2C Fulfillment Settings"
DEFAULT_WAREHOUSE = "Main Warehouse - WTBBPL"
DEFAULT_PREFIX = "SHP"
# Deferred-invoice SI (raised after the label is fetched, not at DN submit).
SI_SERIES = "SHPSI27-.#####"
D2C_INCOME_ACCOUNT = "Sales - WTBBPL"

# ClickPost label fetch-by-AWB — for couriers (e.g. Shadowfax) that assign an AWB
# but do NOT write the presigned label URL back onto the DN. cp_id is ClickPost's
# per-courier id; seed the common ones, discover + cache the rest. API key lives
# in D2C Fulfillment Settings.clickpost_api_key (not in code).
CLICKPOST_LABEL_API = "https://www.clickpost.in/api/v1/fetch/shippinglabel/"
DEFAULT_CPID_SEED = {"shadowfax": 9, "delhivery": 4}

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
DEFAULT_MAX_ORDER_LINES = 6    # jumbo guard: more distinct lines than this -> not Phase 1


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


def _max_order_lines(settings):
    # 0 in settings means "use default"; we always want some jumbo ceiling.
    val = cint(settings.get("max_order_lines"))
    return val if val > 0 else DEFAULT_MAX_ORDER_LINES


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
    for n in nestables:  # nestables ride in the first parcel
        parcels[0]["items"].append(n)
    return parcels


def _order_box_count(so, box_map, settings):
    """Number of physical boxes this order ships in = number of parcels the split
    would produce (see _order_parcels), floored at 1. == 1 means single-AWB
    (Phase 1); >= 2 is multi-box.

    Jumbo guard: an order with more distinct lines than max_order_lines is too
    complex to trust the stacking assumptions (validated on a 7-line triply order
    that shipped 2 AWBs) — force >= 2 (sentinel) so it stays on the sheet and is
    never auto-split."""
    lines = [it for it in so.items if cint(it.get("qty")) > 0]
    if len(lines) > _max_order_lines(settings):
        return 2  # jumbo sentinel — not a trustworthy parcel quantity
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
         appliance / cookware stack would need AWBs the CP combo branch won't mint);
      4. not tripped by the jumbo guard.
    When all hold, box_count == 2 and CP produces exactly those 2 AWBs; the AWB
    guard still backstops it before dispatch."""
    combos = _split_combos(settings)
    live = [it for it in so.items if cint(it.get("qty")) > 0]
    if len(live) > _max_order_lines(settings):
        return False
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
    plan = []
    for p in parcels:
        items = [{"item_code": i["item_code"], "qty": cint(i["qty"])}
                 for i in p.get("items", []) if i.get("item_code")]
        value = sum(rate_of.get(i["item_code"], 1.0) * i["qty"] for i in items)
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

def release_d2c_shipments():
    """Scheduler entry (*/15). Auto-create + submit Delivery Notes for eligible
    single-AWB Shopify Sales Orders. Idempotent, gated, per-order isolated.

    The whole body is wrapped so a defect here can NEVER propagate into the
    shared scheduler runner (protects every other app's jobs, incl. Amazon DF)."""
    try:
        return _release_d2c_shipments()
    except Exception:
        frappe.db.rollback()
        _log("D2C Release", "FATAL (swallowed): " + frappe.get_traceback())
        return None


def _release_d2c_shipments():
    settings = _settings()
    if not cint(settings.get("release_enabled")):
        return

    # Optional hard stop after the daily cutoff hour (default: ship immediately,
    # so this is off unless enforce_cutoff_on_release is set).
    if cint(settings.get("enforce_cutoff_on_release")):
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
            #  (b) PHASE 2a: its parcel plan is EXACTLY 2 parcels and the
            #      release_multibox_2p toggle is ON — the plan is stamped on the
            #      DN and the ClickPost script executes it (one AWB per parcel,
            #      value-weighted COD). Jumbo (box_count sentinel from the
            #      line-count guard) never has a trustworthy plan, and 3+ parcel
            #      orders stay on the manual sheet.
            covered_by_combo = (cint(settings.get("release_known_combos"))
                                and _fully_covered_by_known_combos(so, box_map, settings))
            if not covered_by_combo and cint(settings.get("release_multibox_2p")):
                live_lines = [it for it in so.items if cint(it.get("qty")) > 0]
                if len(live_lines) <= _max_order_lines(settings):
                    parcels = _order_parcels(so, box_map, settings)
                    if len(parcels) == 2 and box_count == 2:
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
              "custom_awb_shortfall"):
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
    """(awb, courier) pairs for a DN. Automation DNs are single-parcel so this is
    normally one pair; multi-box combos (awb_number + custom_awb_2) yield two."""
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
    urls = ["https://www.clickpost.in/tracking/#/" + a for a in awbs]
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
    (labelled or not — unlabelled ones surface in missing_labels rather than
    silently vanishing), in a stable pack sequence: batch identical single-SKU
    orders together, then by AWB."""
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
                "shipping_label"],
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
    """DNs already included in a prior prepare batch within the rolling window.
    Window matches _todays_d2c_dns so a DN batched yesterday can't reprint today."""
    start = add_days(on_date, -lookback)
    batches = frappe.get_all("D2C Prepare Batch",
                             filters={"date": ["between", [start, on_date]]},
                             fields=["name"], limit_page_length=0)
    if not batches:
        return set()
    rows = frappe.get_all(
        "D2C Prepare Batch DN",
        filters={"parent": ["in", [b.name for b in batches]],
                 "parenttype": "D2C Prepare Batch"},
        fields=["delivery_note"],
        limit_page_length=0,
    )
    return {r.delivery_note for r in rows}


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
    dns = [d for d in all_dns if d["name"] not in already]

    batch_no = len(frappe.get_all("D2C Prepare Batch", filters={"date": on_date},
                                  limit_page_length=0)) + 1
    summary = {
        "date": str(on_date),
        "batch_no": batch_no,
        "run_type": run_type if run_type in ("Ad-hoc", "Wave") else "Ad-hoc",
        "orders": len(dns),
        "already_prepared": len(already),
        "units": sum(flt(i.qty) for d in dns for i in d["items"]),
        "missing_labels": [],
        "pick_list_url": None,
        "labels_pdf_url": None,
    }
    if not dns:
        summary["message"] = (
            "No NEW D2C Delivery Notes for {0} — {1} order(s) already covered by "
            "earlier batches. Use reprint_batch to regenerate a past batch."
            .format(on_date, len(already)))
        return summary

    # Generation stamp MMDDHHMM (e.g. 07120900) — names the files and traces the
    # batch to the moment it was made. Each DN (and therefore each AWB) is in
    # exactly one batch, so no AWB can appear in two files.
    stamp = now_datetime().strftime("%m%d%H%M")
    summary["batch_stamp"] = stamp

    result = _render_batch_files(dns, on_date, batch_no, stamp)
    summary.update(result)

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
             "label_found": 0 if (d.get("shopify_order_number") or d.get("shopify_order_id") or d["name"])
                                 in result["missing_labels"] else 1}
            for d in dns
        ],
    })
    batch.flags.ignore_permissions = True
    batch.insert(ignore_permissions=True)
    _attach_outputs_to_batch(batch.name, result)
    frappe.db.commit()
    summary["batch"] = batch.name
    _email_batch(batch.name, summary, settings)
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
        site = frappe.utils.get_url()
        missing = summary.get("missing_labels") or []
        body = (
            "<p><b>D2C dispatch batch {batch}</b> — {orders} orders / {units:g} units "
            "({run_type}, {date}, stamp {stamp})</p>"
            "<ul><li>Pick list: <a href='{site}{pl}'>{pl_name}</a></li>"
            "<li>Labels PDF: <a href='{site}{lb}'>{lb_name}</a></li></ul>"
            "{attach_note}{missing_note}"
            "<p>Ship these from the Atlas batch and SKIP them on the manual sheet.</p>"
        ).format(
            batch=batch_name, orders=summary.get("orders"), units=summary.get("units") or 0,
            run_type=summary.get("run_type") or "", date=summary.get("date"),
            stamp=summary.get("batch_stamp"), site=site,
            pl=summary.get("pick_list_url") or "", pl_name=(summary.get("pick_list_url") or "").rsplit("/", 1)[-1],
            lb=summary.get("labels_pdf_url") or "", lb_name=(summary.get("labels_pdf_url") or "").rsplit("/", 1)[-1],
            attach_note="<p>Both PDFs attached.</p>" if attachments else
                        "<p>PDFs too large to attach — use the links (Atlas login needed).</p>",
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
    return {"batch": batch.name, "batch_stamp": stamp, "orders": len(dns), **result}


def _render_batch_files(dns, on_date, batch_no, stamp):
    pick_url = _build_pick_list_pdf(dns, on_date, batch_no, stamp)
    labels_url, missing = _build_combined_labels_pdf(dns, on_date, batch_no, stamp)
    return {
        "pick_list_url": pick_url,
        "labels_pdf_url": labels_url,
        "missing_labels": missing,
        "labelled": len(dns) - len(missing),
    }


def _sku_summary(dns):
    agg = {}
    for d in dns:
        for it in d["items"]:
            row = agg.setdefault(it.item_code, {"item_name": it.item_name, "qty": 0})
            row["qty"] += flt(it.qty)
    return sorted(
        ({"item_code": k, **v} for k, v in agg.items()),
        key=lambda r: r["item_code"],
    )


def _build_pick_list_pdf(dns, on_date, batch_no, stamp):
    from frappe.utils.pdf import get_pdf

    sku_rows = _sku_summary(dns)
    total_units = sum(r["qty"] for r in sku_rows)

    def esc(s):
        return frappe.utils.escape_html(str(s or ""))

    pick_rows = "".join(
        "<tr><td>{0}</td><td>{1}</td><td style='text-align:right'>{2:g}</td></tr>".format(
            esc(r["item_code"]), esc(r["item_name"]), r["qty"])
        for r in sku_rows
    )
    pack_rows = "".join(
        "<tr><td>{0}</td><td>{1}</td><td>{2}</td><td>{3}</td><td>{4}</td></tr>".format(
            i + 1, esc(d.get("shopify_order_number") or d.get("shopify_order_id") or d["name"]), esc(d.get("awb_number")),
            esc(d.get("courier_partner")),
            esc(", ".join("{0}×{1:g}".format(it.item_code, flt(it.qty)) for it in d["items"])))
        for i, d in enumerate(dns)
    )

    html = """
    <div style="font-family:Arial,sans-serif;font-size:11px">
      <h2 style="margin:0">D2C Pick List — {date} — Batch {batch_no} ({stamp})</h2>
      <p style="margin:2px 0 10px">Orders: <b>{orders}</b> &nbsp; Units: <b>{units:g}</b>
         &nbsp; SKUs: <b>{skus}</b> &nbsp;
         <span style="color:#888">generated {gen}</span></p>

      <h3 style="margin:12px 0 4px">A. PICK (by SKU)</h3>
      <table border="1" cellspacing="0" cellpadding="4" width="100%"
             style="border-collapse:collapse">
        <thead><tr style="background:#f0f0f0">
          <th align="left">SKU</th><th align="left">Item</th><th align="right">Qty</th>
        </tr></thead>
        <tbody>{pick_rows}
          <tr style="background:#fafafa;font-weight:bold">
            <td colspan="2" align="right">Total units</td>
            <td align="right">{units:g}</td></tr>
        </tbody>
      </table>

      <h3 style="margin:16px 0 4px">B. PACK (by order — matches label sequence)</h3>
      <table border="1" cellspacing="0" cellpadding="4" width="100%"
             style="border-collapse:collapse">
        <thead><tr style="background:#f0f0f0">
          <th>#</th><th align="left">Order</th><th align="left">AWB</th>
          <th align="left">Courier</th><th align="left">Contents</th>
        </tr></thead>
        <tbody>{pack_rows}</tbody>
      </table>
    </div>
    """.format(date=on_date, batch_no=batch_no, stamp=stamp, orders=len(dns),
               units=total_units, skus=len(sku_rows),
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
        content = _label_pdf_bytes(d["name"], d.get("shipping_label"))
        if not content:
            missing.append(d.get("shopify_order_number") or d.get("shopify_order_id") or d["name"])
            continue
        try:
            # add_page loop is version-agnostic (works where PdfWriter.append
            # may not); merges each label's page(s) in pack sequence.
            for page in PdfReader(io.BytesIO(content)).pages:
                writer.add_page(page)
        except Exception as e:
            if first_err is None:
                first_err = "{0}: {1}".format(d["name"], str(e)[:160])
            missing.append(d.get("shopify_order_number") or d.get("shopify_order_id") or d["name"])

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
