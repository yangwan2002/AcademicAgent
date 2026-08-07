"""OpenAlex 检索 provider（推荐的主源）。

OpenAlex 免费、无需 key、限流宽松，覆盖 2.4 亿+ 学术作品，且返回摘要
（以倒排索引形式，需重建）。提供 mailto 可进入更稳定的 polite pool。

文档：https://docs.openalex.org/  内容经改写以符合合规要求。
"""

from __future__ import annotations

import os

from paper_agent.observability.current import ambient_publish
from paper_agent.providers.retrieval.base import RetrievalNotFound, RetrievalProvider
from paper_agent.providers.retrieval.http_util import get_with_retry
from paper_agent.workspace.models import ReferenceEntry

_WORKS = "https://api.openalex.org/works"

#: 部分引住时，引住前 N 个词，其余留作自由词。
_PHRASE_WORDS = 3

#: 每级至少要拿到这么多条才采纳（不足则丢弃、退往下一级）。
_PHRASE_MIN_RESULTS = 5


def _has_cjk(text: str) -> bool:
    """是否含 CJK 字符。中文 seed 加引号实测有害（`"空地协同" 跨视角 图像匹配`
    只剩 2 条，裸词 202 条）—— OpenAlex 对 CJK 的分词与短语匹配不是一回事。"""
    return any("㐀" <= ch <= "鿿" or "豈" <= ch <= "﫿" for ch in text)


def _publish_search(strategy: str, query: str, phrase_query: str, results: int) -> None:
    """发 `PROVIDER_SEARCH`：短语优先到底有没有生效、回退了几次，在 trace 里可读。

    与 `RETRIEVAL_START/END` 同一隐私口径（design D15）：**只出长度与计数，
    不出查询原文**。
    """
    ambient_publish(
        "PROVIDER_SEARCH",
        source="openalex",
        strategy=strategy,
        results=results,
        query_len=len(query),
        phrase_len=len(phrase_query),
    )


def build_phrase_ladder(query: str) -> list[tuple[str, str]]:
    """构造「由紧到松」的查询阶梯，返回 `[(strategy, query), ...]`。

    **为什么需要**：OpenAlex `search=` 是 OR-ish 词袋 + 相关性排序，词给得越多
    匹配面越大、越向该领域高被引大热门倾斜，词序相邻信息被丢掉。实测
    `Cross-view image matching` 裸词 43 万条、前 10 只 6 条切题，而
    `"Cross-view image matching"` 只剩 220 条、10/10 切题。引号把相邻性要回来。

    **阶梯而非单一策略**：收窄到什么程度合适，取决于这个词组学界是否真在用，
    这件事静态判断不了，只能试了看命中数。实测（2026-08-06 直连）：

    | 查询 | total | 前 10 切题 |
    |---|---|---|
    | `Cross-view image matching` 裸词 | 430528 | 6/10 |
    | `"Cross-view image matching"` 整句 | 220 | **10/10** |
    | `"Air-ground image matching"` 整句 | **4** | 1/10 |
    | `cross-view image matching UAV ground vehicle` 裸词 | 9512 | 2/10 |
    | 同上引前 3 词 | 84 | **7/10** |
    | 同上整句引住 | **0** | — |

    `cross-view image matching` 是成熟术语，整句引住最优；`air-ground image
    matching` 是 planner 拼的组合，学界不这么写，整句引住只剩 4 条反而更差；
    6 词整句引住直接归零。三者无法靠词数区分 —— 故逐级退、由命中数决定。

    早期版本用 `len(tokens) > 3` 预判，结果 planner 的真实输出几乎全是 3 词
    （`Cross-view image matching` / `UAV-to-satellite image matching`），
    恰好卡在门槛下，真机 dogfood 实测 strategy 分布是 `{'bare': 2}` ——
    功能上线了却从未执行。词数与归零无因果，归零由「说法是否存在」决定，
    而那正是回退机制本就兜住的事。

    不改写的两种情形（阶梯只含裸词一级）：
    - query 已含 `"`：用户/planner 显式给了短语，尊重原样，不二次加工；
    - 含 CJK：中文 seed 加引号实测有害，见 `_has_cjk`。
    """
    tokens = query.split()
    bare = [("bare", query)]
    if '"' in query or _has_cjk(query) or len(tokens) < 2:
        # 单词查询整句引住等价于裸词（`"transformer"` 与 transformer 同义），
        # 白花一次 GET，故也直接走裸词。
        return bare

    ladder: list[tuple[str, str]] = [("phrase_exact", '"' + query + '"')]
    if len(tokens) > _PHRASE_WORDS:
        head, tail = tokens[:_PHRASE_WORDS], tokens[_PHRASE_WORDS:]
        ladder.append(("phrase_head", '"' + " ".join(head) + '" ' + " ".join(tail)))
    return ladder + bare


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
        *,
        phrase_first: bool = True,
    ) -> None:
        # 提供 mailto 进入 polite pool（更稳定）。可从环境变量读取。
        self._mailto = mailto or os.environ.get("OPENALEX_MAILTO")
        self._timeout = timeout
        # 按构造参数注入（由 factory 读 config 传入），provider 内部不读全局 config。
        self._phrase_first = phrase_first

    def _base_params(self) -> dict:
        params: dict = {}
        if self._mailto:
            params["mailto"] = self._mailto
        return params

    def _search_raw(self, query: str, limit: int) -> list[ReferenceEntry]:
        params = {
            **self._base_params(),
            "search": query,
            "per_page": limit,
        }
        resp = get_with_retry(_WORKS, params=params, timeout=self._timeout)
        return [self._to_entry(w) for w in resp.json().get("results", [])]

    def search(self, query: str, limit: int = 10) -> list[ReferenceEntry]:
        """沿 `build_phrase_ladder` 由紧到松逐级尝试，够数即采纳。

        **召回下界与改造前完全相同** —— 阶梯最后一级恒为裸词，且只有拿到足够
        结果的那一级会被采纳，不足则整批丢弃、继续往下退。最坏情况（两级都太窄）
        多两次 GET（实测单次 ≈1.6s，相对单次 attempt 的 ~84s、其中 relevance LLM
        36s + verify 44s，在噪声范围内）。

        真实分布下大多数查询在第一级就够数（一次 GET，与改造前持平），只有
        planner 拼出的生僻组合才付这个代价 —— 而那正是最该被拦下的那批。
        """
        if not self._phrase_first:
            entries = self._search_raw(query, limit)
            _publish_search("bare", query, "", len(entries))
            return entries

        ladder = build_phrase_ladder(query)
        for i, (strategy, q) in enumerate(ladder):
            entries = self._search_raw(q, limit)
            is_last = i == len(ladder) - 1
            # 最后一级（恒为裸词）无条件采纳：它就是改造前的行为，也是召回下界。
            if is_last or len(entries) >= min(limit, _PHRASE_MIN_RESULTS):
                _publish_search(
                    strategy, query, "" if strategy == "bare" else q, len(entries)
                )
                return entries
        return []  # 不可达：ladder 恒非空且末级无条件返回

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
