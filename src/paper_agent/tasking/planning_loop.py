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
from paper_agent.tasking.budget import BudgetExceeded, PlanningBudget
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
    ExecutionReplanContext,
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

try:  # 观测事件（可选，None-safe）
    from paper_agent.observability.events import EventKind
except Exception:  # noqa: BLE001
    EventKind = None  # type: ignore


def _publish(bus, kind_name: str, **payload) -> None:
    """发业务事件（None-safe）；观测失败绝不拖垮规划。"""
    if bus is None or EventKind is None:
        return
    kind = getattr(EventKind, kind_name, None)
    if kind is None:
        return
    try:
        bus.publish(kind, **payload)
    except Exception:  # noqa: BLE001
        pass


def _understand(
    text: str, llm: LLMProvider, analyze_fn, bus=None,
    budget: PlanningBudget | None = None,
) -> tuple[AnalyzeResult, list]:
    """analyze → 各操作 pre_extract；返回 (AnalyzeResult, extractions)。

    在调用边界发 `OPERATION_ANALYSIS` / `PRE_EXTRACT`（纯函数本身不依赖 bus）——
    否则 active 的 trace 看不到理解阶段，解析失败时无从下手（#10）。
    """
    if budget is not None:
        with budget.measured("analyzer"):
            ar = analyze_fn(text, llm)
    else:
        ar = analyze_fn(text, llm)
    _publish(bus, "OPERATION_ANALYSIS",
             call_status=ar.call_status.value,
             n_operations=len(ar.operations),
             n_requirements=len(ar.requirements),
             n_unmapped=len(ar.unmapped_text),
             error_category=ar.error_category)
    extractions = []
    if ar.call_status is CallStatus.SUCCESS:
        # 操作数上限：在开始抽槽（每个操作一次 LLM 调用）之前就拦住
        if budget is not None:
            budget.check_operations(len(ar.operations))
        for op in ar.operations:
            ex = build_extractor(op.intent, llm)
            if ex is None:
                continue
            if budget is not None:
                with budget.measured("extractor"):
                    er = ex.pre_extract(op, ar.requirements)
            else:
                er = ex.pre_extract(op, ar.requirements)
            extractions.append(er)
            _publish(bus, "PRE_EXTRACT",
                     intent=op.intent.value,
                     call_status=er.call_status.value,
                     n_slots=len(er.slots),
                     n_consumed=len(er.consumed_requirement_ids),
                     error_category=er.error_category)
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
    replan: ExecutionReplanContext | None = None,
    budget: PlanningBudget | None = None,
    bus=None,
) -> ConfirmedPlan | PlanningExit:
    """有界规划循环；返回 ConfirmedPlan 或 PlanningExit。

    `replan`（可选）：执行期失效后的重规划上下文。给出时**以原始请求为基础**重建，
    且只针对未执行步骤——绝不重跑已成功的操作（Property 11）。
    """
    # 输入规模上限：超长消息直接拒，不浪费 provider 调用（#9）
    if budget is not None:
        try:
            budget.check_input(text)
        except BudgetExceeded as exc:
            return PlanningExit(status=PlanStatus.REJECTED, message=str(exc))

    if replan is not None:
        # 重规划：原始请求 + 失效原因一并交给理解层，previous_plan 用于版本血缘
        revision = PlanRevisionRequest(
            previous_plan=replan.previous_plan,
            revision_text=_replan_text(replan),
            revision_reason=RevisionReason.NEEDS_REPLAN,
        )
        previous_plan = replan.previous_plan
    else:
        revision = PlanRevisionRequest(previous_plan=None, revision_text=text,
                                       revision_reason=RevisionReason.INITIAL)
        previous_plan = None

    for _ in range(max_plan_revisions):
        try:
            ar, extractions = _understand(
                revision.revision_text, llm, analyze_fn, bus, budget
            )
        except BudgetExceeded as exc:
            # 受控终止：不写任何文件，如实告知原因（#9）
            _publish(bus, "PLAN_VALIDATION", phase="budget", result="rejected",
                     issue_codes=[exc.kind])
            return PlanningExit(status=PlanStatus.REJECTED, message=str(exc))
        if ar.call_status is not CallStatus.SUCCESS:
            # 理解失败：本循环聚焦编排，provider/解析失败 → 不写、要求澄清（D3）。
            return PlanningExit(status=PlanStatus.NEEDS_INPUT,
                                message=f"理解失败（{ar.call_status.value}）")

        if previous_plan is None:
            plan = build_plan(turn_id, ar.operations, extractions, ar.requirements,
                              unmapped_text=ar.unmapped_text)
        else:
            remaining_ids = (
                tuple(op.operation_id for op in replan.remaining_operations)
                if replan is not None else None
            )
            plan = build_revision(previous_plan, revision, ar.operations, extractions,
                                  ar.requirements, unmapped_text=ar.unmapped_text,
                                  remaining_operation_ids=remaining_ids)

        # P0：没有任何可执行操作 → 如实退出，绝不进入执行（否则会被报成 done）。
        # 典型场景：请求整体落在五意图之外（查重/翻译），或全部要求被忽略。
        if not plan.operations:
            return PlanningExit(
                status=PlanStatus.NO_ACTION,
                message=_no_action_message(plan),
            )

        # 静态校验（硬问题直接拒绝）
        validation = validate_plan(plan, current_sections=current_sections,
                                   current_format=current_format)
        _publish(bus, "PLAN_VALIDATION", phase="static",
                 result=validation.result,
                 n_issues=len(validation.issues),
                 issue_codes=[i.code for i in validation.issues],
                 n_missing_fields=len(validation.missing_fields))
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
        _publish(bus, "PLAN_VALIDATION", phase="post_clarify",
                 result=validation.result,
                 n_issues=len(validation.issues),
                 issue_codes=[i.code for i in validation.issues])
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


def _replan_text(replan: ExecutionReplanContext) -> str:
    """重规划的理解层输入：原始请求 + 已完成步骤 + 失效原因（不丢原意图）。"""
    parts = [f"原始请求：{replan.original_request}"]
    if replan.completed_operation_ids:
        parts.append(
            f"已成功完成 {len(replan.completed_operation_ids)} 个步骤，"
            "这些不要重做。"
        )
    if replan.remaining_operations:
        remaining = "、".join(
            op.intent.value for op in replan.remaining_operations
        )
        parts.append(f"仍需完成：{remaining}。")
    if replan.replan_reason:
        parts.append(f"上次执行中断的原因：{replan.replan_reason}")
    return "\n".join(parts)


def _no_action_message(plan) -> str:
    """空计划的诚实说明：告诉用户"没做任何事"以及为什么。"""
    if plan.unmapped_requirements:
        return (
            "这些要求不在我能执行的范围内（润色/改写/新增章节/格式转换/排版）："
            + "；".join(plan.unmapped_requirements)
        )
    return "没有识别出可执行的操作，未做任何改动。"


def _issue_summary(validation) -> str:
    return "；".join(f"{i.code}:{i.message}" for i in validation.issues) or "计划不可执行"


__all__ = ["run_planning_loop"]
