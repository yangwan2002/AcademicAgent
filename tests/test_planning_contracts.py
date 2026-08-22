"""intent-planning Task 1 验收：数据模型。

- 值对象 frozen（改属性抛错）
- Plan_Execution_State 可变、to_cancelled_report 正确
- 枚举取值集合正确（field_status 含 unavailable、op status 含 blocked_needs_replan/skipped_*）
- 跨模块结果模型字段齐全
"""

from __future__ import annotations

import dataclasses
import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.intent import Intent
from paper_agent.tasking.contracts import (
    AnalyzeResult,
    AtomicRequirement,
    CallStatus,
    ConfirmedPlan,
    ExecutionOutcome,
    ExtractionResult,
    FailureCategory,
    FieldStatus,
    Operation,
    OperationResult,
    OperationStatus,
    PlanExecutionReport,
    PlanRevisionRequest,
    PlanStatus,
    PlanningExit,
    PlanningSnapshot,
    Plan_Execution_State,
    RawOperation,
    RevisionReason,
    SectionSummary,
    SessionBaseline,
    SlotSource,
    SlotValue,
    Task_Plan,
    ValidationIssue,
    ValidationOutcome,
    ReorderSuggestion,
)


# --- 值对象 frozen ---

def test_value_objects_are_frozen():
    ar = AtomicRequirement(requirement_id="r1", verbatim="不改数字")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ar.verbatim = "x"  # type: ignore[misc]

    sv = SlotValue(value="intro", field_status=FieldStatus.PRESENT,
                   source=SlotSource.USER_EXPLICIT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sv.value = "y"  # type: ignore[misc]

    plan = Task_Plan(plan_id="p1", source_turn_id="t1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.status = PlanStatus.DONE  # type: ignore[misc]

    rep = PlanExecutionReport(plan_status=PlanStatus.DONE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rep.rollback_available = True  # type: ignore[misc]

    base = SessionBaseline(baseline_id="b1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        base.git_commit = "sha"  # type: ignore[misc]


# --- 枚举完整性 ---

def test_field_status_has_unavailable():
    assert FieldStatus.UNAVAILABLE.value == "unavailable"
    assert {s.value for s in FieldStatus} == {
        "present", "user_absent", "ambiguous", "invalid", "unavailable"
    }


def test_call_status_four_states():
    assert {s.value for s in CallStatus} == {
        "success", "provider_failed", "parse_failed", "schema_failed"
    }


def test_operation_status_has_replan_and_skips():
    vals = {s.value for s in OperationStatus}
    assert "blocked_needs_replan" in vals
    assert "skipped_due_to_previous_failure" in vals
    assert "skipped_due_to_cancellation" in vals
    assert "skipped_due_to_replan" in vals


def test_plan_status_and_failure_category():
    pvals = {s.value for s in PlanStatus}
    for expected in ("done", "partially_completed", "needs_replan",
                     "rolled_back", "revision_limit_exceeded"):
        assert expected in pvals
    assert {c.value for c in FailureCategory} == {
        "execution_failed", "validation_rejected"
    }


def test_slot_source_no_inferred():
    # 去掉了 inferred（design D3）
    vals = {s.value for s in SlotSource}
    assert vals == {"user_explicit", "normalized", "defaulted", "user_confirmed"}
    assert "inferred" not in vals


# --- Plan_Execution_State 可变 + to_cancelled_report ---

def test_execution_state_is_mutable():
    state = Plan_Execution_State()
    state.current_operation_id = "op1"          # 不抛错
    state.completed_operations.append(
        OperationResult(operation_id="op1", status=OperationStatus.SUCCEEDED)
    )
    state.mark_cancelled()
    assert state.cancelled is True
    assert state.completed_ids == ["op1"]


def test_to_cancelled_report_partial():
    state = Plan_Execution_State(baseline=SessionBaseline(baseline_id="b1"))
    state.completed_operations.append(
        OperationResult(operation_id="op1", status=OperationStatus.SUCCEEDED)
    )
    rep = state.to_cancelled_report()
    assert rep.plan_status is PlanStatus.PARTIALLY_COMPLETED
    assert rep.completed_operation_ids == ("op1",)
    assert rep.rollback_available is True


def test_to_cancelled_report_no_writes():
    state = Plan_Execution_State()   # 无 baseline、无已完成
    rep = state.to_cancelled_report()
    assert rep.plan_status is PlanStatus.CANCELLED
    assert rep.rollback_available is False


# --- 跨模块模型字段齐全 ---

def test_cross_module_models_present():
    ar = AtomicRequirement(requirement_id="r1", verbatim="v")
    ao = AnalyzeResult(
        call_status=CallStatus.SUCCESS,
        operations=(RawOperation(intent=Intent.POLISH, verbatim="润色引言"),),
        requirements=(ar,),
    )
    assert ao.operations[0].intent is Intent.POLISH

    er = ExtractionResult(call_status=CallStatus.SUCCESS,
                          slots={"section_id": SlotValue(value="intro")})
    assert er.slots["section_id"].value == "intro"

    vo = ValidationOutcome(
        result="rejected",
        issues=(ValidationIssue(operation_id="op1", code="section_not_found"),),
        reorder_suggestions=(ReorderSuggestion(reason="convert 必须最后"),),
    )
    assert vo.issues[0].code == "section_not_found"

    snap = PlanningSnapshot(
        working_hash="h", file_type="tex",
        sections=(SectionSummary(section_id="intro", title="Introduction"),),
    )
    assert snap.sections[0].title == "Introduction"

    rev = PlanRevisionRequest(previous_plan=None, revision_text="第二步改 md",
                              revision_reason=RevisionReason.USER_MODIFY)
    assert rev.revision_reason is RevisionReason.USER_MODIFY

    outcome = ExecutionOutcome(
        report=PlanExecutionReport(plan_status=PlanStatus.NEEDS_REPLAN),
        plan_status=PlanStatus.NEEDS_REPLAN,
        remaining_operations=(Operation(operation_id="op2", intent=Intent.CONVERT_FORMAT),),
    )
    assert outcome.remaining_operations[0].operation_id == "op2"

    assert ConfirmedPlan(plan=Task_Plan(plan_id="p", source_turn_id="t")).plan.plan_id == "p"
    assert PlanningExit(status=PlanStatus.REJECTED).status is PlanStatus.REJECTED
