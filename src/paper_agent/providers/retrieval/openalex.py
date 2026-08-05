"""OpenAlex 检索 provider（推荐的主源）。

OpenAlex 免费、无需 key、限流宽松，覆盖 2.4 亿+ 学术作品，且返回摘要
（以倒排索引形式，需重建）。提供 mailto 可进入更稳定的 polite pool。

文档：https://docs.openalex.org/  内容经改写以符合合规要求。
"""

from __future__ import annotations

import os

from paper_agent.providers.retrieval.base import RetrievalNotFound, RetrievalProvider
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
        # 契约（Req 2.7）：返回 None 仅表示查无此文；调用失败一律向上抛
        # RetrievalUnavailable，绝不用 None 掩盖失败。
        try:
            resp = get_with_retry(url, params=self._base_params(), timeout=self._timeout)
        except RetrievalNotFound:
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
        # A1：保留全部标识符（数据本就在响应里，不增加网络调用）。
        work_ids: dict[str, str] = {}
        if doi:
            work_ids["doi"] = doi
        if openalex_id:
            work_ids["openalex_work"] = openalex_id
        container_ids: dict[str, str] = {}
        primary = work.get("primary_location") or {}
        src = primary.get("source") if isinstance(primary, dict) else None
        if isinstance(src, dict):
            issn_l = src.get("issn_l")
            if issn_l:
                container_ids["issn"] = issn_l
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
        # 著录字段：期刊名在 primary_location.source.display_name，卷期页在 biblio。
        # 这些字段完整格式（GB/T 7714 的「刊名, 年, 卷(期): 页」）必需；原先未取
        # ⇒ 账本里覆盖率恒为 0 ⇒ 渲染只能退化成「作者. 标题. 年.」。
        container_title = ""
        if isinstance(src, dict):
            container_title = (src.get("display_name") or "").strip()
        publisher = ""
        if isinstance(src, dict):
            publisher = (src.get("host_organization_name") or "").strip()
        biblio = work.get("biblio") or {}
        if not isinstance(biblio, dict):
            biblio = {}
        first_page = (biblio.get("first_page") or "").strip()
        last_page = (biblio.get("last_page") or "").strip()
        pages = f"{first_page}-{last_page}" if first_page and last_page else first_page
        return ReferenceEntry(
            id=f"openalex:{openalex_id}" if openalex_id else f"doi:{doi}",
            title=work.get("display_name") or work.get("title") or "",
            authors=[a for a in authors if a],
            year=work.get("publication_year"),
            source_id=source_id,
            source="openalex",
            abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
            language=(work.get("language") or "").strip(),   # A2a 语言判定的源信号
            pdf_url=pdf_url,
            work_identifiers=work_ids,
            container_identifiers=container_ids,
            reference_type=(work.get("type") or "").strip(),
            container_title=container_title,
            volume=(biblio.get("volume") or "").strip(),
            issue=(biblio.get("issue") or "").strip(),
            pages=pages,
            publisher=publisher,
            url=(work.get("doi") or "").strip(),
        )
