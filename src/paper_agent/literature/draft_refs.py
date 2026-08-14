"""核验用户初稿里手写的参考文献（B-9）。

`session.extractor.extract_refs` 从初稿抽出的条目是**一整串著录文本**
（`"Zhang X. Visual SLAM. 2021."`），既没有 DOI 也没拆开字段。而 Crossref 的
`search_bibliographic` 需要题名/作者/年份分开。本模块补这两步：

1. :func:`parse_citation` —— 正则启发式把著录串拆成 (作者, 题名, 年)。
   不用 LLM：拆解是确定性的字符串活儿，而 LLM 在这里的失败模式特别坏 ——
   它倾向于把「拆解」做成「补全」，给缺字段的条目臆造出刊名卷期，
   那正是核验要发现的问题，反而被它抹平了。
2. :func:`verify_draft_refs` —— 拿拆出的字段反查 Crossref，比对题名。

**三态，不是两态。** 这是本模块最要紧的设计约束：

- ``MATCHED``      回查命中且题名相符 → 这条文献真实存在
- ``MISMATCH``     回查命中但题名实质不符 → 元数据对不上，值得用户看一眼
- ``NOT_IN_SOURCE`` Crossref 里没有 → **不等于文献不存在**
- ``UNCHECKED``    拆不出可用字段 / 源不可用 → 没问到，绝不说成「没有」

`NOT_IN_SOURCE` 一律不得表述为「疑似虚假」。Crossref 只覆盖注册了 DOI 的作品，
中文期刊、书籍、学位论文、相当一部分会议论文根本不在里面。把「查不到」报成
「虚假」会在中文稿件上大面积误报，而误报的代价是用户学会无视整个提示 ——
连真正编造的文献一起忽略。宁可少报，也不能训练用户忽略警告。

同理，`PROVIDER_ERROR` 走 ``UNCHECKED`` 而非 ``NOT_IN_SOURCE``：
「没问到」和「问了没有」是两回事，混同就是把网络故障伪装成文献造假。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum

from paper_agent.literature import dedup as _dedup

#: 题名相符判定阈值（≥ 视为同一篇）。与 `cross_verify` 的默认口径一致。
TITLE_MATCH_THRESHOLD = 0.75

#: 「像但不是」的下界。落在 [NEAR, MATCH) 之间才值得报 MISMATCH；低于此值只是
#: Crossref 模糊检索的噪声，按「没找到」处理 —— 见 `verify_one` 里的说明。
TITLE_NEAR_THRESHOLD = 0.55

#: 反查取前几条候选比对（Crossref 按相关度排序，取多几条能容忍轻微措辞差异）。
_CANDIDATE_ROWS = 3

_YEAR = re.compile(r"(?<!\d)(1[6-9]\d{2}|20\d{2})(?!\d)")
_ET_AL = re.compile(r"[,，]?\s*(?:等|et\s*al\.?)\s*[,，.]?", re.I)
#: 著录串开头的编号残留（`[1]`、`1.`、`（1）`）—— 抽取一般已剥掉，防御性再剥一次。
_LEADING_NUM = re.compile(r"^\s*[\[（(]?\d+[\]）)]?[.、]?\s*")
#: 尾部的卷期页残留（`, 2019, 8(2): 149-169.`）在年份切分后一并丢弃。
_TRAILING_LOC = re.compile(r"[,，]\s*\d+\s*[(（]\d+[)）]\s*[:：].*$")


class DraftRefStatus(str, Enum):
    MATCHED = "matched"
    MISMATCH = "mismatch"
    NOT_IN_SOURCE = "not_in_source"
    UNCHECKED = "unchecked"


@dataclass(frozen=True)
class ParsedCitation:
    """从著录串拆出的字段。任一项可为空 —— 拆不出就是拆不出，不猜。"""

    authors: tuple[str, ...] = ()
    title: str = ""
    year: int | None = None

    @property
    def usable(self) -> bool:
        """够不够拿去反查。题名是硬要求：只有作者和年份查出来的是噪声。"""
        return len(self.title) >= 8


@dataclass(frozen=True)
class DraftRefResult:
    ref_id: str
    raw: str
    parsed: ParsedCitation
    status: DraftRefStatus
    matched_title: str = ""
    matched_doi: str = ""
    detail: str = ""


@dataclass
class DraftVerifyReport:
    results: list[DraftRefResult] = field(default_factory=list)

    def _of(self, status: DraftRefStatus) -> list[DraftRefResult]:
        return [r for r in self.results if r.status is status]

    @property
    def matched(self) -> list[DraftRefResult]:
        return self._of(DraftRefStatus.MATCHED)

    @property
    def mismatch(self) -> list[DraftRefResult]:
        return self._of(DraftRefStatus.MISMATCH)

    @property
    def not_in_source(self) -> list[DraftRefResult]:
        return self._of(DraftRefStatus.NOT_IN_SOURCE)

    @property
    def unchecked(self) -> list[DraftRefResult]:
        return self._of(DraftRefStatus.UNCHECKED)

    @property
    def has_findings(self) -> bool:
        """有没有值得跟用户说的事。全部 MATCHED 时不打扰。"""
        return bool(self.mismatch or self.not_in_source or self.unchecked)


def _split_authors(chunk: str) -> tuple[str, ...]:
    chunk = _ET_AL.sub("", chunk).strip(" ,，.")
    if not chunk:
        return ()
    parts = [p.strip() for p in re.split(r"[,，;；]|\sand\s", chunk) if p.strip()]
    # 单个作者写成 "Zhang X." 时上面切不开，直接整块作为一个作者。
    return tuple(parts) if parts else (chunk,)


def parse_citation(raw: str) -> ParsedCitation:
    """把一条著录串拆成 (作者, 题名, 年)。拆不出的字段留空,不臆造。

    启发式基于 GB/T 7714 的骨架 `作者. 题名[J]. 刊名, 年, 卷(期): 页.`：
    首个句点前是作者、其后是题名、四位数字是年份。这对英文常规著录够用；
    中文文献和非常规排布会拆歪，拆歪的结果是 `usable` 为假或反查不中，
    最终落到 UNCHECKED / NOT_IN_SOURCE —— 都不会被误报成造假。
    """
    text = _LEADING_NUM.sub("", (raw or "").strip())
    if not text:
        return ParsedCitation()

    year = None
    m_year = _YEAR.search(text)
    if m_year:
        year = int(m_year.group(1))
        # 年份及其后的卷期页对反查无用，切掉以免污染题名。
        text = text[: m_year.start()]
    text = _TRAILING_LOC.sub("", text).strip(" ,，.")

    authors: tuple[str, ...] = ()
    title = text
    # 作者段与题名以句点分隔。要求句点后还有实质内容，否则整串都是题名。
    m = re.match(r"^(.{2,120}?[.。])\s*(.+)$", text, re.S)
    if m:
        head, rest = m.group(1), m.group(2).strip()
        # 「Zhang X. Visual SLAM」里 `X.` 是缩写名而非作者段结束 —— 若首段短到
        # 只剩缩写，向后再找一个句点。
        if len(rest) >= 8:
            authors = _split_authors(head)
            title = rest

    # 题名尾部的 `[J]` / `[C]` 类型标识与残留标点。
    title = re.sub(r"\[[A-Z/]{1,5}\]\s*$", "", title.strip()).strip(" ,，.:：")
    # 题名里若还挂着刊名（`Title. Journal Name`），反查靠 Crossref 自身容错，
    # 这里不再切 —— 切错会把真题名截断，比多带一段更糟。
    return ParsedCitation(authors=authors, title=title, year=year)


def _title_similarity(a: str, b: str) -> float:
    """0..1 的题名相似度。任一为空 → 0（无从比较）。

    子串关系直接给满分：稿中题名常带刊名残留
    （`"Multiple UAV Systems: A Survey[J]. Unmanned Systems"`），
    Crossref 返回的是纯题名，此时 ratio 会被拖低但两者其实是同一篇。
    """
    na, nb = _dedup.normalize_title(a), _dedup.normalize_title(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _doi_of(candidate) -> str:
    ids = getattr(candidate, "work_identifiers", None) or {}
    if isinstance(ids, dict):
        return str(ids.get("doi") or "")
    for key, value in ids:
        if key == "doi":
            return str(value or "")
    return ""


def verify_one(ref_id: str, raw: str, provider, *, cache=None) -> DraftRefResult:
    """核验一条。异常不外溢 —— 单条失败按 UNCHECKED 计,不毁整批。"""
    from paper_agent.providers.retrieval.base import RetrievalUnavailable
    from paper_agent.providers.retrieval.capabilities import BibliographicQuery

    parsed = parse_citation(raw)
    if not parsed.usable:
        return DraftRefResult(
            ref_id=ref_id, raw=raw, parsed=parsed,
            status=DraftRefStatus.UNCHECKED,
            detail="著录格式无法拆出题名，未能反查",
        )

    key = _dedup.normalize_title(parsed.title)
    if cache is not None and key in cache:
        cached = cache[key]
        return DraftRefResult(
            ref_id=ref_id, raw=raw, parsed=parsed,
            status=cached.status, matched_title=cached.matched_title,
            matched_doi=cached.matched_doi, detail=cached.detail,
        )

    query = BibliographicQuery(
        title=parsed.title, authors=parsed.authors, year=parsed.year
    )
    try:
        candidates = provider.search_bibliographic(query, limit=_CANDIDATE_ROWS)
    except RetrievalUnavailable as exc:
        # 源不可用 → 「没问到」，不是「没有」。
        return DraftRefResult(
            ref_id=ref_id, raw=raw, parsed=parsed,
            status=DraftRefStatus.UNCHECKED,
            detail=f"核验源暂不可用（{type(exc).__name__}）",
        )
    except Exception as exc:  # noqa: BLE001 —— 解析/网络异常同样只是「没问到」
        return DraftRefResult(
            ref_id=ref_id, raw=raw, parsed=parsed,
            status=DraftRefStatus.UNCHECKED,
            detail=f"核验未完成（{type(exc).__name__}）",
        )

    # 关键：Crossref 的 `query.bibliographic` 是**模糊**检索，几乎总会返回点什么。
    # 实测编造的「A Totally Fabricated Paper About Quantum Badgers」也能拿回
    # 「What "X Does Not Exist" Says About We Who Do Exist」这种毫不相干的结果。
    # 所以「返回了候选」远不足以判 MISMATCH —— 那会把「查不到」误报成「元数据对不上」，
    # 让用户去核对一个根本不相关的题名。判据必须是相似度：
    #   ≥ MATCH   同一篇
    #   ≥ NEAR    像是同一篇但著录有出入 → 值得看一眼（MISMATCH）
    #   <  NEAR   只是模糊检索的噪声 → 等同于没找到（NOT_IN_SOURCE）
    best, best_score = None, 0.0
    for c in candidates:
        score = _title_similarity(parsed.title, getattr(c, "title", ""))
        if score > best_score:
            best, best_score = c, score

    if best is not None and best_score >= TITLE_MATCH_THRESHOLD:
        result = DraftRefResult(
            ref_id=ref_id, raw=raw, parsed=parsed,
            status=DraftRefStatus.MATCHED,
            matched_title=getattr(best, "title", ""),
            matched_doi=_doi_of(best),
        )
    elif best is not None and best_score >= TITLE_NEAR_THRESHOLD:
        result = DraftRefResult(
            ref_id=ref_id, raw=raw, parsed=parsed,
            status=DraftRefStatus.MISMATCH,
            matched_title=getattr(best, "title", ""),
            matched_doi=_doi_of(best),
            detail="反查到高度相似但不完全一致的记录，著录可能有误",
        )
    else:
        result = DraftRefResult(
            ref_id=ref_id, raw=raw, parsed=parsed,
            status=DraftRefStatus.NOT_IN_SOURCE,
            detail="未在 Crossref 找到匹配记录",
        )

    if cache is not None:
        cache[key] = result
    return result


def verify_draft_refs(refs, provider, *, cache=None, limit: int = 60) -> DraftVerifyReport:
    """批量核验初稿文献。

    ``limit`` 是本次最多发几次反查的上限（默认 60）。超出的条目计 UNCHECKED
    并说明原因 —— 不静默截断，否则用户会以为剩下的都验过了。
    """
    report = DraftVerifyReport()
    spent = 0
    for ref in refs:
        ref_id = str(getattr(ref, "id", "") or "")
        raw = str(getattr(ref, "title", "") or "")
        if spent >= limit:
            report.results.append(DraftRefResult(
                ref_id=ref_id, raw=raw, parsed=parse_citation(raw),
                status=DraftRefStatus.UNCHECKED,
                detail=f"超出本次核验条数上限（{limit}），未反查",
            ))
            continue
        before = len(cache) if cache is not None else 0
        result = verify_one(ref_id, raw, provider, cache=cache)
        # 命中缓存不计入预算（没真发请求）。
        if cache is None or len(cache) != before:
            spent += 1
        report.results.append(result)
    return report


def format_report(report: DraftVerifyReport, *, max_list: int = 5) -> list[str]:
    """把报告排成给用户看的几行。措辞是本模块的成败所在,见模块文档。"""
    if not report.results:
        return []
    lines: list[str] = []
    n = len(report.results)
    lines.append(
        f"文献核验：{n} 条中 {len(report.matched)} 条在 Crossref 得到确认。"
    )

    def _listing(items):
        shown = "、".join(f"[{r.ref_id}]" for r in items[:max_list])
        return shown + ("…" if len(items) > max_list else "")

    if report.mismatch:
        lines.append(
            f"  · {len(report.mismatch)} 条反查到的记录题名与你写的不符："
            f"{_listing(report.mismatch)}"
        )
        for r in report.mismatch[:max_list]:
            lines.append(f"      稿中：{r.parsed.title[:60]}")
            lines.append(f"      查到：{r.matched_title[:60]}")
    if report.not_in_source:
        lines.append(
            f"  · {len(report.not_in_source)} 条未在 Crossref 找到："
            f"{_listing(report.not_in_source)}"
        )
        # 这句不能省 —— 没有它，上一行就会被读成「这些是假的」。
        lines.append(
            "    Crossref 只收录注册了 DOI 的作品，中文期刊、书籍、学位论文和"
            "部分会议论文本就不在其中，查不到不代表文献不存在。"
        )
    if report.unchecked:
        lines.append(
            f"  · {len(report.unchecked)} 条未能核验（格式拆不开或源不可用）："
            f"{_listing(report.unchecked)}"
        )
    return lines


# ---------------------------------------------------------------- 缓存落盘

#: 缓存文件名（与 facts.json 同目录，按项目隔离）。
CACHE_FILENAME = "draft_refs_cache.json"


def cache_path_for(facts_path: str) -> str:
    import os
    return os.path.join(os.path.dirname(facts_path), CACHE_FILENAME)


def load_cache(path: str) -> dict:
    """读缓存。文件缺失/损坏 → 空缓存（重新核验一次，不阻塞载入）。

    只缓存**稳定结论**（MATCHED / MISMATCH / NOT_IN_SOURCE）。UNCHECKED 不入盘：
    它多半来自网络故障或格式拆不开，把它固化下来等于让一次偶发失败永久生效。
    """
    import json
    import os

    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:  # noqa: BLE001 —— 缓存不可读不该影响会话
        return {}
    out: dict = {}
    for key, d in (raw.get("entries") or {}).items():
        try:
            status = DraftRefStatus(d["status"])
        except Exception:  # noqa: BLE001 —— 旧版本/脏数据条目跳过
            continue
        if status is DraftRefStatus.UNCHECKED:
            continue
        out[key] = DraftRefResult(
            ref_id=str(d.get("ref_id", "")), raw=str(d.get("raw", "")),
            parsed=parse_citation(str(d.get("raw", ""))),
            status=status,
            matched_title=str(d.get("matched_title", "")),
            matched_doi=str(d.get("matched_doi", "")),
            detail=str(d.get("detail", "")),
        )
    return out


def save_cache(path: str, cache: dict) -> None:
    """写缓存（原子）。只落稳定结论。写失败静默 —— 缓存是优化，不是正确性依赖。"""
    import json
    import os

    entries = {
        key: {
            "ref_id": r.ref_id, "raw": r.raw, "status": r.status.value,
            "matched_title": r.matched_title, "matched_doi": r.matched_doi,
            "detail": r.detail,
        }
        for key, r in cache.items()
        if r.status is not DraftRefStatus.UNCHECKED
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "entries": entries}, fh,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "DraftRefStatus", "ParsedCitation", "DraftRefResult", "DraftVerifyReport",
    "parse_citation", "verify_one", "verify_draft_refs", "format_report",
    "TITLE_MATCH_THRESHOLD", "TITLE_NEAR_THRESHOLD", "CACHE_FILENAME",
    "cache_path_for", "load_cache", "save_cache",
]
