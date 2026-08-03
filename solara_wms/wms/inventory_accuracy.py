"""Blind physical counts and WMS-versus-Atlas reconciliation.

Nothing in this module creates or submits an ERP stock document.  A mismatch is
evidence to investigate; it is never authority for an automatic adjustment.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from solara_wms.wms.inventory import IdempotencyConflict, _balance_name
from solara_wms.wms.inventory_domain import (
    InventoryInvariantError,
    canonical_qty,
    decimal_qty,
    evaluate_blind_count,
    reconcile_inventory_bridge,
    request_hash,
)
from solara_wms.wms.safety import require_wms_mode


COUNT_DOCTYPE = "WMS Cycle Count"
ENTRY_DOCTYPE = "WMS Count Entry"
BALANCE_DOCTYPE = "WMS Bin Balance"
COUNTER_ROLES = {"System Manager", "Stock Manager", "Stock User"}
MANAGER_ROLES = {"System Manager", "Stock Manager"}


def _require_scope(warehouse, manager=False):
    require_wms_mode("Shadow", "Draft Handoff")
    roles = set(frappe.get_roles())
    required = MANAGER_ROLES if manager else COUNTER_ROLES
    if not roles.intersection(required):
        frappe.throw(_("A warehouse counting role is required"), frappe.PermissionError)
    pilot = frappe.db.get_single_value("WMS Settings", "pilot_warehouse")
    if pilot and warehouse != pilot:
        frappe.throw(_("Counts are restricted to pilot warehouse {0}").format(pilot))


def _key(value):
    value = (value or "").strip()
    if len(value) < 8 or len(value) > 140:
        frappe.throw(_("Idempotency Key must be between 8 and 140 characters"))
    return value


def _conflict(message):
    frappe.throw(message, exc=IdempotencyConflict, title=_("Idempotency Conflict"))


def _quantity(value):
    try:
        qty = decimal_qty(value)
    except InventoryInvariantError as exc:
        frappe.throw(_(str(exc)))
    if qty < 0:
        frappe.throw(_("Counted Quantity cannot be negative"))
    return qty


def _locked_count(name):
    row = frappe.db.sql(
        """SELECT name, warehouse, bin, status
             FROM `tabWMS Cycle Count`
            WHERE name = %s FOR UPDATE""",
        name,
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Cycle Count {0} was not found").format(name))
    return row[0]


def _entry_result(doc, replayed=False):
    return {
        "entry": doc.name,
        "cycle_count": doc.cycle_count,
        "item_code": doc.item_code,
        "bin": doc.bin,
        "attempt": int(doc.attempt),
        "counted_qty": flt(doc.counted_qty),
        "replayed": bool(replayed),
    }


@frappe.whitelist(methods=["POST"])
def start_blind_count(cycle_count):
    locked = _locked_count(cycle_count)
    _require_scope(locked.warehouse, manager=True)
    if locked.status != "Draft":
        frappe.throw(_("Only Draft counts can be started"))
    if not locked.bin:
        frappe.throw(_("A physical bin is required for a blind count"))

    doc = frappe.get_doc(COUNT_DOCTYPE, cycle_count)
    balances = frappe.db.sql(
        """SELECT name, item_code, physical_qty, last_movement
             FROM `tabWMS Bin Balance`
            WHERE warehouse = %s AND bin = %s
              AND (physical_qty != 0 OR allocated_qty != 0 OR hold_qty != 0)
            ORDER BY item_code
            FOR UPDATE""",
        (doc.warehouse, doc.bin),
        as_dict=True,
    )
    if not balances:
        frappe.throw(_("The selected physical bin has no WMS balance to count"))

    doc.items = []
    for balance in balances:
        atlas = frappe.db.get_value(
            "Bin",
            {"warehouse": doc.warehouse, "item_code": balance.item_code},
            ["valuation_rate"],
            as_dict=True,
        )
        doc.append(
            "items",
            {
                "item_code": balance.item_code,
                "bin": doc.bin,
                "snapshot_balance": balance.name,
                "snapshot_movement": balance.last_movement,
                "book_qty": flt(balance.physical_qty),
                "valuation_rate": flt(atlas.valuation_rate) if atlas else 0,
                "row_status": "Pending",
            },
        )
    doc.status = "In Progress"
    doc.snapshot_at = now_datetime()
    doc.started_by = frappe.session.user
    doc.review_status = ""
    doc.save(ignore_permissions=True)
    return get_blind_count_task(cycle_count)


@frappe.whitelist(methods=["GET"])
def get_blind_count_task(cycle_count):
    doc = frappe.get_doc(COUNT_DOCTYPE, cycle_count)
    _require_scope(doc.warehouse)
    return {
        "cycle_count": doc.name,
        "status": doc.status,
        "warehouse": doc.warehouse,
        "bin": doc.bin,
        "items": [
            {
                "item_code": row.item_code,
                "bin": row.bin,
                "first_count_recorded": row.counted_qty not in (None, ""),
                "recount_required": row.row_status == "Recount Required",
                "recount_recorded": row.recount_qty not in (None, ""),
            }
            for row in doc.items
        ],
    }


@frappe.whitelist(methods=["POST"])
def submit_blind_count(
    idempotency_key,
    cycle_count,
    item_code,
    counted_qty,
    attempt=1,
    device_id=None,
):
    key = _key(idempotency_key)
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Scan a valid Atlas Item Code"))
    attempt = int(attempt)
    if attempt not in (1, 2):
        frappe.throw(_("Count attempt must be 1 or 2"))
    qty = _quantity(counted_qty)
    locked = _locked_count(cycle_count)
    _require_scope(locked.warehouse)
    expected_status = "In Progress" if attempt == 1 else "Recount Required"
    if locked.status != expected_status:
        frappe.throw(
            _("Attempt {0} is not allowed while the count is {1}").format(
                attempt, locked.status
            )
        )

    payload = {
        "command": "Blind Count",
        "idempotency_key": key,
        "cycle_count": cycle_count,
        "item_code": (item_code or "").strip(),
        "counted_qty": canonical_qty(qty),
        "attempt": attempt,
        "device_id": (device_id or "").strip(),
    }
    hash_value = request_hash(payload)
    existing = frappe.db.get_value(ENTRY_DOCTYPE, {"idempotency_key": key}, "name")
    if existing:
        entry = frappe.get_doc(ENTRY_DOCTYPE, existing)
        if entry.request_hash != hash_value:
            _conflict(_("Idempotency Key was reused for a different count"))
        return _entry_result(entry, replayed=True)

    doc = frappe.get_doc(COUNT_DOCTYPE, cycle_count)
    row = next((r for r in doc.items if r.item_code == payload["item_code"]), None)
    if row is None:
        if attempt != 1:
            frappe.throw(_("An unexpected item must first be recorded in attempt 1"))
        balance_name = _balance_name(doc.warehouse, doc.bin, payload["item_code"])
        balance = frappe.db.get_value(
            BALANCE_DOCTYPE,
            balance_name,
            ["name", "physical_qty", "last_movement"],
            as_dict=True,
        )
        row = doc.append(
            "items",
            {
                "item_code": payload["item_code"],
                "bin": doc.bin,
                "snapshot_balance": balance.name if balance else "",
                "snapshot_movement": balance.last_movement if balance else "",
                "book_qty": flt(balance.physical_qty) if balance else 0,
                "row_status": "Pending",
            },
        )
        doc.save(ignore_permissions=True)

    duplicate = frappe.db.get_value(
        ENTRY_DOCTYPE,
        {"cycle_count_item": row.name, "attempt": attempt},
        "name",
    )
    if duplicate:
        _conflict(_("This item already has count evidence for attempt {0}").format(attempt))
    if attempt == 2 and row.counted_by == frappe.session.user:
        frappe.throw(_("The recount must be performed by a different operator"))
    if attempt == 2 and row.row_status != "Recount Required":
        frappe.throw(_("This item does not require a recount"))

    now = now_datetime()
    entry = frappe.get_doc(
        {
            "doctype": ENTRY_DOCTYPE,
            "cycle_count": cycle_count,
            "cycle_count_item": row.name,
            "attempt": attempt,
            "warehouse": doc.warehouse,
            "bin": doc.bin,
            "item_code": row.item_code,
            "counted_qty": float(qty),
            "idempotency_key": key,
            "request_hash": hash_value,
            "device_id": payload["device_id"],
            "counted_at": now,
            "counted_by": frappe.session.user,
        }
    )
    entry.insert(ignore_permissions=True)
    values = (
        {"counted_qty": float(qty), "counted_by": frappe.session.user,
         "counted_at": now, "row_status": "Counted"}
        if attempt == 1
        else {"recount_qty": float(qty), "recounted_by": frappe.session.user,
              "recounted_at": now}
    )
    frappe.db.set_value("WMS Cycle Count Item", row.name, values, update_modified=False)
    return _entry_result(entry)


def _balance_unchanged(row, warehouse):
    name = row.snapshot_balance or _balance_name(warehouse, row.bin, row.item_code)
    current = frappe.db.sql(
        """SELECT physical_qty, last_movement
             FROM `tabWMS Bin Balance`
            WHERE name = %s FOR UPDATE""",
        name,
        as_dict=True,
    )
    if not current:
        return not row.snapshot_balance and flt(row.book_qty) == 0
    current = current[0]
    return (
        flt(current.physical_qty) == flt(row.book_qty)
        and (current.last_movement or "") == (row.snapshot_movement or "")
    )


@frappe.whitelist(methods=["POST"])
def finalize_blind_count(cycle_count):
    locked = _locked_count(cycle_count)
    _require_scope(locked.warehouse, manager=True)
    if locked.status not in ("In Progress", "Recount Required"):
        frappe.throw(_("Only an active blind count can be finalized"))
    doc = frappe.get_doc(COUNT_DOCTYPE, cycle_count)
    invalid = [r for r in doc.items if not _balance_unchanged(r, doc.warehouse)]
    if invalid:
        for row in invalid:
            frappe.db.set_value(
                "WMS Cycle Count Item", row.name,
                {"row_status": "Invalidated", "error_message": "Balance moved after snapshot"},
                update_modified=False,
            )
        frappe.db.set_value(
            COUNT_DOCTYPE, doc.name,
            {"status": "Invalidated", "review_status": "Investigation Required"},
        )
        return {"cycle_count": doc.name, "status": "Invalidated",
                "invalidated_items": [r.item_code for r in invalid]}

    needs_recount = []
    review = []
    variance_value = 0
    for row in doc.items:
        if row.counted_qty in (None, ""):
            frappe.throw(_("Every listed item requires a zero or positive count"))
        try:
            result = evaluate_blind_count(
                row.book_qty,
                row.counted_qty,
                row.recount_qty if row.recount_qty not in (None, "") else None,
            )
        except InventoryInvariantError as exc:
            frappe.throw(_(str(exc)))
        status = result["status"]
        variance = result["variance_qty"]
        frappe.db.set_value(
            "WMS Cycle Count Item",
            row.name,
            {
                "row_status": status,
                "variance_qty": float(variance),
                "variance_pct": (
                    float(variance / decimal_qty(row.book_qty) * 100)
                    if flt(row.book_qty) else (100 if variance else 0)
                ),
                "variance_value": float(variance) * flt(row.valuation_rate),
                "review_status": "Pending" if variance else "Resolved",
            },
            update_modified=False,
        )
        variance_value += float(variance) * flt(row.valuation_rate)
        if status == "Recount Required":
            needs_recount.append(row.item_code)
        elif status in ("Confirmed Variance", "Counter Disagreement"):
            review.append(row.item_code)

    if needs_recount:
        status, review_status = "Recount Required", "Recount Pending"
    elif review:
        status, review_status = "Variance Review", "Investigation Required"
    else:
        status, review_status = "Completed", "Not Required"
    frappe.db.set_value(
        COUNT_DOCTYPE,
        doc.name,
        {"status": status, "review_status": review_status,
         "counted_at": now_datetime(), "last_count_date": now_datetime().date(),
         "items_with_variance": len(review),
         "total_variance_value": variance_value},
    )
    return {"cycle_count": doc.name, "status": status,
            "recount_items": needs_recount, "review_items": review}


@frappe.whitelist(methods=["POST"])
def classify_variance(
    cycle_count,
    item_code,
    reason,
    reference_doctype=None,
    reference_name=None,
):
    locked = _locked_count(cycle_count)
    _require_scope(locked.warehouse, manager=True)
    if locked.status != "Variance Review":
        frappe.throw(_("Only a count in Variance Review can be classified"))
    allowed = {
        "Location Error", "Unbooked Receipt", "Unbooked Dispatch",
        "Unbooked Transfer", "Damage / Hold", "Counting Error",
        "Confirmed Loss / Gain", "Unknown",
    }
    if reason not in allowed:
        frappe.throw(_("Select a controlled variance reason"))
    doc = frappe.get_doc(COUNT_DOCTYPE, cycle_count)
    row = next((r for r in doc.items if r.item_code == item_code), None)
    if not row or row.row_status not in ("Confirmed Variance", "Counter Disagreement"):
        frappe.throw(_("The selected item has no reviewable variance"))
    source_reasons = {
        "Location Error", "Unbooked Receipt", "Unbooked Dispatch",
        "Unbooked Transfer", "Damage / Hold", "Counting Error",
    }
    review_status = (
        "Source Correction Required" if reason in source_reasons
        else "Adjustment Approval Required"
    )
    frappe.db.set_value(
        "WMS Cycle Count Item",
        row.name,
        {"variance_reason": reason, "review_status": review_status,
         "reference_doctype": reference_doctype or "",
         "reference_name": reference_name or ""},
        update_modified=False,
    )
    return {"cycle_count": cycle_count, "item_code": item_code,
            "reason": reason, "review_status": review_status,
            "stock_document_created": False}


@frappe.whitelist(methods=["GET"])
def reconcile_warehouse(warehouse, only_variances=0):
    _require_scope(warehouse, manager=True)
    physical = defaultdict(float)
    for row in frappe.db.sql(
        """SELECT item_code, SUM(physical_qty) AS qty
             FROM `tabWMS Bin Balance`
            WHERE warehouse = %s GROUP BY item_code""",
        warehouse,
        as_dict=True,
    ):
        physical[row.item_code] = flt(row.qty)
    atlas = defaultdict(float)
    for row in frappe.get_all(
        "Bin", filters={"warehouse": warehouse}, fields=["item_code", "actual_qty"]
    ):
        atlas[row.item_code] = flt(row.actual_qty)
    outbound = defaultdict(float)
    for row in frappe.db.sql(
        """SELECT line.item_code, SUM(line.allocated_qty) AS qty
             FROM `tabWMS Work Line` line
             JOIN `tabWMS Work` work ON work.name = line.parent
            WHERE work.warehouse = %s AND work.work_type = 'Pick'
              AND work.status IN ('Allocated', 'In Progress')
              AND work.reference_doctype = 'Delivery Note'
            GROUP BY line.item_code""",
        warehouse,
        as_dict=True,
    ):
        outbound[row.item_code] = flt(row.qty)

    rows = []
    for item in sorted(set(physical) | set(atlas) | set(outbound)):
        try:
            result = reconcile_inventory_bridge(
                atlas[item], physical[item], outbound[item], 0
            )
        except InventoryInvariantError as exc:
            frappe.throw(_(str(exc)))
        variance = flt(result["unexplained_variance_qty"])
        if int(only_variances) and abs(variance) < 0.000001:
            continue
        rows.append({
            "item_code": item,
            "atlas_qty": flt(result["atlas_qty"]),
            "wms_physical_qty": flt(result["wms_physical_qty"]),
            "pending_outbound_qty": flt(result["pending_outbound_qty"]),
            "adjusted_wms_qty": flt(result["adjusted_wms_qty"]),
            "unexplained_variance_qty": variance,
            "status": "Matched" if abs(variance) < 0.000001 else "Variance",
        })
    return {
        "warehouse": warehouse,
        "generated_at": now_datetime(),
        "items": rows,
        "variance_items": sum(1 for row in rows if row["status"] == "Variance"),
    }


def scheduled_inventory_reconciliation():
    """Compact 15-minute tripwire; disabled unless the pilot is explicitly enabled."""
    enabled = frappe.db.get_single_value(
        "WMS Settings", "reconciliation_monitor_enabled"
    )
    mode = frappe.db.get_single_value("WMS Settings", "operating_mode") or "Disabled"
    warehouse = frappe.db.get_single_value("WMS Settings", "pilot_warehouse")
    if not enabled or mode == "Disabled" or not warehouse:
        return
    report = reconcile_warehouse(warehouse, only_variances=1)
    variance_count = int(report["variance_items"])
    previous = frappe.db.get_single_value(
        "WMS Settings", "last_reconciliation_status"
    ) or ""
    status = "Variance" if variance_count else "Matched"
    frappe.db.set_value(
        "WMS Settings",
        "WMS Settings",
        {
            "last_reconciliation_at": now_datetime(),
            "last_reconciliation_status": status,
            "last_unexplained_variance_items": variance_count,
        },
        update_modified=False,
    )
    if status != previous:
        sample = report["items"][:20]
        frappe.log_error(
            title="WMS Inventory Reconciliation " + status,
            message=frappe.as_json(
                {"warehouse": warehouse, "variance_items": variance_count,
                 "sample": sample}, indent=2
            ),
        )
