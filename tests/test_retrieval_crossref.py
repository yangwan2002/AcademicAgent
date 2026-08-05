"""A2c-1 wave 2 / Task 3：CrossrefVerifyProvider（stub HTTP，离线）。"""

from __future__ import annotations

import pytest

from paper_agent.providers.retrieval.base import RetrievalNotFound, RetrievalUnavailable
from paper_agent.providers.retrieval.capabilities import (
    BibliographicQuery,
    FetchStatus,
    VerifyProvider,
)
from paper_agent.providers.retrieval.crossref import CrossrefVerifyProvider


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


_WORK_MSG = {
    "message": {
        "DOI": "10.1145/3292500.3330701",
        "title": ["Attention Is All You Need"],
        "author": [{"given": "Ashish", "family": "Vaswani"}],
        "issued": {"date-parts": [[2017, 6, 12]]},
        "abstract": "<jats:p>We propose the Transformer.</jats:p>",
        "type": "journal-article",
        "ISSN": ["1234-5678"],
    }
}


def _provider(http_get):
    return CrossrefVerifyProvider(http_get=http_get)


# ---------------------------------------------------------------- 协议一致性

def test_conforms_to_verify_provider():
    assert isinstance(CrossrefVerifyProvider(http_get=lambda *a, **k: _Resp({})), VerifyProvider)


# ---------------------------------------------------------------- fetch_metadata

def test_fetch_found():
    p = _provider(lambda *a, **k: _Resp(_WORK_MSG))
    r = p.fetch_metadata("doi", "10.1145/3292500.3330701")
    assert r.status is FetchStatus.FOUND
    assert r.candidate.source == "crossref"
    assert r.candidate.work_identifiers["doi"] == "10.1145/3292500.3330701"
    assert r.candidate.title == "Attention Is All You Need"
    assert r.candidate.year == 2017


def test_fetch_404_is_not_found_at_source():
    def _get(*a, **k):
        raise RetrievalNotFound("HTTP 404")
    r = _provider(_get).fetch_metadata("doi", "10.9/missing")
    assert r.status is FetchStatus.NOT_FOUND_AT_SOURCE
    assert r.candidate is None


def test_fetch_unavailable_is_provider_error():
    def _get(*a, **k):
        raise RetrievalUnavailable("timeout")
    r = _provider(_get).fetch_metadata("doi", "10.1/x")
    assert r.status is FetchStatus.PROVIDER_ERROR


def test_fetch_non_doi_type_not_found_not_faked():
    # Crossref 不核验非 DOI 类型 → 明确 NOT_FOUND_AT_SOURCE，不假装成功、不发请求
    called = []
    p = _provider(lambda *a, **k: called.append(1) or _Resp(_WORK_MSG))
    r = p.fetch_metadata("arxiv", "1706.03762")
    assert r.status is FetchStatus.NOT_FOUND_AT_SOURCE
    assert called == []


def test_fetch_empty_doi():
    p = _provider(lambda *a, **k: _Resp(_WORK_MSG))
    assert p.fetch_metadata("doi", "").status is FetchStatus.NOT_FOUND_AT_SOURCE


# ---------------------------------------------------------------- search_bibliographic

def test_bibliographic_hits():
    payload = {"message": {"items": [_WORK_MSG["message"]]}}
    p = _provider(lambda *a, **k: _Resp(payload))
    out = p.search_bibliographic(BibliographicQuery(title="Attention Is All You Need", year=2017))
    assert len(out) == 1 and out[0].work_identifiers["doi"].startswith("10.1145")


def test_bibliographic_no_match_returns_empty():
    # 无匹配（空 items 或 404）→ 空列表 = UNRESOLVED_NO_MATCH，不产生 not_found
    p = _provider(lambda *a, **k: _Resp({"message": {"items": []}}))
    assert p.search_bibliographic(BibliographicQuery(title="nonexistent paper xyz")) == []

    def _get_404(*a, **k):
        raise RetrievalNotFound("404")
    assert _provider(_get_404).search_bibliographic(BibliographicQuery(title="x")) == []


def test_bibliographic_unavailable_propagates():
    # provider 不可用绝不伪装成「没有匹配」→ 上抛
    def _get(*a, **k):
        raise RetrievalUnavailable("429")
    with pytest.raises(RetrievalUnavailable):
        _provider(_get).search_bibliographic(BibliographicQuery(title="x"))


def test_bibliographic_empty_query_returns_empty():
    p = _provider(lambda *a, **k: _Resp({"message": {"items": []}}))
    assert p.search_bibliographic(BibliographicQuery(title="")) == []
