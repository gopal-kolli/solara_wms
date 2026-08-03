"""Pure tests for the shadow physical-inventory invariants."""

from decimal import Decimal

import pytest

from solara_wms.wms.inventory_domain import (
    BalanceState,
    InventoryInvariantError,
    apply_internal_move,
    canonical_qty,
    request_hash,
)


def test_request_hash_is_order_independent_and_quantity_is_canonical():
    first = {"warehouse": "HYD", "qty": canonical_qty("10.00")}
    second = {"qty": canonical_qty(Decimal("10.0")), "warehouse": "HYD"}
    changed = {"warehouse": "HYD", "qty": canonical_qty("10.01")}

    assert first["qty"] == "10"
    assert request_hash(first) == request_hash(second)
    assert request_hash(first) != request_hash(changed)


def test_internal_move_conserves_physical_quantity_and_preserves_reservations():
    source = BalanceState.from_values(100, allocated=10, held=5)
    target = BalanceState.from_values(12, allocated=2, held=1)

    source_after, target_after = apply_internal_move(source, target, "25")

    assert source.available == Decimal("85")
    assert source_after.physical == Decimal("75")
    assert source_after.allocated == source.allocated
    assert source_after.held == source.held
    assert target_after.physical == Decimal("37")
    assert source_after.physical + target_after.physical == Decimal("112")


@pytest.mark.parametrize("qty", [0, -1, "NaN", "Infinity", "not-a-number"])
def test_internal_move_rejects_invalid_quantity(qty):
    with pytest.raises(InventoryInvariantError):
        apply_internal_move(BalanceState.from_values(10), BalanceState.from_values(0), qty)


def test_internal_move_cannot_consume_allocated_or_held_quantity():
    source = BalanceState.from_values(10, allocated=4, held=3)

    with pytest.raises(InventoryInvariantError, match="3 available, 4 requested"):
        apply_internal_move(source, BalanceState.from_values(0), 4)


def test_balance_rejects_negative_available_quantity():
    with pytest.raises(InventoryInvariantError):
        BalanceState.from_values(5, allocated=4, held=2).validate()
