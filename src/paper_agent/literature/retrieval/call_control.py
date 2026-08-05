"""Operation-scoped control for one physical external provider attempt.

The order is intentional: optional single-flight leadership, atomic budget
reservation, provider lease, final cancel/deadline check, mark-started, then
the physical call.  A started call is always settled; a call that never starts
is released.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, TypeVar

from paper_agent.literature.retrieval.budget import (
    BudgetLedger,
    CancelToken,
    Reservation,
)


T = TypeVar("T")


class ExternalCallStatus(str, Enum):
    SUCCESS = "success"
    BUDGET_REJECTED = "budget_rejected"
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    GATE_REJECTED = "gate_rejected"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True)
class CallOutcome:
    status: ExternalCallStatus
    provider_name: str
    capability: str
    value: object | None = None
    error: Exception | None = None
    reservation: Reservation | None = None
    physical_call_started: bool = False
    elapsed_s: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.status is ExternalCallStatus.SUCCESS


class ProviderLeaseProtocol(Protocol):
    def release(self) -> None: ...


class ProviderCallGate(Protocol):
    def acquire(
        self,
        *,
        provider_name: str,
        capability: str,
        cancel_token: CancelToken | None,
        deadline_at: float | None,
    ) -> ProviderLeaseProtocol | None: ...


class SingleFlightCoordinator(Protocol):
    def run(self, key: object, fn: Callable[[], CallOutcome]) -> CallOutcome: ...


class _NoopProviderLease:
    def __init__(self) -> None:
        self._released = False

    def release(self) -> None:
        self._released = True


class NoopProviderCallGate:
    """Serial-path gate placeholder; B3 may inject a bounded real gate."""

    def acquire(
        self,
        *,
        provider_name: str,
        capability: str,
        cancel_token: CancelToken | None,
        deadline_at: float | None,
    ) -> ProviderLeaseProtocol:
        return _NoopProviderLease()


class ExternalCallControl:
    def __init__(
        self,
        ledger: BudgetLedger,
        *,
        provider_gate: ProviderCallGate | None = None,
        single_flight: SingleFlightCoordinator | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ledger = ledger
        self._provider_gate = provider_gate or NoopProviderCallGate()
        self._single_flight = single_flight
        self._clock = clock

    def execute(
        self,
        *,
        provider_name: str,
        capability: str,
        budget_dims: dict[str, int],
        fn: Callable[[], T],
        cancel_token: CancelToken | None = None,
        deadline_at: float | None = None,
        single_flight_key: object | None = None,
    ) -> CallOutcome:
        def leader() -> CallOutcome:
            return self._execute_leader(
                provider_name=provider_name,
                capability=capability,
                budget_dims=budget_dims,
                fn=fn,
                cancel_token=cancel_token,
                deadline_at=deadline_at,
            )

        if single_flight_key is not None and self._single_flight is not None:
            return self._single_flight.run(single_flight_key, leader)
        return leader()

    def _execute_leader(
        self,
        *,
        provider_name: str,
        capability: str,
        budget_dims: dict[str, int],
        fn: Callable[[], T],
        cancel_token: CancelToken | None,
        deadline_at: float | None,
    ) -> CallOutcome:
        reservation = self._ledger.try_reserve(budget_dims)
        if reservation is None:
            return self._outcome(
                ExternalCallStatus.BUDGET_REJECTED,
                provider_name,
                capability,
            )

        lease: ProviderLeaseProtocol | None = None
        try:
            try:
                lease = self._provider_gate.acquire(
                    provider_name=provider_name,
                    capability=capability,
                    cancel_token=cancel_token,
                    deadline_at=deadline_at,
                )
            except Exception as exc:  # noqa: BLE001
                self._ledger.release(reservation)
                return self._outcome(
                    ExternalCallStatus.GATE_REJECTED,
                    provider_name,
                    capability,
                    error=exc,
                    reservation=reservation,
                )

            if lease is None:
                self._ledger.release(reservation)
                return self._outcome(
                    self._stop_status(cancel_token, deadline_at)
                    or ExternalCallStatus.GATE_REJECTED,
                    provider_name,
                    capability,
                    reservation=reservation,
                )

            stopped = self._stop_status(cancel_token, deadline_at)
            if stopped is not None:
                self._ledger.release(reservation)
                return self._outcome(
                    stopped,
                    provider_name,
                    capability,
                    reservation=reservation,
                )

            self._ledger.mark_started(reservation)
            started_at = self._clock()
            try:
                value = fn()
            except Exception as exc:  # noqa: BLE001
                return self._outcome(
                    ExternalCallStatus.PROVIDER_ERROR,
                    provider_name,
                    capability,
                    error=exc,
                    reservation=reservation,
                    physical_call_started=True,
                    elapsed_s=max(0.0, self._clock() - started_at),
                )
            else:
                return self._outcome(
                    ExternalCallStatus.SUCCESS,
                    provider_name,
                    capability,
                    value=value,
                    reservation=reservation,
                    physical_call_started=True,
                    elapsed_s=max(0.0, self._clock() - started_at),
                )
            finally:
                self._ledger.settle(reservation)
        finally:
            if lease is not None:
                lease.release()

    def _stop_status(
        self,
        cancel_token: CancelToken | None,
        deadline_at: float | None,
    ) -> ExternalCallStatus | None:
        if cancel_token is not None and cancel_token.cancelled:
            return ExternalCallStatus.CANCELLED
        if deadline_at is not None and self._clock() >= deadline_at:
            return ExternalCallStatus.DEADLINE_EXCEEDED
        return None

    @staticmethod
    def _outcome(
        status: ExternalCallStatus,
        provider_name: str,
        capability: str,
        **kwargs,
    ) -> CallOutcome:
        return CallOutcome(
            status=status,
            provider_name=provider_name,
            capability=capability,
            **kwargs,
        )


class ExternalCallRejected(RuntimeError):
    def __init__(self, outcome: CallOutcome) -> None:
        self.outcome = outcome
        super().__init__(f"external call rejected: {outcome.status.value}")


class BudgetAwareLLMProvider:
    """LLM wrapper that controls every physical ``complete`` attempt.

    Wrapping at this boundary means ``structured_call``'s compatibility retry
    performs a second reservation and settlement, while unwrapped legacy calls
    retain their original behavior.
    """

    def __init__(
        self,
        provider,
        control: ExternalCallControl,
        *,
        provider_name: str,
        cancel_token: CancelToken | None = None,
        deadline_at: float | None = None,
    ) -> None:
        self._provider = provider
        self._control = control
        self._provider_name = provider_name
        self._cancel_token = cancel_token
        self._deadline_at = deadline_at
        self._physical_attempts = 0

    @property
    def physical_attempts(self) -> int:
        """Number of calls that crossed mark_started on this wrapper."""
        return self._physical_attempts

    @property
    def structured_call_capability_owner(self):
        """Keep adaptive structured-call capabilities on the stable provider."""
        return self._provider

    def complete(self, messages, **opts):
        outcome = self._control.execute(
            provider_name=self._provider_name,
            capability="llm.complete",
            budget_dims={"llm_calls": 1},
            cancel_token=self._cancel_token,
            deadline_at=self._deadline_at,
            fn=lambda: self._provider.complete(messages, **opts),
        )
        if outcome.physical_call_started:
            self._physical_attempts += 1
        if outcome.succeeded:
            return outcome.value
        if outcome.error is not None:
            raise outcome.error
        raise ExternalCallRejected(outcome)


__all__ = [
    "ExternalCallStatus",
    "CallOutcome",
    "ProviderLeaseProtocol",
    "ProviderCallGate",
    "SingleFlightCoordinator",
    "NoopProviderCallGate",
    "ExternalCallControl",
    "ExternalCallRejected",
    "BudgetAwareLLMProvider",
]
