"""A2e：真实源 VerifyProvider 适配 + 组合层注入 + 端到端（全离线 stub inner）。"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.providers.retrieval.base import RetrievalNotFound, RetrievalUnavailable
from paper_agent.providers.retrieval.capabilities import (
    BibliographicQuery,
    FetchStatus,
    VerifyProvider,
)
from paper_agent.providers.retrieval.verify_adapters import (
    ArxivVerifyProvider,
    OpenAlexVerifyProvider,
    SemanticScholarVerifyProvider,
)
from paper_agent.workspace.models import ReferenceEntry


class _StubInner:
    """可编程旧 RetrievalProvider：记录调用,按脚本返回/抛。"""

    def __init__(self, *, fetch=None, search_result=None):
        self._fetch = fetch                 # callable(ident) -> Entry|None|raise
        self._search_result = search_result
        self.fetch_calls: list = []
        self.search_calls: list = []

    def fetch_metadata(self, identifier):
        self.fetch_calls.append(identifier)
        if callable(self._fetch):
            return self._fetch(identifier)
        return self._fetch

    def search(self, query, limit=10):
        self.search_calls.append((query, limit))
        if isinstance(self._search_result, Exception):
            raise self._search_result
        return list(self._search_result or [])


def _entry(rid="arxiv:1", title="T"):
    return ReferenceEntry(id=rid, title=title, authors=["A"], year=2020,
                          source_id="1", source="arxiv")


# ---------------------------------------------------------------- 协议一致

def test_adapters_satisfy_verify_protocol():
    for cls in (ArxivVerifyProvider, OpenAlexVerifyProvider, SemanticScholarVerifyProvider):
        assert isinstance(cls(inner=_StubInner()), VerifyProvider)


# ---------------------------------------------------------------- 三态忠实（Req 2.7）

def test_found_maps_to_found():
    inner = _StubInner(fetch=lambda i: _entry())
    r = ArxivVerifyProvider(inner=inner).fetch_metadata("arxiv", "1706.03762")
    assert r.status is FetchStatus.FOUND and r.candidate is not None


def test_none_maps_to_not_found():
    inner = _StubInner(fetch=lambda i: None)
    r = ArxivVerifyProvider(inner=inner).fetch_metadata("arxiv", "9999.99999")
    assert r.status is FetchStatus.NOT_FOUND_AT_SOURCE


def test_retrieval_not_found_maps_to_not_found():
    def _raise(_i):
        raise RetrievalNotFound("404")

    r = ArxivVerifyProvider(inner=_StubInner(fetch=_raise)).fetch_metadata("arxiv", "x")
    assert r.status is FetchStatus.NOT_FOUND_AT_SOURCE


def test_unavailable_maps_to_provider_error_not_swallowed():
    def _raise(_i):
        raise RetrievalUnavailable("timeout")

    r = ArxivVerifyProvider(inner=_StubInner(fetch=_raise)).fetch_metadata("arxiv", "x")
    assert r.status is FetchStatus.PROVIDER_ERROR      # 绝不塌缩成 not-found
    assert "timeout" in r.detail


# ---------------------------------------------------------------- 不支持类型不发网络

def test_arxiv_does_not_query_doi():
    inner = _StubInner(fetch=lambda i: _entry())
    r = ArxivVerifyProvider(inner=inner).fetch_metadata("doi", "10.1/x")
    assert r.status is FetchStatus.NOT_FOUND_AT_SOURCE
    assert inner.fetch_calls == []                     # inner 未被调用


def test_openalex_does_not_query_arxiv():
    inner = _StubInner(fetch=lambda i: _entry())
    r = OpenAlexVerifyProvider(inner=inner).fetch_metadata("arxiv", "1706.03762")
    assert r.status is FetchStatus.NOT_FOUND_AT_SOURCE
    assert inner.fetch_calls == []


def test_openalex_queries_doi_and_openalex_work():
    inner = _StubInner(fetch=lambda i: _entry())
    ad = OpenAlexVerifyProvider(inner=inner)
    assert ad.fetch_metadata("doi", "10.1/x").status is FetchStatus.FOUND
    assert ad.fetch_metadata("openalex_work", "W123").status is FetchStatus.FOUND
    assert inner.fetch_calls == ["10.1/x", "W123"]


# ---------------------------------------------------------------- S2 前缀映射

def test_s2_prefix_mapping():
    inner = _StubInner(fetch=lambda i: _entry())
    ad = SemanticScholarVerifyProvider(inner=inner)
    ad.fetch_metadata("doi", "10.1/x")
    ad.fetch_metadata("arxiv", "1706.03762")
    ad.fetch_metadata("pmid", "12345")
    ad.fetch_metadata("semantic_scholar_paper", "abc123")
    assert inner.fetch_calls == ["DOI:10.1/x", "ARXIV:1706.03762", "PMID:12345", "abc123"]


# ---------------------------------------------------------------- search_bibliographic

def test_search_bibliographic_empty_query_returns_empty():
    inner = _StubInner(search_result=[_entry()])
    assert ArxivVerifyProvider(inner=inner).search_bibliographic(BibliographicQuery(title="")) == []
    assert inner.search_calls == []


def test_search_bibliographic_hit():
    inner = _StubInner(search_result=[_entry(title="Attention")])
    hits = ArxivVerifyProvider(inner=inner).search_bibliographic(
        BibliographicQuery(title="Attention", authors=("Vaswani",), year=2017))
    assert hits and hits[0].title == "Attention"
    assert "Attention" in inner.search_calls[0][0] and "2017" in inner.search_calls[0][0]


def test_search_bibliographic_no_match_returns_empty():
    inner = _StubInner(search_result=RetrievalNotFound("404"))
    assert OpenAlexVerifyProvider(inner=inner).search_bibliographic(
        BibliographicQuery(title="nope")) == []


def test_search_bibliographic_unavailable_propagates():
    import pytest

    inner = _StubInner(search_result=RetrievalUnavailable("429"))
    with pytest.raises(RetrievalUnavailable):
        OpenAlexVerifyProvider(inner=inner).search_bibliographic(BibliographicQuery(title="q"))


# ================================================================ CrossVerifier 端到端（离线）

from paper_agent.literature.retrieval.cross_verify import (
    CrossVerifier,
    VerificationCache,
    VerifyBudget,
)
from paper_agent.literature.verification import VerificationStatus


def _cand(work_ids, title="Deep Learning for X"):
    return ReferenceEntry(id="c:1", title=title, authors=["A"], year=2022,
                          source_id=next(iter(work_ids.values())), source="x",
                          abstract="abstract", work_identifiers=work_ids)


def test_arxiv_only_candidate_reaches_verified_via_arxiv_adapter():
    """arXiv-only（无 DOI）候选:经 arxiv 适配 final → strict VERIFIED（此前仅 crossref 时排除）。"""
    inner = _StubInner(fetch=lambda i: _entry(title="Deep Learning for X"))
    cv = CrossVerifier((("arxiv", ArxivVerifyProvider(inner=inner)),))
    a = cv.assess(_cand({"arxiv": "1706.03762"}),
                  budget=VerifyBudget(), cache=VerificationCache())
    assert a.strict_status is VerificationStatus.VERIFIED
    assert a.meets_strict_threshold is True


def test_doi_only_via_openalex_is_corroboration_not_final():
    """DOI 命中 openalex 只记 corroboration,单靠聚合库不构成 strict VERIFIED（属性 4）。"""
    inner = _StubInner(fetch=lambda i: _entry(title="Deep Learning for X"))
    cv = CrossVerifier((("openalex", OpenAlexVerifyProvider(inner=inner)),))
    a = cv.assess(_cand({"doi": "10.1/x"}),
                  budget=VerifyBudget(), cache=VerificationCache())
    assert a.meets_strict_threshold is False           # 无 registry final → 不达门槛


def test_provider_error_does_not_collapse_to_not_found():
    """inner 抛 Unavailable → PROVIDER_ERROR 事件,不误判 not-found（不 canonical_not_found）。"""
    def _raise(_i):
        raise RetrievalUnavailable("timeout")

    cv = CrossVerifier((("arxiv", ArxivVerifyProvider(inner=_StubInner(fetch=_raise))),))
    a = cv.assess(_cand({"arxiv": "1706.03762"}),
                  budget=VerifyBudget(), cache=VerificationCache())
    assert a.strict_status is not VerificationStatus.VERIFIED
    assert a.canonical_verified is False
    # 事件里应是 PROVIDER_ERROR,而非 NOT_FOUND
    kinds = [e.kind.value for e in a.events]
    assert "provider_error" in kinds and "not_found" not in kinds
