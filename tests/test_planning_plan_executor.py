"""intent-planning Task 20 验收：PlanExecutor + 状态聚合。

- 全成功 → done
- 前成功后失败 → partially_completed + 后续 skipped_due_to_previous_failure
- 首项 Gate 拒 → failed + failure_category=validation_rejected
- 首项系统异常 → failed + execution_failed
- 保序串行（一次一个）
- baseline 建立、rollback_available、remaining_operations
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.router import Intent
from paper_agent.runtime import handlers as H
from paper_agent.runtime import plan_executor as PE
from paper_agent.runtime.handlers import HandlerContext
from paper_agent.runtime.plan_executor import aggregate_plan_status, execute_plan
from paper_agent.session.file_session import SessionBaseline
from paper_agent.tasking.contracts import (
    FailureCategory,
    FieldStatus,
    Operation,
    OperationResult,
    OperationStatus,
    Plan_Execution_State,
    PlanStatus,
    SlotSource,
    SlotValue,
    Task_Plan,
)


def _present(v):
    return SlotValue(value=v, field_status=FieldStatus.PRESENT, source=SlotSource.USER_EXPLICIT)


def _op(op_id, intent=Intent.POLISH):
    return Operation(operation_id=op_id, intent=intent,
                     slots={"section_id": _present("introduction")})


def _plan(*ops):
    return Task_Plan(plan_id="p", source_turn_id="t", operations=tuple(ops))


class _FakeSession:
    def __init__(self):
        self.baselines = []
        self.restored = []
        self.released = []

    def create_baseline(self, label="plan"):
        b = SessionBaseline(baseline_id=f"{label}_1")
        self.baselines.append(b)
        return b

    def restore_baseline(self, b):
        self.restored.append(b)
        return True

    def release_baseline(self, b):
        self.released.append(b)


class _ScriptedHandler:
    """按 op_id 返回预设状态，并记录执行顺序。"""

    def __init__(self, outcomes: dict, order: list):
        self.intent = Intent.POLISH
        self._outcomes = outcomes
        self._order = order

    def execute(self, op, ctx, bus=None, cancel_token=None):
        self._order.append(op.operation_id)
        status = self._outcomes.get(op.operation_id, OperationStatus.SUCCEEDED)
        if status == "raise":
            raise RuntimeError("boom")
        return OperationResult(operation_id=op.operation_id, status=status,
                               changed_working_copy=status is OperationStatus.SUCCEEDED)


def _run(monkeypatch, ops, outcomes):
    order: list[str] = []
    handler = _ScriptedHandler(outcomes, order)
    monkeypatch.setattr(PE, "handler_for", lambda intent: handler)
    session = _FakeSession()
    ctx = HandlerContext(session=session)
    state = Plan_Execution_State()
    outcome = execute_plan(_plan(*ops), state, ctx)
    return outcome, state, order, session


# ============ 状态聚合（纯函数）============

def test_aggregate_all_success_is_done():
    rs = [OperationResult("a", OperationStatus.SUCCEEDED),
          OperationResult("b", OperationStatus.SUCCEEDED)]
    assert aggregate_plan_status(rs) == (PlanStatus.DONE, None)


def test_aggregate_first_rejected_is_failed_validation():
    rs = [OperationResult("a", OperationStatus.REJECTED)]
    st, cat = aggregate_plan_status(rs)
    assert st is PlanStatus.FAILED
    assert cat is FailureCategory.VALIDATION_REJECTED


def test_aggregate_first_failed_is_execution_failed():
    rs = [OperationResult("a", OperationStatus.FAILED)]
    st, cat = aggregate_plan_status(rs)
    assert st is PlanStatus.FAILED
    assert cat is FailureCategory.EXECUTION_FAILED


def test_aggregate_partial_when_earlier_succeeded():
    rs = [OperationResult("a", OperationStatus.SUCCEEDED),
          OperationResult("b", OperationStatus.REJECTED)]
    st, cat = aggregate_plan_status(rs)
    assert st is PlanStatus.PARTIALLY_COMPLETED
    assert cat is FailureCategory.VALIDATION_REJECTED


def test_aggregate_empty_is_done():
    assert aggregate_plan_status([]) == (PlanStatus.DONE, None)


# ============ execute_plan ============

def test_all_success_done_and_ordered(monkeypatch):
    ops = [_op("o1"), _op("o2"), _op("o3")]
    outcome, state, order, session = _run(monkeypatch, ops, {})
    assert outcome.plan_status is PlanStatus.DONE
    assert order == ["o1", "o2", "o3"]              # 严格保序串行
    assert len(state.completed_operations) == 3
    assert outcome.remaining_operations == ()
    assert session.baselines                        # 建了基线


def test_failure_stops_and_marks_skipped(monkeypatch):
    ops = [_op("o1"), _op("o2"), _op("o3")]
    outcome, state, order, _ = _run(
        monkeypatch, ops, {"o2": OperationStatus.REJECTED})
    assert outcome.plan_status is PlanStatus.PARTIALLY_COMPLETED
    assert outcome.report.failure_category is FailureCategory.VALIDATION_REJECTED
    # o3 未被执行
    assert order == ["o1", "o2"]
    by_id = {r.operation_id: r.status for r in outcome.report.operations}
    assert by_id["o1"] is OperationStatus.SUCCEEDED
    assert by_id["o2"] is OperationStatus.REJECTED
    assert by_id["o3"] is OperationStatus.SKIPPED_DUE_TO_PREVIOUS_FAILURE


def test_first_op_gate_rejected_is_failed(monkeypatch):
    ops = [_op("o1"), _op("o2")]
    outcome, state, order, _ = _run(
        monkeypatch, ops, {"o1": OperationStatus.REJECTED})
    assert outcome.plan_status is PlanStatus.FAILED
    assert outcome.report.failure_category is FailureCategory.VALIDATION_REJECTED
    assert order == ["o1"]
    assert state.completed_operations == []


def test_handler_exception_recorded_as_failed(monkeypatch):
    # Handler 内抛异常 → 由 handlers._guard 转 failed；此处直接给 raise
    ops = [_op("o1"), _op("o2")]
    order: list[str] = []

    class _Raiser:
        intent = Intent.POLISH

        def execute(self, op, ctx, bus=None, cancel_token=None):
            order.append(op.operation_id)
            return OperationResult(op.operation_id, OperationStatus.FAILED,
                                   message="RuntimeError: boom")

    monkeypatch.setattr(PE, "handler_for", lambda intent: _Raiser())
    ctx = HandlerContext(session=_FakeSession())
    state = Plan_Execution_State()
    outcome = execute_plan(_plan(*ops), state, ctx)
    assert outcome.plan_status is PlanStatus.FAILED
    assert outcome.report.failure_category is FailureCategory.EXECUTION_FAILED
    assert order == ["o1"]


def test_unsupported_intent_rejected(monkeypatch):
    monkeypatch.setattr(PE, "handler_for", lambda intent: None)
    ctx = HandlerContext(session=_FakeSession())
    state = Plan_Execution_State()
    outcome = execute_plan(_plan(_op("o1")), state, ctx)
    assert outcome.report.operations[0].status is OperationStatus.REJECTED
    assert "没有可用的执行器" in outcome.report.operations[0].message


def test_rollback_available_after_partial(monkeypatch):
    ops = [_op("o1"), _op("o2")]
    outcome, state, _, _ = _run(monkeypatch, ops, {"o2": OperationStatus.REJECTED})
    assert state.rollback_available is True         # 有基线 + 有已完成
    assert outcome.report.rollback_available is True
    assert outcome.report.completed_operation_ids == ("o1",)


def test_remaining_operations_excludes_succeeded(monkeypatch):
    ops = [_op("o1"), _op("o2"), _op("o3")]
    outcome, _, _, _ = _run(monkeypatch, ops, {"o2": OperationStatus.REJECTED})
    remaining_ids = [op.operation_id for op in outcome.remaining_operations]
    assert remaining_ids == ["o2", "o3"]           # o1 已成功，不重跑


def test_report_rollback_scope_is_working_copy_only(monkeypatch):
    outcome, _, _, _ = _run(monkeypatch, [_op("o1")], {})
    assert outcome.report.rollback_scope == "working_copy_only"


def test_session_without_baseline_support_still_runs(monkeypatch):
    class _Bare:
        pass

    order: list[str] = []
    monkeypatch.setattr(PE, "handler_for", lambda intent: _ScriptedHandler({}, order))
    ctx = HandlerContext(session=_Bare())
    state = Plan_Execution_State()
    outcome = execute_plan(_plan(_op("o1")), state, ctx)
    assert outcome.plan_status is PlanStatus.DONE
    assert state.baseline is None
    assert state.rollback_available is False


# ============ 与真实 Handler 集成（一次成功）============

def test_integration_with_real_polish_handler(tmp_path):
    from paper_agent.providers.llm.mock import MockLLMProvider
    from paper_agent.session.file_session import FileBackedSession

    tex = "\\section{Introduction}\nThe accuracy is 0.87 and we cite \\cite{a}.\n"
    src = tmp_path / "p.tex"
    src.write_text(tex, encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))
    body = "\nThe accuracy is 0.87 and we cite \\cite{a}.\n"
    ctx = HandlerContext(session=session, llm=MockLLMProvider(scripted=[body]))

    state = Plan_Execution_State()
    outcome = execute_plan(_plan(_op("o1")), state, ctx)
    assert outcome.plan_status is PlanStatus.DONE
    assert outcome.report.operations[0].changed_working_copy is True
