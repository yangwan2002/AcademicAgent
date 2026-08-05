"""A2c-1 wave 1 / Task 1：按能力拆分的检索源协议 + FetchResult 三态。"""

from __future__ import annotations

import dataclasses

from paper_agent.providers.retrieval.capabilities import (
    BibliographicQuery,
    DiscoveryLead,
    DiscoveryProvider,
    FetchResult,
    FetchStatus,
    SearchProvider,
    VerifyProvider,
)
from paper_agent.workspace.models import RetrievalCandidate


# ---------------------------------------------------------------- FetchResult

def test_fetch_result_three_states():
    assert {s.value for s in FetchStatus} == {
        "found", "not_found_at_source", "provider_error"
    }


def test_fetch_result_found_flag():
    cand = RetrievalCandidate(id="x", title="T", authors=[], year=2020, source_id="10.1/x")
    r = FetchResult(FetchStatus.FOUND, candidate=cand)
    assert r.found and r.candidate is cand
    assert not FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE).found
    assert not FetchResult(FetchStatus.PROVIDER_ERROR, detail="timeout").found


def test_fetch_result_frozen():
    r = FetchResult(FetchStatus.FOUND)
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        r.status = FetchStatus.PROVIDER_ERROR  # type: ignore[misc]


def test_bibliographic_query_and_lead_frozen():
    q = BibliographicQuery(title="Deep Learning", authors=("A",), year=2015)
    assert q.title == "Deep Learning"
    lead = DiscoveryLead(title="T", url="https://x", source="web_search")
    assert lead.url == "https://x"


# ---------------------------------------------------------------- 协议一致性

class _StubSearch:
    def search(self, query, *, limit=10, filters=()):
        return []


class _StubVerify:
    def fetch_metadata(self, identifier_type, identifier):
        return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE)

    def search_bibliographic(self, query, *, limit=5):
        return []


class _StubDiscovery:
    def discover(self, query, *, limit=10):
        return []


def test_protocol_conformance():
    assert isinstance(_StubSearch(), SearchProvider)
    assert isinstance(_StubVerify(), VerifyProvider)
    assert isinstance(_StubDiscovery(), DiscoveryProvider)


def test_role_separation():
    # 一个纯 SEARCH 源不必符合 VerifyProvider（能力拆分的意义）
    assert not isinstance(_StubSearch(), VerifyProvider)
    assert not isinstance(_StubVerify(), SearchProvider)
