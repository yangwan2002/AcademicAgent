"""OpenAlex 检索 provider（推荐的主源）。

OpenAlex 免费、无需 key、限流宽松，覆盖 2.4 亿+ 学术作品，且返回摘要
（以倒排索引形式，需重建）。提供 mailto 可进入更稳定的 polite pool。

文档：https://docs.openalex.org/  内容经改写以符合合规要求。
"""

from __future__ import annotations

import os

from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.providers.retrieval.http_util import get_with_retry
from paper_agent.workspace.models import ReferenceEntry

_WORKS = "https://api.openalex.org/works"


def reconstruct_abstract(inverted_index: dict | None) -> str:
    """从 OpenAlex 的 abstract_inverted_index 重建摘要文本。

    倒排索引形如 {"word": [pos1, pos2], ...}，按位置还原为有序文本。
    """
    if not inverted_index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort(key=lambda p: p[0])
    return " ".join(word for _, word in positions)


def _strip_doi(doi: str | None) -> str:
    if not doi:
        return ""
    return doi.replace("https://doi.org/", "").replace("http://doi.org/", "")


class OpenAlexRetrievalProvider(RetrievalProvider):
    def __init__(
        self,
        mailto: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        # 提供 mailto 进入 polite pool（更稳定）。可从环境变量读取。
        self._mailto = mailto or os.environ.get("OPENALEX_MAILTO")
        self._timeout = timeout

    def _base_params(self) -> dict:
        params: dict = {}
        if self._mailto:
            params["mailto"] = self._mailto
        return params

    def search(self, query: str, limit: int = 10) -> list[ReferenceEntry]:
        params = {
            **self._base_params(),
            "search": query,
            "per_page": limit,
        }
        resp = get_with_retry(_WORKS, params=params, timeout=self._timeout)
        return [self._to_entry(w) for w in resp.json().get("results", [])]

    def fetch_metadata(self, identifier: str) -> ReferenceEntry | None:
        # 支持 DOI 或 OpenAlex id。
        ident = identifier
        if "/" in identifier and not identifier.startswith("W"):
            ident = f"https://doi.org/{identifier}"
        url = f"{_WORKS}/{ident}"
        try:
            resp = get_with_retry(url, params=self._base_params(), timeout=self._timeout)
        except Exception:
            return None
        data = resp.json()
        return self._to_entry(data) if data else None

    @staticmethod
    def _to_entry(work: dict) -> ReferenceEntry:
        doi = _strip_doi(work.get("doi"))
        openalex_id = (work.get("id") or "").rsplit("/", 1)[-1]
        source_id = doi or openalex_id
        authors = [
            (a.get("author") or {}).get("display_name", "")
            for a in work.get("authorships", [])
        ]
        # Round 6：保留 PDF 全文 URL（之前被丢弃）。优先 open_access.oa_url，
        # 其次 primary_location.pdf_url——前者更稳定（OpenAlex 已聚合 OA 来源）。
        pdf_url = ""
        oa = work.get("open_access") or {}
        if isinstance(oa, dict):
            pdf_url = (oa.get("oa_url") or "").strip()
        if not pdf_url:
            primary = work.get("primary_location") or {}
            if isinstance(primary, dict):
                pdf_url = (primary.get("pdf_url") or "").strip()
        return ReferenceEntry(
            id=f"openalex:{openalex_id}" if openalex_id else f"doi:{doi}",
            title=work.get("display_name") or work.get("title") or "",
            authors=[a for a in authors if a],
            year=work.get("publication_year"),
            source_id=source_id,
            source="openalex",
            abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
            pdf_url=pdf_url,
        )
