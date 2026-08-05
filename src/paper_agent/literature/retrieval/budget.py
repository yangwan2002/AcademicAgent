"""检索预算、停止原因与取消（A2c-4 / 任务 1，design D11）。

`BudgetLedger` 是 Loop 的**唯一权威预算源**：统一记账所有昂贵调用
（search / verify / llm / discovery / provider 时间 / 迭代 / 候选数）。A2c-1 CrossVerifier
的 verify 调用经 `verify_budget_view()` 汇入本 ledger，避免双重记账。

不 import `tasking`、不读全局 config、不读全局时钟（monotonic clock 注入，可测）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Callable

from paper_agent.literature.retrieval.cross_verify import VerifyBudget


@dataclass(frozen=True)
class RetrievalLimits:
    max_iterations: int = 6
    max_provider_calls: int = 60
    max_search_calls: int = 30
    max_verify_calls: int = 120
    max_discovery_calls: int = 10
    max_llm_calls: int = 30
    max_candidates: int = 500
    provider_time_budget_s: float = 120.0
    max_no_progress_rounds: int = 2


class StopReason(str, Enum):
    QUOTA_SATISFIED = "quota_satisfied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_PROGRESS = "no_progress"
    SOURCE_EXHAUSTED = "source_exhausted"            # 有源成功响应但策略穷尽
    ALL_SOURCES_UNAVAILABLE = "all_sources_unavailable"  # 所有尝试均 provider error
    CANCELLED = "cancelled"


class CancelToken:
    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


class ReservationState(str, Enum):
    RESERVED = "reserved"
    STARTED = "started"
    SETTLED = "settled"
    RELEASED = "released"


class ReservationError(RuntimeError):
    """Invalid reservation ownership or state transition."""


@dataclass(frozen=True)
class Reservation:
    """Opaque ticket; its authoritative state remains inside the ledger."""

    ticket_id: int
    dims: tuple[tuple[str, int], ...]
    _ledger: object = field(repr=False, compare=False)

    @property
    def state(self) -> ReservationState:
        return self._ledger._reservation_state(self)  # type: ignore[attr-defined]

    def dimensions(self) -> dict[str, int]:
        return dict(self.dims)


_COUNTER_LIMITS = {
    "iterations": "max_iterations",
    "search_calls": "max_search_calls",
    "verify_calls": "max_verify_calls",
    "discovery_calls": "max_discovery_calls",
    "llm_calls": "max_llm_calls",
    "candidates": "max_candidates",
}
_PROVIDER_COUNTERS = ("search_calls", "verify_calls", "discovery_calls")
_EXPENSIVE_PIPELINE_COUNTERS = ("verify_calls", "llm_calls")


class BudgetLedger:
    """统一预算记账。触顶维度可查询，供 BUDGET_EXHAUSTED diagnostics 精确标注。"""

    def __init__(self, limits: RetrievalLimits, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._limits = limits
        self._clock = clock
        self._start = clock()
        self.iterations = 0
        self.search_calls = 0
        self.verify_calls = 0
        self.discovery_calls = 0
        self.llm_calls = 0
        self.candidates = 0
        self._lock = RLock()
        self._reserved = {name: 0 for name in _COUNTER_LIMITS}
        self._reservation_states: dict[int, ReservationState] = {}
        self._next_ticket_id = 1
        self._reservation_stats = {
            "granted": 0,
            "rejected": 0,
            "started": 0,
            "settled": 0,
            "released": 0,
        }

    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._start

    # -- charge --
    def charge_iteration(self) -> None:
        self._charge("iterations", 1)

    def charge_search(self, n: int = 1) -> None:
        self._charge("search_calls", n)

    def charge_verify(self, n: int = 1) -> None:
        self._charge("verify_calls", n)

    def charge_discovery(self, n: int = 1) -> None:
        self._charge("discovery_calls", n)

    def charge_llm(self, n: int = 1) -> None:
        self._charge("llm_calls", n)

    def charge_candidates(self, n: int) -> None:
        self._charge("candidates", n)

    def _charge(self, dimension: str, n: int) -> None:
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError("budget charge must be a non-negative integer")
        with self._lock:
            setattr(self, dimension, getattr(self, dimension) + n)

    # -- atomic reservations for physical external calls --
    def try_reserve(self, dims: dict[str, int]) -> Reservation | None:
        """Atomically reserve count dimensions without exceeding any limit.

        ``provider_time_budget_s`` is a monotonic wall-clock deadline, not a
        latency sum, and is therefore checked rather than reserved.
        """

        normalized = self._normalize_dims(dims)
        with self._lock:
            if self._would_reject(normalized):
                self._reservation_stats["rejected"] += 1
                return None
            ticket_id = self._next_ticket_id
            self._next_ticket_id += 1
            for name, amount in normalized:
                self._reserved[name] += amount
            self._reservation_states[ticket_id] = ReservationState.RESERVED
            self._reservation_stats["granted"] += 1
            return Reservation(ticket_id, normalized, self)

    def mark_started(self, reservation: Reservation) -> None:
        with self._lock:
            self._transition(
                reservation,
                expected=ReservationState.RESERVED,
                target=ReservationState.STARTED,
            )
            self._reservation_stats["started"] += 1

    def settle(self, reservation: Reservation) -> None:
        """Move a started physical call from reserved to used exactly once."""

        with self._lock:
            self._transition(
                reservation,
                expected=ReservationState.STARTED,
                target=ReservationState.SETTLED,
            )
            for name, amount in reservation.dims:
                self._reserved[name] -= amount
                setattr(self, name, getattr(self, name) + amount)
            self._reservation_stats["settled"] += 1

    def release(self, reservation: Reservation) -> None:
        """Release a reservation only when no physical call was started."""

        with self._lock:
            self._transition(
                reservation,
                expected=ReservationState.RESERVED,
                target=ReservationState.RELEASED,
            )
            for name, amount in reservation.dims:
                self._reserved[name] -= amount
            self._reservation_stats["released"] += 1

    def reserved_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._reserved)

    def reservation_stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._reservation_stats)

    def _reservation_state(self, reservation: Reservation) -> ReservationState:
        with self._lock:
            self._validate_owner(reservation)
            try:
                return self._reservation_states[reservation.ticket_id]
            except KeyError as exc:
                raise ReservationError("unknown reservation ticket") from exc

    @staticmethod
    def _normalize_dims(dims: dict[str, int]) -> tuple[tuple[str, int], ...]:
        if not isinstance(dims, dict) or not dims:
            raise ValueError("reservation dims must be a non-empty dict")
        normalized: list[tuple[str, int]] = []
        for name, amount in dims.items():
            if name not in _COUNTER_LIMITS:
                raise ValueError(f"unsupported budget dimension: {name}")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise ValueError(f"reservation amount for {name} must be positive")
            normalized.append((name, amount))
        return tuple(sorted(normalized))

    def _would_reject(self, dims: tuple[tuple[str, int], ...]) -> bool:
        if self.elapsed_s >= self._limits.provider_time_budget_s:
            return True

        requested = dict(dims)
        # A verify/LLM limit stops later expensive candidate work.  A search
        # limit only prevents another search: candidates returned by the last
        # allowed search must still be judgeable/verifiable.
        for name in _EXPENSIVE_PIPELINE_COUNTERS:
            limit_name = _COUNTER_LIMITS[name]
            used_and_reserved = getattr(self, name) + self._reserved[name]
            if used_and_reserved >= getattr(self._limits, limit_name):
                return True

        provider_used = sum(getattr(self, name) for name in _PROVIDER_COUNTERS)
        provider_reserved = sum(self._reserved[name] for name in _PROVIDER_COUNTERS)
        if provider_used + provider_reserved >= self._limits.max_provider_calls:
            return True

        for name, amount in dims:
            limit = getattr(self._limits, _COUNTER_LIMITS[name])
            if getattr(self, name) + self._reserved[name] + amount > limit:
                return True

        provider_requested = sum(requested.get(name, 0) for name in _PROVIDER_COUNTERS)
        return (
            provider_used + provider_reserved + provider_requested
            > self._limits.max_provider_calls
        )

    def _validate_owner(self, reservation: Reservation) -> None:
        if not isinstance(reservation, Reservation) or reservation._ledger is not self:
            raise ReservationError("reservation belongs to another ledger")

    def _transition(
        self,
        reservation: Reservation,
        *,
        expected: ReservationState,
        target: ReservationState,
    ) -> None:
        self._validate_owner(reservation)
        current = self._reservation_states.get(reservation.ticket_id)
        if current is None:
            raise ReservationError("unknown reservation ticket")
        if current is not expected:
            raise ReservationError(
                f"invalid reservation transition: {current.value} -> {target.value}"
            )
        self._reservation_states[reservation.ticket_id] = target

    # -- 查询 --
    def exhausted_dimensions(self) -> tuple[str, ...]:
        with self._lock:
            lim = self._limits
            out: list[str] = []
            for name, limit_name in _COUNTER_LIMITS.items():
                if getattr(self, name) + self._reserved[name] >= getattr(lim, limit_name):
                    out.append(limit_name)
            provider_reserved = sum(self._reserved[name] for name in _PROVIDER_COUNTERS)
            if self.provider_calls + provider_reserved >= lim.max_provider_calls:
                out.append("max_provider_calls")
            if self.elapsed_s >= lim.provider_time_budget_s:
                out.append("provider_time_budget_s")
            return tuple(out)

    @property
    def provider_calls(self) -> int:
        with self._lock:
            return self.search_calls + self.verify_calls + self.discovery_calls

    def can_continue(self) -> bool:
        """能否**开启新一轮迭代**（含 max_iterations）。"""
        return not self.exhausted_dimensions()

    def call_budget_ok(self) -> bool:
        """迭代**内**能否再发一次外部调用（排除 max_iterations——已在允许的迭代内）。"""
        return not [d for d in self.exhausted_dimensions() if d != "max_iterations"]

    def verify_budget_view(self) -> VerifyBudget:
        """返回一个把 charge 汇入本 ledger 的 VerifyBudget 适配，避免双重记账。"""
        return _LedgerVerifyBudget(self)


class _LedgerVerifyBudget(VerifyBudget):
    """VerifyBudget 适配：charge / exhausted 都走 BudgetLedger（单一权威源）。"""

    def __init__(self, ledger: BudgetLedger) -> None:
        super().__init__(
            max_verify_calls=ledger._limits.max_verify_calls,
            provider_time_budget_s=ledger._limits.provider_time_budget_s,
        )
        self._ledger = ledger

    @property
    def exhausted(self) -> bool:
        dims = self._ledger.exhausted_dimensions()
        return ("max_verify_calls" in dims or "max_provider_calls" in dims
                or "provider_time_budget_s" in dims)

    def charge(self, *, elapsed_s: float = 0.0) -> None:
        self._ledger.charge_verify(1)


__all__ = [
    "RetrievalLimits",
    "StopReason",
    "CancelToken",
    "ReservationState",
    "ReservationError",
    "Reservation",
    "BudgetLedger",
]
