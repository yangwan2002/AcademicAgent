"""add_section SOP（卡 3）：为论文新增一个不存在的章节。

流程：（可选）搜文献 → 生成章节正文 → 新数字确认 → AddSectionGate → 通过则在
指定位置插入 working（其他章节逐字不变）→ 更新 facts.claims（跨 chat 复用）→
返回 diff 与预览。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from paper_agent.gates.diff_gate import AddSectionGate, data_numbers
from paper_agent.generate.context_builder import build_add_section_messages
from paper_agent.intents.interview import AddSectionPlan
from paper_agent.literature.bibliography_sync import apply_bibliography
from paper_agent.intents.retry_feedback import (
    exhausted_message,
    is_stagnant,
    merge_violations,
    stagnation_message,
)
from paper_agent.intents.rewrite import ConfirmNumber
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.session.facts import SessionFacts
from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.section_index import SectionIndex
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.literature import search_and_verify

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass
class AddSectionResult:
    ok: bool
    title: str
    attempts: int = 0
    preview: str = ""
    message: str = ""
    search_note: str = ""
    # A2d-5：带配额时的配额报告；无配额路径为 None → 结果结构与现状一致。
    quota_report: object | None = None


def _snippet(text: str, value: str, width: int = 40) -> str:
    i = text.find(value)
    if i < 0:
        return value
    return "…" + text[max(0, i - width): i + len(value) + width].replace("\n", " ") + "…"


def execute_add_section(
    session: FileBackedSession,
    plan: AddSectionPlan,
    facts: SessionFacts,
    llm: LLMProvider,
    *,
    retrieval_provider: RetrievalProvider,
    verifier: CitationVerifier,
    confirm: ConfirmNumber,
    max_retry: int = 2,
    bus=None,
    citation_context=None,
) -> AddSectionResult:
    index = SectionIndex(session.read_working(), session.ext)
    if index.find(plan.title) is not None:
        return AddSectionResult(
            ok=False, title=plan.title,
            message=f"章节「{plan.title}」已存在，请用 rewrite 修改。",
        )

    # 搜文献子 SOP。**带配额时跳过**：Preflight 已是唯一检索入口（Req 13.12 / design D8）。
    search_note = ""
    if citation_context is not None:
        search_note = "（带引用配额：文献已由写作前预检备齐，未再触发旧搜索流程。）"
    elif plan.want_search and plan.search_query:
        out = search_and_verify(
            plan.search_query, plan.search_limit, retrieval_provider, verifier, facts
        )
        search_note = out.error or (
            f"搜「{plan.search_query}」：请求 {out.requested} 条，"
            f"核验通过 {out.verified} 条，新增 {len(out.added)} 条。"
        )
        facts.save()

    heading = index.render_heading(plan.title, level=1)
    expected_subs = plan.subsections if plan.structure_kind in ("list", "mimic") else None
    gate = AddSectionGate(session.ext, facts)
    confirmed: set[str] = set()
    violations: list[str] = []
    attempts = 0
    result = None
    stagnated = False

    quota_gate = None
    bundle = getattr(citation_context, "bundle", None)
    if citation_context is not None:
        from paper_agent.gates.citation_quota_gate import CitationQuotaGate
        quota_gate = CitationQuotaGate()
    quota_result = None

    for _ in range(max_retry + 1):
        messages = build_add_section_messages(
            plan.title, plan.purpose, plan.structure_desc, plan.subsections,
            facts.known_refs, sorted(facts.number_values()),
            session.ext, facts.claims, violations or None, bundle=bundle,
            source_text=getattr(plan, "source_text", "") or "",
        )
        resp = llm.complete(messages)
        attempts += 1
        body = (resp.content or "").strip()
        block = f"{heading}\n{body}"

        for value in data_numbers(block) - (facts.number_values() | confirmed):
            if confirm(value, _snippet(block, value)):
                confirmed.add(value)
                facts.add_number(value, source="user_confirmed", confirmed=True)

        result = gate.check(
            block, title=plan.title,
            expected_subsections=expected_subs, confirmed_numbers=confirmed,
        )

        # 带配额：同轮跑 CitationQuotaGate（新增章节 → original_text=""，全部引用都须在 Bundle）
        quota_result = None
        if quota_gate is not None:
            quota_result = quota_gate.check(
                block, ext=session.ext, bundle=bundle, ledger=facts.ledger,
                quota=citation_context.quota,
                effective_constraints=citation_context.effective_constraints,
                original_text="", classifier=citation_context.classifier,
                policy=citation_context.policy,
            )

        if result.passed and (quota_result is None or quota_result.passed):
            return _commit_add_section(session, index, plan, block, facts, attempts,
                                       search_note, bus, quota_result,
                                       ctx=citation_context)

        prev_violations = violations
        violations = merge_violations(result, quota_result,
                                      ledger=facts.ledger, ext=session.ext)
        # 违规与上一轮逐字相同 ⇒ 回灌不再带来新信息，再试也是同一结果，提前停。
        if is_stagnant(prev_violations, violations):
            stagnated = True
            break

    # 预算耗尽：只差配额（结构 Gate 通过 + 无非法引用）且用户显式接受 → 才写回
    if (quota_result is not None and result is not None and result.passed
            and not (quota_result.new_unresolved_tokens or quota_result.new_ambiguous_tokens
                     or quota_result.new_out_of_bundle_ids)):
        port = getattr(citation_context, "port", None)
        if port is not None:
            decision = port.resolve_final_gate({
                "effective_gaps": [(g.dimension.value, g.amount) for g in quota_result.effective_gaps],
                "original_gaps": [(g.dimension.value, g.amount) for g in quota_result.original_gaps],
                "used": list(quota_result.used_reference_ids),
            })
            if getattr(decision, "action", "") == "accept":
                return _commit_add_section(session, index, plan, block, facts, attempts,
                                           search_note, bus, quota_result,
                                           ctx=citation_context, accepted_final_gate=True)

    return AddSectionResult(
        ok=False, title=plan.title, attempts=attempts,
        message=(stagnation_message(attempts, violations) if stagnated
                 else exhausted_message(attempts, violations)),
        search_note=search_note,
        quota_report=_quota_report(quota_result, citation_context) if quota_result else None,
    )


def _quota_report(qr, ctx=None, *, accepted_final_gate: bool = False):
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


def _commit_add_section(session, index, plan, block, facts, attempts, search_note, bus,
                        quota_result, *, ctx=None, accepted_final_gate: bool = False):
    offset = index.insertion_offset(plan.position)
    new_full = index.insert_text(offset, f"\n\n{block}\n")
    # 与正文同一次写入，理由见 apply_bibliography docstring（:undo 原子性）。
    new_full, bib_report = apply_bibliography(new_full, session.ext, facts.ledger)
    session.write_working(new_full, message=f"add_section {plan.title}", bus=bus)
    _update_claims(facts, plan)
    facts.save()
    note = f"已新增章节「{plan.title}」（{attempts} 次生成通过校验）。"
    if accepted_final_gate:
        note += "（配额未完全达标，已按你的确认接受当前结果。）"
    if bib_report is not None and bib_report.message:
        note += "\n" + bib_report.message
    return AddSectionResult(
        ok=True, title=plan.title, attempts=attempts, preview=block[:300],
        message=note, search_note=search_note,
        quota_report=_quota_report(quota_result, ctx, accepted_final_gate=accepted_final_gate),
    )


def _update_claims(facts: SessionFacts, plan: AddSectionPlan) -> None:
    facts.set_section_intent(plan.section_type, plan.purpose)
    structure_repr = ", ".join(plan.subsections) if plan.subsections else plan.structure_desc
    if structure_repr:
        facts.set_section_structure(plan.section_type, structure_repr)


__all__ = ["AddSectionResult", "execute_add_section"]
