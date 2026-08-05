"""Crossref registry 核验源（A2c-1 / 任务 3，design-a2c D3）。

Crossref 是 DOI 注册机构本身，对 ``doi`` 有**最终核验权**（`sources.py` 已声明
profile：VERIFY / registry / final={doi}）。它符合 A2c 的 ``VerifyProvider`` 协议：

- ``fetch_metadata("doi", doi)``：精确回查。命中 → ``FetchResult(FOUND)``；
  HTTP 404 → ``FetchResult(NOT_FOUND_AT_SOURCE)``；连接/超时/限流 →
  ``FetchResult(PROVIDER_ERROR)``。**「Crossref 没找到」不等于「论文不存在」**
  （DataCite 等注册库的 DOI 可能不在 Crossref），故用 ``NOT_FOUND_AT_SOURCE`` 而非
  「疑似不存在」。
- ``search_bibliographic``：书目文本模糊反查，供 A2c-5 Identifier_Resolution 比对。
  **无匹配返回空列表**（语义 ``UNRESOLVED_NO_MATCH``）；provider 不可用则**上抛**
  ``RetrievalUnavailable``（绝不把「没问到」伪装成「没有」）。

不进主题检索（不实现 ``search``）——Crossref 不该为满足过宽接口伪造 SEARCH 能力。

离线可测：构造时可注入 ``http_get``（默认 :func:`http_util.get_with_retry`），
测试传入 stub 即可覆盖全部分支，不发网络请求。真实连通性属 env-gated 冒烟。
"""

from __future__ import annotations

import html as _html

import os
from typing import Callable

from paper_agent.providers.retrieval.base import RetrievalNotFound, RetrievalUnavailable
from paper_agent.providers.retrieval.capabilities import (
    BibliographicQuery,
    FetchResult,
    FetchStatus,
)
from paper_agent.providers.retrieval.http_util import get_with_retry
from paper_agent.workspace.models import RetrievalCandidate

_WORKS = "https://api.crossref.org/works"


class CrossrefVerifyProvider:
    """符合 ``VerifyProvider`` 协议的 Crossref 核验源。"""

    def __init__(
        self,
        *,
        mailto: str | None = None,
        timeout: float = 15.0,
        http_get: Callable | None = None,
    ) -> None:
        # polite pool：提供 mailto 更稳定。
        self._mailto = mailto or os.environ.get("CROSSREF_MAILTO") or os.environ.get("OPENALEX_MAILTO")
        self._timeout = timeout
        self._http_get = http_get or get_with_retry

    def _base_params(self) -> dict:
        params: dict = {}
        if self._mailto:
            params["mailto"] = self._mailto
        return params

    # -- 精确回查 --

    def fetch_metadata(self, identifier_type: str, identifier: str) -> FetchResult:
        if (identifier_type or "").strip().lower() != "doi":
            # Crossref 只对 DOI 有权威；其他类型明确「本源不支持」，不假装成功。
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE,
                               detail=f"crossref 不核验标识符类型 {identifier_type!r}")
        doi = (identifier or "").strip()
        if not doi:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE, detail="空 DOI")
        url = f"{_WORKS}/{doi}"
        try:
            resp = self._http_get(url, params=self._base_params(), timeout=self._timeout)
        except RetrievalNotFound:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE, detail="HTTP 404")
        except RetrievalUnavailable as exc:
            return FetchResult(FetchStatus.PROVIDER_ERROR, detail=str(exc))
        message = (resp.json() or {}).get("message") or {}
        if not message:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE, detail="空 message")
        return FetchResult(FetchStatus.FOUND, candidate=self._to_candidate(message))

    # -- 书目模糊反查 --

    def search_bibliographic(
        self,
        query: BibliographicQuery,
        *,
        limit: int = 5,
    ) -> list[RetrievalCandidate]:
        bib = " ".join(
            part for part in (query.title, " ".join(query.authors),
                              str(query.year) if query.year else "")
            if part
        ).strip()
        if not bib:
            return []
        params = {**self._base_params(), "query.bibliographic": bib, "rows": limit}
        try:
            resp = self._http_get(_WORKS, params=params, timeout=self._timeout)
        except RetrievalNotFound:
            return []               # 无匹配 → UNRESOLVED_NO_MATCH（不产生 not_found）
        # RetrievalUnavailable 上抛：provider 不可用绝不伪装成「没有匹配」。
        items = ((resp.json() or {}).get("message") or {}).get("items") or []
        return [self._to_candidate(it) for it in items]

    # -- 解析 --

    @staticmethod
    def _to_candidate(message: dict) -> RetrievalCandidate:
        doi = (message.get("DOI") or "").strip().lower()
        title_list = message.get("title") or []
        title = _html.unescape(title_list[0]) if title_list else ""
        authors = [
            " ".join(p for p in (a.get("given", ""), a.get("family", "")) if p).strip()
            for a in (message.get("author") or [])
        ]
        year = None
        issued = (message.get("issued") or {}).get("date-parts") or []
        if issued and issued[0] and isinstance(issued[0][0], int):
            year = issued[0][0]
        work_ids: dict[str, str] = {}
        if doi:
            work_ids["doi"] = doi
        container_ids: dict[str, str] = {}
        issn = message.get("ISSN") or []
        if issn:
            container_ids["issn"] = issn[0]
        # 著录字段直接在候选上承载：转换函数只能读候选已有字段，provider 不取则
        # 下游无从映射（完整格式因此永远缺刊名/卷期页）。Crossref 的这些字段权威度最高。
        container = message.get("container-title") or []
        # Crossref 的刊名带 HTML 实体（`IEEE Robotics &amp; Automation Magazine`）。
        # 不解码会原样进参考文献表 —— 打印出来就是错的刊名。
        container_title = _html.unescape(container[0]) if container else ""
        event = message.get("event") or {}
        conference_name = _html.unescape(event.get("name") or "") if isinstance(event, dict) else ""
        return RetrievalCandidate(
            id=f"crossref:{doi}" if doi else "crossref:",
            title=title,
            authors=[a for a in authors if a],
            year=year,
            source_id=doi,
            source="crossref",
            abstract=message.get("abstract") or "",   # 可能含 JATS 标签，A2a 清洗处理
            work_identifiers=work_ids,
            container_identifiers=container_ids,
            reference_type=(message.get("type") or "").strip(),
            container_title=container_title,
            volume=(message.get("volume") or "").strip(),
            issue=(message.get("issue") or "").strip(),
            pages=(message.get("page") or "").strip(),
            article_number=(message.get("article-number") or "").strip(),
            publisher=_html.unescape((message.get("publisher") or "").strip()),
            conference_name=conference_name,
            url=(message.get("URL") or "").strip(),
        )


__all__ = ["CrossrefVerifyProvider"]
