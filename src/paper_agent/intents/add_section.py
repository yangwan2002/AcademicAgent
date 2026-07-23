"""add_section SOP（卡 3）：为论文新增一个不存在的章节。

流程：（可选）搜文献 → 生成章节正文 → 新数字确认 → AddSectionGate → 通过则在
指定位置插入 working（其他章节逐字不变）→ 更新 facts.claims（跨 chat 复用）→
返回 diff 与预览。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paper_agent.gates.diff_gate import AddSectionGate, data_numbers
from paper_agent.generate.context_builder import build_add_section_messages
from paper_agent.intents.interview import AddSectionPlan
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
) -> AddSectionResult:
    index = SectionIndex(session.read_working(), session.ext)
    if index.find(plan.title) is not None:
        return AddSectionResult(
            ok=False, title=plan.title,
            message=f"章节「{plan.title}」已存在，请用 rewrite 修改。",
        )

    # 搜文献子 SOP
    search_note = ""
    if plan.want_search and plan.search_query:
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

    for _ in range(max_retry + 1):
        messages = build_add_section_messages(
            plan.title, plan.purpose, plan.structure_desc, plan.subsections,
            facts.known_refs, sorted(facts.number_values()),
            session.ext, facts.claims, violations or None,
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
        if result.passed:
            offset = index.insertion_offset(plan.position)
            new_full = index.insert_text(offset, f"\n\n{block}\n")
            session.write_working(new_full, message=f"add_section {plan.title}", bus=bus)
            _update_claims(facts, plan)
            facts.save()
            return AddSectionResult(
                ok=True, title=plan.title, attempts=attempts,
                preview=block[:300],
                message=f"已新增章节「{plan.title}」（{attempts} 次生成通过校验）。",
                search_note=search_note,
            )
        violations = result.violations

    return AddSectionResult(
        ok=False, title=plan.title, attempts=attempts,
        message=(
            f"我尝试了 {attempts} 次仍未通过校验。最后一次违反：\n"
            + "\n".join(f"  - {v}" for v in (result.violations if result else []))
        ),
        search_note=search_note,
    )


def _update_claims(facts: SessionFacts, plan: AddSectionPlan) -> None:
    facts.set_section_intent(plan.section_type, plan.purpose)
    structure_repr = ", ".join(plan.subsections) if plan.subsections else plan.structure_desc
    if structure_repr:
        facts.set_section_structure(plan.section_type, structure_repr)


__all__ = ["AddSectionResult", "execute_add_section"]
