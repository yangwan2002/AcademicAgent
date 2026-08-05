"""Mock 检索 provider。

返回固定的、带合法标识符的样例文献，供骨架运行与测试使用。
fetch_metadata 对「已知」标识符返回条目，对未知标识符返回 None，
便于验证引用真实性核验逻辑（真实存在 vs 不存在）。
"""

from __future__ import annotations

from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.providers.retrieval.capabilities import (
    BibliographicQuery,
    FetchResult,
    FetchStatus,
)
from paper_agent.workspace.models import ReferenceEntry

_SAMPLE = [
    ReferenceEntry(
        id="arxiv:2301.10140",
        title="The Semantic Scholar Open Data Platform",
        authors=["Kinney", "et al."],
        year=2023,
        source_id="2301.10140",
        source="arxiv",
        work_identifiers={"arxiv": "2301.10140"},
    ),
    ReferenceEntry(
        id="arxiv:1706.03762",
        title="Attention Is All You Need",
        authors=["Vaswani", "et al."],
        year=2017,
        source_id="1706.03762",
        source="arxiv",
        work_identifiers={"arxiv": "1706.03762"},
    ),
]


class MockRetrievalProvider(RetrievalProvider):
    def __init__(self, entries: list[ReferenceEntry] | None = None) -> None:
        self._entries = list(entries if entries is not None else _SAMPLE)
        self._by_source = {e.source_id: e for e in self._entries}

    def search(self, query: str, limit: int = 10) -> list[ReferenceEntry]:
        return [
            ReferenceEntry(**vars(e)) for e in self._entries[:limit]
        ]

    def fetch_metadata(self, identifier: str) -> ReferenceEntry | None:
        e = self._by_source.get(identifier)
        return ReferenceEntry(**vars(e)) if e else None


class MockVerifyProvider:
    """符合 A2c ``VerifyProvider`` 协议的离线核验源（联调/测试用）。

    仅用于**无真实网络**时把 mock 候选走通「严格核验」通路：``mock`` 源在
    ``sources.py`` 声明对全部标识符类型有 final 权，故其 FOUND 事件足以让候选达严格门槛。
    真实运行请用 :class:`CrossrefVerifyProvider`（本类不发网络、不冒充真实注册库）。

    - ``fetch_metadata``：命中样例库的 work identifier → ``FOUND``；否则
      ``NOT_FOUND_AT_SOURCE``（明确「本源未命中」，不伪装成 provider 错误）。
    - ``search_bibliographic``：按标题子串朴素匹配；无匹配返回 ``[]``
      （语义 ``UNRESOLVED_NO_MATCH``，绝不伪造 not_found）。
    """

    def __init__(self, entries: list[ReferenceEntry] | None = None) -> None:
        self._entries = list(entries if entries is not None else _SAMPLE)

    def _find(self, identifier_type: str, identifier: str) -> ReferenceEntry | None:
        idt = (identifier_type or "").strip().lower()
        idv = (identifier or "").strip().lower()
        for e in self._entries:
            for k, v in (e.work_identifiers or {}).items():
                if k.lower() == idt and str(v).strip().lower() == idv:
                    return e
            if idv and e.source_id.strip().lower() == idv:
                return e
        return None

    def fetch_metadata(self, identifier_type: str, identifier: str) -> FetchResult:
        e = self._find(identifier_type, identifier)
        if e is None:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE,
                               detail=f"mock 样例库无 {identifier_type}={identifier}")
        return FetchResult(FetchStatus.FOUND, candidate=ReferenceEntry(**vars(e)))

    def search_bibliographic(
        self,
        query: BibliographicQuery,
        *,
        limit: int = 5,
    ) -> list[ReferenceEntry]:
        title = (query.title or "").strip().lower()
        if not title:
            return []
        hits = [ReferenceEntry(**vars(e)) for e in self._entries
                if title in e.title.lower() or e.title.lower() in title]
        return hits[:limit]
