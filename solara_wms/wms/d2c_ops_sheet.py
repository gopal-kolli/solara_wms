# Copyright (c) 2026, SOLARA and contributors
# For license information, please see license.txt
"""Server-side push of the D2C ops Google Sheet (Run Log / Exceptions /
Auto-Shipped tabs) — replaces the laptop-cron version of scripts/d2c_ops_sheet.py
in the finance repo. Runs every 30 min from the bench scheduler, gated by
D2C Fulfillment Settings.ops_sheet_enabled.

Secrets: the Google service-account key lives in the `ops_sheet_sa_key`
PASSWORD field on D2C Fulfillment Settings — encrypted at rest in __Auth and
masked over REST (FC's dashboard only permits whitelisted site-config keys, so
site config wasn't available; frappe.conf still wins if a key is ever set there). Uses google-auth
(a frappe core dependency) + the Sheets v4 REST API directly — no gspread /
discovery client needed on the bench.

Exception categorisation calls the SAME gate helpers the release job runs
(_candidate_sos / _order_box_count / _fully_covered_by_known_combos), so the
sheet can never drift from deployed behaviour.
"""
import json
import re

import frappe
from frappe.utils import add_days, cint, flt, now_datetime, nowdate

from solara_wms.wms import d2c_fulfillment as d2c

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets/{0}"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

RUN_LOG_HEADER = [
    "Time (IST)", "Job", "Released", "Multibox", "On-Hold", "PPCOD",
    "Broken-PPCOD", "DN-Exists", "Bad-Data", "No-Stock", "Failed", "Dry-Run",
    "Labels", "Invoiced", "Fulfilled", "Errors", "AWB-Shortfall", "Failure detail",
]
EXCEPTIONS_HEADER = ["Sales Order", "Order Date", "Customer", "Amount",
                     "Category", "What to do"]
AUTO_SHIPPED_HEADER = ["Shopify Order", "Sales Order", "Delivery Note", "Date",
                       "Released At", "Customer", "Amount", "Boxes", "AWB(s)", "Status"]


def push_ops_sheet():
    """Scheduler entry (*/30). Wrapped so a defect can never wedge the shared
    scheduler (same contract as the other D2C jobs)."""
    try:
        settings = d2c._settings()
        if not cint(settings.get("ops_sheet_enabled")):
            return
        sheet_id = (settings.get("ops_sheet_id") or "").strip()
        # Site config would be preferable, but FC's dashboard only allows
        # whitelisted config keys — so the key lives in a Password field
        # (encrypted in __Auth, masked over REST). site config still wins if set.
        sa_key = frappe.conf.get("ops_sheet_sa_key")
        if not sa_key:
            try:
                sa_key = frappe.get_doc(d2c.SETTINGS_DOCTYPE).get_password(
                    "ops_sheet_sa_key", raise_exception=False)
            except Exception:
                sa_key = None
        if not sheet_id or not sa_key:
            d2c._log("D2C Ops Sheet", "enabled, but ops_sheet_id or the "
                     "ops_sheet_sa_key secret is missing — skipped")
            return
        session = _session(sa_key)
        stamp = now_datetime().strftime("%Y-%m-%d %H:%M")
        _write_tab(session, sheet_id, "Auto-Shipped (EXCLUDE from sheet)",
                   AUTO_SHIPPED_HEADER, _auto_shipped_rows(),
                   "⛔ Atlas already shipped these — SKIP them on the manual sheet "
                   "(AWB exists; shipping again = double shipment). Refreshed {0} (server)".format(stamp))
        _write_tab(session, sheet_id, "Exceptions (ship manually)",
                   EXCEPTIONS_HEADER, _exception_rows(settings),
                   "✅ Orders the automation is NOT shipping — the manual/sheet team's "
                   "to process. Refreshed {0} (server)".format(stamp))
        _write_tab(session, sheet_id, "Run Log",
                   RUN_LOG_HEADER, _run_log_rows(),
                   "D2C auto-fulfillment run log — one row per 15-min window "
                   "(last 3 days). Refreshed {0} (server)".format(stamp))
    except Exception:
        frappe.db.rollback()
        d2c._log("D2C Ops Sheet", "FATAL (swallowed): " + frappe.get_traceback())


# ------------------------------------------------------------------ auth + IO

def _session(sa_key):
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession
    info = json.loads(sa_key) if isinstance(sa_key, str) else sa_key
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(creds)


def _write_tab(session, sheet_id, title, header, rows, note):
    base = SHEETS_API.format(sheet_id)
    meta = session.get(base + "?fields=sheets.properties.title", timeout=30)
    meta.raise_for_status()
    titles = {s["properties"]["title"] for s in meta.json().get("sheets", [])}
    if title not in titles:
        r = session.post(base + ":batchUpdate", json={
            "requests": [{"addSheet": {"properties": {"title": title}}}]}, timeout=30)
        r.raise_for_status()
    quoted = "'{0}'".format(title.replace("'", "''"))
    r = session.post(base + "/values/{0}!A1:Z20000:clear".format(quoted), json={}, timeout=30)
    r.raise_for_status()
    values = [[note], [str(h) for h in header]] + [[str(c) for c in row] for row in rows]
    r = session.put(
        base + "/values/{0}!A1?valueInputOption=RAW".format(quoted),
        json={"values": values}, timeout=60)
    r.raise_for_status()


# ------------------------------------------------------------------- tab data

def _run_log_rows(days=3):
    since = add_days(nowdate(), -days)
    logs = frappe.get_all(
        "Error Log",
        filters={"method": ["like", "D2C%"], "creation": [">=", since]},
        fields=["creation", "method", "error"],
        order_by="creation asc",
        limit_page_length=0,
    )
    out = []
    for r in logs:
        t = str(r.creation)[:19]
        kv = dict(re.findall(r"(\w+)=(\d+)", (r.error or "").split("\n")[0]))
        if r.method.startswith("D2C Release"):
            fails = ""
            m = re.search(r"failures=(\[.*\])\s*$", r.error or "", re.S)
            if m:
                try:
                    fails = "; ".join(
                        "{0}: {1}".format(f.get("so"), (f.get("err") or "").split("\n")[0][:80])
                        for f in json.loads(m.group(1)))
                except Exception:
                    fails = m.group(1)[:150]
            out.append([t, "release", kv.get("created", ""), kv.get("skipped_multibox", ""),
                        kv.get("skipped_on_hold", ""), kv.get("skipped_ppcod", ""),
                        kv.get("skipped_broken_ppcod", ""), kv.get("skipped_dn_exists", ""),
                        kv.get("skipped_bad_data", ""), kv.get("skipped_nostock", ""),
                        kv.get("failed", ""), kv.get("dry_run", ""),
                        "", "", "", "", "", fails])
        elif r.method.startswith("D2C Label Fetch") and "attached=" in (r.error or ""):
            out.append([t, "labels", "", "", "", "", "", "", "", "", "", "",
                        kv.get("attached", ""), kv.get("invoiced", ""),
                        kv.get("fulfilled", ""), kv.get("errors", ""),
                        kv.get("awb_shortfall", ""), ""])
        elif r.method.startswith("D2C AWB Guard"):
            out.append([t, "awb-guard", "", "", "", "", "", "", "", "", "", "",
                        "", "", "", "", "1", (r.error or "").split("\n")[0][:150]])
        elif r.method.startswith("D2C Prepare Wave"):
            out.append([t, "wave", "", "", "", "", "", "", "", "", "", "",
                        "", "", "", "", "", (r.error or "").split("\n")[0][:150]])
    return out


def _recent_failures():
    """SO -> first-line reason from the recent release logs (dedup, newest wins)."""
    reasons = {}
    for r in frappe.get_all(
            "Error Log",
            filters={"method": ["like", "D2C Release%"],
                     "creation": [">=", add_days(nowdate(), -2)]},
            fields=["error"], order_by="creation desc", limit_page_length=8):
        m = re.search(r"failures=(\[.*\])\s*$", r.error or "", re.S)
        if not m:
            continue
        try:
            for f in json.loads(m.group(1)):
                reasons.setdefault(f.get("so"), (f.get("err") or "").split("\n")[0][:120])
        except Exception:
            pass
    return reasons


def _exception_rows(settings):
    """Categorise currently-pending SHP orders with the SAME gates the release
    job applies — direct calls into d2c_fulfillment, zero re-implementation."""
    limit = cint(settings.get("release_batch_size")) or 200
    candidates = d2c._candidate_sos(settings, limit)
    already = d2c._sos_with_existing_dn([c.name for c in candidates])
    box_map = d2c._box_config(settings)
    release_ppcod = cint(settings.get("release_ppcod"))
    release_known = cint(settings.get("release_known_combos"))
    failed_reasons = _recent_failures()

    rows = []
    for cand in candidates:
        if cand.name in already:
            continue
        try:
            so = frappe.get_doc("Sales Order", cand.name)
        except Exception:
            continue
        base = [so.name, str(so.transaction_date), so.customer_name or "",
                round(flt(so.grand_total), 2)]
        order_type = (so.get("custom_order_type") or "")
        if so.name in failed_reasons:
            rows.append(base + ["GUARD-FAILED", failed_reasons[so.name]])
        elif any(not it.item_code for it in so.items):
            rows.append(base + ["BAD-DATA", "SO line without item_code — fix or cancel"])
        elif cint(so.get("custom_shopify_hold")):
            rows.append(base + ["ON-HOLD", "Shopify hold flag — clears on next sync"])
        elif order_type == "PPCOD" and not release_ppcod:
            rows.append(base + ["PPCOD", "Phase-1 exclusion — ship via manual sheet"])
        elif (order_type == "PPCOD" and flt(so.get("custom_cod_amount")) < 0.5
              and flt(so.get("grand_total")) > 0.5):
            rows.append(base + ["BROKEN-PPCOD", "PPCOD with COD amount 0 — fix classification"])
        else:
            bc = d2c._order_box_count(so, box_map, settings)
            if bc >= 2 and not (release_known and
                                d2c._fully_covered_by_known_combos(so, box_map, settings)):
                detail = "+".join(sorted({it.item_code for it in so.items if it.item_code})[:4])
                rows.append(base + ["MULTIBOX ({0} boxes)".format(bc),
                                    "ship via manual sheet — " + detail])
        # anything else releases on the next */15 window — not an exception
    return rows


def _auto_shipped_rows(days=7):
    since = add_days(nowdate(), -days)
    dns = frappe.get_all(
        "Delivery Note",
        filters={"custom_d2c_defer_si": 1, "docstatus": 1,
                 "posting_date": [">=", since]},
        fields=["name", "posting_date", "creation", "customer_name",
                "shopify_order_id", "awb_number", "custom_awb_2",
                "custom_box_count", "grand_total", "custom_shopify_fulfilled",
                "per_billed", "custom_awb_shortfall"],
        order_by="creation desc",
        limit_page_length=0,
    )
    so_of = {}
    names = [d.name for d in dns]
    if names:
        for r in frappe.get_all(
                "Delivery Note Item",
                filters={"parent": ["in", names], "docstatus": 1},
                fields=["parent", "against_sales_order"],
                limit_page_length=0):
            if r.against_sales_order:
                so_of.setdefault(r.parent, r.against_sales_order)
    rows = []
    for d in dns:
        awbs = d.awb_number or ""
        if d.get("custom_awb_2"):
            awbs += " + " + d.custom_awb_2
        if cint(d.get("custom_awb_shortfall")):
            status = "HELD (AWB shortfall)"
        elif flt(d.per_billed) == 100 and cint(d.get("custom_shopify_fulfilled")):
            status = "Done (fulfilled + invoiced)"
        elif cint(d.get("custom_shopify_fulfilled")):
            status = "Fulfilled, invoice pending"
        else:
            status = "Label/fulfillment in progress"
        rows.append([
            str(d.shopify_order_id or ""), so_of.get(d.name, ""), d.name,
            str(d.posting_date), str(d.creation)[11:16], d.customer_name or "",
            round(flt(d.grand_total), 2), cint(d.get("custom_box_count")) or 1,
            awbs, status,
        ])
    return rows
