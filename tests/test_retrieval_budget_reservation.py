"""Task 3 atomic BudgetLedger reservation tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from paper_agent.literature.retrieval.budget import (
    BudgetLedger,
    ReservationError,
    ReservationState,
    RetrievalLimits,
)


def _limits(**overrides) -> RetrievalLimits:
    values = {
        "max_iterations": 20,
        "max_provider_calls": 20,
        "max_search_calls": 20,
        "max_verify_calls": 20,
        "max_discovery_calls": 20,
        "max_llm_calls": 20,
        "max_candidates": 200,
        "provider_time_budget_s": 60.0,
        "max_no_progress_rounds": 2,
    }
    values.update(overrides)
    return RetrievalLimits(**values)


def test_reserve_started_settled_moves_count_exactly_once():
    ledger = BudgetLedger(_limits(max_llm_calls=1), clock=lambda: 0.0)
    reservation = ledger.try_reserve({"llm_calls": 1})
    assert reservation is not None
    assert reservation.state is ReservationState.RESERVED
    assert ledger.llm_calls == 0
    assert ledger.reserved_counts()["llm_calls"] == 1

    ledger.mark_started(reservation)
    assert reservation.state is ReservationState.STARTED
    ledger.settle(reservation)
    assert reservation.state is ReservationState.SETTLED
    assert ledger.llm_calls == 1
    assert ledger.reserved_counts()["llm_calls"] == 0
    assert ledger.try_reserve({"llm_calls": 1}) is None

    with pytest.raises(ReservationError):
        ledger.settle(reservation)
    assert ledger.llm_calls == 1


def test_release_is_only_for_a_call_that_never_started():
    ledger = BudgetLedger(_limits(max_verify_calls=1), clock=lambda: 0.0)
    reservation = ledger.try_reserve({"verify_calls": 1})
    assert reservation is not None
    ledger.release(reservation)
    assert reservation.state is ReservationState.RELEASED
    assert ledger.verify_calls == 0
    assert ledger.reserved_counts()["verify_calls"] == 0
    assert ledger.try_reserve({"verify_calls": 1}) is not None
    with pytest.raises(ReservationError):
        ledger.release(reservation)


def test_started_reservation_cannot_be_released():
    ledger = BudgetLedger(_limits(), clock=lambda: 0.0)
    reservation = ledger.try_reserve({"search_calls": 1})
    assert reservation is not None
    ledger.mark_started(reservation)
    with pytest.raises(ReservationError):
        ledger.release(reservation)
    assert reservation.state is ReservationState.STARTED


def test_cross_ledger_ticket_is_rejected_without_changing_counts():
    first = BudgetLedger(_limits(), clock=lambda: 0.0)
    second = BudgetLedger(_limits(), clock=lambda: 0.0)
    reservation = first.try_reserve({"llm_calls": 1})
    assert reservation is not None
    with pytest.raises(ReservationError):
        second.mark_started(reservation)
    assert second.llm_calls == 0
    assert second.reserved_counts()["llm_calls"] == 0


def test_parallel_reservations_never_overshoot_dimension_limit():
    ledger = BudgetLedger(_limits(max_llm_calls=7), clock=lambda: 0.0)
    with ThreadPoolExecutor(max_workers=32) as pool:
        reservations = list(
            pool.map(lambda _: ledger.try_reserve({"llm_calls": 1}), range(100))
        )
    granted = [reservation for reservation in reservations if reservation is not None]
    assert len(granted) == 7
    assert ledger.reserved_counts()["llm_calls"] == 7
    assert ledger.llm_calls == 0
    for reservation in granted:
        ledger.mark_started(reservation)
        ledger.settle(reservation)
    assert ledger.llm_calls == 7
    assert ledger.reserved_counts()["llm_calls"] == 0


def test_provider_total_is_reserved_atomically_across_call_kinds():
    ledger = BudgetLedger(_limits(max_provider_calls=2), clock=lambda: 0.0)
    search = ledger.try_reserve({"search_calls": 1})
    verify = ledger.try_reserve({"verify_calls": 1})
    assert search is not None and verify is not None
    assert ledger.try_reserve({"discovery_calls": 1}) is None
    assert "max_provider_calls" in ledger.exhausted_dimensions()


def test_any_exhausted_expensive_dimension_stops_new_physical_work():
    ledger = BudgetLedger(_limits(max_verify_calls=1), clock=lambda: 0.0)
    ledger.charge_verify()
    assert ledger.try_reserve({"llm_calls": 1}) is None


def test_last_allowed_search_results_can_still_be_judged():
    ledger = BudgetLedger(_limits(max_search_calls=1), clock=lambda: 0.0)
    ledger.charge_search()
    assert ledger.try_reserve({"llm_calls": 1}) is not None


def test_monotonic_deadline_is_checked_not_summed_as_provider_latency():
    now = {"value": 0.0}
    ledger = BudgetLedger(
        _limits(provider_time_budget_s=5.0), clock=lambda: now["value"]
    )
    reservation = ledger.try_reserve({"verify_calls": 1})
    assert reservation is not None
    ledger.mark_started(reservation)
    ledger.settle(reservation)
    assert ledger.verify_calls == 1
    now["value"] = 5.0
    assert ledger.try_reserve({"verify_calls": 1}) is None
    assert "provider_time_budget_s" in ledger.exhausted_dimensions()


@pytest.mark.parametrize(
    "dims",
    [{}, {"unknown": 1}, {"llm_calls": 0}, {"llm_calls": -1}, {"llm_calls": True}],
)
def test_invalid_reservation_dimensions_are_explicit(dims):
    ledger = BudgetLedger(_limits(), clock=lambda: 0.0)
    with pytest.raises(ValueError):
        ledger.try_reserve(dims)


def test_legacy_charge_and_verify_adapter_still_use_one_ledger():
    ledger = BudgetLedger(_limits(max_verify_calls=2), clock=lambda: 0.0)
    view = ledger.verify_budget_view()
    view.charge(elapsed_s=999.0)
    assert ledger.verify_calls == 1
    assert view.exhausted is False
    view.charge()
    assert ledger.verify_calls == 2
    assert view.exhausted is True
