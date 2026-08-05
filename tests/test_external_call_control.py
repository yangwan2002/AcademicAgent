"""Task 3 physical external-call boundary tests."""

from __future__ import annotations

import ast
from pathlib import Path

from paper_agent.literature.retrieval.budget import (
    BudgetLedger,
    CancelToken,
    ReservationState,
    RetrievalLimits,
)
from paper_agent.literature.retrieval.call_control import (
    BudgetAwareLLMProvider,
    ExternalCallControl,
    ExternalCallStatus,
)
from paper_agent.providers.llm.base import LLMResponse, Message, ToolCall
from paper_agent.providers.llm.structured import CallStatus, structured_call


def _ledger(*, llm=10, verify=10):
    return BudgetLedger(
        RetrievalLimits(
            max_iterations=10,
            max_provider_calls=20,
            max_search_calls=10,
            max_verify_calls=verify,
            max_discovery_calls=10,
            max_llm_calls=llm,
            max_candidates=100,
            provider_time_budget_s=60.0,
        ),
        clock=lambda: 0.0,
    )


class _Lease:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


class _Gate:
    def __init__(self, ledger, *, cancel=None, fail=None):
        self.ledger = ledger
        self.cancel = cancel
        self.fail = fail
        self.lease = _Lease()
        self.saw_reserved = False

    def acquire(self, **kwargs):
        self.saw_reserved = self.ledger.reserved_counts()["llm_calls"] == 1
        if self.cancel is not None:
            self.cancel.cancel()
        if self.fail is not None:
            raise self.fail
        return self.lease


def test_success_marks_started_settles_and_releases_lease():
    ledger = _ledger()
    gate = _Gate(ledger)
    outcome = ExternalCallControl(ledger, provider_gate=gate, clock=lambda: 0.0).execute(
        provider_name="reviewer",
        capability="llm.complete",
        budget_dims={"llm_calls": 1},
        fn=lambda: "ok",
    )
    assert outcome.status is ExternalCallStatus.SUCCESS
    assert outcome.value == "ok"
    assert outcome.physical_call_started is True
    assert outcome.reservation.state is ReservationState.SETTLED
    assert ledger.llm_calls == 1
    assert gate.saw_reserved is True
    assert gate.lease.released == 1


def test_cancel_after_gate_releases_without_starting_provider():
    ledger = _ledger()
    token = CancelToken()
    gate = _Gate(ledger, cancel=token)
    called = []
    outcome = ExternalCallControl(ledger, provider_gate=gate, clock=lambda: 0.0).execute(
        provider_name="reviewer",
        capability="llm.complete",
        budget_dims={"llm_calls": 1},
        cancel_token=token,
        fn=lambda: called.append(True),
    )
    assert outcome.status is ExternalCallStatus.CANCELLED
    assert outcome.physical_call_started is False
    assert outcome.reservation.state is ReservationState.RELEASED
    assert ledger.llm_calls == 0
    assert called == []
    assert gate.lease.released == 1


def test_deadline_before_physical_call_releases_reservation():
    ledger = _ledger()
    outcome = ExternalCallControl(ledger, clock=lambda: 10.0).execute(
        provider_name="crossref",
        capability="verify.fetch_metadata",
        budget_dims={"verify_calls": 1},
        deadline_at=10.0,
        fn=lambda: "must-not-run",
    )
    assert outcome.status is ExternalCallStatus.DEADLINE_EXCEEDED
    assert outcome.reservation.state is ReservationState.RELEASED
    assert ledger.verify_calls == 0


def test_provider_exception_is_still_one_settled_physical_call():
    ledger = _ledger()

    def fail():
        raise TimeoutError("offline timeout")

    outcome = ExternalCallControl(ledger, clock=lambda: 0.0).execute(
        provider_name="crossref",
        capability="verify.fetch_metadata",
        budget_dims={"verify_calls": 1},
        fn=fail,
    )
    assert outcome.status is ExternalCallStatus.PROVIDER_ERROR
    assert isinstance(outcome.error, TimeoutError)
    assert outcome.reservation.state is ReservationState.SETTLED
    assert ledger.verify_calls == 1


def test_budget_rejection_never_calls_gate_or_provider():
    ledger = _ledger(llm=1)
    ledger.charge_llm()
    called = []
    outcome = ExternalCallControl(ledger, clock=lambda: 0.0).execute(
        provider_name="reviewer",
        capability="llm.complete",
        budget_dims={"llm_calls": 1},
        fn=lambda: called.append(True),
    )
    assert outcome.status is ExternalCallStatus.BUDGET_REJECTED
    assert outcome.reservation is None
    assert called == []
    assert ledger.llm_calls == 1


def test_gate_failure_releases_reservation_without_charging():
    ledger = _ledger()
    gate = _Gate(ledger, fail=RuntimeError("gate unavailable"))
    outcome = ExternalCallControl(ledger, provider_gate=gate, clock=lambda: 0.0).execute(
        provider_name="reviewer",
        capability="llm.complete",
        budget_dims={"llm_calls": 1},
        fn=lambda: "must-not-run",
    )
    assert outcome.status is ExternalCallStatus.GATE_REJECTED
    assert outcome.reservation.state is ReservationState.RELEASED
    assert ledger.llm_calls == 0


class _CompatibilityRetryLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, **opts):
        self.calls += 1
        if "forced_tool_name" in opts:
            raise TypeError("unexpected keyword forced_tool_name")
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="1",
                    name="report",
                    arguments={"value": "ok"},
                )
            ],
        )


_TOOL = {
    "function": {
        "name": "report",
        "parameters": {"type": "object"},
    }
}


def test_structured_compatibility_retry_settles_two_physical_llm_calls():
    ledger = _ledger(llm=2)
    raw = _CompatibilityRetryLLM()
    wrapped = BudgetAwareLLMProvider(
        raw,
        ExternalCallControl(ledger, clock=lambda: 0.0),
        provider_name="reviewer",
    )
    result = structured_call(
        wrapped,
        [Message(role="user", content="offline")],
        tool_schema=_TOOL,
    )
    assert result.call_status is CallStatus.SUCCESS
    assert result.payload == {"value": "ok"}
    assert raw.calls == 2
    assert ledger.llm_calls == 2
    assert ledger.reservation_stats()["settled"] == 2
    assert result.provider_attempts == 2
    assert result.compatibility_retries == 1


def test_structured_compatibility_capability_is_reused_after_first_probe():
    ledger = _ledger(llm=3)
    raw = _CompatibilityRetryLLM()
    control = ExternalCallControl(ledger, clock=lambda: 0.0)

    first = structured_call(
        BudgetAwareLLMProvider(raw, control, provider_name="reviewer"),
        [Message(role="user", content="first")],
        tool_schema=_TOOL,
    )
    second = structured_call(
        BudgetAwareLLMProvider(raw, control, provider_name="reviewer"),
        [Message(role="user", content="second")],
        tool_schema=_TOOL,
    )

    assert first.call_status is second.call_status is CallStatus.SUCCESS
    assert first.provider_attempts == 2
    assert first.compatibility_retries == 1
    assert second.provider_attempts == 1
    assert second.compatibility_retries == 0
    assert raw.calls == ledger.llm_calls == 3


def test_compatibility_retry_is_stopped_when_second_physical_budget_is_absent():
    ledger = _ledger(llm=1)
    raw = _CompatibilityRetryLLM()
    wrapped = BudgetAwareLLMProvider(
        raw,
        ExternalCallControl(ledger, clock=lambda: 0.0),
        provider_name="reviewer",
    )
    result = structured_call(
        wrapped,
        [Message(role="user", content="offline")],
        tool_schema=_TOOL,
    )
    assert result.call_status is CallStatus.PROVIDER_FAILED
    assert raw.calls == 1
    assert ledger.llm_calls == 1
    assert ledger.reservation_stats()["rejected"] == 1


def test_unwrapped_structured_call_retains_legacy_behavior():
    raw = _CompatibilityRetryLLM()
    result = structured_call(
        raw,
        [Message(role="user", content="offline")],
        tool_schema=_TOOL,
    )
    assert result.call_status is CallStatus.SUCCESS
    assert raw.calls == 2


def test_call_control_obeys_literature_architecture_boundary():
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "paper_agent"
        / "literature"
        / "retrieval"
        / "call_control.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    forbidden = (
        "paper_agent.tasking",
        "paper_agent.application",
        "paper_agent.session",
        "paper_agent.config",
    )
    assert not any(name.startswith(forbidden) for name in imports)
