"""有界规划循环（Task 17）：run_planning_loop。

把「理解 → 校验 → 澄清 → 未映射处置 → 确认」串成一个**有界循环**（≤max_plan_revisions，
默认 3）。补充说明 / 用户修改 / needs_replan 都统一走 `PlanRevisionRequest` **回到循环
顶重建计划**（build_revision），绝不原地改已确认计划、绝不丢原请求（design D2/#2）。

出口：`ConfirmedPlan`（用户确认、可执行）或 `PlanningExit`（rejected /
revision_limit_exceeded / cancelled / needs_input）。执行本身不在此——本循环只产出
「可执行的确认计划」，执行与 needs_replan 回灌由 TurnService/PlanExecutor（Task 20+）驱动。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass, replace
from enum import Enum

from paper_agent.providers.llm.base import LLMProvider
from paper_agent.tasking.budget import BudgetExceeded, PlanningBudget
from paper_agent.tasking.clarifier import (
    PlanningInteractor,
    clarify,
    confirm_plan,
    resolve_unmapped,
    summarize_plan,
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
    SlotSource,
)
from paper_agent.tasking.extractors import build_extractor
from paper_agent.tasking.operation_analyzer import ATTRIBUTION_FALLBACK_ALL
from paper_agent.tasking.operation_analyzer import analyze as default_analyze
from paper_agent.tasking.operation_analyzer import requirements_for
from paper_agent.tasking.plan_builder import build_plan, build_revision
from paper_agent.tasking.plan_validator import (
    SECTION_EXISTS,
    SECTION_NOT_FOUND,
    validate_plan,
)
from paper_agent.tasking.spans import locate_requirement

try:  # 观测事件（可选，None-safe）
    from paper_agent.observability.events import EventKind
except Exception:  # noqa: BLE001
    EventKind = None  # type: ignore


#: 可由「带确定性提示重规划」自行修复的校验问题。
#:
#: **判据（两条都要满足）**：
#: 1. 校验器已经知道正确做法 —— `section_exists` 的消息原文就是
#:    「章节「引言」已存在，应改用 rewrite」。知道答案却把纠错推给用户，
#:    与 backlog B-2 里 fix_bibliography 是同一性质的责任转移（那个意图已按
#:    此理由删除：系统自己写的引用，该在 commit 时自动对齐，不等用户来说）。
#: 2. **修复只改变「怎么做」，不改变「做什么」** —— 这一条比第 1 条更关键。
#:
#: `convert_not_terminal` 满足第 1 条但**不满足第 2 条**，故刻意排除：
#: 它的修复方式只有「挪到最后」或「本轮不做转换」，前者改了操作顺序
#: （「转 docx 再润色」≠「润色再转 docx」，顺序有语义），后者直接丢掉一条用户要求。
#: 这种取舍必须由用户做，系统替他决定就是静默改需求。
#: （`tests/test_planning_clarifier_loop.py::test_loop_rejects_convert_not_terminal`
#: 是这条决定的锚点，它在本功能开发中正确地拦住了一次误放宽。）
#:
#: 其余不在此列的：`format_lacks_sections`（改不了文件格式）、
#: `unsupported_intent`（能力不存在）、`conflicting_constraints`（需用户裁决）、
#: `convert_same_format`、`format_requires_docx`。重试只会烧调用。
_REPAIRABLE_ISSUES = frozenset({
    SECTION_EXISTS,         # add_section 打到已有章节 → 应改用 rewrite
    SECTION_NOT_FOUND,      # 章节指代落空 → 给出现有章节清单让它重选
})

#: 自修复次数上限。给了明确指令还改不对，再试也不会对，只是多花钱。
_MAX_VALIDATION_REPAIRS = 1

#: 修订文本确实来自用户的原因码。只有这两类 delta 可进入 provenance 允许集合；
#: VALIDATION_REPAIR / NEEDS_REPLAN 的 revision_text 是系统提示，必须排除。
_USER_AUTHORED_REVISION_REASONS = frozenset({
    RevisionReason.SUPPLEMENT,
    RevisionReason.USER_MODIFY,
})


def _revision_analysis_text(
    original_request: str,
    revision: PlanRevisionRequest,
) -> str:
    """Build the complete input used to re-analyze a plan revision.

    A supplement such as ``"只做前一步"`` is not a standalone request. Feeding
    only that sentence to OperationAnalyzer drops the already-understood
    operations and can turn the revision into an empty plan. Every non-initial
    pass therefore carries three pieces of context: the original request, the
    previous plan summary, and the delta that caused this revision.

    Keep ``PlanRevisionRequest.revision_text`` as the raw delta. Besides making
    the contract truthful, this prevents nested revisions from repeatedly
    wrapping an already-composed prompt.
    """
    if revision.revision_reason is RevisionReason.INITIAL:
        return revision.revision_text

    previous = revision.previous_plan
    previous_summary = summarize_plan(previous) if previous is not None else "（无上一版计划）"
    labels = {
        RevisionReason.SUPPLEMENT: "用户补充说明",
        RevisionReason.USER_MODIFY: "用户修改意见",
        RevisionReason.VALIDATION_REPAIR: "静态校验修复信息",
        RevisionReason.NEEDS_REPLAN: "执行期重规划信息",
    }
    label = labels.get(revision.revision_reason, "本次修订信息")
    return "\n".join((
        "这是同一条用户请求的计划修订，请输出修订后的完整操作列表。",
        f"原始请求：{original_request}",
        "上一版计划摘要：",
        previous_summary,
        f"{label}：",
        revision.revision_text,
        "未被本次修订明确删除或替换的操作与约束应继续保留。",
    ))


def _slot_value_evidence(value) -> tuple[str, ...]:
    """Flatten a user-sourced slot value into label-free provenance fragments."""
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Enum):
        return _slot_value_evidence(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _slot_value_evidence(asdict(value))
    if isinstance(value, Mapping):
        return tuple(
            fragment
            for nested in value.values()
            for fragment in _slot_value_evidence(nested)
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(
            fragment
            for nested in value
            for fragment in _slot_value_evidence(nested)
        )
    text = str(value).strip()
    return (text,) if text else ()


def _revision_user_evidence(
    original_request: str,
    revision: PlanRevisionRequest,
) -> tuple[str, ...]:
    """Return only user-derived text allowed to survive revision analysis.

    ``summarize_plan`` is intentionally not an evidence source: strings such as
    ``计划包含`` and ``目标=`` are UI labels authored by the system.  Prior-plan
    evidence instead comes from requirement verbatims and slots explicitly
    supplied or confirmed by the user.  User revision text is admitted only for
    SUPPLEMENT / USER_MODIFY; repair and replan deltas are system-authored.
    """
    fragments: list[str] = []

    def add(value: str | None) -> None:
        text = str(value or "").strip()
        if text and text not in fragments:
            fragments.append(text)

    add(original_request)
    previous = revision.previous_plan
    if previous is not None:
        for disposition in previous.dispositions:
            add(disposition.verbatim)
        for operation in previous.operations:
            for slot in operation.slots.values():
                if slot.source not in (
                    SlotSource.USER_EXPLICIT,
                    SlotSource.USER_CONFIRMED,
                ):
                    continue
                add(slot.verbatim)
                for value_fragment in _slot_value_evidence(slot.value):
                    add(value_fragment)
    if revision.revision_reason in _USER_AUTHORED_REVISION_REASONS:
        add(revision.revision_text)
    return tuple(fragments)


def _section_menu(current_sections: Sequence[str], limit: int = 30) -> str:
    """现有章节清单（保序去重）。用于 section_not_found 的修复提示。"""
    seen: set[str] = set()
    out: list[str] = []
    for s in current_sections:
        key = str(s).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return "、".join(out[:limit])


def _repair_hint(validation, current_sections: Sequence[str]) -> str:
    """把校验拒绝翻成给理解层的修订指令；不可修复返回 ""。

    只使用校验器**已有的确定性信息**（问题码 + 现有章节清单），不调 LLM、不猜意图。

    关于「operations 是消息的纯函数」这条刻意维持的性质（见 plan_validator docstring
    与 backlog B-3）：**初次** analyze 仍然只看消息，不看文档状态。这里注入章节清单
    发生在**修订**路径上，而 `revision_text` 本来就承载任意文本（用户补充说明、
    执行期重规划原因）。初次理解的确定性未被削弱。
    """
    codes = {i.code for i in validation.issues}
    # 混有不可修复问题 → 整体拒绝。半修复会让用户看到一份「改了一半」的计划。
    if not codes or not codes <= _REPAIRABLE_ISSUES:
        return ""

    lines = ["上一版计划未通过静态校验。请据以下事实重新规划，保持用户原意不变："]
    lines += [f"- {i.message}" for i in validation.issues]
    if SECTION_EXISTS in codes:
        lines.append(
            "完善 / 改进 / 扩写 / 补充一个**已存在**的章节，应使用 rewrite"
            "（section_id 填该章节），不要用 add_section。"
        )
    if SECTION_NOT_FOUND in codes:
        menu = _section_menu(current_sections)
        lines.append(
            ("文档现有章节：" + menu) if menu else "文档当前没有任何章节。"
        )
        lines.append("请从上述章节中选择，或改用 add_section 新建。")
    return "\n".join(lines)


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


def _is_semantically_empty(ar: AnalyzeResult) -> bool:
    """A successful envelope that contains no interpretation of the input."""
    return (
        ar.call_status is CallStatus.SUCCESS
        and not ar.operations
        and not ar.unmapped_text
        and not ar.project_facts
    )


def _keep_user_authored_parts(
    ar: AnalyzeResult,
    allowed_user_evidence: Sequence[str],
    original_request: str,
) -> AnalyzeResult:
    """Drop system-scaffold noise without dropping the user's own constraints.

    Every composite revision prompt contains system-authored labels.  Analyzer is
    required to return verbatim spans, so exact presence inside one provenance
    fragment is the boundary.  Fragments come only from the original request,
    prior user requirements/slots, and (for user revisions) the raw delta.

    Re-locate retained requirements against the real user request as well.  Their
    offsets were originally calculated against the composite replan prompt and
    would otherwise point at the wrong source text in the rebuilt plan.
    """
    if ar.call_status is not CallStatus.SUCCESS:
        return ar

    def is_user_evidence(value: str) -> bool:
        return bool(value) and any(value in source for source in allowed_user_evidence)

    kept_requirements = tuple(
        (
            locate_requirement(original_request, requirement)
            if requirement.verbatim in original_request
            else replace(
                requirement,
                source_start=-1,
                source_end=-1,
                ambiguous_span=True,
            )
        )
        for requirement in ar.requirements
        if is_user_evidence(requirement.verbatim)
    )
    kept_requirement_ids = {
        requirement.requirement_id for requirement in kept_requirements
    }
    kept_unmapped = tuple(
        item for item in ar.unmapped_text
        if is_user_evidence(item)
    )
    kept_project_facts = tuple(
        (
            key,
            value,
            tuple(rid for rid in requirement_ids if rid in kept_requirement_ids),
        )
        for key, value, requirement_ids in ar.project_facts
        if is_user_evidence(value)
    )
    return replace(
        ar,
        requirements=kept_requirements,
        unmapped_text=kept_unmapped,
        project_facts=kept_project_facts,
    )


def _understand(
    text: str, llm: LLMProvider, analyze_fn, bus=None,
    budget: PlanningBudget | None = None,
    revision_user_evidence: Sequence[str] | None = None,
    revision_original_request: str | None = None,
) -> tuple[AnalyzeResult, list]:
    """analyze → 各操作 pre_extract；返回 (AnalyzeResult, extractions)。

    在调用边界发 `OPERATION_ANALYSIS` / `PRE_EXTRACT`（纯函数本身不依赖 bus）——
    否则 Planning trace 看不到理解阶段，解析失败时无从下手（#10）。
    """
    semantic_empty_retries = 0
    if budget is not None:
        with budget.measured("analyzer"):
            ar = analyze_fn(text, llm)
    else:
        ar = analyze_fn(text, llm)
    if revision_user_evidence is not None:
        ar = _keep_user_authored_parts(
            ar,
            revision_user_evidence,
            revision_original_request or "",
        )

    # Production Planning always carries a budget.  A structurally valid but
    # semantically empty answer gives us no positive evidence that the user had
    # a read-only request; retry it once inside the same Turn budget.  Two such
    # answers are an analyzer protocol failure, not an AskLoop routing signal.
    if budget is not None and _is_semantically_empty(ar):
        semantic_empty_retries = 1
        with budget.measured("analyzer_semantic_empty_retry"):
            ar = analyze_fn(text, llm)
        if revision_user_evidence is not None:
            ar = _keep_user_authored_parts(
                ar,
                revision_user_evidence,
                revision_original_request or "",
            )
        if _is_semantically_empty(ar):
            ar = AnalyzeResult(
                call_status=CallStatus.SCHEMA_FAILED,
                error_category="SemanticEmpty",
            )
    _publish(bus, "OPERATION_ANALYSIS",
             call_status=ar.call_status.value,
             n_operations=len(ar.operations),
             n_requirements=len(ar.requirements),
             n_unmapped=len(ar.unmapped_text),
             error_category=ar.error_category,
             semantic_empty_retries=semantic_empty_retries)
    extractions = []
    if ar.call_status is CallStatus.SUCCESS:
        # 操作数上限：在开始抽槽（每个操作一次 LLM 调用）之前就拦住
        if budget is not None:
            budget.check_operations(len(ar.operations))
        n_ops = len(ar.operations)
        for op in ar.operations:
            ex = build_extractor(op.intent, llm)
            if ex is None:
                continue
            # **路由**：只把归属于本操作的要求交给它的抽取器。
            # 此前这里传的是 `ar.requirements`（**全部**要求）——既让抽取器看不到
            # 被 verbatim 裁掉的修饰语（真机缺陷），又让多操作时每个抽取器都看见
            # 别人的要求（交叉污染，被前一个问题掩盖了）。记账规则不变，见
            # `operation_analyzer.requirements_for` 的说明。
            owned, attribution = requirements_for(op, ar.requirements, n_ops)
            if attribution == ATTRIBUTION_FALLBACK_ALL:
                # analyzer 未声明归属且计划有多个操作 → 退回现状（不倒退），
                # 但必须留痕：该字段的填充质量从未被度量过。
                _publish(bus, "PRE_EXTRACT", intent=op.intent.value,
                         call_status="attribution_fallback",
                         attribution=attribution, n_operations=n_ops)
            if budget is not None:
                with budget.measured("extractor"):
                    er = ex.pre_extract(op, owned)
            else:
                er = ex.pre_extract(op, owned)
            extractions.append(er)
            _publish(bus, "PRE_EXTRACT",
                     intent=op.intent.value,
                     call_status=er.call_status.value,
                     n_slots=len(er.slots),
                     n_consumed=len(er.consumed_requirement_ids),
                     attribution=attribution,
                     n_owned_requirements=len(owned),
                     error_category=er.error_category)
    return ar, extractions


def _persist_project_facts(project_facts, facts, bus) -> None:
    """把项目级陈述（「课题是 X」）写入 `facts.claims`，跨操作与跨会话复用。

    X-11：这类要求此前**无处可去** —— REWRITE_SPEC 的七个槽都是「本次操作要做什么」，
    没有承载「这篇论文在研究什么」的位置，于是被记 UNMAPPED 弹窗退回。放槽里也不对：
    课题是项目属性而非操作属性，做成槽意味着每次会话重打一遍、且两个操作可以填出
    互相矛盾的值。`facts.json` 按源文件路径派生，claims 天然跨会话。

    `facts` 为 None（部分测试/无会话路径）时静默跳过：项目记忆是增强项，
    缺它退回既有行为，不应让规划失败。
    """
    if facts is None or not project_facts:
        return
    recorded = False
    for key, value, _rids in project_facts:
        try:
            facts.set_claim(key, value)
        except Exception:  # noqa: BLE001 - 项目记忆写入失败不应阻断规划
            continue
        recorded = True
        # 只发 key 与长度，不发原文 —— 与 CLARIFICATION_QA 一致（trace 不落用户正文）。
        _publish(bus, "PROJECT_FACT_RECORDED", key=key, value_len=len(str(value)))
    # 立即落盘：`facts.save()` 现仅由 rewrite/add_section 在**写作成功后**调用，
    # 若计划被驳回或无可执行操作，用户交代的课题就白说了。项目记忆与写作成败无关。
    if recorded:
        try:
            facts.save()
        except Exception:  # noqa: BLE001
            pass


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
    facts=None,
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

    original_request = replan.original_request if replan is not None else text
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

    repairs_used = 0

    for _ in range(max_plan_revisions):
        analysis_text = _revision_analysis_text(original_request, revision)
        revision_user_evidence = (
            _revision_user_evidence(original_request, revision)
            if revision.revision_reason is not RevisionReason.INITIAL
            else None
        )
        try:
            ar, extractions = _understand(
                analysis_text,
                llm,
                analyze_fn,
                bus,
                budget,
                revision_user_evidence=revision_user_evidence,
                revision_original_request=original_request,
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

        # X-11：项目级事实的 requirement 无槽位可落，需显式告知 PlanBuilder，
        # 否则会被记 UNMAPPED 并弹窗（真机上「课题是…」就是这样被丢的）。
        pf_rids = tuple(
            rid for _k, _v, rids in ar.project_facts for rid in rids
        )
        _persist_project_facts(ar.project_facts, facts, bus)
        if previous_plan is None:
            plan = build_plan(turn_id, ar.operations, extractions, ar.requirements,
                              unmapped_text=ar.unmapped_text,
                              project_fact_requirement_ids=pf_rids,
                              source_text=text)
        else:
            remaining_ids = (
                tuple(op.operation_id for op in replan.remaining_operations)
                if replan is not None else None
            )
            plan = build_revision(previous_plan, revision, ar.operations, extractions,
                                  ar.requirements, unmapped_text=ar.unmapped_text,
                                  remaining_operation_ids=remaining_ids,
                                  project_fact_requirement_ids=pf_rids,
                                  source_text=text)

        # P0：没有任何可执行操作 → 如实退出，绝不进入执行（否则会被报成 done）。
        # 典型场景：请求整体落在五意图之外（查重/翻译），或全部要求被忽略。
        if not plan.operations:
            # 只允许「首轮 Analyzer 成功、确实为零写操作，且明确给出 unmapped
            # 作为语义证据」交给 AskLoop。全空包不是只读意图的正面证据。
            # 修订 / 自修复 / needs_replan 的空结果表示重建失败或用户删空了计划，
            # 绝不等价于原始消息是只读问题，否则同一写作请求会在修订途中走错系统。
            initial_zero_supported_operations = (
                revision.revision_reason is RevisionReason.INITIAL
                and revision.previous_plan is None
                and not ar.operations
                and bool(ar.unmapped_text)
            )
            return PlanningExit(
                status=PlanStatus.NO_ACTION,
                message=_no_action_message(plan),
                route_to_readonly=initial_zero_supported_operations,
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
            # 先试自修复：问题码已经蕴含正确做法时，回灌提示重规划一次，
            # 而不是把「你应该改用 rewrite」这句话丢给用户让他自己改措辞。
            hint = _repair_hint(validation, current_sections)
            if hint and repairs_used < _MAX_VALIDATION_REPAIRS:
                repairs_used += 1
                _publish(bus, "PLAN_VALIDATION", phase="self_repair",
                         result="repairing",
                         issue_codes=[i.code for i in validation.issues],
                         n_issues=len(validation.issues))
                previous_plan = plan
                revision = PlanRevisionRequest(
                    plan, hint, RevisionReason.VALIDATION_REPAIR
                )
                continue
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
