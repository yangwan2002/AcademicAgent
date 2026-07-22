"""Mock 检索 provider。

返回固定的、带合法标识符的样例文献，供骨架运行与测试使用。
fetch_metadata 对「已知」标识符返回条目，对未知标识符返回 None，
便于验证引用真实性核验逻辑（真实存在 vs 不存在）。
"""

from __future__ import annotations

from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.workspace.models import ReferenceEntry

_SAMPLE = [
    ReferenceEntry(
        id="arxiv:2301.10140",
        title="The Semantic Scholar Open Data Platform",
        authors=["Kinney", "et al."],
        year=2023,
        source_id="2301.10140",
        source="arxiv",
    ),
    ReferenceEntry(
        id="arxiv:1706.03762",
        title="Attention Is All You Need",
        authors=["Vaswani", "et al."],
        year=2017,
        source_id="1706.03762",
        source="arxiv",
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
