"""Shadow-mode allocation and replenishment work services.

The request transaction owns commit/rollback. These APIs never create or submit
ERPNext stock documents.
"""

import hashlib

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from solara_wms.wms.inventory import (
    MOVEMENT_DOCTYPE,
    _balance_name,
    _conflict,
    _idempotency_key,
    _locked_balances,
    _movement_doc,
    _require_shadow_write,
    _validate_bin,
)
from solara_wms.wms.inventory_domain import (
    BalanceState,
    InventoryInvariantError,
    allocate_balance,
    canonical_qty,
    complete_allocated_move,
    decimal_qty,
    execute_allocated_pick,
    plan_replenishment,
    release_allocation,
    request_hash,
)


WORK_DOCTYPE = "WMS Work"
WORK_LINE_DOCTYPE = "WMS Work Line"
WORK_EVENT_DOCTYPE = "WMS Work Event"


def _decimal(value):
    try:
        return decimal_qty(value)
    except InventoryInvariantError as exc:
        frappe.throw(_(str(exc)))


def _domain(function, *args):
    try:
        return function(*args)
    except InventoryInvariantError as exc:
        frappe.throw(_(str(exc)))


def _ensure_creation_key_available(idempotency_key):
    if frappe.db.get_value(
        MOVEMENT_DOCTYPE, {"idempotency_key": idempotency_key}, "name"
    ):
        _conflict(_("Idempotency Key was already used for a physical movement"))
    if frappe.db.get_value(
        WORK_EVENT_DOCTYPE, {"idempotency_key": idempotency_key}, "name"
    ):
        _conflict(_("Idempotency Key was already used for a work command"))


def _work_line(work_name):
    rows = frappe.db.get_all(
        WORK_LINE_DOCTYPE,
        filters={"parent": work_name, "parenttype": WORK_DOCTYPE},
        fields=[
            "name",
            "state",
            "item_code",
            "source_bin",
            "target_bin",
            "requested_qty",
            "allocated_qty",
            "executed_qty",
        ],
        order_by="idx asc",
        limit_page_length=2,
    )
    if len(rows) != 1:
        frappe.throw(_("WMS Work must contain exactly one line"))
    return rows[0]


def _work_result(work_name, replayed=False):
    work = frappe.db.get_value(
        WORK_DOCTYPE,
        work_name,
        [
            "name",
            "work_type",
            "status",
            "priority",
            "warehouse",
            "assigned_to",
            "reference_doctype",
            "reference_name",
            "parcel_awb",
            "pack_handoff",
            "packed_at",
            "created_event",
            "last_event",
        ],
        as_dict=True,
    )
    if not work:
        frappe.throw(_("WMS Work {0} does not exist").format(work_name))
    line = _work_line(work_name)
    return {
        **dict(work),
        "line": {
            **dict(line),
            "requested_qty": flt(line.requested_qty),
            "allocated_qty": flt(line.allocated_qty),
            "executed_qty": flt(line.executed_qty),
        },
        "replayed": bool(replayed),
    }


def _event_result(doc, replayed=False):
    result = {
        "event": doc.name,
        "event_type": doc.event_type,
        "work": doc.work,
        "warehouse": doc.warehouse,
        "item_code": doc.item_code,
        "status_before": doc.status_before,
        "status_after": doc.status_after,
        "allocated_before": flt(doc.allocated_before),
        "allocated_after": flt(doc.allocated_after),
        "executed_before": flt(doc.executed_before),
        "executed_after": flt(doc.executed_after),
        "event_qty": flt(doc.event_qty),
        "scanned_bin": doc.scanned_bin,
        "exception_code": doc.exception_code,
        "movement": doc.movement,
        "replayed": bool(replayed),
    }
    result["work_state"] = _work_result(doc.work)
    return result


def _existing_work(idempotency_key, expected_hash):
    row = frappe.db.get_value(
        WORK_DOCTYPE,
        {"creation_idempotency_key": idempotency_key},
        ["name", "creation_request_hash", "created_event"],
        as_dict=True,
    )
    if not row:
        return None
    if row.creation_request_hash != expected_hash:
        _conflict(_("Idempotency Key was already used for different work"))
    if not row.created_event:
        frappe.throw(_("WMS Work creation evidence is missing"))
    return _event_result(
        frappe.get_doc(WORK_EVENT_DOCTYPE, row.created_event), replayed=True
    )


def _existing_event(idempotency_key, expected_hash):
    name = frappe.db.get_value(
        WORK_EVENT_DOCTYPE, {"idempotency_key": idempotency_key}, "name"
    )
    if not name:
        return None
    doc = frappe.get_doc(WORK_EVENT_DOCTYPE, name)
    if doc.request_hash != expected_hash:
        _conflict(_("Idempotency Key was already used for a different work command"))
    return _event_result(doc, replayed=True)


def _work_event(payload, hash_value, **values):
    doc = frappe.get_doc(
        {
            "doctype": WORK_EVENT_DOCTYPE,
            "idempotency_key": payload["idempotency_key"],
            "request_hash": hash_value,
            "posted_at": now_datetime(),
            "posted_by": frappe.session.user,
            **values,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


def _set_balance_allocation(balance_name, before, after):
    frappe.db.sql(
        """
        UPDATE `tabWMS Bin Balance`
           SET allocated_qty = %s, available_qty = %s,
               modified = %s, modified_by = %s, last_updated_by = %s
         WHERE name = %s AND available_qty >= %s
        """,
        (
            float(after.allocated),
            float(after.available),
            now_datetime(),
            frappe.session.user,
            frappe.session.user,
            balance_name,
            float(after.allocated - before.allocated),
        ),
    )
    if frappe.db.sql("SELECT ROW_COUNT()")[0][0] != 1:
        frappe.throw(_("Balance changed concurrently; retry with the same request"))


def _create_work(
    payload,
    hash_value,
    work_type,
    warehouse,
    item_code,
    source_bin,
    qty,
    target_bin=None,
    priority="Medium",
    assigned_to=None,
    reference_doctype=None,
    reference_name=None,
    parcel_awb=None,
    device_id=None,
    notes=None,
):
    work = frappe.get_doc(
        {
            "doctype": WORK_DOCTYPE,
            "work_type": work_type,
            "status": "Allocated",
            "priority": priority or "Medium",
            "warehouse": warehouse,
            "assigned_to": assigned_to,
            "creation_idempotency_key": payload["idempotency_key"],
            "creation_request_hash": hash_value,
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "parcel_awb": parcel_awb,
            "created_at": now_datetime(),
            "created_by": frappe.session.user,
            "notes": notes,
            "lines": [
                {
                    "state": "Allocated",
                    "item_code": item_code,
                    "source_bin": source_bin,
                    "target_bin": target_bin,
                    "requested_qty": float(qty),
                    "allocated_qty": float(qty),
                    "executed_qty": 0,
                }
            ],
        }
    )
    work.insert(ignore_permissions=True)
    event = _work_event(
        payload,
        hash_value,
        event_type="Allocate",
        work=work.name,
        warehouse=warehouse,
        item_code=item_code,
        status_before="",
        status_after="Allocated",
        allocated_before=0,
        allocated_after=float(qty),
        executed_before=0,
        executed_after=0,
        device_id=device_id,
        notes=notes,
    )
    frappe.db.sql(
        """
        UPDATE `tabWMS Work`
           SET created_event = %s, last_event = %s
         WHERE name = %s
        """,
        (event.name, event.name, work.name),
    )
    return _event_result(event)


def _location_policy(warehouse, item_code, bin_name, location_role):
    row = frappe.db.get_value(
        "WMS Item Location",
        {
            "warehouse": warehouse,
            "item_code": item_code,
            "bin": bin_name,
            "location_role": location_role,
            "is_active": 1,
        },
        [
            "name",
            "minimum_qty",
            "maximum_qty",
            "replenish_qty",
            "priority",
        ],
        as_dict=True,
    )
    if not row:
        frappe.throw(
            _("{0} is not an active {1} location for {2}").format(
                bin_name, location_role, item_code
            )
        )
    return row


def _locked_work(work_name):
    work_rows = frappe.db.sql(
        """
        SELECT name, work_type, status, warehouse
          FROM `tabWMS Work`
         WHERE name = %s
         FOR UPDATE
        """,
        (work_name,),
        as_dict=True,
    )
    if not work_rows:
        frappe.throw(_("WMS Work {0} does not exist").format(work_name))
    line_rows = frappe.db.sql(
        """
        SELECT name, state, item_code, source_bin, target_bin, requested_qty,
               allocated_qty, executed_qty
          FROM `tabWMS Work Line`
         WHERE parent = %s AND parenttype = %s
         ORDER BY idx
         FOR UPDATE
        """,
        (work_name, WORK_DOCTYPE),
        as_dict=True,
    )
    if len(line_rows) != 1:
        frappe.throw(_("WMS Work must contain exactly one line"))
    return work_rows[0], line_rows[0]


@frappe.whitelist(methods=["POST"])
def create_pick_work(
    idempotency_key,
    warehouse,
    item_code,
    source_bin,
    qty,
    priority="Medium",
    assigned_to=None,
    reference_doctype=None,
    reference_name=None,
    parcel_awb=None,
    device_id=None,
    notes=None,
):
    """Atomically allocate available physical quantity for one bounded pick line."""
    _require_shadow_write(warehouse)
    key = _idempotency_key(idempotency_key)
    _validate_bin(warehouse, source_bin)
    allocation_qty = _decimal(qty)
    if allocation_qty <= 0:
        frappe.throw(_("Pick Quantity must be greater than zero"))
    parcel_awb = (parcel_awb or "").strip()
    if parcel_awb and (reference_doctype != "Delivery Note" or not reference_name):
        frappe.throw(_("Parcel AWB pick work requires a Delivery Note reference"))
    payload = {
        "command": "Allocate Pick",
        "idempotency_key": key,
        "warehouse": warehouse,
        "item_code": item_code,
        "source_bin": source_bin,
        "qty": canonical_qty(allocation_qty),
        "priority": priority or "Medium",
        "assigned_to": assigned_to or "",
        "reference_doctype": reference_doctype or "",
        "reference_name": reference_name or "",
        "parcel_awb": parcel_awb,
        "device_id": (device_id or "").strip(),
        "notes": notes or "",
    }
    hash_value = request_hash(payload)
    replay = _existing_work(key, hash_value)
    if replay:
        return replay
    _ensure_creation_key_available(key)

    balance_name = _balance_name(warehouse, source_bin, item_code)
    locked = _locked_balances([balance_name])
    if balance_name not in locked:
        frappe.throw(_("Source bin has no physical balance for item {0}").format(item_code))
    row = locked[balance_name]
    before = BalanceState.from_values(
        row.physical_qty, row.allocated_qty, row.hold_qty
    )
    after = _domain(allocate_balance, before, allocation_qty)
    _set_balance_allocation(balance_name, before, after)
    return _create_work(
        payload,
        hash_value,
        "Pick",
        warehouse,
        item_code,
        source_bin,
        allocation_qty,
        priority=priority,
        assigned_to=assigned_to,
        reference_doctype=reference_doctype,
        reference_name=reference_name,
        parcel_awb=parcel_awb,
        device_id=payload["device_id"],
        notes=notes,
    )


@frappe.whitelist(methods=["POST"])
def create_replenishment_work(
    idempotency_key,
    warehouse,
    item_code,
    source_bin,
    target_bin,
    priority="Medium",
    assigned_to=None,
    device_id=None,
    notes=None,
):
    """Allocate a policy-bounded Reserve-to-Home replenishment."""
    _require_shadow_write(warehouse)
    key = _idempotency_key(idempotency_key)
    if source_bin == target_bin:
        frappe.throw(_("Source and Target Bin must be different"))
    _validate_bin(warehouse, source_bin)
    _validate_bin(warehouse, target_bin)
    _location_policy(warehouse, item_code, source_bin, "Reserve")
    home = _location_policy(warehouse, item_code, target_bin, "Home")
    payload = {
        "command": "Allocate Replenishment",
        "idempotency_key": key,
        "warehouse": warehouse,
        "item_code": item_code,
        "source_bin": source_bin,
        "target_bin": target_bin,
        "priority": priority or "Medium",
        "assigned_to": assigned_to or "",
        "device_id": (device_id or "").strip(),
        "notes": notes or "",
    }
    hash_value = request_hash(payload)
    replay = _existing_work(key, hash_value)
    if replay:
        return replay
    _ensure_creation_key_available(key)

    source_name = _balance_name(warehouse, source_bin, item_code)
    target_name = _balance_name(warehouse, target_bin, item_code)
    locked = _locked_balances([source_name, target_name])
    if source_name not in locked or target_name not in locked:
        frappe.throw(_("Both Reserve and Home bins require physical balances"))
    source_row = locked[source_name]
    target_row = locked[target_name]
    source = BalanceState.from_values(
        source_row.physical_qty, source_row.allocated_qty, source_row.hold_qty
    )
    target = BalanceState.from_values(
        target_row.physical_qty, target_row.allocated_qty, target_row.hold_qty
    )
    qty = _domain(
        plan_replenishment,
        source,
        target,
        home.minimum_qty,
        home.maximum_qty,
        home.replenish_qty,
    )
    source_after = _domain(allocate_balance, source, qty)
    _set_balance_allocation(source_name, source, source_after)
    return _create_work(
        payload,
        hash_value,
        "Replenishment",
        warehouse,
        item_code,
        source_bin,
        qty,
        target_bin=target_bin,
        priority=priority,
        assigned_to=assigned_to,
        device_id=payload["device_id"],
        notes=notes,
    )


@frappe.whitelist(methods=["POST"])
def release_work(idempotency_key, warehouse, work, device_id=None, notes=None):
    """Release all unexecuted allocation and cancel one work line."""
    _require_shadow_write(warehouse)
    key = _idempotency_key(idempotency_key)
    payload = {
        "command": "Release Work",
        "idempotency_key": key,
        "warehouse": warehouse,
        "work": work,
        "device_id": (device_id or "").strip(),
        "notes": notes or "",
    }
    hash_value = request_hash(payload)
    replay = _existing_event(key, hash_value)
    if replay:
        return replay
    if frappe.db.get_value(
        MOVEMENT_DOCTYPE, {"idempotency_key": key}, "name"
    ):
        _conflict(_("Idempotency Key was already used for a physical movement"))

    work_row, line = _locked_work(work)
    if work_row.warehouse != warehouse:
        frappe.throw(_("WMS Work does not belong to warehouse {0}").format(warehouse))
    if work_row.status != "Allocated" or line.state != "Allocated":
        frappe.throw(_("Only Allocated work can be released"))
    qty = _decimal(line.allocated_qty)
    if qty <= 0:
        frappe.throw(_("Work has no allocation to release"))
    balance_name = _balance_name(warehouse, line.source_bin, line.item_code)
    locked = _locked_balances([balance_name])
    if balance_name not in locked:
        frappe.throw(_("Source balance does not exist"))
    row = locked[balance_name]
    before = BalanceState.from_values(
        row.physical_qty, row.allocated_qty, row.hold_qty
    )
    after = _domain(release_allocation, before, qty)
    frappe.db.sql(
        """
        UPDATE `tabWMS Bin Balance`
           SET allocated_qty = %s, available_qty = %s, modified = %s,
               modified_by = %s, last_updated_by = %s
         WHERE name = %s AND allocated_qty >= %s
        """,
        (
            float(after.allocated),
            float(after.available),
            now_datetime(),
            frappe.session.user,
            frappe.session.user,
            balance_name,
            float(qty),
        ),
    )
    if frappe.db.sql("SELECT ROW_COUNT()")[0][0] != 1:
        frappe.throw(_("Allocation changed concurrently; retry with the same request"))
    frappe.db.sql(
        """
        UPDATE `tabWMS Work Line`
           SET state = 'Cancelled', allocated_qty = 0
         WHERE name = %s
        """,
        (line.name,),
    )
    event = _work_event(
        payload,
        hash_value,
        event_type="Release",
        work=work,
        warehouse=warehouse,
        item_code=line.item_code,
        status_before="Allocated",
        status_after="Cancelled",
        allocated_before=float(qty),
        allocated_after=0,
        executed_before=flt(line.executed_qty),
        executed_after=flt(line.executed_qty),
        device_id=payload["device_id"],
        notes=notes,
    )
    frappe.db.sql(
        """
        UPDATE `tabWMS Work`
           SET status = 'Cancelled', last_event = %s,
               modified = %s, modified_by = %s
         WHERE name = %s
        """,
        (event.name, now_datetime(), frappe.session.user, work),
    )
    return _event_result(event)


@frappe.whitelist(methods=["POST"])
def complete_replenishment(
    idempotency_key,
    warehouse,
    work,
    device_id=None,
    notes=None,
):
    """Complete one allocated replenishment and atomically move physical stock."""
    _require_shadow_write(warehouse)
    key = _idempotency_key(idempotency_key)
    payload = {
        "command": "Complete Replenishment",
        "idempotency_key": key,
        "warehouse": warehouse,
        "work": work,
        "device_id": (device_id or "").strip(),
        "notes": notes or "",
    }
    hash_value = request_hash(payload)
    replay = _existing_event(key, hash_value)
    if replay:
        return replay
    if frappe.db.get_value(
        MOVEMENT_DOCTYPE, {"idempotency_key": key}, "name"
    ):
        _conflict(_("Idempotency Key was already used for a physical movement"))

    work_row, line = _locked_work(work)
    if work_row.warehouse != warehouse:
        frappe.throw(_("WMS Work does not belong to warehouse {0}").format(warehouse))
    if work_row.work_type != "Replenishment":
        frappe.throw(_("Only Replenishment work can use this command"))
    if work_row.status != "Allocated" or line.state != "Allocated":
        frappe.throw(_("Only Allocated replenishment can be completed"))
    qty = _decimal(line.allocated_qty)
    if qty <= 0:
        frappe.throw(_("Replenishment has no allocated quantity"))
    home = _location_policy(
        warehouse, line.item_code, line.target_bin, "Home"
    )
    source_name = _balance_name(warehouse, line.source_bin, line.item_code)
    target_name = _balance_name(warehouse, line.target_bin, line.item_code)
    locked = _locked_balances([source_name, target_name])
    if source_name not in locked or target_name not in locked:
        frappe.throw(_("Both Reserve and Home balances must exist"))
    source_row = locked[source_name]
    target_row = locked[target_name]
    source = BalanceState.from_values(
        source_row.physical_qty, source_row.allocated_qty, source_row.hold_qty
    )
    target = BalanceState.from_values(
        target_row.physical_qty, target_row.allocated_qty, target_row.hold_qty
    )
    maximum = _decimal(home.maximum_qty)
    if maximum <= 0 or target.physical + qty > maximum:
        frappe.throw(_("Replenishment would exceed the Home maximum quantity"))
    source_after, target_after = _domain(
        complete_allocated_move, source, target, qty
    )
    now = now_datetime()
    frappe.db.sql(
        """
        UPDATE `tabWMS Bin Balance`
           SET physical_qty = %s, allocated_qty = %s, available_qty = %s,
               modified = %s, modified_by = %s, last_updated_by = %s
         WHERE name = %s AND physical_qty >= %s AND allocated_qty >= %s
        """,
        (
            float(source_after.physical),
            float(source_after.allocated),
            float(source_after.available),
            now,
            frappe.session.user,
            frappe.session.user,
            source_name,
            float(qty),
            float(qty),
        ),
    )
    if frappe.db.sql("SELECT ROW_COUNT()")[0][0] != 1:
        frappe.throw(_("Source changed concurrently; retry with the same request"))
    frappe.db.sql(
        """
        UPDATE `tabWMS Bin Balance`
           SET physical_qty = %s, available_qty = %s, modified = %s,
               modified_by = %s, last_updated_by = %s
         WHERE name = %s
        """,
        (
            float(target_after.physical),
            float(target_after.available),
            now,
            frappe.session.user,
            frappe.session.user,
            target_name,
        ),
    )

    physical_key = "work-physical:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    movement_payload = {
        "movement_type": "Replenishment",
        "idempotency_key": physical_key,
        "warehouse": warehouse,
        "source_bin": line.source_bin,
        "target_bin": line.target_bin,
        "item_code": line.item_code,
        "qty": canonical_qty(qty),
        "work": work,
    }
    movement = _movement_doc(
        movement_payload,
        request_hash(movement_payload),
        movement_type="Replenishment",
        warehouse=warehouse,
        item_code=line.item_code,
        source_bin=line.source_bin,
        target_bin=line.target_bin,
        qty=float(qty),
        source_before=flt(source_row.physical_qty),
        source_after=float(source_after.physical),
        target_before=flt(target_row.physical_qty),
        target_after=float(target_after.physical),
        device_id=payload["device_id"],
        reference_doctype=WORK_DOCTYPE,
        reference_name=work,
        notes=notes,
    )
    frappe.db.sql(
        """
        UPDATE `tabWMS Bin Balance`
           SET last_movement = %s
         WHERE name IN (%s, %s)
        """,
        (movement.name, source_name, target_name),
    )
    frappe.db.sql(
        """
        UPDATE `tabWMS Work Line`
           SET state = 'Completed', allocated_qty = 0, executed_qty = %s
         WHERE name = %s
        """,
        (float(qty), line.name),
    )
    event = _work_event(
        payload,
        hash_value,
        event_type="Complete Replenishment",
        work=work,
        warehouse=warehouse,
        item_code=line.item_code,
        status_before="Allocated",
        status_after="Completed",
        allocated_before=float(qty),
        allocated_after=0,
        executed_before=flt(line.executed_qty),
        executed_after=float(qty),
        movement=movement.name,
        device_id=payload["device_id"],
        notes=notes,
    )
    frappe.db.sql(
        """
        UPDATE `tabWMS Work`
           SET status = 'Completed', last_event = %s, completed_at = %s,
               completed_by = %s, modified = %s, modified_by = %s
         WHERE name = %s
        """,
        (
            event.name,
            now,
            frappe.session.user,
            now,
            frappe.session.user,
            work,
        ),
    )
    return _event_result(event)


PICK_SHORTAGE_CODES = {
    "Bin Empty",
    "Quantity Short",
    "Damaged Stock",
    "Stock Not Found",
    "Other",
}


@frappe.whitelist(methods=["POST"])
def scan_pick(
    idempotency_key,
    warehouse,
    work,
    scanned_bin,
    scanned_item_code,
    qty=1,
    device_id=None,
    notes=None,
):
    """Execute one idempotent scanner pick without touching ERP stock."""
    _require_shadow_write(warehouse)
    key = _idempotency_key(idempotency_key)
    _validate_bin(warehouse, scanned_bin)
    scan_qty = _decimal(qty)
    if scan_qty <= 0:
        frappe.throw(_("Pick Quantity must be greater than zero"))
    payload = {
        "command": "Pick Scan",
        "idempotency_key": key,
        "warehouse": warehouse,
        "work": work,
        "scanned_bin": scanned_bin,
        "scanned_item_code": scanned_item_code,
        "qty": canonical_qty(scan_qty),
        "device_id": (device_id or "").strip(),
        "notes": notes or "",
    }
    hash_value = request_hash(payload)
    replay = _existing_event(key, hash_value)
    if replay:
        return replay
    if frappe.db.get_value(MOVEMENT_DOCTYPE, {"idempotency_key": key}, "name"):
        _conflict(_("Idempotency Key was already used for a physical movement"))

    work_row, line = _locked_work(work)
    if work_row.warehouse != warehouse:
        frappe.throw(_("WMS Work does not belong to warehouse {0}").format(warehouse))
    if work_row.work_type != "Pick":
        frappe.throw(_("Only Pick work can use this command"))
    if work_row.status not in ("Allocated", "In Progress") or line.state not in (
        "Allocated",
        "In Progress",
    ):
        frappe.throw(_("Only open Pick work can be scanned"))
    if scanned_bin != line.source_bin:
        frappe.throw(_("Wrong bin: scan assigned bin {0}").format(line.source_bin))
    if scanned_item_code != line.item_code:
        frappe.throw(_("Wrong item: scan assigned item {0}").format(line.item_code))

    balance_name = _balance_name(warehouse, line.source_bin, line.item_code)
    locked = _locked_balances([balance_name])
    if balance_name not in locked:
        frappe.throw(_("Source balance does not exist"))
    row = locked[balance_name]
    before = BalanceState.from_values(
        row.physical_qty, row.allocated_qty, row.hold_qty
    )
    after, remaining, executed = _domain(
        execute_allocated_pick,
        before,
        line.allocated_qty,
        line.executed_qty,
        scan_qty,
    )
    now = now_datetime()
    frappe.db.sql(
        """
        UPDATE `tabWMS Bin Balance`
           SET physical_qty = %s, allocated_qty = %s, available_qty = %s,
               modified = %s, modified_by = %s, last_updated_by = %s
         WHERE name = %s AND physical_qty >= %s AND allocated_qty >= %s
        """,
        (
            float(after.physical),
            float(after.allocated),
            float(after.available),
            now,
            frappe.session.user,
            frappe.session.user,
            balance_name,
            float(scan_qty),
            float(scan_qty),
        ),
    )
    if frappe.db.sql("SELECT ROW_COUNT()")[0][0] != 1:
        frappe.throw(_("Pick balance changed concurrently; retry with the same request"))

    state_after = "Completed" if remaining == 0 else "In Progress"
    frappe.db.sql(
        """
        UPDATE `tabWMS Work Line`
           SET state = %s, allocated_qty = %s, executed_qty = %s
         WHERE name = %s
        """,
        (state_after, float(remaining), float(executed), line.name),
    )
    physical_key = "pick-physical:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    movement_payload = {
        "movement_type": "Pick",
        "idempotency_key": physical_key,
        "warehouse": warehouse,
        "source_bin": line.source_bin,
        "item_code": line.item_code,
        "qty": canonical_qty(scan_qty),
        "work": work,
    }
    movement = _movement_doc(
        movement_payload,
        request_hash(movement_payload),
        movement_type="Pick",
        warehouse=warehouse,
        item_code=line.item_code,
        source_bin=line.source_bin,
        qty=float(scan_qty),
        source_before=flt(row.physical_qty),
        source_after=float(after.physical),
        device_id=payload["device_id"],
        reference_doctype=WORK_DOCTYPE,
        reference_name=work,
        notes=notes,
    )
    frappe.db.sql(
        """
        UPDATE `tabWMS Bin Balance`
           SET last_movement = %s
         WHERE name = %s
        """,
        (movement.name, balance_name),
    )
    event = _work_event(
        payload,
        hash_value,
        event_type="Pick Scan",
        work=work,
        warehouse=warehouse,
        item_code=line.item_code,
        status_before=work_row.status,
        status_after=state_after,
        allocated_before=flt(line.allocated_qty),
        allocated_after=float(remaining),
        executed_before=flt(line.executed_qty),
        executed_after=float(executed),
        event_qty=float(scan_qty),
        scanned_bin=scanned_bin,
        movement=movement.name,
        device_id=payload["device_id"],
        notes=notes,
    )
    completed_values = (
        ", completed_at = %s, completed_by = %s" if state_after == "Completed" else ""
    )
    parameters = [
        state_after,
        event.name,
        now,
        frappe.session.user,
    ]
    if state_after == "Completed":
        parameters.extend([now, frappe.session.user])
    parameters.append(work)
    frappe.db.sql(
        f"""
        UPDATE `tabWMS Work`
           SET status = %s, last_event = %s, modified = %s, modified_by = %s
               {completed_values}
         WHERE name = %s
        """,
        tuple(parameters),
    )
    return _event_result(event)


@frappe.whitelist(methods=["POST"])
def close_pick_shortage(
    idempotency_key,
    warehouse,
    work,
    scanned_bin,
    scanned_item_code,
    exception_code,
    device_id=None,
    notes=None,
):
    """Release an open pick remainder and retain mandatory shortage evidence."""
    _require_shadow_write(warehouse)
    key = _idempotency_key(idempotency_key)
    _validate_bin(warehouse, scanned_bin)
    code = (exception_code or "").strip()
    if code not in PICK_SHORTAGE_CODES:
        frappe.throw(_("Select a valid Pick Shortage reason"))
    if code == "Other" and not (notes or "").strip():
        frappe.throw(_("Notes are required when Pick Shortage reason is Other"))
    payload = {
        "command": "Pick Shortage",
        "idempotency_key": key,
        "warehouse": warehouse,
        "work": work,
        "scanned_bin": scanned_bin,
        "scanned_item_code": scanned_item_code,
        "exception_code": code,
        "device_id": (device_id or "").strip(),
        "notes": notes or "",
    }
    hash_value = request_hash(payload)
    replay = _existing_event(key, hash_value)
    if replay:
        return replay
    if frappe.db.get_value(MOVEMENT_DOCTYPE, {"idempotency_key": key}, "name"):
        _conflict(_("Idempotency Key was already used for a physical movement"))

    work_row, line = _locked_work(work)
    if work_row.warehouse != warehouse:
        frappe.throw(_("WMS Work does not belong to warehouse {0}").format(warehouse))
    if work_row.work_type != "Pick":
        frappe.throw(_("Only Pick work can report a Pick Shortage"))
    if work_row.status not in ("Allocated", "In Progress") or line.state not in (
        "Allocated",
        "In Progress",
    ):
        frappe.throw(_("Only open Pick work can report a shortage"))
    if scanned_bin != line.source_bin:
        frappe.throw(_("Wrong bin: scan assigned bin {0}").format(line.source_bin))
    if scanned_item_code != line.item_code:
        frappe.throw(_("Wrong item: scan assigned item {0}").format(line.item_code))

    remaining = _decimal(line.allocated_qty)
    if remaining <= 0:
        frappe.throw(_("Pick work has no remaining allocation"))
    balance_name = _balance_name(warehouse, line.source_bin, line.item_code)
    locked = _locked_balances([balance_name])
    if balance_name not in locked:
        frappe.throw(_("Source balance does not exist"))
    row = locked[balance_name]
    before = BalanceState.from_values(
        row.physical_qty, row.allocated_qty, row.hold_qty
    )
    after = _domain(release_allocation, before, remaining)
    now = now_datetime()
    frappe.db.sql(
        """
        UPDATE `tabWMS Bin Balance`
           SET allocated_qty = %s, available_qty = %s, modified = %s,
               modified_by = %s, last_updated_by = %s
         WHERE name = %s AND allocated_qty >= %s
        """,
        (
            float(after.allocated),
            float(after.available),
            now,
            frappe.session.user,
            frappe.session.user,
            balance_name,
            float(remaining),
        ),
    )
    if frappe.db.sql("SELECT ROW_COUNT()")[0][0] != 1:
        frappe.throw(_("Allocation changed concurrently; retry with the same request"))
    frappe.db.sql(
        """
        UPDATE `tabWMS Work Line`
           SET state = 'Short', allocated_qty = 0
         WHERE name = %s
        """,
        (line.name,),
    )
    event = _work_event(
        payload,
        hash_value,
        event_type="Pick Shortage",
        work=work,
        warehouse=warehouse,
        item_code=line.item_code,
        status_before=work_row.status,
        status_after="Short",
        allocated_before=float(remaining),
        allocated_after=0,
        executed_before=flt(line.executed_qty),
        executed_after=flt(line.executed_qty),
        event_qty=float(remaining),
        scanned_bin=scanned_bin,
        exception_code=code,
        device_id=payload["device_id"],
        notes=notes,
    )
    frappe.db.sql(
        """
        UPDATE `tabWMS Work`
           SET status = 'Short', last_event = %s, completed_at = %s,
               completed_by = %s, modified = %s, modified_by = %s
         WHERE name = %s
        """,
        (event.name, now, frappe.session.user, now, frappe.session.user, work),
    )
    return _event_result(event)


@frappe.whitelist(methods=["GET"])
def get_work(work):
    return _work_result(work)
