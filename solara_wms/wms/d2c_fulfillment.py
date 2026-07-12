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
exactly 1. Box count = sum over lines of Item.custom_boxes_per_unit x qty
(0 = nestable accessory that packs inside a parent's box, 1 = own box [default],
2+ = multi-box combo e.g. airfryer+juicer), floored at 1. The Item field is the
source of truth; the settings sku_box_config JSON is a max-only safety net.
Multi-box orders (count >= 2) stay on the manual sheet until Phase 2.

Reversibility is already handled by the installed base: "Cancel Clickpost
Shipment" voids the AWB on DN cancel. This module never cancels or deletes.
"""

import io
import json

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime, nowdate, add_days, getdate

from solara_wms.wms.utils import get_available_qty


SETTINGS_DOCTYPE = "D2C Fulfillment Settings"
DEFAULT_WAREHOUSE = "Main Warehouse - WTBBPL"
DEFAULT_PREFIX = "SHP"
# Releasable Sales Order statuses: goods still to go out, nothing delivered yet.
RELEASABLE_STATUSES = ("To Deliver and Bill", "To Deliver")


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


def _order_box_count(so, box_map):
    """Boxes this order ships in = sum of per-unit boxes x qty over lines.
    Nestable accessories (0) ride inside a parent; an all-nestable order still
    ships as one parcel -> floored at 1. == 1 means a single AWB (Phase 1);
    >= 2 means multi-AWB (Phase 2)."""
    total = sum(_item_boxes(it.item_code, box_map) * cint(it.qty) for it in so.items)
    return max(total, 1)


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
    if result["created"] or result["failed"] or result["skipped_bad_data"]:
        _log(
            "D2C Release",
            "created={0} skipped_multibox={1} skipped_nostock={2} "
            "skipped_dn_exists={3} skipped_bad_data={4} failed={5} dry_run={6}\n"
            "bad_data_sos={7}\nfailures={8}".format(
                result["created"], result["skipped_multibox"],
                result["skipped_nostock"], result["skipped_dn_exists"],
                result["skipped_bad_data"], result["failed"],
                cint(settings.get("dry_run")),
                json.dumps(result["bad_data_sos"][:20]),
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

        # Gate 1: single-parcel orders only. Nestable accessories count 0 boxes
        # (they ride inside the parent's box); true combos count 2+. Multi-box
        # orders are Phase 2 territory — they stay on the manual sheet.
        if _order_box_count(so, box_map) != 1:
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

        dn_name = _make_and_submit_dn(so_name, warehouse, res)
        if dn_name:
            res["created"] += 1
            res["created_dns"].append({"so": so_name, "dn": dn_name})

    return res


def _make_and_submit_dn(so_name, warehouse, res):
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
        dn.flags.ignore_permissions = True
        dn.insert(ignore_permissions=True)
        dn.submit()  # ← triggers CP AWB + Shopify sync + SHPSI27 (LIVE scripts)
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

    # D2C DNs are identified by their Shopify linkage, not by naming series
    # (series differs across sites: SHPDN27- on LIVE, DN-26- on TEST).
    dns = frappe.get_all(
        "Delivery Note",
        filters={
            "shopify_order_id": ["is", "set"],
            "docstatus": 1,
            "posting_date": [">=", add_days(nowdate(), -lookback)],
            "shipping_label": ["is", "set"],
        },
        fields=["name", "shipping_label"],
        limit_page_length=cint(settings.get("release_batch_size")) or 200,
    )

    fetched = pending = 0
    for dn in dns:
        if _has_attached_label(dn.name):
            continue
        ok = _download_and_attach_label(dn.name, dn.shipping_label)
        if ok:
            fetched += 1
        else:
            pending += 1

    if fetched or pending:
        _log("D2C Label Fetch", "attached={0} pending={1}".format(fetched, pending))
    return {"attached": fetched, "pending": pending}


def _label_file_name(dn_name):
    return "d2c-label-{0}.pdf".format(dn_name)


def _has_attached_label(dn_name):
    return bool(frappe.db.exists(
        "File",
        {"attached_to_doctype": "Delivery Note",
         "attached_to_name": dn_name,
         "file_name": _label_file_name(dn_name)},
    ))


def _download_and_attach_label(dn_name, url):
    """Download a label PDF URL and attach it privately to the DN. Best-effort."""
    if not url:
        return False
    try:
        import requests
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200 or not resp.content:
            _log("D2C Label Fetch", "download {0}: HTTP {1}".format(dn_name, resp.status_code))
            return False
        f = frappe.get_doc({
            "doctype": "File",
            "file_name": _label_file_name(dn_name),
            "attached_to_doctype": "Delivery Note",
            "attached_to_name": dn_name,
            "is_private": 1,
            "content": resp.content,
        })
        f.flags.ignore_permissions = True
        f.insert(ignore_permissions=True)
        frappe.db.commit()
        return True
    except Exception as e:
        _log("D2C Label Fetch", "attach {0}: {1}".format(dn_name, str(e)[:300]))
        return False


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

def _todays_d2c_dns(settings, on_date):
    """ALL submitted D2C DNs for the date (labelled or not — unlabelled ones
    surface in missing_labels rather than silently vanishing), in a stable
    pack sequence: batch identical single-SKU orders together, then by AWB."""
    dns = frappe.get_all(
        "Delivery Note",
        filters={
            "shopify_order_id": ["is", "set"],
            "docstatus": 1,
            "posting_date": on_date,
        },
        fields=["name", "awb_number", "courier_partner", "customer",
                "customer_name", "shopify_order_id", "shipping_label"],
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


def _batched_dn_names(on_date):
    """DNs already included in a prior prepare batch for the date."""
    batches = frappe.get_all("D2C Prepare Batch", filters={"date": on_date},
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
def prepare_todays_shipments(on_date=None):
    """Warehouse action. Build (1) a pick-list PDF and (2) a combined-labels PDF
    for the day's NOT-YET-PREPARED D2C Delivery Notes, and record them as a
    D2C Prepare Batch. Batch-aware: clicking again only emits orders that were
    not part of an earlier batch — re-clicks can never re-print already-packed
    orders. Creates no stock/accounting documents; reprints via reprint_batch."""
    settings = _settings()
    on_date = getdate(on_date) if on_date else getdate(nowdate())

    all_dns = _todays_d2c_dns(settings, on_date)
    already = _batched_dn_names(on_date)
    dns = [d for d in all_dns if d["name"] not in already]

    batch_no = len(frappe.get_all("D2C Prepare Batch", filters={"date": on_date},
                                  limit_page_length=0)) + 1
    summary = {
        "date": str(on_date),
        "batch_no": batch_no,
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

    result = _render_batch_files(dns, on_date, batch_no)
    summary.update(result)

    batch = frappe.get_doc({
        "doctype": "D2C Prepare Batch",
        "date": on_date,
        "batch_no": batch_no,
        "orders": len(dns),
        "units": summary["units"],
        "pick_list_url": result["pick_list_url"],
        "labels_pdf_url": result["labels_pdf_url"],
        "missing_labels": json.dumps(result["missing_labels"]),
        "delivery_notes": [
            {"delivery_note": d["name"],
             "shopify_order_id": d.get("shopify_order_id"),
             "awb_number": d.get("awb_number"),
             "label_found": 0 if (d.get("shopify_order_id") or d["name"])
                                 in result["missing_labels"] else 1}
            for d in dns
        ],
    })
    batch.flags.ignore_permissions = True
    batch.insert(ignore_permissions=True)
    frappe.db.commit()
    summary["batch"] = batch.name
    return summary


@frappe.whitelist()
def reprint_batch(batch_name):
    """Regenerate the pick list + labels PDF for an existing batch, exactly as
    originally scoped (same DN set). No new batch is created."""
    batch = frappe.get_doc("D2C Prepare Batch", batch_name)
    settings = _settings()
    dns = _todays_d2c_dns(settings, batch.date)
    wanted = {r.delivery_note for r in batch.delivery_notes}
    dns = [d for d in dns if d["name"] in wanted]
    result = _render_batch_files(dns, batch.date, batch.batch_no)
    return {"batch": batch.name, "orders": len(dns), **result}


def _render_batch_files(dns, on_date, batch_no):
    pick_url = _build_pick_list_pdf(dns, on_date, batch_no)
    labels_url, missing = _build_combined_labels_pdf(dns, on_date, batch_no)
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


def _build_pick_list_pdf(dns, on_date, batch_no):
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
            i + 1, esc(d.get("shopify_order_id") or d["name"]), esc(d.get("awb_number")),
            esc(d.get("courier_partner")),
            esc(", ".join("{0}×{1:g}".format(it.item_code, flt(it.qty)) for it in d["items"])))
        for i, d in enumerate(dns)
    )

    html = """
    <div style="font-family:Arial,sans-serif;font-size:11px">
      <h2 style="margin:0">D2C Pick List — {date} — Batch {batch_no}</h2>
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
    """.format(date=on_date, batch_no=batch_no, orders=len(dns), units=total_units,
               skus=len(sku_rows), gen=now_datetime().strftime("%Y-%m-%d %H:%M"),
               pick_rows=pick_rows, pack_rows=pack_rows)

    pdf_bytes = get_pdf(html)
    return _save_output_file(
        "d2c-pick-list-{0}-batch{1}.pdf".format(on_date, batch_no), pdf_bytes)


def _build_combined_labels_pdf(dns, on_date, batch_no):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    missing = []
    first_err = None
    for d in dns:
        content = _label_pdf_bytes(d["name"], d.get("shipping_label"))
        if not content:
            missing.append(d.get("shopify_order_id") or d["name"])
            continue
        try:
            # add_page loop is version-agnostic (works where PdfWriter.append
            # may not); merges each label's page(s) in pack sequence.
            for page in PdfReader(io.BytesIO(content)).pages:
                writer.add_page(page)
        except Exception as e:
            if first_err is None:
                first_err = "{0}: {1}".format(d["name"], str(e)[:160])
            missing.append(d.get("shopify_order_id") or d["name"])

    if first_err:
        _log("D2C Prepare", "label merge first error — " + first_err)

    if len(writer.pages) == 0:
        return None, missing

    buf = io.BytesIO()
    writer.write(buf)
    url = _save_output_file(
        "d2c-labels-{0}-batch{1}.pdf".format(on_date, batch_no), buf.getvalue())
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
