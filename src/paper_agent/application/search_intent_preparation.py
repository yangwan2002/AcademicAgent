"""检索意图准备服务（A2f-2，design-a2f D4/D9）。

Handler 把 operation 字段 + 项目上下文交给本服务，消费 READY 结果;**不** import LLM 实现。
本服务负责:调合成器 → 判定是否需确认/澄清 → typed 交互 → 预算计费,产出
`SearchIntentPreparationOutcome`。

确认纪律（D4）:
- 显式 query 或**已确认 operation 字段的确定性投影** → 直接 READY,不重复确认;
- LLM 改写/合成的主题、或主要来自 PROJECT/MIXED claims → 必须经 `confirm_search_intent`
  展示后 accept/modify/cancel;
- 信号不足 → `request_search_topic` 澄清,受 `max_revisions` 限;
- 非交互(port=None)且需确认/澄清 → 返回 NEEDS_INPUT,**不默认接受**;
- 系统失败(合成 error 且无确定性信号)→ ERROR,与 NEEDS_INPUT 分离。

预算（D9）:合成的**每次真实 LLM 调用**扣一次 `charge_llm`;确定性合成不扣。计费与检索
Loop 共用同一 BudgetLedger。
"""

from __future__ import annotations

from dataclasses import replace

from paper_agent.literature.retrieval.search_intent import (
    SearchContext,
    SearchIntent,
    SearchIntentOrigin,
    SearchIntentPreparationOutcome,
    SearchIntentPreparationStatus,
)

_CONFIRM_ORIGINS = frozenset({SearchIntentOrigin.PROJECT_DERIVED, SearchIntentOrigin.MIXED})


def _ready(intent: SearchIntent) -> SearchIntentPreparationOutcome:
    return SearchIntentPreparationOutcome(
        status=SearchIntentPreparationStatus.READY, intent=intent)


def _fail(status: SearchIntentPreparationStatus, code: str) -> SearchIntentPreparationOutcome:
    return SearchIntentPreparationOutcome(status=status, issue_code=code)


def _needs_confirmation(result, context: SearchContext) -> bool:
    """是否需要在检索前让用户确认合成主题（D4）。"""
    intent = result.intent
    if intent is None:
        return False
    if intent.origin is SearchIntentOrigin.USER_EXPLICIT:
        return False                       # 用户原话,计划摘要已见
    if result.llm_called:
        return True                        # LLM 改写/合成主题 → 必确认
    return intent.origin in _CONFIRM_ORIGINS   # 主要来自 project claims → 确认


def prepare_search_intent(
    context: SearchContext,
    *,
    synthesizer,
    interaction_port=None,
    budget=None,
    max_revisions: int = 2,
) -> SearchIntentPreparationOutcome:
    """据上下文合成检索意图,必要时经 port 确认/澄清。返回准备结果（含状态）。"""
    revisions = 0
    ctx = context
    while True:
        result = synthesizer.synthesize(ctx)
        if result.llm_called and budget is not None:
            budget.charge_llm()            # 真实 LLM 调用才计费（确定性不进此分支）

        if result.intent is not None:
            if not _needs_confirmation(result, ctx):
                return _ready(result.intent)
            # 需确认
            if interaction_port is None:
                return _fail(SearchIntentPreparationStatus.NEEDS_INPUT,
                             "NEEDS_SEARCH_INTENT_CONFIRMATION")
            decision = interaction_port.confirm_search_intent(_intent_summary(result.intent))
            if decision.action == "accept":
                return _ready(result.intent)
            if decision.action == "cancel":
                return _fail(SearchIntentPreparationStatus.CANCELLED, "user_cancelled")
            if decision.action == "modify" and revisions < max_revisions:
                revisions += 1
                ctx = replace(ctx, explicit_query=decision.text or ctx.explicit_query)
                continue
            return _fail(SearchIntentPreparationStatus.NEEDS_INPUT,
                         "NEEDS_SEARCH_INTENT_CONFIRMATION")

        # intent 为空:合成失败(error) vs 信号不足
        if result.error and not _has_signal(ctx):
            return _fail(SearchIntentPreparationStatus.ERROR, result.error)
        if interaction_port is None:
            return _fail(SearchIntentPreparationStatus.NEEDS_INPUT, "NEEDS_SEARCH_TOPIC")
        if revisions >= max_revisions:
            return _fail(SearchIntentPreparationStatus.NEEDS_INPUT, "NEEDS_SEARCH_TOPIC")
        topic = interaction_port.request_search_topic(_context_summary(ctx))
        if topic.action == "provide" and topic.text.strip():
            revisions += 1
            ctx = replace(ctx, explicit_query=topic.text)
            continue
        if topic.action == "cancel":
            return _fail(SearchIntentPreparationStatus.CANCELLED, "user_cancelled")
        return _fail(SearchIntentPreparationStatus.NEEDS_INPUT, "NEEDS_SEARCH_TOPIC")


def _has_signal(ctx: SearchContext) -> bool:
    return any((ctx.explicit_query, ctx.purpose, ctx.direction, ctx.details,
                ctx.project_gap, ctx.project_novelty)) or bool(ctx.project_contributions)


def _intent_summary(intent: SearchIntent) -> dict:
    """交给 port 的摘要（供 UI 临时展示；不进持久化 trace）。"""
    return {
        "semantic_topic": intent.semantic_topic,
        "section_purpose": intent.section_purpose,
        "seeds": [(s.text, s.target.value) for s in intent.seeds],
        "origin": intent.origin.value,
    }


def _context_summary(ctx: SearchContext) -> dict:
    return {"operation_kind": ctx.operation_kind, "target_title": ctx.target_title}


__all__ = ["prepare_search_intent"]
