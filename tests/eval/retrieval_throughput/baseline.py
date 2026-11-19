"""Deterministic single-item characterization of the unified production attempt.

This module deliberately uses only in-memory stubs.  Volatile fields (UUIDs,
timestamps and elapsed wall time) are excluded from the semantic snapshot.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import candidate_to_record, record_from_dict
from paper_agent.literature.quota import (
    EligibilityFilter,
    FilterField,
    FilterOperator,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
)
from paper_agent.literature.relevance import JudgeVersion, RelevanceCriteria, RelevanceJudge, RelevanceStore
from paper_agent.literature.retrieval.attempt import AttemptDeps, run_attempt
from paper_agent.literature.retrieval.batch import RetrievalBatch
from paper_agent.literature.retrieval.cross_verify import CrossVerifier, VerificationCache, VerifyBudget
from paper_agent.literature.retrieval.registry import ProviderRegistry, named
from paper_agent.literature.verification import VerificationPolicy
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.providers.retrieval.base import RetrievalUnavailable
from paper_agent.providers.retrieval.capabilities import FetchResult, FetchStatus
from paper_agent.workspace.models import RetrievalCandidate


class SearchStub:
    def __init__(self, candidates=(), *, fail: bool = False) -> None:
        self.candidates = tuple(candidates)
        self.fail = fail
        self.calls = 0

    def search(self, query, *, limit=10, filters=()):
        self.calls += 1
        if self.fail:
            raise RetrievalUnavailable("offline-search-failure")
        return list(self.candidates[:limit])


class VerifyStub:
    def __init__(self, titles: dict[tuple[str, str], str], *, missing=()) -> None:
        self.titles = dict(titles)
        self.missing = set(missing)
        self.calls: list[tuple[str, str]] = []

    def fetch_metadata(self, identifier_type: str, identifier: str) -> FetchResult:
        key = (identifier_type, identifier)
        self.calls.append(key)
        if key in self.missing:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE)
        title = self.titles.get(key, "")
        candidate = RetrievalCandidate(
            id=f"verified:{identifier_type}:{identifier}",
            title=title,
            authors=["Verifier"],
            year=2022,
            source_id=identifier,
            source="crossref" if identifier_type == "doi" else "arxiv",
            work_identifiers={identifier_type: identifier},
        )
        return FetchResult(FetchStatus.FOUND, candidate=candidate)

    def search_bibliographic(self, query, *, limit=5):
        return []


class ReviewerStub:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, **opts):
        self.calls += 1
        text = "\n".join(m.content for m in messages)
        if "Reviewer Error Study" in text:
            raise RuntimeError("offline-reviewer-failure")
        verdict = "unrelated" if "Unrelated Study" in text else "relevant"
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"review-{self.calls}",
                    name="report_relevance",
                    arguments={"verdict": verdict, "confidence": "high"},
                )
            ],
        )


def candidate(
    doi: str,
    title: str,
    *,
    year: int = 2022,
    abstract: str = "A relevant English abstract for the characterization fixture.",
    extra_work_ids: dict[str, str] | None = None,
) -> RetrievalCandidate:
    work_ids = {"doi": doi}
    work_ids.update(extra_work_ids or {})
    return RetrievalCandidate(
        id=f"candidate:{doi}:{title}",
        title=title,
        authors=["Author"],
        year=year,
        source_id=doi,
        source="openalex",
        abstract=abstract,
        language="en",
        work_identifiers=work_ids,
    )


def quota(*, year_gte: int | None = None) -> QuotaSpec:
    filters = ()
    if year_gte is not None:
        filters = (EligibilityFilter(FilterField.YEAR, FilterOperator.GTE, year_gte),)
    return QuotaSpec(QuotaKind.RETRIEVAL, QuotaScope.NEW_ONLY, eligibility_filters=filters)


CRITERIA = RelevanceCriteria(topic="legacy retrieval throughput characterization")


def batch(batch_id: str, q: QuotaSpec) -> RetrievalBatch:
    return RetrievalBatch(
        batch_id=batch_id,
        task_fingerprint=CRITERIA.fingerprint(),
        quota=q,
        verification_policy=VerificationPolicy.STRICT_FINAL_V1,
    )


def deps(
    candidates,
    *,
    ledger: ReferenceLedger | None = None,
    store: RelevanceStore | None = None,
    budget: VerifyBudget | None = None,
    fail_search: bool = False,
):
    search = SearchStub(candidates, fail=fail_search)
    reviewer = ReviewerStub()
    titles: dict[tuple[str, str], str] = {}
    for cand in candidates:
        for id_type, identifier in (cand.work_identifiers or {}).items():
            titles[(id_type, identifier.lower())] = cand.title
    missing = {("doi", "10.1000/missing")}
    verify = VerifyStub(titles, missing=missing)
    registry = ProviderRegistry(
        search_sources=(named("openalex", search),),
        verify_sources=(named("crossref", verify), named("arxiv", verify)),
    )
    attempt_deps = AttemptDeps(
        registry=registry,
        ledger=ledger or ReferenceLedger(),
        cross_verifier=CrossVerifier(registry.verify_pairs()),
        judge=RelevanceJudge(reviewer, judge_version=JudgeVersion(provider="stub", model_id="legacy")),
        relevance_store=store or RelevanceStore(),
        budget=budget or VerifyBudget(max_verify_calls=100, provider_time_budget_s=1000),
        verify_cache=VerificationCache(),
    )
    return attempt_deps, search, verify, reviewer


def _event_signature(event, seq: int) -> dict:
    return {
        "kind": event.kind.value,
        "source": event.source,
        "authority": event.authority,
        "identifier_type": event.identifier_type,
        "identifier": event.identifier,
        "seq": seq,
    }


def _item_signature(item) -> dict:
    relevance = item.relevance
    proposed = None
    if item.proposed_record_snapshot is not None:
        payload = json.loads(item.proposed_record_snapshot.canonical_record_payload)
        record = record_from_dict(payload)
        proposed = {
            "reference_id": record.reference_id,
            "title": record.title,
            "year": record.year,
            "work_identifiers": [list(x) for x in record.work_identifiers],
            "bibkey": record.bibkey,
            "numeric_aliases": list(record.numeric_aliases),
            "has_abstract": bool(record.abstract),
        }
    return {
        "candidate_title": item.candidate_snapshot.title,
        "disposition": item.disposition.value,
        "target_reference_id": item.target_reference_id,
        "rejection_reasons": list(item.rejection_reasons),
        "language": None
        if item.language_verdict is None
        else {
            "language": item.language_verdict.language_code,
            "quota_bucket": item.language_verdict.quota_bucket,
            "confidence": item.language_verdict.confidence,
        },
        "relevance": None
        if relevance is None
        else {
            "reference_id": relevance.reference_id,
            "status": relevance.status,
            "verdict": relevance.verdict,
            "confidence": relevance.confidence,
            "basis": relevance.basis,
            "error_kind": relevance.error_kind,
            "content_hash": relevance.reference_content_hash,
            "judge_key": relevance.judge_key,
        },
        "verification_events": [
            _event_signature(event, seq)
            for seq, event in enumerate(item.verification_events)
        ],
        "proposed": proposed,
    }


def _result_signature(result, *, search, verify, reviewer, budget) -> dict:
    stats = asdict(result.stats)
    return {
        "stats": stats,
        "calls": {
            "search": search.calls,
            "verify": len(verify.calls),
            "reviewer": reviewer.calls,
            "verify_budget": budget.calls_made,
        },
        "items": [_item_signature(item) for item in result.items],
    }


def _run_mixed() -> dict:
    candidates = [
        candidate("10.1000/accept", "Relevant Accepted Study"),
        candidate("10.1000/accept", "Relevant Accepted Study"),
        candidate("10.1000/old", "Old Filtered Study", year=2010),
        candidate("10.1000/missing", "Missing Verification Study"),
        candidate("10.1000/unrelated", "Unrelated Study"),
        candidate("10.1000/title-only", "No Abstract Study", abstract=""),
        candidate("10.1000/reviewer-error", "Reviewer Error Study"),
        candidate(
            "10.1000/multi",
            "Multi Identifier Study",
            extra_work_ids={"arxiv": "2401.00001"},
        ),
    ]
    q = quota(year_gte=2020)
    attempt_deps, search, verify, reviewer = deps(candidates)
    result = run_attempt("legacy mixed", CRITERIA, q, batch("legacy-mixed", q), attempt_deps)
    return _result_signature(
        result,
        search=search,
        verify=verify,
        reviewer=reviewer,
        budget=attempt_deps.budget,
    )


def _run_reuse_and_merge() -> dict:
    existing_candidate = candidate("10.1000/existing", "Existing Ledger Study")
    reuse_ledger = ReferenceLedger()
    reuse_ledger.add_record(candidate_to_record(existing_candidate))

    reuse_deps, reuse_search, reuse_verify, reuse_reviewer = deps(
        [existing_candidate], ledger=reuse_ledger
    )
    q = quota()
    reuse = run_attempt("reuse", CRITERIA, q, batch("legacy-reuse", q), reuse_deps)

    merge_ledger = ReferenceLedger()
    merge_ledger.add_record(
        candidate_to_record(candidate("10.1000/merge", "Merge Ledger Study", abstract=""))
    )
    merge_candidate = candidate(
        "10.1000/merge",
        "Merge Ledger Study",
        abstract="New abstract added by a duplicate source.",
    )
    merge_deps, merge_search, merge_verify, merge_reviewer = deps(
        [merge_candidate], ledger=merge_ledger
    )
    merge = run_attempt("merge", CRITERIA, q, batch("legacy-merge", q), merge_deps)
    return {
        "reuse": _result_signature(
            reuse,
            search=reuse_search,
            verify=reuse_verify,
            reviewer=reuse_reviewer,
            budget=reuse_deps.budget,
        ),
        "merge": _result_signature(
            merge,
            search=merge_search,
            verify=merge_verify,
            reviewer=merge_reviewer,
            budget=merge_deps.budget,
        ),
    }


def _run_budget_stop() -> dict:
    candidates = [candidate("10.1000/budget", "Budget Stopped Study")]
    stop_budget = VerifyBudget(max_verify_calls=0, provider_time_budget_s=1000)
    attempt_deps, search, verify, reviewer = deps(candidates, budget=stop_budget)
    q = quota()
    result = run_attempt("budget", CRITERIA, q, batch("legacy-budget", q), attempt_deps)
    return _result_signature(
        result, search=search, verify=verify, reviewer=reviewer, budget=attempt_deps.budget
    )


def _run_source_failure() -> dict:
    attempt_deps, search, verify, reviewer = deps([], fail_search=True)
    q = quota()
    result = run_attempt("failure", CRITERIA, q, batch("legacy-source-failure", q), attempt_deps)
    return _result_signature(
        result, search=search, verify=verify, reviewer=reviewer, budget=attempt_deps.budget
    )


def _run_cache_reuse() -> dict:
    candidates = [candidate("10.1000/cache", "Cached Relevance Study")]
    store = RelevanceStore()
    q = quota()
    first_deps, first_search, first_verify, first_reviewer = deps(candidates, store=store)
    first = run_attempt("cache", CRITERIA, q, batch("legacy-cache-1", q), first_deps)
    second_deps, second_search, second_verify, second_reviewer = deps(candidates, store=store)
    second = run_attempt("cache", CRITERIA, q, batch("legacy-cache-2", q), second_deps)
    return {
        "first": _result_signature(
            first,
            search=first_search,
            verify=first_verify,
            reviewer=first_reviewer,
            budget=first_deps.budget,
        ),
        "second": _result_signature(
            second,
            search=second_search,
            verify=second_verify,
            reviewer=second_reviewer,
            budget=second_deps.budget,
        ),
    }


def build_single_item_snapshot() -> dict:
    """Keep the historical golden format while running the unified single-item strategy."""
    return {
        "schema_version": "retrieval-throughput-legacy-characterization-v1",
        "normalization": {
            "excluded": [
                "BatchItem.item_id",
                "VerificationEvent.event_id",
                "VerificationEvent.checked_at",
                "RelevanceAssessment.assessed_at",
                "provider elapsed time",
            ]
        },
        "scenarios": {
            "mixed_pipeline": _run_mixed(),
            "reuse_and_merge": _run_reuse_and_merge(),
            "budget_stop": _run_budget_stop(),
            "source_failure": _run_source_failure(),
            "cache_reuse": _run_cache_reuse(),
        },
    }


__all__ = ["build_single_item_snapshot"]
