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
        over today's labelled SHP DNs:
          ├─ Pick List PDF  (SKU-summary section + per-order pack section)
          └─ Combined Labels PDF (attached label PDFs merged in pack sequence)

Phase 1 handles single-AWB orders only. Multi-box SKUs (config `multibox_skus`)
are excluded from auto-release and stay on the manual sheet until Phase 2.

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


def _multibox_set(settings):
    """Parse the comma/newline-separated multi-box SKU list into an upper-cased set."""
    raw = (settings.get("multibox_skus") or "").replace("\n", ",")
    return {tok.strip().upper() for tok in raw.split(",") if tok.strip()}


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
    if result["created"] or result["failed"]:
        _log(
            "D2C Release",
            "created={0} skipped_multibox={1} skipped_nostock={2} "
            "skipped_dn_exists={3} failed={4} dry_run={5}\nfailures={6}".format(
                result["created"], result["skipped_multibox"],
                result["skipped_nostock"], result["skipped_dn_exists"],
                result["failed"], cint(settings.get("dry_run")),
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
    multibox = _multibox_set(settings)
    require_stock = cint(settings.get("require_stock"))

    candidates = _candidate_sos(settings, limit)
    already = _sos_with_existing_dn([c.name for c in candidates])

    res = {
        "created": 0, "failed": 0,
        "skipped_multibox": 0, "skipped_nostock": 0, "skipped_dn_exists": 0,
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

        # Gate 1: no multi-box SKU (Phase 2 territory — leave on the manual sheet).
        if any((it.item_code or "").upper() in multibox for it in so.items):
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
    prefix = _prefix(settings)

    dns = frappe.get_all(
        "Delivery Note",
        filters={
            "name": ["like", prefix + "%"],
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


def _label_pdf_bytes(dn_name, shipping_label_url):
    """Return label PDF bytes: prefer the attached File, fall back to a live download."""
    fname = frappe.db.get_value(
        "File",
        {"attached_to_doctype": "Delivery Note", "attached_to_name": dn_name,
         "file_name": _label_file_name(dn_name)},
        "name",
    )
    if fname:
        try:
            return frappe.get_doc("File", fname).get_content()
        except Exception:
            pass
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

def _todays_labelled_dns(settings, on_date):
    """Submitted SHP DNs for the date that already have a label URL, in a stable
    pack sequence: batch identical single-SKU orders together, then by AWB."""
    prefix = _prefix(settings)
    dns = frappe.get_all(
        "Delivery Note",
        filters={
            "name": ["like", prefix + "%"],
            "docstatus": 1,
            "posting_date": on_date,
            "shipping_label": ["is", "set"],
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


@frappe.whitelist()
def prepare_todays_shipments(on_date=None):
    """Warehouse action. Build (1) a pick-list PDF and (2) a combined-labels PDF
    for the day's labelled D2C Delivery Notes. Returns file URLs + a summary.
    Stateless / re-runnable: does not mutate the Delivery Notes."""
    settings = _settings()
    on_date = getdate(on_date) if on_date else getdate(nowdate())

    dns = _todays_labelled_dns(settings, on_date)
    summary = {
        "date": str(on_date),
        "orders": len(dns),
        "units": sum(flt(i.qty) for d in dns for i in d["items"]),
        "missing_labels": [],
        "pick_list_url": None,
        "labels_pdf_url": None,
    }
    if not dns:
        summary["message"] = "No labelled D2C Delivery Notes for {0}.".format(on_date)
        return summary

    pick_url = _build_pick_list_pdf(dns, on_date)
    labels_url, missing = _build_combined_labels_pdf(dns, on_date)
    summary["pick_list_url"] = pick_url
    summary["labels_pdf_url"] = labels_url
    summary["missing_labels"] = missing
    summary["labelled"] = len(dns) - len(missing)
    return summary


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


def _build_pick_list_pdf(dns, on_date):
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
      <h2 style="margin:0">D2C Pick List — {date}</h2>
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
    """.format(date=on_date, orders=len(dns), units=total_units, skus=len(sku_rows),
               gen=now_datetime().strftime("%Y-%m-%d %H:%M"),
               pick_rows=pick_rows, pack_rows=pack_rows)

    pdf_bytes = get_pdf(html)
    return _save_output_file("d2c-pick-list-{0}.pdf".format(on_date), pdf_bytes)


def _build_combined_labels_pdf(dns, on_date):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    missing = []
    for d in dns:
        content = _label_pdf_bytes(d["name"], d.get("shipping_label"))
        if not content:
            missing.append(d.get("shopify_order_id") or d["name"])
            continue
        try:
            writer.append(PdfReader(io.BytesIO(content)))
        except Exception:
            missing.append(d.get("shopify_order_id") or d["name"])

    if len(writer.pages) == 0:
        return None, missing

    buf = io.BytesIO()
    writer.write(buf)
    url = _save_output_file("d2c-labels-{0}.pdf".format(on_date), buf.getvalue())
    return url, missing


def _save_output_file(file_name, content):
    """Save a generated PDF as a private File and return its URL. Overwrites the
    prior same-name output so re-running Prepare doesn't pile up duplicates."""
    for old in frappe.get_all("File", filters={"file_name": file_name, "attached_to_name": ""}):
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
