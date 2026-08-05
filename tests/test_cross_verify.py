"""A2c-1 wave 3 / Task 4：CrossVerifier + 缓存 + 预算（stub provider，离线）。"""

from __future__ import annotations

import dataclasses

from paper_agent.literature.retrieval.cross_verify import (
    CrossVerifier,
    VerificationCache,
    VerifyBudget,
)
from paper_agent.literature.verification import VerificationStatus
from paper_agent.providers.retrieval.capabilities import FetchResult, FetchStatus
from paper_agent.workspace.models import RetrievalCandidate


@dataclasses.dataclass
class _Cand:
    title: str = "Attention Is All You Need"
    work_identifiers: dict | None = None

    def __post_init__(self):
        if self.work_identifiers is None:
            self.work_identifiers = {}


class _StubVerify:
    """按 (id_type) → FetchResult 预置返回，并记录调用次数。"""

    def __init__(self, table, *, fetched_title="Attention Is All You Need"):
        self._table = table
        self._fetched_title = fetched_title
        self.calls = 0

    def fetch_metadata(self, identifier_type, identifier):
        self.calls += 1
        status = self._table.get(identifier_type, FetchStatus.NOT_FOUND_AT_SOURCE)
        cand = None
        if status is FetchStatus.FOUND:
            cand = RetrievalCandidate(
                id="x", title=self._fetched_title, authors=[], year=2017,
                source_id=identifier, source="stub",
            )
        return FetchResult(status, candidate=cand)

    def search_bibliographic(self, query, *, limit=5):
        return []


def _fixed_clock():
    t = {"v": 0.0}
    def clock():
        t["v"] += 1.0
        return t["v"]
    return clock


# ---- aggregator-only DOI 未达严格门槛；加 Crossref 达门槛 ----

def test_aggregator_only_doi_not_strict():
    cand = _Cand(work_identifiers={"doi": "10.1/x"})
    cv = CrossVerifier([("openalex", _StubVerify({"doi": FetchStatus.FOUND}))])
    a = cv.assess(cand, budget=VerifyBudget(), cache=VerificationCache())
    # openalex 对 doi 只有 corroboration → 严格视图未 VERIFIED
    assert a.strict_status is VerificationStatus.UNVERIFIED
    assert a.meets_strict_threshold is False


def test_crossref_doi_meets_strict():
    cand = _Cand(work_identifiers={"doi": "10.1/x"})
    cv = CrossVerifier([("crossref", _StubVerify({"doi": FetchStatus.FOUND}))])
    a = cv.assess(cand, budget=VerifyBudget(), cache=VerificationCache())
    assert a.strict_status is VerificationStatus.VERIFIED
    assert a.meets_strict_threshold is True
    assert a.canonical_verified is True
    assert a.authority_registry_version


def test_arxiv_native_final():
    cand = _Cand(work_identifiers={"arxiv": "1706.03762"})
    cv = CrossVerifier([("arxiv", _StubVerify({"arxiv": FetchStatus.FOUND}))])
    a = cv.assess(cand, budget=VerifyBudget(), cache=VerificationCache())
    assert a.strict_status is VerificationStatus.VERIFIED


# ---- 标题不符 → CONFLICT ----

def test_title_mismatch_is_conflict():
    cand = _Cand(title="Attention Is All You Need", work_identifiers={"doi": "10.1/x"})
    cv = CrossVerifier([("crossref", _StubVerify({"doi": FetchStatus.FOUND},
                                                 fetched_title="A Completely Different Paper About Frogs"))])
    a = cv.assess(cand, budget=VerifyBudget(), cache=VerificationCache())
    assert a.strict_status is VerificationStatus.CONFLICT
    assert a.meets_strict_threshold is False


# ---- canonical not-found 不可接受；次要 not-found 记 issue ----

def test_canonical_not_found_not_acceptable():
    cand = _Cand(work_identifiers={"doi": "10.1/missing"})
    cv = CrossVerifier([("crossref", _StubVerify({"doi": FetchStatus.NOT_FOUND_AT_SOURCE}))])
    a = cv.assess(cand, budget=VerifyBudget(), cache=VerificationCache())
    assert a.meets_strict_threshold is False
    assert a.canonical_verified is False


def test_secondary_not_found_recorded_as_issue():
    # canonical=doi 由 crossref VERIFIED；次要 arxiv not-found → 记 issue 不静默删
    cand = _Cand(work_identifiers={"doi": "10.1/x", "arxiv": "9999.99999"})
    cv = CrossVerifier([
        ("crossref", _StubVerify({"doi": FetchStatus.FOUND})),
        ("arxiv", _StubVerify({"arxiv": FetchStatus.NOT_FOUND_AT_SOURCE})),
    ])
    a = cv.assess(cand, budget=VerifyBudget(), cache=VerificationCache())
    assert a.meets_strict_threshold is True             # canonical doi 已 strict verified
    assert any(i.startswith("secondary_not_found:arxiv") for i in a.issues)


# ---- 预算触顶 ----

def test_budget_exhausted_stops():
    cand = _Cand(work_identifiers={"doi": "10.1/x", "arxiv": "1706.03762"})
    stub_c = _StubVerify({"doi": FetchStatus.FOUND})
    stub_a = _StubVerify({"arxiv": FetchStatus.FOUND})
    cv = CrossVerifier([("crossref", stub_c), ("arxiv", stub_a)])
    budget = VerifyBudget(max_verify_calls=1)
    a = cv.assess(cand, budget=budget, cache=VerificationCache())
    assert budget.calls_made == 1
    assert "budget_exhausted" in a.issues


# ---- 缓存 ----

def test_cache_avoids_duplicate_call():
    cand = _Cand(work_identifiers={"doi": "10.1/x"})
    stub = _StubVerify({"doi": FetchStatus.FOUND})
    cv = CrossVerifier([("crossref", stub)], clock=_fixed_clock())
    cache = VerificationCache()
    cv.assess(cand, budget=VerifyBudget(), cache=cache)
    cv.assess(cand, budget=VerifyBudget(), cache=cache)
    assert stub.calls == 1                    # 第二次命中缓存，不重复调用


def test_cache_error_short_ttl_allows_retry():
    cand = _Cand(work_identifiers={"doi": "10.1/x"})
    stub = _StubVerify({"doi": FetchStatus.PROVIDER_ERROR})
    # clock 递增，error_ttl=0 → 立即过期，第二次重试
    cv = CrossVerifier([("crossref", stub)], clock=_fixed_clock())
    cache = VerificationCache(error_ttl_s=0.0)
    cv.assess(cand, budget=VerifyBudget(), cache=cache)
    cv.assess(cand, budget=VerifyBudget(), cache=cache)
    assert stub.calls == 2
