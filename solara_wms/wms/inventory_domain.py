"""Pure inventory invariants shared by WMS services and unit tests."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json


class InventoryInvariantError(ValueError):
    pass


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
