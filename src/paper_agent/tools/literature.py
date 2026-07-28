"""搜文献子 SOP（共享设施 B，A1 起以 ReferenceLedger 为入库后端）。

被 rewrite / add_section 复用：检索候选 → 真实性核验 → 去重入库。
只有核验通过的文献才入库，写作时才允许引用（防编造）。

A1 变化：入库改走 ``facts.ledger.upsert``（多标识符去重 + 稳定主键 + 核验事件），
核验改走 ``ReferenceVerifier``（产出带类型的 VerificationEvent）。签名与
``SearchOutcome`` 字段**保持不变**，故 rewrite/add_section 零改动。

``require_final`` 默认 False → 与现状一致：成功核验记为 final、状态 VERIFIED、可引用。
A2 接入 Crossref 后由调用方传 ``config.literature_require_final_verification=True``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.literature.bibkey import make_bibkey as _make_bibkey  # noqa: F401 (兼容再导出)
from paper_agent.literature.ledger import UpsertAction
from paper_agent.literature.migration import candidate_to_record
from paper_agent.literature.verification import EventKind
from paper_agent.literature.verifier import ReferenceVerifier
from paper_agent.providers.retrieval.base import RetrievalError, RetrievalProvider
from paper_agent.session.facts import KnownRef, SessionFacts
from paper_agent.tools.citation import CitationVerifier


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
    *,
    require_final: bool = False,
) -> SearchOutcome:
    """检索并核验文献，将通过核验的文献去重入库。返回本次新增结果。"""
    try:
        candidates = provider.search(query, limit=limit)
    except RetrievalError as exc:
        return SearchOutcome(requested=limit, error=f"检索失败：{exc}")

    ref_verifier = ReferenceVerifier(verifier, require_final=require_final)
    added: list[KnownRef] = []
    verified_count = 0
    for cand in candidates:
        event = ref_verifier.assess(cand)
        # A1 口径：仅「核验通过」的候选入库（与旧 verify_and_mark→verified 一致）。
        if event.kind != EventKind.VERIFIED:
            continue
        verified_count += 1
        record = candidate_to_record(cand)
        result = facts.ledger.upsert(record, [event])
        if result.action in (UpsertAction.CREATED, UpsertAction.CREATED_WITH_SUGGESTION):
            rec = facts.ledger.get(result.reference_id)
            if rec is not None:
                added.append(rec.to_known_ref())

    return SearchOutcome(
        added=added, requested=len(candidates), verified=verified_count
    )


__all__ = ["SearchOutcome", "search_and_verify"]
