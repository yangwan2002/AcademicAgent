"""rewrite SOP（卡 2）：受控改写单个章节。

流程：（可选）搜文献子 SOP 冻结证据集 → 生成 → 新数字经用户确认 → RewriteGate
→ 通过则原地 splice 写回 working → 返回 diff。源文件不动，其他章节逐字不变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from paper_agent.gates.diff_gate import RewriteGate, data_numbers
from paper_agent.generate.context_builder import build_rewrite_messages
from paper_agent.intents.interview import RewritePlan
from paper_agent.intents.polish import unified_diff
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
) -> RewriteResult:
    index = SectionIndex(session.read_working(), session.ext)
    section = index.find(plan.section_id)
    if section is None:
        return RewriteResult(
            ok=False, section_id=plan.section_id,
            message=f"章节不存在：{plan.section_id}",
        )
    original = index.body_of(section)

    # 搜文献子 SOP（冻结证据集）
    search_note = ""
    if plan.want_search and plan.search_query:
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

    gate = RewriteGate(session.ext, facts, section.level)
    confirmed: set[str] = set()
    violations: list[str] = []
    attempts = 0
    result = None

    for _ in range(max_retry + 1):
        messages = build_rewrite_messages(
            original, plan.direction, plan.details,
            facts.known_refs, sorted(facts.number_values()),
            session.ext, violations or None,
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
        if result.passed:
            new_full = index.splice(section, candidate)
            session.write_working(new_full, message=f"rewrite {section.id}", bus=bus)
            facts.save()
            diff = unified_diff(original, candidate, section.title)
            return RewriteResult(
                ok=True, section_id=section.id, attempts=attempts, diff=diff,
                message=f"已改写章节「{section.title}」（{attempts} 次生成通过校验）。",
                search_note=search_note,
            )
        violations = result.violations

    return RewriteResult(
        ok=False, section_id=section.id, attempts=attempts,
        message=(
            f"我尝试了 {attempts} 次仍未通过校验。最后一次违反：\n"
            + "\n".join(f"  - {v}" for v in (result.violations if result else []))
        ),
        search_note=search_note,
    )


__all__ = ["RewriteResult", "execute_rewrite", "ConfirmNumber"]
