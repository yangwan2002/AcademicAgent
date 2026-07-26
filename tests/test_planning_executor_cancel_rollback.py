"""intent-planning Task 21-23 验收：取消 / Baseline 处置 / Pre-Op 校验。

Task 21：取消进 trace（finalize 前发 PLAN_CANCELLED）、保留已完成列表 +
         rollback_available、未执行记 skipped_due_to_cancellation、
         每 op 发 OPERATION_RESULT、终了发 PLAN_RESULT。
Task 22：done 自动 release；rollback→restore+release；partial+keep→release；
         零写入取消→release；部分写入取消→询问；不删既有用户输出。
Task 23：执行期失效→needs_replan（不重澄清）+ 后续 skipped_due_to_replan；
         先加再改执行期通过；needs_replan 只重规划未执行。
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.errors import OperationCancelled
from paper_agent.intents.router import Intent
from paper_agent.runtime import plan_executor as PE
from paper_agent.runtime.handlers import HandlerContext
from paper_agent.runtime.plan_executor import (
    KEEP,
    ROLLBACK,
    dispose_baseline,
    execute_plan,
    pre_operation_check,
)
from paper_agent.session.file_session import FileBackedSession, SessionBaseline
from paper_agent.tasking.contracts import (
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


def _op(op_id, intent=Intent.POLISH, **slots):
    s = {k: _present(v) for k, v in slots.items()} or {"section_id": _present("introduction")}
    return Operation(operation_id=op_id, intent=intent, slots=s)


def _plan(*ops):
    return Task_Plan(plan_id="p", source_turn_id="t", operations=tuple(ops))


class _FakeSession:
    ext = ""

    def __init__(self):
        self.baselines, self.restored, self.released = [], [], []

    def create_baseline(self, label="plan"):
        b = SessionBaseline(baseline_id=f"{label}_1")
        self.baselines.append(b)
        return b

    def restore_baseline(self, b):
        self.restored.append(b)
        return True

    def release_baseline(self, b):
        self.released.append(b)


class _RecordingBus:
    def __init__(self):
        self.events: list[tuple] = []

    def publish(self, kind, **payload):
        self.events.append((getattr(kind, "value", str(kind)), payload))

    def span(self, name):
        bus = self

        class _S:
            def __enter__(self_inner):
                bus.events.append(("span_enter", {"name": name}))
                return self_inner

            def __exit__(self_inner, *exc):
                return False
        return _S()

    def kinds(self):
        return [k for k, _ in self.events]


class _CancelAt:
    """执行到指定 op 时抛 OperationCancelled。"""

    intent = Intent.POLISH

    def __init__(self, cancel_on: str):
        self._cancel_on = cancel_on
        self.executed: list[str] = []

    def execute(self, op, ctx, bus=None, cancel_token=None):
        if op.operation_id == self._cancel_on:
            raise OperationCancelled()
        self.executed.append(op.operation_id)
        return OperationResult(op.operation_id, OperationStatus.SUCCEEDED,
                               changed_working_copy=True)


class _AlwaysOk:
    intent = Intent.POLISH

    def execute(self, op, ctx, bus=None, cancel_token=None):
        return OperationResult(op.operation_id, OperationStatus.SUCCEEDED,
                               changed_working_copy=True)


# ============ Task 21：取消 ============

def test_cancel_publishes_plan_cancelled_before_raise(monkeypatch):
    monkeypatch.setattr(PE, "handler_for", lambda i: _CancelAt("o2"))
    bus = _RecordingBus()
    state = Plan_Execution_State()
    ctx = HandlerContext(session=_FakeSession())
    with pytest.raises(OperationCancelled):
        execute_plan(_plan(_op("o1"), _op("o2"), _op("o3")), state, ctx, bus=bus)
    # 取消事件在上抛前发布（进 trace）
    assert "plan_cancelled" in bus.kinds()
    assert "plan_result" in bus.kinds()


def test_cancel_keeps_completed_and_rollback_available(monkeypatch):
    monkeypatch.setattr(PE, "handler_for", lambda i: _CancelAt("o2"))
    state = Plan_Execution_State()
    ctx = HandlerContext(session=_FakeSession())
    with pytest.raises(OperationCancelled):
        execute_plan(_plan(_op("o1"), _op("o2")), state, ctx)
    assert state.cancelled is True
    assert state.completed_ids == ["o1"]
    # 部分写入 + 无 ask_disposition → baseline 保留（pending），仍可回滚
    assert state.rollback_available is True


def test_cancel_marks_unexecuted_as_skipped_due_to_cancellation(monkeypatch):
    monkeypatch.setattr(PE, "handler_for", lambda i: _CancelAt("o2"))
    state = Plan_Execution_State()
    ctx = HandlerContext(session=_FakeSession())
    with pytest.raises(OperationCancelled):
        execute_plan(_plan(_op("o1"), _op("o2"), _op("o3")), state, ctx)
    report = state.to_cancelled_report()
    assert report.plan_status is PlanStatus.PARTIALLY_COMPLETED


def test_cancel_before_any_write_is_cancelled_status(monkeypatch):
    monkeypatch.setattr(PE, "handler_for", lambda i: _CancelAt("o1"))
    bus = _RecordingBus()
    state = Plan_Execution_State()
    session = _FakeSession()
    ctx = HandlerContext(session=session)
    with pytest.raises(OperationCancelled):
        execute_plan(_plan(_op("o1")), state, ctx, bus=bus)
    cancelled = [p for k, p in bus.events if k == "plan_cancelled"][0]
    assert cancelled["plan_status"] == "cancelled"
    # 零写入取消 → baseline 直接 release
    assert session.released


def test_operation_result_events_emitted(monkeypatch):
    monkeypatch.setattr(PE, "handler_for", lambda i: _AlwaysOk())
    bus = _RecordingBus()
    state = Plan_Execution_State()
    ctx = HandlerContext(session=_FakeSession())
    execute_plan(_plan(_op("o1"), _op("o2")), state, ctx, bus=bus)
    op_results = [p for k, p in bus.events if k == "operation_result"]
    assert len(op_results) == 2
    assert all(p["status"] == "succeeded" for p in op_results)
    plan_res = [p for k, p in bus.events if k == "plan_result"][0]
    assert plan_res["plan_status"] == "done"


def test_plan_result_shows_skipped_count(monkeypatch):
    class _FailSecond:
        intent = Intent.POLISH

        def execute(self, op, ctx, bus=None, cancel_token=None):
            st = (OperationStatus.REJECTED if op.operation_id == "o2"
                  else OperationStatus.SUCCEEDED)
            return OperationResult(op.operation_id, st, changed_working_copy=(st is OperationStatus.SUCCEEDED))

    monkeypatch.setattr(PE, "handler_for", lambda i: _FailSecond())
    bus = _RecordingBus()
    state = Plan_Execution_State()
    ctx = HandlerContext(session=_FakeSession())
    execute_plan(_plan(_op("o1"), _op("o2"), _op("o3")), state, ctx, bus=bus)
    plan_res = [p for k, p in bus.events if k == "plan_result"][0]
    assert plan_res["plan_status"] == "partially_completed"
    assert plan_res["skipped"] == 1


# ============ Task 22：Baseline 处置 ============

def test_done_releases_baseline(monkeypatch):
    monkeypatch.setattr(PE, "handler_for", lambda i: _AlwaysOk())
    session = _FakeSession()
    state = Plan_Execution_State()
    execute_plan(_plan(_op("o1")), state, HandlerContext(session=session))
    assert session.released and not session.restored
    assert state.baseline is None


def test_partial_with_keep_releases(monkeypatch):
    class _FailSecond:
        intent = Intent.POLISH

        def execute(self, op, ctx, bus=None, cancel_token=None):
            st = (OperationStatus.REJECTED if op.operation_id == "o2"
                  else OperationStatus.SUCCEEDED)
            return OperationResult(op.operation_id, st, changed_working_copy=True)

    monkeypatch.setattr(PE, "handler_for", lambda i: _FailSecond())
    session = _FakeSession()
    state = Plan_Execution_State()
    execute_plan(_plan(_op("o1"), _op("o2")), state, HandlerContext(session=session),
                 ask_disposition=lambda ids: KEEP)
    assert session.released and not session.restored


def test_partial_with_rollback_restores_then_releases(monkeypatch):
    class _FailSecond:
        intent = Intent.POLISH

        def execute(self, op, ctx, bus=None, cancel_token=None):
            st = (OperationStatus.REJECTED if op.operation_id == "o2"
                  else OperationStatus.SUCCEEDED)
            return OperationResult(op.operation_id, st, changed_working_copy=True)

    monkeypatch.setattr(PE, "handler_for", lambda i: _FailSecond())
    session = _FakeSession()
    state = Plan_Execution_State()
    execute_plan(_plan(_op("o1"), _op("o2")), state, HandlerContext(session=session),
                 ask_disposition=lambda ids: ROLLBACK)
    assert session.restored and session.released
    assert state.baseline is None


def test_no_answer_keeps_baseline_pending():
    session = _FakeSession()
    state = Plan_Execution_State(baseline=session.create_baseline())
    state.completed_operations.append(OperationResult("o1", OperationStatus.SUCCEEDED))
    action = dispose_baseline(state, HandlerContext(session=session),
                             PlanStatus.PARTIALLY_COMPLETED, ask_disposition=None)
    assert action == "pending"
    assert state.baseline is not None        # 不静默丢弃
    assert not session.released


def test_cleanup_only_removes_new_outputs(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    src = tmp_path / "p.tex"
    src.write_text("\\section{Introduction}\nx\n", encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))

    # 执行前就存在的用户文件（不得被删）
    pre_existing = out / "p.docx"
    pre_existing.write_text("user's own file", encoding="utf-8")

    existed = {str(pre_existing): True, str(out / "p.md"): False}
    (out / "p.md").write_text("this round's output", encoding="utf-8")
    removed = PE._cleanup_new_outputs(existed)

    assert pre_existing.exists()                      # 既有用户输出保住
    assert str(out / "p.md") in removed               # 只删本轮新建的
    assert not (out / "p.md").exists()


# ============ Task 23：Pre-Op 校验 + needs_replan ============

def _tex_session(tmp_path, text):
    src = tmp_path / "p.tex"
    src.write_text(text, encoding="utf-8")
    return FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))


def test_pre_op_passes_for_existing_section(tmp_path):
    session = _tex_session(tmp_path, "\\section{Introduction}\nbody\n")
    ctx = HandlerContext(session=session)
    assert pre_operation_check(_op("o1", section_id="introduction"), ctx) is None


def test_pre_op_detects_missing_section(tmp_path):
    session = _tex_session(tmp_path, "\\section{Introduction}\nbody\n")
    ctx = HandlerContext(session=session)
    reason = pre_operation_check(_op("o1", section_id="discussion"), ctx)
    assert reason and "已不存在" in reason


def test_pre_op_add_section_detects_already_exists(tmp_path):
    session = _tex_session(tmp_path, "\\section{Discussion}\nbody\n")
    ctx = HandlerContext(session=session)
    reason = pre_operation_check(
        _op("o1", Intent.ADD_SECTION, title="Discussion"), ctx)
    assert reason and "已存在" in reason


def test_pre_op_format_requires_docx(tmp_path):
    session = _tex_session(tmp_path, "\\section{X}\nbody\n")
    ctx = HandlerContext(session=session)
    reason = pre_operation_check(_op("o1", Intent.FORMAT_DOCX, operations=[]), ctx)
    assert reason and "docx" in reason


def test_execution_time_invalidation_yields_needs_replan(tmp_path, monkeypatch):
    # o1 成功；o2 目标章节不存在 → blocked_needs_replan；o3 skipped_due_to_replan
    session = _tex_session(tmp_path, "\\section{Introduction}\nbody\n")
    monkeypatch.setattr(PE, "handler_for", lambda i: _AlwaysOk())
    state = Plan_Execution_State()
    plan = _plan(
        _op("o1", section_id="introduction"),
        _op("o2", section_id="ghost"),
        _op("o3", section_id="introduction"),
    )
    outcome = execute_plan(plan, state, HandlerContext(session=session))
    assert outcome.plan_status is PlanStatus.NEEDS_REPLAN
    by_id = {r.operation_id: r.status for r in outcome.report.operations}
    assert by_id["o1"] is OperationStatus.SUCCEEDED
    assert by_id["o2"] is OperationStatus.BLOCKED_NEEDS_REPLAN
    assert by_id["o3"] is OperationStatus.SKIPPED_DUE_TO_REPLAN
    assert outcome.replan_reason


def test_needs_replan_remaining_excludes_succeeded(tmp_path, monkeypatch):
    session = _tex_session(tmp_path, "\\section{Introduction}\nbody\n")
    monkeypatch.setattr(PE, "handler_for", lambda i: _AlwaysOk())
    state = Plan_Execution_State()
    plan = _plan(
        _op("o1", section_id="introduction"),
        _op("o2", section_id="ghost"),
        _op("o3", section_id="introduction"),
    )
    outcome = execute_plan(plan, state, HandlerContext(session=session))
    ids = [op.operation_id for op in outcome.remaining_operations]
    assert ids == ["o2", "o3"]           # o1 已成功，不重跑


def test_needs_replan_keeps_baseline(tmp_path, monkeypatch):
    # needs_replan 后规划循环还要继续，基线必须保留
    session = _tex_session(tmp_path, "\\section{Introduction}\nbody\n")
    monkeypatch.setattr(PE, "handler_for", lambda i: _AlwaysOk())
    state = Plan_Execution_State()
    plan = _plan(_op("o1", section_id="introduction"), _op("o2", section_id="ghost"))
    outcome = execute_plan(plan, state, HandlerContext(session=session))
    assert outcome.plan_status is PlanStatus.NEEDS_REPLAN
    assert state.baseline is not None


def test_pre_op_does_not_reclarify(tmp_path, monkeypatch):
    """执行期失效只报 needs_replan，绝不在循环内追问用户。"""
    session = _tex_session(tmp_path, "\\section{Introduction}\nbody\n")
    monkeypatch.setattr(PE, "handler_for", lambda i: _AlwaysOk())
    asked = []
    state = Plan_Execution_State()
    plan = _plan(_op("o1", section_id="ghost"))
    outcome = execute_plan(plan, state, HandlerContext(session=session),
                           ask_disposition=lambda ids: asked.append(ids) or KEEP)
    assert outcome.plan_status is PlanStatus.NEEDS_REPLAN
    assert asked == []                   # 没有任何用户交互
