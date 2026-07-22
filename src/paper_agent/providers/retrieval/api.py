"""真实文献检索 provider：OpenAlex（主）+ arXiv + Semantic Scholar（可选）。

聚合策略（ApiRetrievalProvider）：OpenAlex 优先（免费、含摘要、相关性好），
arXiv 兜底；Semantic Scholar 仅在配置了 API Key 时启用（匿名极易 429）。
单源失败不影响其他源。

依赖 httpx（可选依赖，见 [api] extra），惰性导入以保持核心零依赖。
返回的 ReferenceEntry 带可核验 source_id，供 CitationVerifier 核验真实性。
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

from paper_agent.providers.retrieval.base import RetrievalError, RetrievalProvider
from paper_agent.providers.retrieval.http_util import get_with_retry
from paper_agent.providers.retrieval.openalex import OpenAlexRetrievalProvider
from paper_agent.workspace.models import ReferenceEntry

_ARXIV_API = "https://export.arxiv.org/api/query"
_S2_API = "https://api.semanticscholar.org/graph/v1"
_ATOM = "{http://www.w3.org/2005/Atom}"
_VERSION_SUFFIX = re.compile(r"v\d+$")


class ArxivRetrievalProvider(RetrievalProvider):
    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = timeout

    def search(self, query: str, limit: int = 10) -> list[ReferenceEntry]:
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": limit,
        }
        resp = get_with_retry(_ARXIV_API, params=params, timeout=self._timeout)
        return self._parse_atom(resp.text)

    def fetch_metadata(self, identifier: str) -> ReferenceEntry | None:
        params = {"id_list": identifier, "max_results": 1}
        resp = get_with_retry(_ARXIV_API, params=params, timeout=self._timeout)
        entries = self._parse_atom(resp.text)
        return entries[0] if entries else None

    @staticmethod
    def _extract_arxiv_id(raw_id: str) -> str:
        """从 atom id URL 提取 arXiv id，保留旧格式的类别前缀。

        例：
          http://arxiv.org/abs/2509.16909v1     -> 2509.16909
          http://arxiv.org/abs/physics/0506741  -> physics/0506741
        """
        if not raw_id:
            return ""
        marker = "/abs/"
        tail = raw_id.split(marker, 1)[1] if marker in raw_id else raw_id
        return _VERSION_SUFFIX.sub("", tail.strip())

    @classmethod
    def _parse_atom(cls, xml_text: str) -> list[ReferenceEntry]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:  # pragma: no cover
            raise RetrievalError(f"解析 arXiv 响应失败：{exc}") from exc
        results: list[ReferenceEntry] = []
        for entry in root.findall(f"{_ATOM}entry"):
            raw_id = (entry.findtext(f"{_ATOM}id") or "").strip()
            arxiv_id = cls._extract_arxiv_id(raw_id)
            if not arxiv_id:
                continue
            title = " ".join((entry.findtext(f"{_ATOM}title") or "").split())
            published = (entry.findtext(f"{_ATOM}published") or "").strip()
            year = int(published[:4]) if published[:4].isdigit() else None
            authors = [
                (a.findtext(f"{_ATOM}name") or "").strip()
                for a in entry.findall(f"{_ATOM}author")
            ]
            summary = " ".join((entry.findtext(f"{_ATOM}summary") or "").split())
            results.append(
                ReferenceEntry(
                    id=f"arxiv:{arxiv_id}",
                    title=title,
                    authors=[a for a in authors if a],
                    year=year,
                    source_id=arxiv_id,
                    source="arxiv",
                    abstract=summary,
                )
            )
        return results


class SemanticScholarRetrievalProvider(RetrievalProvider):
    def __init__(self, api_key: str | None = None, timeout: float = 15.0) -> None:
        self._api_key = api_key or os.environ.get("S2_API_KEY")
        self._timeout = timeout

    def _headers(self) -> dict:
        return {"x-api-key": self._api_key} if self._api_key else {}

    def search(self, query: str, limit: int = 10) -> list[ReferenceEntry]:
        url = f"{_S2_API}/paper/search"
        params = {
            "query": query,
            "limit": limit,
            "fields": "title,authors,year,externalIds,abstract",
        }
        resp = get_with_retry(
            url, params=params, headers=self._headers(), timeout=self._timeout
        )
        return [self._to_entry(p) for p in resp.json().get("data", [])]

    def fetch_metadata(self, identifier: str) -> ReferenceEntry | None:
        url = f"{_S2_API}/paper/{identifier}"
        params = {"fields": "title,authors,year,externalIds,abstract"}
        try:
            resp = get_with_retry(
                url, params=params, headers=self._headers(), timeout=self._timeout
            )
        except RetrievalError:
            return None
        return self._to_entry(resp.json())

    @staticmethod
    def _to_entry(paper: dict) -> ReferenceEntry:
        ext = paper.get("externalIds") or {}
        source_id = ext.get("DOI") or paper.get("paperId", "")
        return ReferenceEntry(
            id=f"s2:{paper.get('paperId', source_id)}",
            title=paper.get("title", ""),
            authors=[a.get("name", "") for a in paper.get("authors", [])],
            year=paper.get("year"),
            source_id=source_id,
            source="semantic_scholar",
            abstract=paper.get("abstract") or "",
        )


class ApiRetrievalProvider(RetrievalProvider):
    """聚合多个真实源，合并去重。

    默认：OpenAlex（主）+ arXiv（兜底）。仅当存在 S2_API_KEY 时加入
    Semantic Scholar（匿名访问极易被限流）。
    """

    # arXiv id 形态：纯数字点串（1706.03762）或类别前缀（physics/0506741）。
    _ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})$")
    # DOI 形态：10.xxxx/...
    _DOI = re.compile(r"^10\.\d{4,}/", re.IGNORECASE)

    def __init__(self, providers: list[RetrievalProvider] | None = None) -> None:
        if providers is not None:
            self._providers = providers
        else:
            chain: list[RetrievalProvider] = [
                OpenAlexRetrievalProvider(),
                ArxivRetrievalProvider(),
            ]
            if os.environ.get("S2_API_KEY"):
                chain.append(SemanticScholarRetrievalProvider())
            self._providers = chain

    def search(self, query: str, limit: int = 10) -> list[ReferenceEntry]:
        seen: set[str] = set()
        merged: list[ReferenceEntry] = []
        for provider in self._providers:
            try:
                for entry in provider.search(query, limit=limit):
                    key = (entry.source_id or entry.id).lower()
                    if key not in seen:
                        seen.add(key)
                        merged.append(entry)
            except RetrievalError:
                continue  # 单源失败不影响其他源
        return merged[:limit]

    def fetch_metadata(self, identifier: str) -> ReferenceEntry | None:
        # #6：按 identifier 形态路由——arxiv id 直接走 arxiv，避免对每个 arxiv id
        # 都白打一次 OpenAlex 404（既慢又消耗限额）。其余按原顺序尝试所有源。
        for provider in self._route_order(identifier):
            try:
                found = provider.fetch_metadata(identifier)
            except RetrievalError:
                continue
            if found is not None:
                return found
        return None

    def _route_order(self, identifier: str) -> list[RetrievalProvider]:
        """据 identifier 形态给出优先尝试的源顺序。"""
        ident = (identifier or "").strip()
        if self._ARXIV_ID.match(ident):
            # arxiv id → arxiv 优先，其余兜底。
            arxiv_first = [
                p for p in self._providers if isinstance(p, ArxivRetrievalProvider)
            ]
            rest = [p for p in self._providers if not isinstance(p, ArxivRetrievalProvider)]
            return arxiv_first + rest
        return list(self._providers)
