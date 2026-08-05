"""A2c-1 wave 1 / Task 2：严格核验策略（读时投影，不改历史事件）。"""

from __future__ import annotations

from paper_agent.literature.verification import (
    STRICT_FINAL_V1_REGISTRY,
    STRICT_FINAL_V1_VERSION,
    EventKind,
    VerificationPolicy,
    VerificationStatus,
    aggregate_status,
    make_event,
    status_under_policy,
)


def _ev(kind, source, id_type, identifier, *, authority, seq=0, target=""):
    return make_event(
        kind, source=source, identifier_type=id_type, identifier=identifier,
        authority=authority, checked_at="2026-01-01T00:00:00Z", target_event_id=target, seq=seq,
    )


# ---- 核心：aggregator DOI 的 compat-final 在严格视图被降级 ----

def test_openalex_doi_compat_final_downgraded_in_strict():
    # A1 兼容路径写下的 openalex/doi final 事件
    ev = _ev(EventKind.VERIFIED, "openalex", "doi", "10.1/x", authority="final")
    # COMPAT：读存储 authority=final → VERIFIED（现状）
    assert status_under_policy([ev], VerificationPolicy.COMPAT_A1) == VerificationStatus.VERIFIED
    assert aggregate_status([ev]) == VerificationStatus.VERIFIED
    # STRICT：openalex 对 doi 只有 corroboration → 未达 VERIFIED
    assert status_under_policy([ev], VerificationPolicy.STRICT_FINAL_V1) == VerificationStatus.UNVERIFIED


def test_crossref_doi_final_in_strict():
    ev = _ev(EventKind.VERIFIED, "crossref", "doi", "10.1/x", authority="final")
    assert status_under_policy([ev], VerificationPolicy.STRICT_FINAL_V1) == VerificationStatus.VERIFIED


def test_arxiv_native_id_final_in_strict():
    ev = _ev(EventKind.VERIFIED, "arxiv", "arxiv", "1706.03762", authority="final")
    assert status_under_policy([ev], VerificationPolicy.STRICT_FINAL_V1) == VerificationStatus.VERIFIED


def test_strict_ignores_stored_authority_both_ways():
    # 即便历史误写 corroboration，严格视图按 registry 重算 → crossref/doi 仍是 final
    ev = _ev(EventKind.VERIFIED, "crossref", "doi", "10.1/x", authority="corroboration")
    assert status_under_policy([ev], VerificationPolicy.STRICT_FINAL_V1) == VerificationStatus.VERIFIED


# ---- 冲突在严格视图按 final 阻塞 ----

def test_final_conflict_blocks_in_strict():
    good = _ev(EventKind.VERIFIED, "crossref", "doi", "10.1/x", authority="final", seq=1)
    bad = _ev(EventKind.CONFLICT, "crossref", "doi", "10.1/x", authority="final", seq=2)
    assert status_under_policy([good, bad], VerificationPolicy.STRICT_FINAL_V1) == VerificationStatus.CONFLICT


def test_aggregator_conflict_not_blocking_in_strict():
    # openalex/doi 的 conflict 在严格视图只算 corroboration → 不阻塞
    good = _ev(EventKind.VERIFIED, "crossref", "doi", "10.1/x", authority="final", seq=1)
    weak = _ev(EventKind.CONFLICT, "openalex", "doi", "10.1/x", authority="final", seq=2)
    assert status_under_policy([good, weak], VerificationPolicy.STRICT_FINAL_V1) == VerificationStatus.VERIFIED


# ---- 版本可追溯 ----

def test_registry_version_stable():
    assert STRICT_FINAL_V1_REGISTRY.version == STRICT_FINAL_V1_VERSION
    assert STRICT_FINAL_V1_REGISTRY.authority_of("crossref", "doi") == "final"
    assert STRICT_FINAL_V1_REGISTRY.authority_of("openalex", "doi") == "corroboration"
    assert STRICT_FINAL_V1_REGISTRY.authority_of("web_search", "doi") == "none"


# ---- 空/未核验 ----

def test_empty_events_unverified():
    assert status_under_policy([], VerificationPolicy.STRICT_FINAL_V1) == VerificationStatus.UNVERIFIED
