"""IntentHandlers（Task 19）：把 Operation 的槽位喂进现有 `execute_*`。

铁律（design D8 / Property 10）：
- **薄适配、不重写 SOP**：每个 Handler 只做「Slot → 现有 *Plan / execute_* 参数」的
  翻译，执行逻辑仍在 `intents/*.py`。
- **业务失败翻译**：现有 `execute_*` 用 `Result(ok=False)` 表达业务失败（含 Gate 拒），
  不抛异常。故 `ok=True → succeeded`、否则 `rejected`；**不看 run_status**（否则 Gate
  失败会被记成 run completed / 计划成功）。系统异常 → `failed`。
- **抽取的槽位必须真正产生执行效果**（防「提取了但没执行」）：
  - `polish.constraints` → `PolishPlan.constraints` → `run_polish(constraints=...)`
    → `build_polish_messages` 的硬约束块（真影响生成）。
  - `format_docx.operations` → `execute_format(ops=...)`（可选参数，直接落文档，
    不做「FormatOp → 中文 → 再解析」的有损往返）。
- 记录 `changed_working_copy` / `generated_outputs`，供报告与失败清理定位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from paper_agent.errors import OperationCancelled
from paper_agent.intents.add_section import execute_add_section
from paper_agent.intents.convert import execute_convert
from paper_agent.intents.format_docx import execute_format
from paper_agent.intents.interview import (
    AddSectionPlan,
    ConvertPlan,
    PolishPlan,
    RewritePlan,
)
from paper_agent.intents.polish import execute_polish
from paper_agent.intents.rewrite import execute_rewrite
from paper_agent.intents.router import Intent
from paper_agent.session.section_index import slugify
from paper_agent.tasking.contracts import (
    FieldStatus,
    Operation,
    OperationResult,
    OperationStatus,
)


@dataclass
class HandlerContext:
    """Handler 执行所需的外部依赖（由 PlanExecutor 装配）。"""

    session: Any
    llm: Any = None
    facts: Any = None
    retrieval_provider: Any = None
    verifier: Any = None
    # (value, ctx) -> bool。**默认保守拒绝**：未经用户确认的新数字视为编造，交由 Gate
    # 拦下（绝不因为「没人回答」就默认放行编造数字）。
    confirm_number: Any = None
    max_retry: int = 2
    max_char_change_ratio: float = 0.30
    stream: bool = False
    output_dir: str = "output"
    extra: dict = field(default_factory=dict)
    # --- A2d-5：引用配额写作接入（全部默认 None/False → 无配额路径逐字节不变）---
    writing_integration_enabled: bool = False
    literature_orchestrator: Any = None      # WritingLiteratureOrchestrator
    literature_port: Any = None              # LiteratureInteractionPort
    base_year: int | None = None
    classifier: Any = None
    language_policy: str = "high_only"
    # --- A2f：检索意图合成（默认关 → 走旧 initial_query 路径，零影响）---
    search_synthesis_enabled: bool = False
    search_synthesizer: Any = None           # SearchIntentSynthesizer
    max_search_intent_revisions: int = 2
    retrieval_limits: Any = None             # RetrievalLimits（共享预算用）


class IntentHandler(Protocol):
    intent: Intent

    def execute(self, op: Operation, ctx: HandlerContext, bus=None, cancel_token=None) -> OperationResult:
        ...


# --- 槽位读取助手 -----------------------------------------------------------

def _val(op: Operation, name: str, default=None):
    """取槽位值：只认 present（其余状态无可信值，见 base._VALUED_STATUSES 语义）。"""
    sv = op.slots.get(name)
    if sv is None or sv.field_status is not FieldStatus.PRESENT:
        return default
    return default if sv.value is None else sv.value


def _list(op: Operation, name: str) -> list:
    v = _val(op, name)
    if isinstance(v, (list, tuple)):
        return list(v)
    return []


def _ok_result(op: Operation, res, *, changed: bool, outputs=()) -> OperationResult:
    """按 D8 翻译业务结果：ok → succeeded，否则 rejected（含 Gate 拒）。"""
    ok = bool(getattr(res, "ok", False))
    return OperationResult(
        operation_id=op.operation_id,
        status=OperationStatus.SUCCEEDED if ok else OperationStatus.REJECTED,
        changed_working_copy=changed and ok,
        generated_outputs=tuple(outputs) if ok else (),
        message=str(getattr(res, "message", "")),
    )


def _failed(op: Operation, exc: Exception) -> OperationResult:
    return OperationResult(
        operation_id=op.operation_id,
        status=OperationStatus.FAILED,
        message=f"{type(exc).__name__}: {exc}",
    )


def _reject_unconfirmed(_value: str, _ctx: str) -> bool:
    """未配置数字确认回调时的保守默认：视为编造（Gate 会拦），不放行。"""
    return False


def _confirm_of(ctx: HandlerContext):
    return ctx.confirm_number if callable(ctx.confirm_number) else _reject_unconfirmed


# --- A2d-5：引用配额准备（materialize → Preflight → CitationContext）-----------

@dataclass
class _QuotaPrep:
    """配额准备结果：`context` 非 None 才进写作；`rejection` 非空则明确拒绝。"""

    context: Any = None
    rejection: str = ""


def _prepare_citation_quota(op: Operation, ctx: HandlerContext, *, criteria_topic: str,
                            initial_query: str) -> _QuotaPrep:
    """从槽读 raw quota → materialize → Preflight。无配额返回空 prep（走旧路径）。"""
    raw = _val(op, "citation_quota")
    if raw is None:
        return _QuotaPrep()                      # 用户没提配额 → 旧路径，零影响

    if not ctx.writing_integration_enabled:
        # flag 关但用户明确给了配额 → 明确不支持，**不降级成忽略配额继续写**（design D8）
        return _QuotaPrep(rejection="引用配额功能未启用，无法按你的配额要求写作。")

    from paper_agent.literature.citation_context import CitationContext
    from paper_agent.literature.quota import RawCountClause, RawFilterClause, RawQuotaSpec
    from paper_agent.tasking.quota_materializer import materialize_citation_quota

    # 槽值形态：dict（LLM 抽取产物）或已构造的 RawQuotaSpec
    if isinstance(raw, RawQuotaSpec):
        raw_spec = raw
    elif isinstance(raw, dict):
        raw_spec = RawQuotaSpec(
            filters=tuple(RawFilterClause(**f) for f in raw.get("filters", []) if isinstance(f, dict)),
            constraints=tuple(RawCountClause(**c) for c in raw.get("constraints", [])
                              if isinstance(c, dict)),
            explicit_scope=str(raw.get("explicit_scope", "") or ""),
        )
    else:
        return _QuotaPrep(rejection="无法理解你给出的引用配额，请换一种说法。")

    from datetime import datetime, timezone
    base_year = ctx.base_year or datetime.now(timezone.utc).year
    mat = materialize_citation_quota(raw_spec, base_year=base_year)
    if mat.spec is None:
        detail = "；".join(f"{i.code}: {i.detail}" for i in mat.issues) or "配额无法执行"
        return _QuotaPrep(rejection=f"引用配额无法执行（{detail}），请澄清后重试。")

    orch = ctx.literature_orchestrator
    if orch is None:
        return _QuotaPrep(rejection="引用配额功能未就绪（缺少文献编排器）。")

    from paper_agent.application.literature_flow import (
        FlowState,
        WritingEvidenceRequest,
    )
    from paper_agent.literature.relevance import RelevanceCriteria

    # A2f：合成开启 → 从上下文推检索意图（多 seed），criteria 用语义主题；否则走旧路径。
    search_intent = None
    budget = None
    if ctx.search_synthesis_enabled and ctx.search_synthesizer is not None:
        prep = _synthesize_intent(op, ctx, criteria_topic, initial_query, raw_spec)
        if prep.rejection:
            return prep                         # NEEDS_INPUT/ERROR/CANCELLED → 明确拒绝，不硬搜
        search_intent = prep.context            # 复用 _QuotaPrep.context 临时装 intent
        from paper_agent.literature.retrieval.search_intent import to_relevance_criteria
        criteria = to_relevance_criteria(search_intent, query_intent=initial_query or criteria_topic)
        if ctx.retrieval_limits is not None:
            from paper_agent.literature.retrieval.budget import BudgetLedger
            budget = BudgetLedger(ctx.retrieval_limits)
    else:
        criteria = RelevanceCriteria(topic=criteria_topic, query_intent=initial_query)

    request = WritingEvidenceRequest(
        criteria=criteria, citation_quota=mat.spec,
        initial_query="" if search_intent is not None else (initial_query or criteria_topic),
        operation_id=op.operation_id, search_intent=search_intent,
        # F-3：显式声明章节身份。Handler 是**唯一可靠**知道「在写哪一章」的层——
        # 章节标题/section_id 来自计划槽位，而 criteria.section_purpose 实测是自由文本。
        section_kind=criteria_topic,
    )
    outcome = orch.run(request, facts=ctx.facts, port=ctx.literature_port, budget_ledger=budget)
    if outcome.state is not FlowState.READY_TO_WRITE:
        return _QuotaPrep(rejection=f"写作前文献预检未完成（{outcome.state.value}）：{outcome.message}")

    return _QuotaPrep(context=CitationContext(
        bundle=outcome.bundle, quota=mat.spec,
        effective_constraints=outcome.effective_constraints,
        deviations=outcome.deviations, port=ctx.literature_port,
        classifier=ctx.classifier, policy=ctx.language_policy,
        operation_id=op.operation_id, task_fingerprint=request.task_fingerprint,
    ))


#: 只有语言维度对 seed 定向有意义（`total` 不蕴含任何语言）。
_LANG_DIMENSIONS = ("language.zh", "language.en")


def _requested_language_dimensions(raw_spec) -> tuple[str, ...]:
    """从用户配额里取出**语言维度**，作为合成器的只读提示。

    此前 `SearchContext.requested_dimensions` **全仓库无人填写** —— 定义在
    design-a2f 与 dataclass 里，从未接线（与 `RawOperation.requirement_ids`
    同一类「字段存在但没人用」）。后果：用户要「中文 2 篇」，合成器不知道，
    产出的 seed 全是英文短语，检索源自然只回英文，中文配额填不满。

    只传语言维度、不传篇数：合成器**不得**看到可修改的 quota（design D2 的
    越权红线），它只需要知道「需要哪些语言的文献」。
    """
    out: list[str] = []
    for clause in getattr(raw_spec, "constraints", ()) or ():
        dim = str(getattr(clause, "dimension", "") or "")
        if dim in _LANG_DIMENSIONS and dim not in out:
            out.append(dim)
    return tuple(out)


def _paper_context(ctx: HandlerContext, target: str) -> dict:
    """从工作副本派生**论文上下文**（只读投影）。任何异常一律降级为空。

    这是「让合成器知道这篇论文在研究什么」的唯一来源。此前 `SearchContext`
    完全没有文档内容，真机上对「完善我的引言部分」这类请求，合成器收到的
    user 消息只有「写作操作：rewrite / 改写方向：完善引言部分」两行，
    只能产出通用模板。需要的信息一直在工作副本里。

    降级为空而非抛错：检索意图合成是**辅助**环节，读文档失败不应让整个写作请求崩掉；
    降级后回到「只有 operation 槽位」的旧行为，不比现状更差。
    """
    empty = {"paper_title": "", "paper_section_titles": (), "target_section_body": ""}
    try:
        from paper_agent.session.section_index import SECTIONABLE_EXTS, SectionIndex

        session = getattr(ctx, "session", None)
        if session is None or getattr(session, "ext", "") not in SECTIONABLE_EXTS:
            return empty
        index = SectionIndex(session.read_working(), session.ext)
        sections = index.list()
        if not sections:
            return empty

        # 论文题目：**唯一的**最高层级标题才算题目。多个同级顶层标题（如本项目
        # 的 md 骨架有四个 `##`）说明那是章节而非题目 —— 宁可留空，不猜。
        title = ""
        top_level = min(s.level for s in sections)
        tops = [s for s in sections if s.level == top_level]
        if len(tops) == 1:
            title = tops[0].title
        if not title:
            # X-11 兜底：骨架文档（多个同级顶层标题）恒推不出题目，此时用用户
            # 显式交代的课题（`claims.research_topic`）。这是检索合成能拿到
            # 「这篇论文在研究什么」的**唯一**可靠来源。
            claims = getattr(getattr(ctx, "facts", None), "claims", {}) or {}
            title = str(claims.get("research_topic", "") or "")

        body = ""
        if target:
            sec = index.find(target)
            if sec is not None:
                body = index.body_of(sec)
        return {
            "paper_title": title,
            "paper_section_titles": tuple(s.title for s in sections),
            "target_section_body": body,
        }
    except Exception:  # noqa: BLE001 - 辅助信息，绝不拖垮写作请求
        return empty


def _synthesize_intent(op: Operation, ctx: HandlerContext, criteria_topic: str,
                       initial_query: str, raw_spec=None) -> _QuotaPrep:
    """A2f：据**论文上下文** + operation + facts.claims 合成检索意图。

    READY → context 装 intent；否则 rejection。
    """
    from paper_agent.application.search_intent_preparation import prepare_search_intent
    from paper_agent.literature.retrieval.budget import BudgetLedger
    from paper_agent.literature.retrieval.search_intent import (
        SearchContext,
        SearchIntentPreparationStatus,
    )

    claims = getattr(ctx.facts, "claims", {}) or {}
    paper = _paper_context(ctx, criteria_topic)
    sctx = SearchContext(
        explicit_query=initial_query,
        operation_kind=op.intent.value if hasattr(op.intent, "value") else str(op.intent),
        target_title=criteria_topic,
        purpose=str(_val(op, "purpose", "") or ""),
        direction=str(_val(op, "direction", "") or ""),
        details=str(_val(op, "details", "") or ""),
        structure_terms=tuple(str(s) for s in _list(op, "subsections")),
        project_gap=str(claims.get("gap", "") or ""),
        project_novelty=str(claims.get("novelty", "") or ""),
        project_contributions=tuple(str(c) for c in (claims.get("contributions", []) or [])),
        requested_dimensions=_requested_language_dimensions(raw_spec),
        paper_title=paper["paper_title"],
        paper_section_titles=paper["paper_section_titles"],
        target_section_body=paper["target_section_body"],
    )
    budget = BudgetLedger(ctx.retrieval_limits) if ctx.retrieval_limits is not None else None
    prep = prepare_search_intent(
        sctx, synthesizer=ctx.search_synthesizer, interaction_port=ctx.literature_port,
        budget=budget, max_revisions=ctx.max_search_intent_revisions,
    )
    if prep.status is SearchIntentPreparationStatus.READY:
        return _QuotaPrep(context=prep.intent)
    return _QuotaPrep(rejection=f"未能确定检索主题（{prep.issue_code}）：请补充这一章要引用哪方面的文献。")


def _rejected(op: Operation, message: str) -> OperationResult:
    return OperationResult(
        operation_id=op.operation_id, status=OperationStatus.REJECTED, message=message
    )


def _guard(fn):
    """系统异常 → failed；取消一路上抛（由 PlanExecutor/Coordinator 处理）。"""
    def wrapper(self, op, ctx, bus=None, cancel_token=None):
        try:
            return fn(self, op, ctx, bus, cancel_token)
        except OperationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            return _failed(op, exc)
    return wrapper


# --- 五个 Handler -----------------------------------------------------------

class PolishHandler:
    intent = Intent.POLISH

    @_guard
    def execute(self, op, ctx, bus=None, cancel_token=None) -> OperationResult:
        plan = PolishPlan(
            section_id=str(_val(op, "section_id", "")),
            goal=str(_val(op, "goal", "") or ""),
            # 真执行绑定：constraints → ContextBuilder 硬约束块
            constraints=[str(c) for c in _list(op, "constraints")],
        )
        res = execute_polish(
            ctx.session, plan, ctx.llm,
            max_char_change_ratio=ctx.max_char_change_ratio,
            max_retry=ctx.max_retry, bus=bus, cancel_token=cancel_token,
            stream=ctx.stream,
        )
        return _ok_result(op, res, changed=True)


class RewriteHandler:
    intent = Intent.REWRITE

    @_guard
    def execute(self, op, ctx, bus=None, cancel_token=None) -> OperationResult:
        plan = RewritePlan(
            section_id=str(_val(op, "section_id", "")),
            direction=str(_val(op, "direction", "") or ""),
            details=str(_val(op, "details", "") or ""),
            want_search=bool(_val(op, "want_search", False)),
            search_query=str(_val(op, "search_query", "") or ""),
            search_limit=int(_val(op, "search_limit", 5) or 5),
        )
        prep = _prepare_citation_quota(
            op, ctx, criteria_topic=plan.section_id, initial_query=plan.search_query
        )
        if prep.rejection:
            return _rejected(op, prep.rejection)
        res = execute_rewrite(
            ctx.session, plan, ctx.facts, ctx.llm,
            retrieval_provider=ctx.retrieval_provider, verifier=ctx.verifier,
            confirm=_confirm_of(ctx), max_retry=ctx.max_retry, bus=bus,
            citation_context=prep.context,
        )
        return _ok_result(op, res, changed=True)


class AddSectionHandler:
    intent = Intent.ADD_SECTION

    @_guard
    def execute(self, op, ctx, bus=None, cancel_token=None) -> OperationResult:
        title = str(_val(op, "title", "") or "")
        plan = AddSectionPlan(
            title=title,
            purpose=str(_val(op, "purpose", "") or ""),
            position=str(_val(op, "position", "end") or "end"),
            section_type=slugify(title),
            structure_kind=str(_val(op, "structure_kind", "desc") or "desc"),
            structure_desc=str(_val(op, "structure_desc", "") or ""),
            subsections=[str(s) for s in _list(op, "subsections")],
            want_search=bool(_val(op, "want_search", False)),
            search_query=str(_val(op, "search_query", "") or ""),
            search_limit=int(_val(op, "search_limit", 5) or 5),
        )
        prep = _prepare_citation_quota(
            op, ctx, criteria_topic=plan.title, initial_query=plan.search_query
        )
        if prep.rejection:
            return _rejected(op, prep.rejection)
        res = execute_add_section(
            ctx.session, plan, ctx.facts, ctx.llm,
            retrieval_provider=ctx.retrieval_provider, verifier=ctx.verifier,
            confirm=_confirm_of(ctx), max_retry=ctx.max_retry, bus=bus,
            citation_context=prep.context,
        )
        return _ok_result(op, res, changed=True)


class ConvertHandler:
    intent = Intent.CONVERT_FORMAT

    @_guard
    def execute(self, op, ctx, bus=None, cancel_token=None) -> OperationResult:
        plan = ConvertPlan(
            target_ext=str(_val(op, "target_ext", "") or ""),
            presets=[str(p) for p in _list(op, "presets")],
        )
        res = execute_convert(ctx.session, plan, output_dir=ctx.output_dir)
        outputs = (res.output_path,) if getattr(res, "output_path", "") else ()
        # convert 只产新文件、不改 working copy（D7 回滚边界）
        return _ok_result(op, res, changed=False, outputs=outputs)


class FormatDocxHandler:
    intent = Intent.FORMAT_DOCX

    @_guard
    def execute(self, op, ctx, bus=None, cancel_token=None) -> OperationResult:
        # 真执行绑定：结构化 FormatOp 列表 → execute_format 的可选 ops 参数
        ops = _list(op, "operations")
        message = op.slots.get("operations")
        fallback_text = (message.verbatim if message is not None and message.verbatim else "") or ""
        res = execute_format(ctx.session, fallback_text, ops=ops or None)
        return _ok_result(op, res, changed=True)


_HANDLERS: dict[Intent, IntentHandler] = {
    Intent.POLISH: PolishHandler(),
    Intent.REWRITE: RewriteHandler(),
    Intent.ADD_SECTION: AddSectionHandler(),
    Intent.CONVERT_FORMAT: ConvertHandler(),
    Intent.FORMAT_DOCX: FormatDocxHandler(),
}


def handler_for(intent: Intent) -> IntentHandler | None:
    """按意图取 Handler；未注册返回 None（调用方标 rejected/unsupported）。"""
    return _HANDLERS.get(intent)


__all__ = [
    "HandlerContext", "IntentHandler", "handler_for",
    "PolishHandler", "RewriteHandler", "AddSectionHandler",
    "ConvertHandler", "FormatDocxHandler",
]
