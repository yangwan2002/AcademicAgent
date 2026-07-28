"""搜文献子 SOP（共享设施 B，Week 3）。

被 rewrite / add_section 复用：检索候选 → 真实性核验 → 分配编号 → 入 facts。
只有核验通过的文献才入库，写作时才允许引用（防编造）。

依赖已移植的基础设施：RetrievalProvider（检索）+ CitationVerifier（核验）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.literature.bibkey import make_bibkey as _make_bibkey
from paper_agent.providers.retrieval.base import RetrievalError, RetrievalProvider
from paper_agent.session.facts import KnownRef, SessionFacts
from paper_agent.tools.citation import CitationVerifier
from paper_agent.workspace.models import ReferenceEntry


@dataclass
class SearchOutcome:
    added: list[KnownRef] = field(default_factory=list)
    requested: int = 0
    verified: int = 0
    error: str = ""


def search_and_verify(
    query: str,
    limit: int,
    provider: RetrievalProvider,
    verifier: CitationVerifier,
    facts: SessionFacts,
) -> SearchOutcome:
    """检索并核验文献，将通过核验的文献加入 facts。返回本次新增结果。"""
    try:
        candidates = provider.search(query, limit=limit)
    except RetrievalError as exc:
        return SearchOutcome(requested=limit, error=f"检索失败：{exc}")

    taken = facts.bibkeys()
    added: list[KnownRef] = []
    verified_count = 0
    for cand in candidates:
        marked = verifier.verify_and_mark(cand)
        if not marked.verified:
            continue
        verified_count += 1
        bibkey = _make_bibkey(marked, taken)
        ref = KnownRef(
            id=facts.next_ref_id(),
            bibkey=bibkey,
            title=marked.title,
            verified=True,
        )
        if facts.add_ref(ref):
            taken.add(bibkey)
            added.append(ref)

    return SearchOutcome(
        added=added, requested=len(candidates), verified=verified_count
    )


__all__ = ["SearchOutcome", "search_and_verify"]
