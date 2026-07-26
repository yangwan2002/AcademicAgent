"""有界规划循环（Task 17）：run_planning_loop。

把「理解 → 校验 → 澄清 → 未映射处置 → 确认」串成一个**有界循环**（≤max_plan_revisions，
默认 3）。补充说明 / 用户修改 / needs_replan 都统一走 `PlanRevisionRequest` **回到循环
顶重建计划**（build_revision），绝不原地改已确认计划、绝不丢原请求（design D2/#2）。

出口：`ConfirmedPlan`（用户确认、可执行）或 `PlanningExit`（rejected /
revision_limit_exceeded / cancelled / needs_input）。执行本身不在此——本循环只产出
「可执行的确认计划」，执行与 needs_replan 回灌由 TurnService/PlanExecutor（Task 20+）驱动。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from paper_agent.providers.llm.base import LLMProvider
from paper_agent.tasking.clarifier import (
    PlanningInteractor,
    clarify,
    confirm_plan,
    resolve_unmapped,
)
from paper_agent.tasking.contracts import (
    AnalyzeResult,
    CallStatus,
    ConfirmedPlan,
    PlanningExit,
    PlanRevisionRequest,
    PlanStatus,
    RevisionReason,
    Task_Plan,
)
from paper_agent.tasking.extractors import build_extractor
from paper_agent.tasking.operation_analyzer import analyze as default_analyze
from paper_agent.tasking.plan_builder import build_plan, build_revision
from paper_agent.tasking.plan_validator import validate_plan


def _understand(text: str, llm: LLMProvider, analyze_fn) -> tuple[AnalyzeResult, list]:
    """analyze → 各操作 pre_extract；返回 (AnalyzeResult, extractions)。"""
    ar = analyze_fn(text, llm)
    extractions = []
    if ar.call_status is CallStatus.SUCCESS:
        for op in ar.operations:
            ex = build_extractor(op.intent, llm)
            if ex is None:
                continue
            extractions.append(ex.pre_extract(op, ar.requirements))
    return ar, extractions


def run_planning_loop(
    text: str,
    *,
    llm: LLMProvider,
    interactor: PlanningInteractor,
    current_sections: Sequence[str] = (),
    current_format: str = "",
    turn_id: str = "turn",
    max_plan_revisions: int = 3,
    require_confirmation: bool = True,
    analyze_fn=default_analyze,
    bus=None,
) -> ConfirmedPlan | PlanningExit:
    """有界规划循环；返回 ConfirmedPlan 或 PlanningExit。"""
    revision = PlanRevisionRequest(previous_plan=None, revision_text=text,
                                   revision_reason=RevisionReason.INITIAL)
    previous_plan: Task_Plan | None = None

    for _ in range(max_plan_revisions):
        ar, extractions = _understand(revision.revision_text, llm, analyze_fn)
        if ar.call_status is not CallStatus.SUCCESS:
            # 理解失败：本循环聚焦编排，provider/解析失败 → 不写、要求澄清（D3）。
            return PlanningExit(status=PlanStatus.NEEDS_INPUT,
                                message=f"理解失败（{ar.call_status.value}）")

        if previous_plan is None:
            plan = build_plan(turn_id, ar.operations, extractions, ar.requirements,
                              unmapped_text=ar.unmapped_text)
        else:
            plan = build_revision(previous_plan, revision, ar.operations, extractions,
                                  ar.requirements, unmapped_text=ar.unmapped_text)

        # 静态校验（硬问题直接拒绝）
        validation = validate_plan(plan, current_sections=current_sections,
                                   current_format=current_format)
        if validation.result == "rejected":
            return PlanningExit(status=PlanStatus.REJECTED,
                                message=_issue_summary(validation))

        # 澄清缺口（≤2 轮）
        clar = clarify(plan, ask=interactor.ask_field, max_rounds=2, bus=bus)
        plan = clar.plan
        if not clar.resolved:
            return PlanningExit(status=PlanStatus.NEEDS_INPUT, message="澄清未完成，转草稿")

        # 回填后重跑静态校验（如新给的章节不存在）
        validation = validate_plan(plan, current_sections=current_sections,
                                   current_format=current_format)
        if validation.result == "rejected":
            return PlanningExit(status=PlanStatus.REJECTED,
                                message=_issue_summary(validation))

        # 未映射处置
        if plan.unmapped_requirements:
            umo = resolve_unmapped(plan, interactor, bus=bus)
            if umo.action == "cancel":
                return PlanningExit(status=PlanStatus.CANCELLED, message="用户取消")
            if umo.action == "supplement":
                previous_plan = plan
                revision = PlanRevisionRequest(plan, umo.supplement_text,
                                               RevisionReason.SUPPLEMENT)
                continue
            plan = umo.plan if umo.plan is not None else replace(plan, unmapped_requirements=())

        # 执行前确认
        decision = confirm_plan(plan, interactor,
                                require_confirmation=require_confirmation, bus=bus)
        if decision.kind == "reject":
            return PlanningExit(status=PlanStatus.REJECTED, message="用户否决")
        if decision.kind == "modify":
            previous_plan = plan
            revision = PlanRevisionRequest(plan, decision.text, RevisionReason.USER_MODIFY)
            continue

        return ConfirmedPlan(plan=replace(plan, status=PlanStatus.CONFIRMED))

    return PlanningExit(status=PlanStatus.REVISION_LIMIT_EXCEEDED,
                        message="超过最大修订次数")


def _issue_summary(validation) -> str:
    return "；".join(f"{i.code}:{i.message}" for i in validation.issues) or "计划不可执行"


__all__ = ["run_planning_loop"]
