"""Pure inventory invariants shared by WMS services and unit tests."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from collections import defaultdict


class InventoryInvariantError(ValueError):
    pass


def match_pack_handoff(expected_lines, completed_work_lines):
    """Require completed parcel picks to match the pack piece list exactly."""
    expected = defaultdict(lambda: Decimal("0"))
    picked = defaultdict(lambda: Decimal("0"))
    for row in expected_lines:
        item = str(row.get("item_code") or "").strip()
        qty = decimal_qty(row.get("qty", 0))
        if not item or qty <= 0:
            raise InventoryInvariantError("Expected pack lines require item and quantity")
        expected[item] += qty
    for row in completed_work_lines:
        item = str(row.get("item_code") or "").strip()
        qty = decimal_qty(row.get("executed_qty", 0))
        if not item or qty <= 0:
            raise InventoryInvariantError("Completed pick work requires item and quantity")
        picked[item] += qty
    if not expected:
        raise InventoryInvariantError("Parcel has no physical pieces to hand off")
    mismatches = []
    for item in sorted(set(expected) | set(picked)):
        if expected[item] != picked[item]:
            mismatches.append(
                f"{item}: expected {canonical_qty(expected[item])}, "
                f"picked {canonical_qty(picked[item])}"
            )
    if mismatches:
        raise InventoryInvariantError("Pick handoff mismatch — " + "; ".join(mismatches))
    return {item: expected[item] for item in sorted(expected)}


def decimal_qty(value) -> Decimal:
    try:
        qty = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InventoryInvariantError("Quantity must be numeric") from exc
    if not qty.is_finite():
        raise InventoryInvariantError("Quantity must be finite")
    return qty


def canonical_qty(value) -> str:
    qty = decimal_qty(value)
    text = format(qty.normalize(), "f")
    return "0" if text in ("-0", "") else text


def request_hash(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BalanceState:
    physical: Decimal
    allocated: Decimal = Decimal("0")
    held: Decimal = Decimal("0")

    @classmethod
    def from_values(cls, physical=0, allocated=0, held=0):
        return cls(decimal_qty(physical), decimal_qty(allocated), decimal_qty(held))

    @property
    def available(self) -> Decimal:
        return self.physical - self.allocated - self.held

    def validate(self):
        if self.physical < 0 or self.allocated < 0 or self.held < 0:
            raise InventoryInvariantError("Balance quantities cannot be negative")
        if self.available < 0:
            raise InventoryInvariantError(
                "Allocated plus held quantity cannot exceed physical quantity"
            )
        return self


def apply_internal_move(source: BalanceState, target: BalanceState, qty):
    source.validate()
    target.validate()
    move_qty = decimal_qty(qty)
    if move_qty <= 0:
        raise InventoryInvariantError("Movement quantity must be greater than zero")
    if source.available < move_qty:
        raise InventoryInvariantError(
            f"Insufficient available quantity: {canonical_qty(source.available)} available, "
            f"{canonical_qty(move_qty)} requested"
        )
    source_after = BalanceState(
        physical=source.physical - move_qty,
        allocated=source.allocated,
        held=source.held,
    ).validate()
    target_after = BalanceState(
        physical=target.physical + move_qty,
        allocated=target.allocated,
        held=target.held,
    ).validate()
    return source_after, target_after


def allocate_balance(balance: BalanceState, qty):
    balance.validate()
    allocation_qty = decimal_qty(qty)
    if allocation_qty <= 0:
        raise InventoryInvariantError("Allocation quantity must be greater than zero")
    if balance.available < allocation_qty:
        raise InventoryInvariantError(
            f"Insufficient available quantity: {canonical_qty(balance.available)} available, "
            f"{canonical_qty(allocation_qty)} requested"
        )
    return BalanceState(
        physical=balance.physical,
        allocated=balance.allocated + allocation_qty,
        held=balance.held,
    ).validate()


def release_allocation(balance: BalanceState, qty):
    balance.validate()
    release_qty = decimal_qty(qty)
    if release_qty <= 0:
        raise InventoryInvariantError("Release quantity must be greater than zero")
    if balance.allocated < release_qty:
        raise InventoryInvariantError(
            f"Insufficient allocated quantity: {canonical_qty(balance.allocated)} allocated, "
            f"{canonical_qty(release_qty)} requested"
        )
    return BalanceState(
        physical=balance.physical,
        allocated=balance.allocated - release_qty,
        held=balance.held,
    ).validate()


def execute_allocated_pick(
    balance: BalanceState,
    work_allocated,
    work_executed,
    qty,
):
    """Consume one bounded pick scan from both work and its source balance."""
    balance.validate()
    outstanding = decimal_qty(work_allocated)
    executed = decimal_qty(work_executed)
    pick_qty = decimal_qty(qty)
    if outstanding < 0 or executed < 0:
        raise InventoryInvariantError("Work quantities cannot be negative")
    if pick_qty <= 0:
        raise InventoryInvariantError("Pick quantity must be greater than zero")
    if pick_qty > outstanding:
        raise InventoryInvariantError(
            f"Pick exceeds work allocation: {canonical_qty(outstanding)} remaining, "
            f"{canonical_qty(pick_qty)} scanned"
        )
    if balance.allocated < pick_qty:
        raise InventoryInvariantError(
            f"Insufficient bin allocation: {canonical_qty(balance.allocated)} allocated, "
            f"{canonical_qty(pick_qty)} scanned"
        )
    if balance.physical < pick_qty:
        raise InventoryInvariantError(
            f"Insufficient physical quantity: {canonical_qty(balance.physical)} physical, "
            f"{canonical_qty(pick_qty)} scanned"
        )
    after = BalanceState(
        physical=balance.physical - pick_qty,
        allocated=balance.allocated - pick_qty,
        held=balance.held,
    ).validate()
    return after, outstanding - pick_qty, executed + pick_qty


def complete_allocated_move(source: BalanceState, target: BalanceState, qty):
    source.validate()
    target.validate()
    move_qty = decimal_qty(qty)
    if move_qty <= 0:
        raise InventoryInvariantError("Movement quantity must be greater than zero")
    if source.allocated < move_qty:
        raise InventoryInvariantError(
            f"Insufficient allocated quantity: {canonical_qty(source.allocated)} allocated, "
            f"{canonical_qty(move_qty)} requested"
        )
    if source.physical < move_qty:
        raise InventoryInvariantError(
            f"Insufficient physical quantity: {canonical_qty(source.physical)} physical, "
            f"{canonical_qty(move_qty)} requested"
        )
    source_after = BalanceState(
        physical=source.physical - move_qty,
        allocated=source.allocated - move_qty,
        held=source.held,
    ).validate()
    target_after = BalanceState(
        physical=target.physical + move_qty,
        allocated=target.allocated,
        held=target.held,
    ).validate()
    return source_after, target_after


def plan_replenishment(
    source: BalanceState,
    target: BalanceState,
    minimum_qty,
    maximum_qty,
    replenish_qty=0,
):
    source.validate()
    target.validate()
    minimum = decimal_qty(minimum_qty)
    maximum = decimal_qty(maximum_qty)
    fixed = decimal_qty(replenish_qty)
    if minimum <= 0 or maximum <= 0 or minimum > maximum or fixed < 0:
        raise InventoryInvariantError("Invalid Home replenishment policy")
    if target.physical >= minimum:
        raise InventoryInvariantError(
            f"Home quantity {canonical_qty(target.physical)} is not below minimum "
            f"{canonical_qty(minimum)}"
        )
    capacity = maximum - target.physical
    desired = fixed if fixed > 0 else capacity
    planned = min(desired, capacity, source.available)
    if planned <= 0:
        raise InventoryInvariantError("No available quantity can be replenished")
    return planned
