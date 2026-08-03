"""Pure tests for the shadow physical-inventory invariants."""

from decimal import Decimal

import pytest

from solara_wms.wms.inventory_domain import (
    BalanceState,
    InventoryInvariantError,
    allocate_balance,
    apply_internal_move,
    canonical_qty,
    complete_allocated_move,
    execute_allocated_pick,
    match_pack_handoff,
    plan_replenishment,
    release_allocation,
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


def test_allocate_and_release_preserve_physical_quantity():
    opening = BalanceState.from_values(100, allocated=10, held=5)
    allocated = allocate_balance(opening, 25)
    released = release_allocation(allocated, 25)

    assert allocated.physical == opening.physical
    assert allocated.allocated == Decimal("35")
    assert allocated.available == Decimal("60")
    assert released == opening


def test_two_allocations_cannot_exceed_available_quantity():
    first = allocate_balance(BalanceState.from_values(75), 50)
    with pytest.raises(InventoryInvariantError, match="25 available, 50 requested"):
        allocate_balance(first, 50)


def test_pick_scan_is_bounded_and_consumes_physical_and_allocation():
    balance = BalanceState.from_values(20, allocated=12, held=2)
    after, remaining, executed = execute_allocated_pick(balance, 10, 2, 4)

    assert after.physical == 16
    assert after.allocated == 8
    assert after.held == 2
    assert after.available == 6
    assert remaining == 6
    assert executed == 6


def test_pick_scan_cannot_exceed_its_own_work_allocation():
    with pytest.raises(InventoryInvariantError, match="3 remaining, 4 scanned"):
        execute_allocated_pick(
            BalanceState.from_values(20, allocated=10), 3, 7, 4
        )


def test_pick_scan_rejects_zero_negative_and_non_finite_quantities():
    for qty in (0, -1, "NaN", "Infinity"):
        with pytest.raises(InventoryInvariantError):
            execute_allocated_pick(
                BalanceState.from_values(20, allocated=10), 10, 0, qty
            )


def test_pack_handoff_requires_exact_item_and_quantity_match():
    matched = match_pack_handoff(
        [{"item_code": "A", "qty": 1}, {"item_code": "A", "qty": 2},
         {"item_code": "B", "qty": 1}],
        [{"item_code": "A", "executed_qty": 3},
         {"item_code": "B", "executed_qty": 1}],
    )
    assert matched == {"A": Decimal("3"), "B": Decimal("1")}


@pytest.mark.parametrize(
    "works, message",
    [
        ([{"item_code": "A", "executed_qty": 1}], "B: expected 1, picked 0"),
        ([{"item_code": "A", "executed_qty": 3},
          {"item_code": "B", "executed_qty": 1}], "A: expected 2, picked 3"),
        ([{"item_code": "WRONG", "executed_qty": 1}], "WRONG"),
    ],
)
def test_pack_handoff_rejects_missing_excess_and_wrong_items(works, message):
    with pytest.raises(InventoryInvariantError, match=message):
        match_pack_handoff(
            [{"item_code": "A", "qty": 2}, {"item_code": "B", "qty": 1}],
            works,
        )


def test_allocated_replenishment_conserves_quantity_and_consumes_allocation():
    source = BalanceState.from_values(95, allocated=95)
    target = BalanceState.from_values(15)
    source_after, target_after = complete_allocated_move(source, target, 95)

    assert source_after.physical == 0
    assert source_after.allocated == 0
    assert target_after.physical == 110
    assert source_after.physical + target_after.physical == 110


def test_replenishment_plan_respects_policy_capacity_and_available_quantity():
    assert plan_replenishment(
        BalanceState.from_values(95),
        BalanceState.from_values(15),
        minimum_qty=50,
        maximum_qty=200,
        replenish_qty=150,
    ) == Decimal("95")


def test_replenishment_is_not_created_at_or_above_minimum():
    with pytest.raises(InventoryInvariantError, match="not below minimum"):
        plan_replenishment(
            BalanceState.from_values(100),
            BalanceState.from_values(50),
            minimum_qty=50,
            maximum_qty=200,
            replenish_qty=150,
        )
