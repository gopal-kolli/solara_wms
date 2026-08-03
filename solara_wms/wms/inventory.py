"""Shadow-mode physical-bin inventory services.

These APIs never create or submit ERPNext stock documents. The request
transaction owns the commit/rollback boundary.
"""

from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from solara_wms.wms.inventory_domain import (
    BalanceState,
    InventoryInvariantError,
    apply_internal_move,
    canonical_qty,
    decimal_qty,
    request_hash,
)
from solara_wms.wms.safety import require_wms_mode


BALANCE_DOCTYPE = "WMS Bin Balance"
MOVEMENT_DOCTYPE = "WMS Movement"


class IdempotencyConflict(frappe.ValidationError):
    """The same command identity was reused with a different request body."""

    http_status_code = 409


def _require_shadow_write(warehouse):
    require_wms_mode("Shadow", "Draft Handoff")
    frappe.only_for("System Manager")
    pilot = frappe.db.get_single_value("WMS Settings", "pilot_warehouse")
    if pilot and warehouse != pilot:
        frappe.throw(
            _("Shadow-mode writes are restricted to pilot warehouse {0}").format(pilot)
        )


def _idempotency_key(value):
    key = (value or "").strip()
    if len(key) < 8 or len(key) > 140:
        frappe.throw(_("Idempotency Key must be between 8 and 140 characters"))
    return key


def _conflict(message):
    frappe.throw(
        message,
        exc=IdempotencyConflict,
        title=_("Idempotency Conflict"),
    )


def _validate_bin(warehouse, bin_name):
    row = frappe.db.get_value(
        "Warehouse Bin",
        bin_name,
        ["warehouse", "status", "is_active", "bin_code"],
        as_dict=True,
    )
    if not row or row.warehouse != warehouse:
        frappe.throw(_("Bin {0} does not belong to warehouse {1}").format(bin_name, warehouse))
    if not row.is_active or row.status in ("Blocked", "Maintenance"):
        frappe.throw(_("Bin {0} is not available for movement").format(row.bin_code or bin_name))
    return row


def _balance_name(warehouse, bin_name, item_code):
    doc = frappe.new_doc(BALANCE_DOCTYPE)
    doc.warehouse = warehouse
    doc.bin = bin_name
    doc.item_code = item_code
    doc.autoname()
    return doc.name


def _movement_result(doc, replayed=False):
    return {
        "movement": doc.name,
        "movement_type": doc.movement_type,
        "status": doc.status,
        "warehouse": doc.warehouse,
        "item_code": doc.item_code,
        "source_bin": doc.source_bin,
        "target_bin": doc.target_bin,
        "qty": flt(doc.qty),
        "source_before": flt(doc.source_before),
        "source_after": flt(doc.source_after),
        "target_before": flt(doc.target_before),
        "target_after": flt(doc.target_after),
        "replayed": bool(replayed),
    }


def _existing_movement(idempotency_key, expected_hash):
    name = frappe.db.get_value(
        MOVEMENT_DOCTYPE, {"idempotency_key": idempotency_key}, "name"
    )
    if not name:
        return None
    doc = frappe.get_doc(MOVEMENT_DOCTYPE, name)
    if doc.request_hash != expected_hash:
        _conflict(
            _("Idempotency Key was already used for a different movement request")
        )
    return _movement_result(doc, replayed=True)


def _movement_doc(payload, hash_value, **values):
    doc = frappe.get_doc(
        {
            "doctype": MOVEMENT_DOCTYPE,
            "idempotency_key": payload["idempotency_key"],
            "request_hash": hash_value,
            "status": "Posted",
            "posted_at": now_datetime(),
            "posted_by": frappe.session.user,
            **values,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


def _decimal(value) -> Decimal:
    try:
        return decimal_qty(value)
    except InventoryInvariantError as exc:
        frappe.throw(_(str(exc)))


@frappe.whitelist(methods=["POST"])
def seed_bin_balance(
    idempotency_key,
    warehouse,
    bin,
    item_code,
    physical_qty,
    device_id=None,
    reference_doctype=None,
    reference_name=None,
    notes=None,
):
    """Create one initial physical balance from controlled count evidence."""
    _require_shadow_write(warehouse)
    key = _idempotency_key(idempotency_key)
    _validate_bin(warehouse, bin)
    qty = _decimal(physical_qty)
    if qty <= 0:
        frappe.throw(_("Opening Physical Quantity must be greater than zero"))

    payload = {
        "movement_type": "Opening Balance",
        "idempotency_key": key,
        "warehouse": warehouse,
        "bin": bin,
        "item_code": item_code,
        "physical_qty": canonical_qty(qty),
        "device_id": (device_id or "").strip(),
        "reference_doctype": reference_doctype or "",
        "reference_name": reference_name or "",
        "notes": notes or "",
    }
    hash_value = request_hash(payload)
    replay = _existing_movement(key, hash_value)
    if replay:
        return replay

    balance_name = _balance_name(warehouse, bin, item_code)
    if frappe.db.exists(BALANCE_DOCTYPE, balance_name):
        frappe.throw(
            _("Balance already exists for {0} in {1}; use a count adjustment").format(
                item_code, bin
            )
        )

    movement = _movement_doc(
        payload,
        hash_value,
        movement_type="Opening Balance",
        warehouse=warehouse,
        item_code=item_code,
        target_bin=bin,
        qty=float(qty),
        target_before=0,
        target_after=float(qty),
        device_id=payload["device_id"],
        reference_doctype=reference_doctype,
        reference_name=reference_name,
        notes=notes,
    )
    balance = frappe.get_doc(
        {
            "doctype": BALANCE_DOCTYPE,
            "warehouse": warehouse,
            "bin": bin,
            "item_code": item_code,
            "physical_qty": float(qty),
            "allocated_qty": 0,
            "hold_qty": 0,
            "available_qty": float(qty),
            "last_movement": movement.name,
            "last_updated_by": frappe.session.user,
        }
    )
    balance.insert(ignore_permissions=True)
    result = _movement_result(movement)
    result["target_balance"] = balance.name
    return result


def _locked_balances(names):
    placeholders = ", ".join(["%s"] * len(names))
    rows = frappe.db.sql(
        f"""
        SELECT name, warehouse, bin, item_code, physical_qty, allocated_qty,
               hold_qty, available_qty
          FROM `tabWMS Bin Balance`
         WHERE name IN ({placeholders})
         ORDER BY name
         FOR UPDATE
        """,
        tuple(sorted(names)),
        as_dict=True,
    )
    return {row.name: row for row in rows}


@frappe.whitelist(methods=["POST"])
def move_internal(
    idempotency_key,
    warehouse,
    source_bin,
    target_bin,
    item_code,
    qty,
    device_id=None,
    reference_doctype=None,
    reference_name=None,
    notes=None,
):
    """Atomically move available physical quantity between bins in one warehouse."""
    _require_shadow_write(warehouse)
    key = _idempotency_key(idempotency_key)
    if source_bin == target_bin:
        frappe.throw(_("Source and Target Bin must be different"))
    _validate_bin(warehouse, source_bin)
    _validate_bin(warehouse, target_bin)
    move_qty = _decimal(qty)
    if move_qty <= 0:
        frappe.throw(_("Movement Quantity must be greater than zero"))

    payload = {
        "movement_type": "Internal Move",
        "idempotency_key": key,
        "warehouse": warehouse,
        "source_bin": source_bin,
        "target_bin": target_bin,
        "item_code": item_code,
        "qty": canonical_qty(move_qty),
        "device_id": (device_id or "").strip(),
        "reference_doctype": reference_doctype or "",
        "reference_name": reference_name or "",
        "notes": notes or "",
    }
    hash_value = request_hash(payload)
    replay = _existing_movement(key, hash_value)
    if replay:
        return replay

    source_name = _balance_name(warehouse, source_bin, item_code)
    target_name = _balance_name(warehouse, target_bin, item_code)
    locked = _locked_balances([source_name, target_name])
    if source_name not in locked:
        frappe.throw(_("Source bin has no physical balance for item {0}").format(item_code))
    if target_name not in locked:
        frappe.throw(_("Target bin has no physical balance for item {0}").format(item_code))

    source = locked[source_name]
    target = locked[target_name]
    try:
        source_after, target_after = apply_internal_move(
            BalanceState.from_values(
                source.physical_qty, source.allocated_qty, source.hold_qty
            ),
            BalanceState.from_values(
                target.physical_qty, target.allocated_qty, target.hold_qty
            ),
            move_qty,
        )
    except InventoryInvariantError as exc:
        frappe.throw(_(str(exc)))

    now = now_datetime()
    frappe.db.sql(
        """
        UPDATE `tabWMS Bin Balance`
           SET physical_qty = %s, available_qty = %s, modified = %s, modified_by = %s
         WHERE name = %s AND available_qty >= %s
        """,
        (
            float(source_after.physical),
            float(source_after.available),
            now,
            frappe.session.user,
            source_name,
            float(move_qty),
        ),
    )
    affected = frappe.db.sql("SELECT ROW_COUNT()")[0][0]
    if affected != 1:
        frappe.throw(_("Source balance changed concurrently; retry with the same request"))
    frappe.db.sql(
        """
        UPDATE `tabWMS Bin Balance`
           SET physical_qty = %s, available_qty = %s, modified = %s, modified_by = %s
         WHERE name = %s
        """,
        (
            float(target_after.physical),
            float(target_after.available),
            now,
            frappe.session.user,
            target_name,
        ),
    )

    movement = _movement_doc(
        payload,
        hash_value,
        movement_type="Internal Move",
        warehouse=warehouse,
        item_code=item_code,
        source_bin=source_bin,
        target_bin=target_bin,
        qty=float(move_qty),
        source_before=flt(source.physical_qty),
        source_after=float(source_after.physical),
        target_before=flt(target.physical_qty),
        target_after=float(target_after.physical),
        device_id=payload["device_id"],
        reference_doctype=reference_doctype,
        reference_name=reference_name,
        notes=notes,
    )
    frappe.db.sql(
        """
        UPDATE `tabWMS Bin Balance`
           SET last_movement = %s, last_updated_by = %s
         WHERE name IN (%s, %s)
        """,
        (movement.name, frappe.session.user, source_name, target_name),
    )
    result = _movement_result(movement)
    result["source_balance"] = source_name
    result["target_balance"] = target_name
    return result


@frappe.whitelist(methods=["GET"])
def get_bin_balance(warehouse, bin, item_code):
    name = _balance_name(warehouse, bin, item_code)
    if not frappe.db.exists(BALANCE_DOCTYPE, name):
        return None
    row = frappe.db.get_value(
        BALANCE_DOCTYPE,
        name,
        [
            "name",
            "warehouse",
            "bin",
            "item_code",
            "physical_qty",
            "allocated_qty",
            "hold_qty",
            "available_qty",
            "last_movement",
        ],
        as_dict=True,
    )
    return dict(row)
