"""rewrite SOP（卡 2）：受控改写单个章节。

流程：（可选）搜文献子 SOP 冻结证据集 → 生成 → 新数字经用户确认 → RewriteGate
→ 通过则原地 splice 写回 working → 返回 diff。源文件不动，其他章节逐字不变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from paper_agent.gates.diff_gate import RewriteGate, _count_paragraphs, data_numbers
from paper_agent.generate.context_builder import build_rewrite_messages
from paper_agent.intents.interview import RewritePlan
from paper_agent.intents.polish import unified_diff
from paper_agent.literature.bibliography_sync import apply_bibliography
from paper_agent.intents.retry_feedback import (
    exhausted_message,
    is_stagnant,
    merge_violations,
    stagnation_message,
)
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.session.facts import SessionFacts
from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.section_index import SectionIndex
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.literature import search_and_verify

_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# confirm(value, context) -> True 表示用户确认该数字真实可用
ConfirmNumber = Callable[[str, str], bool]


@dataclass
class RewriteResult:
    ok: bool
    section_id: str
    attempts: int = 0
    diff: str = ""
    message: str = ""
    search_note: str = ""
    report_lines: list[str] = field(default_factory=list)
    # A2d-5：带配额时的配额报告；无配额路径为 None → 结果结构与现状一致。
    quota_report: object | None = None


def _snippet(text: str, value: str, width: int = 40) -> str:
    i = text.find(value)
    if i < 0:
        return value
    start = max(0, i - width)
    end = min(len(text), i + len(value) + width)
    return "…" + text[start:end].replace("\n", " ") + "…"


def execute_rewrite(
    session: FileBackedSession,
    plan: RewritePlan,
    facts: SessionFacts,
    llm: LLMProvider,
    *,
    retrieval_provider: RetrievalProvider,
    verifier: CitationVerifier,
    confirm: ConfirmNumber,
    max_retry: int = 2,
    bus=None,
    citation_context=None,
) -> RewriteResult:
    index = SectionIndex(session.read_working(), session.ext)
    section = index.find(plan.section_id)
    if section is None:
        return RewriteResult(
            ok=False, section_id=plan.section_id,
            message=f"章节不存在：{plan.section_id}",
        )
    original = index.body_of(section)

    # 搜文献子 SOP（冻结证据集）。**带配额时跳过**：Preflight 已是唯一检索入口
    # （Req 13.12 / design D8：禁止双重检索）。
    search_note = ""
    if citation_context is not None:
        search_note = "（带引用配额：文献已由写作前预检备齐，未再触发旧搜索流程。）"
    elif plan.want_search and plan.search_query:
        outcome = search_and_verify(
            plan.search_query, plan.search_limit, retrieval_provider, verifier, facts
        )
        if outcome.error:
            search_note = outcome.error
        else:
            search_note = (
                f"搜「{plan.search_query}」：请求 {outcome.requested} 条，"
                f"核验通过 {outcome.verified} 条，新增入库 {len(outcome.added)} 条。"
            )
        facts.save()

    gate = RewriteGate(session.ext, facts, section.level,
                       expand_budget=_expand_budget(plan, citation_context, original))
    quota_gate = _quota_gate_of(citation_context)
    bundle = getattr(citation_context, "bundle", None)
    confirmed: set[str] = set()
    violations: list[str] = []
    attempts = 0
    result = None
    quota_result = None
    stagnated = False

    for _ in range(max_retry + 1):
        messages = build_rewrite_messages(
            original, plan.direction, plan.details,
            facts.known_refs, sorted(facts.number_values()),
            session.ext, violations or None, bundle=bundle,
            source_text=getattr(plan, "source_text", "") or "",
        )
        resp = llm.complete(messages)
        attempts += 1
        candidate = (resp.content or "").strip()

        # 新数字：逐个请用户确认（确认则纳入 facts）
        allowed = data_numbers(original) | facts.number_values() | confirmed
        for value in data_numbers(candidate) - allowed:
            if confirm(value, _snippet(candidate, value)):
                confirmed.add(value)
                facts.add_number(value, source="user_confirmed", confirmed=True)

        result = gate.check(original, candidate, confirmed)

        # 带配额：同轮跑 CitationQuotaGate，**合并违规一次回灌**、共用同一 retry 预算，
        # 循环内不检索、不换 Bundle（Req 13.12 / 14.7）。
        quota_result = None
        if quota_gate is not None:
            quota_result = quota_gate.check(
                candidate, ext=session.ext, bundle=bundle, ledger=facts.ledger,
                quota=citation_context.quota,
                effective_constraints=citation_context.effective_constraints,
                original_text=original, classifier=citation_context.classifier,
                policy=citation_context.policy,
            )

        if result.passed and (quota_result is None or quota_result.passed):
            return _commit_rewrite(session, index, section, original, candidate, facts,
                                   attempts, search_note, bus, quota_result,
                                   ctx=citation_context)

        prev_violations = violations
        violations = merge_violations(result, quota_result,
                                      ledger=facts.ledger, ext=session.ext)
        # 违规与上一轮逐字相同 ⇒ 回灌不再带来新信息，再试也是同一结果，提前停。
        if is_stagnant(prev_violations, violations):
            stagnated = True
            break

    # 预算耗尽：仅当**只差配额**（无非法引用、结构 Gate 通过）且用户显式接受，才写回。
    if (quota_result is not None and result is not None and result.passed
            and not _has_illegal_citations(quota_result)):
        port = getattr(citation_context, "port", None)
        if port is not None:
            decision = port.resolve_final_gate({
                "effective_gaps": [(g.dimension.value, g.amount) for g in quota_result.effective_gaps],
                "original_gaps": [(g.dimension.value, g.amount) for g in quota_result.original_gaps],
                "used": list(quota_result.used_reference_ids),
            })
            if getattr(decision, "action", "") == "accept":
                return _commit_rewrite(session, index, section, original, candidate, facts,
                                       attempts, search_note, bus, quota_result,
                                       ctx=citation_context, accepted_final_gate=True)

    return RewriteResult(
        ok=False, section_id=section.id, attempts=attempts,
        message=(stagnation_message(attempts, violations) if stagnated
                 else exhausted_message(attempts, violations)),
        search_note=search_note,
        quota_report=_report_of(quota_result, citation_context) if quota_result else None,
    )


_EXPAND_HINTS = ("扩写", "展开", "丰富", "补充论述", "扩充", "写成正文", "详细展开",
                 "expand", "elaborate")


def _expand_budget(plan, citation_context, original: str) -> int:
    """扩写类请求需要放宽段落上限，返回额外允许的段落数。

    两个来源叠加，都基于**用户已明说的东西**，不做无根据放水：
    - 方向/细节里出现扩写意图 → 允许在原文段落数基础上翻一倍；
    - 有引用配额 N → 至少要能容纳 N 处引用的铺陈（每段 1~2 处）。
    """
    text = f"{getattr(plan, 'direction', '') or ''} {getattr(plan, 'details', '') or ''}"
    budget = 0
    if any(h in text for h in _EXPAND_HINTS):
        budget = max(budget, _count_paragraphs(original))
    quota = getattr(citation_context, "quota", None)
    if quota is not None:
        total = 0
        for c in getattr(quota, "count_constraints", ()) or ():
            if getattr(c.dimension, "value", "") == "total":
                val = c.value
                total = max(total, int(val[1] if isinstance(val, tuple) else val))
        budget = max(budget, total)
    return budget


def _quota_gate_of(citation_context):
    if citation_context is None:
        return None
    from paper_agent.gates.citation_quota_gate import CitationQuotaGate

    return CitationQuotaGate()


def _has_illegal_citations(qr) -> bool:
    """非法引用（新增 unresolved/ambiguous/Bundle 外）**永不被配额偏差豁免**。"""
    return bool(qr.new_unresolved_tokens or qr.new_ambiguous_tokens or qr.new_out_of_bundle_ids)


def _report_of(qr, ctx, *, accepted_final_gate: bool = False):
    from paper_agent.literature.citation_context import QuotaReport

    if qr is None:
        return None
    return QuotaReport(
        original_gaps=qr.original_gaps, effective_gaps=qr.effective_gaps,
        used_reference_ids=qr.used_reference_ids,
        unused_reference_ids=qr.unused_reference_ids,
        accepted_final_gate=accepted_final_gate,
        deviations=getattr(ctx, "deviations", ()) if ctx is not None else (),
    )


def _commit_rewrite(session, index, section, original, candidate, facts, attempts,
                    search_note, bus, quota_result, *, ctx=None,
                    accepted_final_gate: bool = False):
    new_full = index.splice(section, candidate)
    # 参考文献区与正文**同一次写入**：分两次写会在 :undo 时留下半截状态
    # （撤销一步只回退文献区，正文引用仍在 → 又变成悬空引用）。
    new_full, bib_report = apply_bibliography(new_full, session.ext, facts.ledger)
    session.write_working(new_full, message=f"rewrite {section.id}", bus=bus)
    facts.save()
    diff = unified_diff(original, candidate, section.title)
    note = f"已改写章节「{section.title}」（{attempts} 次生成通过校验）。"
    if accepted_final_gate:
        note += "（配额未完全达标，已按你的确认接受当前结果。）"
    if bib_report is not None and bib_report.message:
        note += f"\n{bib_report.message}"
    return RewriteResult(
        ok=True, section_id=section.id, attempts=attempts, diff=diff,
        message=note, search_note=search_note,
        quota_report=_report_of(quota_result, ctx, accepted_final_gate=accepted_final_gate),
    )


__all__ = ["RewriteResult", "execute_rewrite", "ConfirmNumber"]
