"""A1 wave 6：verify_detailed 四种 outcome + ReferenceVerifier 事件翻译。"""

from __future__ import annotations

from paper_agent.literature.verification import EventKind, VerificationStatus, aggregate_status
from paper_agent.literature.verifier import ReferenceVerifier
from paper_agent.providers.retrieval.base import (
    RetrievalNotFound,
    RetrievalProvider,
    RetrievalUnavailable,
)
from paper_agent.tools.citation import CitationVerifier
from paper_agent.workspace.models import ReferenceEntry


class _StubProvider(RetrievalProvider):
    """按 source_id 决定行为：命中 / None / 抛 NotFound / 抛 Unavailable。"""

    def __init__(self, mode: str, entry: ReferenceEntry | None = None) -> None:
        self._mode = mode
        self._entry = entry

    def search(self, query, limit=10):
        return []

    def fetch_metadata(self, identifier):
        if self._mode == "found":
            return self._entry
        if self._mode == "none":
            return None
        if self._mode == "notfound":
            raise RetrievalNotFound("404")
        if self._mode == "unavailable":
            raise RetrievalUnavailable("timeout")
        raise AssertionError(self._mode)


_ENTRY = ReferenceEntry(
    id="1", title="Attention Is All You Need", authors=["Vaswani"], year=2017,
    source_id="10.1/x", source="openalex", work_identifiers={"doi": "10.1/x"},
)


def _cv(mode, matched=None):
    return CitationVerifier(_StubProvider(mode, matched), max_retries=0, retry_backoff=0)


def test_verify_detailed_found_is_verified():
    a = _cv("found", _ENTRY).verify_detailed(_ENTRY)
    assert a.outcome == EventKind.VERIFIED and a.matched is _ENTRY


def test_verify_detailed_none_is_not_found():
    assert _cv("none").verify_detailed(_ENTRY).outcome == EventKind.NOT_FOUND


def test_verify_detailed_notfound_is_not_found():
    assert _cv("notfound").verify_detailed(_ENTRY).outcome == EventKind.NOT_FOUND


def test_verify_detailed_unavailable_is_provider_error():
    a = _cv("unavailable").verify_detailed(_ENTRY)
    assert a.outcome == EventKind.PROVIDER_ERROR and a.error_kind == "unavailable"


def test_verify_bool_preserves_old_semantics():
    # 存在 → True；查无/失败 → False（与旧 verify 一致）
    assert _cv("found", _ENTRY).verify(_ENTRY) is True
    assert _cv("none").verify(_ENTRY) is False
    assert _cv("notfound").verify(_ENTRY) is False
    assert _cv("unavailable").verify(_ENTRY) is False


def test_no_source_id_not_found():
    e = ReferenceEntry(id="1", title="T", authors=[], year=None, source_id="")
    assert _cv("found").verify_detailed(e).outcome == EventKind.NOT_FOUND


def test_reference_verifier_default_records_final():
    # 默认 require_final=False：openalex 对 DOI 成功核验记为 final → 状态 VERIFIED
    rv = ReferenceVerifier(_cv("found", _ENTRY))
    ev = rv.assess(_ENTRY)
    assert ev.kind == EventKind.VERIFIED and ev.authority == "final"
    assert ev.identifier_type == "doi" and ev.source == "openalex"
    assert aggregate_status([ev]) == VerificationStatus.VERIFIED


def test_reference_verifier_strict_downgrades_aggregator_doi():
    # require_final=True：openalex 对 DOI 只有佐证权 → corroboration → 非 VERIFIED
    rv = ReferenceVerifier(_cv("found", _ENTRY), require_final=True)
    ev = rv.assess(_ENTRY)
    assert ev.kind == EventKind.VERIFIED and ev.authority == "corroboration"
    assert aggregate_status([ev]) == VerificationStatus.UNVERIFIED


def test_reference_verifier_provider_error_event():
    rv = ReferenceVerifier(_cv("unavailable"))
    ev = rv.assess(_ENTRY)
    assert ev.kind == EventKind.PROVIDER_ERROR
    assert aggregate_status([ev]) == VerificationStatus.UNVERIFIED
