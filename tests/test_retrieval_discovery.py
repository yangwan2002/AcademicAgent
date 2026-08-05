"""A2c-5 / Task 2：Discovery 契约 + IdentifierResolution + 隔离（stub）。"""

from __future__ import annotations

import pytest

from paper_agent.literature.retrieval.cross_verify import CrossVerifier, VerificationCache, VerifyBudget
from paper_agent.literature.retrieval.discovery import (
    DiscoveryStats,
    UnresolvedLead,
    discovery_enabled_precondition,
    resolve_identifier,
    tally,
)
from paper_agent.literature.retrieval.registry import ProviderRegistry, named
from paper_agent.providers.retrieval.capabilities import DiscoveryLead, FetchResult, FetchStatus
from paper_agent.workspace.models import RetrievalCandidate


class _VerifyBib:
    """按书目反查返回预置候选（可控相似/命中）。"""

    def __init__(self, bib_results, *, fetch_ok=True):
        self._bib = bib_results
        self._fetch_ok = fetch_ok

    def fetch_metadata(self, identifier_type, identifier):
        if self._fetch_ok:
            return FetchResult(FetchStatus.FOUND, candidate=RetrievalCandidate(
                id="v", title="", authors=[], year=None, source_id=identifier, source="crossref"))
        return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE)

    def search_bibliographic(self, query, *, limit=5):
        return list(self._bib)


def _registry(bib_results, *, fetch_ok=True):
    return ProviderRegistry(verify_sources=(named("crossref", _VerifyBib(bib_results, fetch_ok=fetch_ok)),))


def _cv(registry):
    return CrossVerifier(registry.verify_pairs())


# ---------------------------------------------------------------- 启用前置

def test_precondition_all_four():
    assert discovery_enabled_precondition(authoritative_tried=True, has_gap=True, enabled=True, has_provider=True)
    assert not discovery_enabled_precondition(authoritative_tried=False, has_gap=True, enabled=True, has_provider=True)
    assert not discovery_enabled_precondition(authoritative_tried=True, has_gap=False, enabled=True, has_provider=True)
    assert not discovery_enabled_precondition(authoritative_tried=True, has_gap=True, enabled=False, has_provider=True)


def test_precondition_enabled_no_provider_warns():
    with pytest.warns(RuntimeWarning):
        ok = discovery_enabled_precondition(authoritative_tried=True, has_gap=True, enabled=True, has_provider=False)
    assert ok is False


# ---------------------------------------------------------------- resolve

def _lead(title="Deep Learning for Medical Diagnosis"):
    return DiscoveryLead(title=title, authors=("A",), year=2022, url="https://x", source="web_search")


def test_resolve_success():
    match = RetrievalCandidate(id="m", title="Deep Learning for Medical Diagnosis", authors=["A"],
                               year=2022, source_id="10.1/x", source="crossref",
                               work_identifiers={"doi": "10.1/x"})
    reg = _registry([match])
    out = resolve_identifier(_lead(), registry=reg, cross_verifier=_cv(reg),
                             budget=VerifyBudget(), cache=VerificationCache())
    assert out.kind == "resolved"
    assert out.verification_assessment.meets_strict_threshold


def test_resolve_no_match_is_not_notfound():
    # 反查返回标题完全不同 → 相似度不足 → UNRESOLVED_NO_MATCH（非 not_found）
    other = RetrievalCandidate(id="m", title="Something Entirely Unrelated About Birds", authors=[],
                               year=2000, source_id="10.9/z", source="crossref",
                               work_identifiers={"doi": "10.9/z"})
    reg = _registry([other])
    out = resolve_identifier(_lead(), registry=reg, cross_verifier=_cv(reg),
                             budget=VerifyBudget(), cache=VerificationCache())
    assert out.kind == "unresolved_no_match"
    assert isinstance(out.lead, UnresolvedLead)


def test_resolve_empty_bib_is_no_match():
    reg = _registry([])
    out = resolve_identifier(_lead(), registry=reg, cross_verifier=_cv(reg),
                             budget=VerifyBudget(), cache=VerificationCache())
    assert out.kind == "unresolved_no_match"


def test_resolve_suspected_nonexistent_on_exact_miss():
    # 书目反查命中相似候选（取得 identifier），但精确核验未命中 → 疑似不存在（非 no_match）
    match = RetrievalCandidate(id="m", title="Deep Learning for Medical Diagnosis", authors=["A"],
                               year=2022, source_id="10.1/x", source="crossref",
                               work_identifiers={"doi": "10.1/x"})
    reg = _registry([match], fetch_ok=False)   # crossref exact fetch → NOT_FOUND
    out = resolve_identifier(_lead(), registry=reg, cross_verifier=_cv(reg),
                             budget=VerifyBudget(), cache=VerificationCache())
    assert out.kind == "suspected_nonexistent"


def test_tally_four_counts():
    from paper_agent.literature.retrieval.discovery import ResolutionOutcome
    outs = [
        ResolutionOutcome("resolved"),
        ResolutionOutcome("unresolved_no_match"),
        ResolutionOutcome("unresolved_no_match"),
        ResolutionOutcome("suspected_nonexistent"),
    ]
    stats = tally(outs, discovered=4)
    assert stats == DiscoveryStats(discovered=4, resolved=1, unresolved_no_match=2, suspected_nonexistent=1)
