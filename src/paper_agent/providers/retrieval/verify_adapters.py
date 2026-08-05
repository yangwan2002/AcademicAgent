"""把既有真实检索源包成 A2c ``VerifyProvider``（A2e，design-a2e D1–D4）。

现状:OpenAlex / arXiv / Semantic Scholar 实现的是**旧** ``RetrievalProvider``
（``fetch_metadata(identifier)`` 单参、无 ``search_bibliographic``）,不满足 A2c 的
``VerifyProvider`` 协议,故无法参与交叉核验——导致只有 DOI（Crossref）能达严格门槛。

本模块**只做协议翻译**,不碰网络/解析（仍在被包裹的旧 provider）。每个适配器持一个
``inner``（默认惰性构造真实 provider,测试可注入 stub）。

三态映射铁律（Req 2.7）:``inner`` 返回 Entry→FOUND、返回 None 或抛
``RetrievalNotFound``→NOT_FOUND_AT_SOURCE、抛 ``RetrievalUnavailable``→PROVIDER_ERROR。
**绝不用空结果掩盖失败**。本源无法回查的 id_type → 直接 NOT_FOUND_AT_SOURCE,不发网络
（沿用 ``crossref.py`` 对非 doi 的先例）。

不 import ``tasking`` / ``literature``（分层:providers 在下游）。
"""

from __future__ import annotations

from paper_agent.providers.retrieval.base import (
    RetrievalNotFound,
    RetrievalUnavailable,
)
from paper_agent.providers.retrieval.capabilities import (
    BibliographicQuery,
    FetchResult,
    FetchStatus,
)


def _bib_query_string(query: BibliographicQuery) -> str:
    parts = [query.title, " ".join(query.authors),
             str(query.year) if query.year else ""]
    return " ".join(p for p in parts if p).strip()


def _fetch_via_inner(inner, ident: str) -> FetchResult:
    """统一把旧 ``fetch_metadata(ident)`` 的三种表现翻译为 FetchResult（Req 2.7）。"""
    try:
        entry = inner.fetch_metadata(ident)
    except RetrievalNotFound as exc:
        return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE, detail=str(exc))
    except RetrievalUnavailable as exc:
        return FetchResult(FetchStatus.PROVIDER_ERROR, detail=str(exc))
    if entry is None:
        return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE, detail="查无此文")
    return FetchResult(FetchStatus.FOUND, candidate=entry)


def _search_via_inner(inner, query: BibliographicQuery, limit: int) -> list:
    """书目模糊反查:无匹配 []（UNRESOLVED_NO_MATCH）;provider 不可用**上抛**。"""
    bib = _bib_query_string(query)
    if not bib:
        return []
    try:
        return list(inner.search(bib, limit=limit))
    except RetrievalNotFound:
        return []                       # 无匹配 ≠ 不存在,绝不产 not_found


class ArxivVerifyProvider:
    """arXiv 核验适配:对 ``arxiv`` 有 final 权;不按 doi 等其它类型回查。"""

    _QUERYABLE = frozenset({"arxiv"})

    def __init__(self, inner=None) -> None:
        if inner is None:
            from paper_agent.providers.retrieval.api import ArxivRetrievalProvider

            inner = ArxivRetrievalProvider()
        self._inner = inner

    def fetch_metadata(self, identifier_type: str, identifier: str) -> FetchResult:
        idt = (identifier_type or "").strip().lower()
        if idt not in self._QUERYABLE:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE,
                               detail=f"arxiv 不按 {idt} 回查")
        ident = (identifier or "").strip()
        if not ident:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE, detail="空标识符")
        return _fetch_via_inner(self._inner, ident)

    def search_bibliographic(self, query: BibliographicQuery, *, limit: int = 5) -> list:
        return _search_via_inner(self._inner, query, limit)


class OpenAlexVerifyProvider:
    """OpenAlex 核验适配:``/works/{id}`` 支持 doi 与 openalex 原生 id。

    本期**不支持** arxiv 回查（需 filter 查询,design-a2e 已定简化）:交 arxiv 源 final。
    """

    _QUERYABLE = frozenset({"doi", "openalex_work"})

    def __init__(self, inner=None) -> None:
        if inner is None:
            from paper_agent.providers.retrieval.openalex import OpenAlexRetrievalProvider

            inner = OpenAlexRetrievalProvider()
        self._inner = inner

    def fetch_metadata(self, identifier_type: str, identifier: str) -> FetchResult:
        idt = (identifier_type or "").strip().lower()
        if idt not in self._QUERYABLE:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE,
                               detail=f"openalex 不按 {idt} 回查")
        ident = (identifier or "").strip()
        if not ident:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE, detail="空标识符")
        return _fetch_via_inner(self._inner, ident)

    def search_bibliographic(self, query: BibliographicQuery, *, limit: int = 5) -> list:
        return _search_via_inner(self._inner, query, limit)


class SemanticScholarVerifyProvider:
    """Semantic Scholar 核验适配:``paper/{prefix}:{id}`` 支持多类标识符。"""

    _PREFIX = {"doi": "DOI:", "arxiv": "ARXIV:", "pmid": "PMID:"}
    _QUERYABLE = frozenset({"doi", "arxiv", "pmid", "semantic_scholar_paper"})

    def __init__(self, inner=None) -> None:
        if inner is None:
            from paper_agent.providers.retrieval.api import (
                SemanticScholarRetrievalProvider,
            )

            inner = SemanticScholarRetrievalProvider()
        self._inner = inner

    def fetch_metadata(self, identifier_type: str, identifier: str) -> FetchResult:
        idt = (identifier_type or "").strip().lower()
        if idt not in self._QUERYABLE:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE,
                               detail=f"semantic_scholar 不按 {idt} 回查")
        ident = (identifier or "").strip()
        if not ident:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE, detail="空标识符")
        # paperId 直接用;doi/arxiv/pmid 加 S2 前缀。
        query_id = f"{self._PREFIX[idt]}{ident}" if idt in self._PREFIX else ident
        return _fetch_via_inner(self._inner, query_id)

    def search_bibliographic(self, query: BibliographicQuery, *, limit: int = 5) -> list:
        return _search_via_inner(self._inner, query, limit)


__all__ = [
    "ArxivVerifyProvider",
    "OpenAlexVerifyProvider",
    "SemanticScholarVerifyProvider",
]
