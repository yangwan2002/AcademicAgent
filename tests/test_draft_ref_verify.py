"""初稿文献核验（B-9）：著录串解析 + 三态判定 + 措辞 + 缓存。

最要紧的性质是**不误报**：「Crossref 查不到」必须区别于「文献是假的」。
中文文献、书籍、学位论文大量不在 Crossref，把它们报成造假会让用户学会
无视整个提示，连真正编造的文献一起漏掉。
"""

from __future__ import annotations

import pytest

from paper_agent.literature.draft_refs import (
    DraftRefStatus,
    cache_path_for,
    format_report,
    load_cache,
    parse_citation,
    save_cache,
    verify_draft_refs,
    verify_one,
)
from paper_agent.providers.retrieval.base import RetrievalUnavailable
from paper_agent.session.facts import KnownRef
from paper_agent.workspace.models import RetrievalCandidate


def _cand(title: str, doi: str = "10.1/x") -> RetrievalCandidate:
    return RetrievalCandidate(
        id=f"doi:{doi}", title=title, authors=["A"], year=2021,
        source="crossref", source_id=doi, work_identifiers={"doi": doi},
    )


class _Stub:
    """可控的 Crossref 替身。`by_title` 命中则返回候选，否则返回空。"""

    def __init__(self, hits=None, *, raise_unavailable=False, raise_other=False):
        self.hits = hits or {}
        self.raise_unavailable = raise_unavailable
        self.raise_other = raise_other
        self.calls = 0

    def search_bibliographic(self, query, *, limit=5):
        self.calls += 1
        if self.raise_unavailable:
            raise RetrievalUnavailable("crossref down")
        if self.raise_other:
            raise ValueError("bad json")
        for key, cands in self.hits.items():
            if key.lower() in (query.title or "").lower():
                return cands[:limit]
        return []


# ---- 著录串解析 ----

@pytest.mark.parametrize("raw,title,year", [
    ("Zhang X. Visual SLAM: current trends. 2021.",
     "Visual SLAM: current trends", 2021),
    ("[1] Li Y. Multi-Robot Collaborative SLAM. 2022.",
     "Multi-Robot Collaborative SLAM", 2022),
    ("Skorobogatov G, Barrado C, Salamí E. Multiple UAV Systems: A Survey[J]. "
     "Unmanned Systems, 2019, 8(2): 149-169.",
     "Multiple UAV Systems: A Survey", 2019),
])
def test_parse_extracts_title_and_year(raw, title, year):
    p = parse_citation(raw)
    assert p.year == year
    assert title.lower() in p.title.lower(), f"题名拆歪了：{p.title!r}"


def test_parse_finds_authors():
    p = parse_citation("Zhang X, Li Y. Some Paper Title Here. 2021.")
    assert p.authors and "Zhang" in p.authors[0]


def test_parse_drops_et_al():
    p = parse_citation("Tourani A, Bavle H, 等. Visual SLAM Trends. 2022.")
    assert not any("等" in a for a in p.authors)


def test_parse_empty_is_unusable():
    assert not parse_citation("").usable
    assert not parse_citation("[1]").usable


def test_parse_too_short_title_unusable():
    """题名太短拿去反查只会得到噪声，宁可判 UNCHECKED。"""
    assert not parse_citation("A. Bc. 2020.").usable


# ---- 三态判定 ----

def test_matched_when_title_agrees():
    stub = _Stub({"Visual SLAM": [_cand("Visual SLAM: Current Trends")]})
    r = verify_one("1", "Zhang X. Visual SLAM: current trends. 2021.", stub)
    assert r.status is DraftRefStatus.MATCHED
    assert r.matched_doi == "10.1/x"


def test_not_in_source_when_no_candidates():
    """查不到 → NOT_IN_SOURCE，且**不是** MISMATCH，也不叫「虚假」。"""
    r = verify_one("2", "Nonexistent Q. A Totally Fabricated Paper. 2023.", _Stub({}))
    assert r.status is DraftRefStatus.NOT_IN_SOURCE
    assert "虚假" not in r.detail and "不存在" not in r.detail


def test_irrelevant_hit_is_not_in_source_not_mismatch():
    """Crossref 模糊检索几乎总会返回东西，不相干的结果必须按「没找到」处理。

    实测：编造的「A Totally Fabricated Paper About Quantum Badgers」会拿回
    「What "X Does Not Exist" Says About We Who Do Exist」。若按「有候选就算
    MISMATCH」判，用户会被要求去核对一个毫不相关的题名。
    """
    stub = _Stub({"Quantum Badgers": [_cand("What X Does Not Exist Says About Us")]})
    r = verify_one("3", "Nonexistent Q. A Totally Fabricated Paper About "
                        "Quantum Badgers That Does Not Exist. 2023.", stub)
    assert r.status is DraftRefStatus.NOT_IN_SOURCE


def test_mismatch_only_for_near_titles():
    """真正的 MISMATCH 是「像同一篇但著录有出入」，不是「毫不相干」。"""
    stub = _Stub({"Visual SLAM": [
        _cand("Visual SLAM: What Are the Current Trends and What to Expect")]})
    r = verify_one("4", "Zhang X. Visual SLAM: What Are Currently the Trend "
                        "and What We Expect Next. 2021.", stub)
    assert r.status in (DraftRefStatus.MATCHED, DraftRefStatus.MISMATCH)


def test_title_with_journal_residue_still_matches():
    """稿中题名常带刊名残留，Crossref 返回纯题名 —— 子串关系应判同一篇。"""
    stub = _Stub({"Multiple UAV": [_cand("Multiple UAV Systems: A Survey")]})
    r = verify_one("5", "Skorobogatov G. Multiple UAV Systems: A Survey[J]. "
                        "Unmanned Systems, 2019, 8(2): 149-169.", stub)
    assert r.status is DraftRefStatus.MATCHED


def test_provider_unavailable_is_unchecked_not_missing():
    """源不可用 = 「没问到」，绝不能变成「查不到」。"""
    r = verify_one("4", "Zhang X. Visual SLAM Trends. 2021.",
                   _Stub(raise_unavailable=True))
    assert r.status is DraftRefStatus.UNCHECKED


def test_unexpected_exception_is_unchecked():
    r = verify_one("5", "Zhang X. Visual SLAM Trends. 2021.",
                   _Stub(raise_other=True))
    assert r.status is DraftRefStatus.UNCHECKED


def test_unparseable_is_unchecked_without_network():
    stub = _Stub({})
    r = verify_one("6", "[7]", stub)
    assert r.status is DraftRefStatus.UNCHECKED
    assert stub.calls == 0, "拆不出题名就不该发请求"


# ---- 批量与预算 ----

def _refs(n):
    return [KnownRef(id=str(i), title=f"Author A. Paper Number {i} Title Here. 2020.")
            for i in range(1, n + 1)]


def test_budget_limit_marks_rest_unchecked():
    stub = _Stub({})
    report = verify_draft_refs(_refs(5), stub, limit=2)
    assert stub.calls == 2
    over = [r for r in report.results if "上限" in r.detail]
    assert len(over) == 3, "超预算的条目必须如实标出，不能静默截断"


def test_cache_prevents_second_call():
    stub = _Stub({"Paper Number 1": [_cand("Paper Number 1 Title Here")]})
    cache: dict = {}
    verify_draft_refs(_refs(1), stub, cache=cache)
    assert stub.calls == 1
    verify_draft_refs(_refs(1), stub, cache=cache)
    assert stub.calls == 1, "第二次应命中缓存"


def test_has_findings_false_when_all_matched():
    stub = _Stub({"Paper Number": [_cand("Paper Number 1 Title Here")]})
    report = verify_draft_refs(_refs(1), stub)
    assert report.matched and not report.has_findings


# ---- 措辞 ----

def test_report_explains_crossref_coverage_limit():
    """NOT_IN_SOURCE 一定要附局限说明，否则会被读成「这些是假的」。"""
    report = verify_draft_refs(_refs(2), _Stub({}))
    text = "\n".join(format_report(report))
    assert "未在 Crossref 找到" in text
    assert "不代表文献不存在" in text
    assert "中文期刊" in text
    assert "虚假" not in text


#: 相似度落在 [NEAR, MATCH) 区间的一对题名（实测 0.556）—— MISMATCH 的判据。
_DRAFT_TITLE = "Deep Reinforcement Learning for Robotic Grasping in Clutter"
_NEAR_TITLE = "Deep Learning Approaches to Robot Grasp Detection"


def test_report_shows_mismatch_both_titles():
    """题名相近（像同一篇但著录有出入）才判 MISMATCH，报告并列两个题名供比对。"""
    stub = _Stub({"Deep Reinforcement": [_cand(_NEAR_TITLE)]})
    report = verify_draft_refs(
        [KnownRef(id="1", title=f"Zhang X. {_DRAFT_TITLE}. 2021.")], stub)
    assert report.mismatch, "该造例应落在 MISMATCH 区间"
    text = "\n".join(format_report(report))
    assert "稿中：" in text and "查到：" in text
    assert _NEAR_TITLE[:20] in text


def test_report_empty_when_no_refs():
    assert format_report(verify_draft_refs([], _Stub({}))) == []


# ---- 缓存落盘 ----

def test_cache_roundtrip(tmp_path):
    stub = _Stub({"Paper Number 1": [_cand("Paper Number 1 Title Here")]})
    cache: dict = {}
    verify_draft_refs(_refs(1), stub, cache=cache)

    p = cache_path_for(str(tmp_path / "facts.json"))
    save_cache(p, cache)
    loaded = load_cache(p)
    assert loaded and next(iter(loaded.values())).status is DraftRefStatus.MATCHED


def test_unchecked_not_persisted(tmp_path):
    """UNCHECKED 不入盘：一次网络故障不该被固化成永久结论。"""
    cache: dict = {}
    verify_draft_refs(_refs(1), _Stub(raise_unavailable=True), cache=cache)
    p = cache_path_for(str(tmp_path / "facts.json"))
    save_cache(p, cache)
    assert load_cache(p) == {}


def test_corrupt_cache_is_empty_not_fatal(tmp_path):
    p = tmp_path / "draft_refs_cache.json"
    p.write_text("{ not json", encoding="utf-8")
    assert load_cache(str(p)) == {}


def test_missing_cache_is_empty(tmp_path):
    assert load_cache(str(tmp_path / "nope.json")) == {}
