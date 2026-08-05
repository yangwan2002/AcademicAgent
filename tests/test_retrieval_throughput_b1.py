"""Task 5 B1 legacy-order batching and active-key wiring tests."""

from __future__ import annotations

import re

from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import record_from_dict
from paper_agent.literature.preflight import (
    assess_availability,
    ensure_relevance_active,
)
from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
    EligibilityFilter,
    FilterField,
    FilterOperator,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
)
from paper_agent.literature.relevance import (
    JudgeVersion,
    RelevanceCriteria,
    RelevanceJudge,
    RelevanceStore,
)
from paper_agent.literature.relevance_batch import (
    BatchRelevanceJudge,
    make_batch_judge_version,
)
from paper_agent.literature.relevance_runtime import (
    ActiveRelevanceRuntime,
    RelevanceTextPolicy,
)
from paper_agent.literature.retrieval.attempt import (
    AttemptDeps,
    RelevanceAcceptancePolicy,
    run_attempt,
)
from paper_agent.literature.retrieval.batch import (
    BatchDisposition,
    RetrievalBatch,
)
from paper_agent.literature.retrieval.budget import BudgetLedger, RetrievalLimits
from paper_agent.literature.retrieval.cross_verify import (
    CrossVerifier,
    VerificationCache,
)
from paper_agent.literature.retrieval.loop import (
    RetrievalRequest,
    run_retrieval_loop,
)
from paper_agent.literature.retrieval.query_planner import DeterministicQueryPlanner
from paper_agent.literature.retrieval.registry import ProviderRegistry, named
from paper_agent.literature.retrieval.window_packer import (
    BatchInputBudget,
    WindowLimits,
)
from paper_agent.literature.verification import VerificationPolicy
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.providers.retrieval.capabilities import FetchResult, FetchStatus
from paper_agent.workspace.models import RetrievalCandidate


CRITERIA = RelevanceCriteria(topic="academic writing agents")


class _Search:
    def __init__(self, candidates):
        self.candidates = list(candidates)
        self.calls = 0

    def search(self, query, *, limit=10, filters=()):
        self.calls += 1
        return self.candidates[:limit]


class _Verify:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls = 0

    def fetch_metadata(self, identifier_type, identifier):
        self.calls += 1
        if identifier in self.missing:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE)
        candidate = RetrievalCandidate(
            id=f"verified:{identifier}",
            title="",
            authors=[],
            year=None,
            source_id=identifier,
            source="crossref",
        )
        return FetchResult(FetchStatus.FOUND, candidate=candidate)

    def search_bibliographic(self, query, *, limit=5):
        return []


class _BatchReviewer:
    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}
        self.calls = 0
        self.keys_per_call = []

    def complete(self, messages, **opts):
        self.calls += 1
        user = "\n".join(message.content for message in messages if message.role == "user")
        keys = re.findall(r"candidate_key=(c\d+)", user)
        self.keys_per_call.append(tuple(keys))
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"batch-{self.calls}",
                    name="report_relevance_batch",
                    arguments={
                        "results": [
                            {
                                "candidate_key": key,
                                "verdict": self.verdicts.get(key, "relevant"),
                                "confidence": "high",
                            }
                            for key in reversed(keys)
                        ]
                    },
                )
            ],
        )


class _LegacyReviewer:
    def complete(self, messages, **opts):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="legacy",
                    name="report_relevance",
                    arguments={"verdict": "relevant", "confidence": "high"},
                )
            ],
        )


def _candidate(index, *, doi=None, year=2022):
    identifier = doi or f"10.1000/b1-{index}"
    return RetrievalCandidate(
        id=f"candidate:{index}",
        title=f"Academic Agent Study {index}",
        authors=["Author"],
        year=year,
        source_id=identifier,
        source="openalex",
        abstract=f"Evidence about academic writing agents, study {index}.",
        language="en",
        work_identifiers={"doi": identifier},
    )


def _limits(**overrides):
    values = dict(
        max_iterations=4,
        max_provider_calls=100,
        max_search_calls=20,
        max_verify_calls=50,
        max_discovery_calls=10,
        max_llm_calls=20,
        max_candidates=100,
        provider_time_budget_s=100,
        max_no_progress_rounds=2,
    )
    values.update(overrides)
    return RetrievalLimits(**values)


def _runtime(reviewer, *, batch_size=8):
    policy = RelevanceTextPolicy(version="b1-active-text-v1")
    budget = BatchInputBudget(
        max_input_chars=40_000,
        max_output_chars=10_000,
        max_output_tokens=4_000,
    )
    evaluator = BatchRelevanceJudge(
        reviewer,
        judge_version=make_batch_judge_version(
            provider="stub",
            model_id="batch",
            text_policy=policy,
            input_budget=budget,
        ),
        text_policy=policy,
        input_budget=budget,
        retry_limit=0,
        now_iso=lambda: "2026-07-31T00:00:00+00:00",
    )
    return ActiveRelevanceRuntime(
        evaluator=evaluator,
        text_policy=policy,
        window_limits=WindowLimits(batch_size=batch_size, input_budget=budget),
    )


def _deps(candidates, reviewer, *, missing=(), store=None, limits=None):
    search = _Search(candidates)
    verify = _Verify(missing)
    registry = ProviderRegistry(
        search_sources=(named("openalex", search),),
        verify_sources=(named("crossref", verify),),
    )
    limits = limits or _limits()
    ledger_budget = BudgetLedger(limits, clock=lambda: 0.0)
    deps = AttemptDeps(
        registry=registry,
        ledger=ReferenceLedger(),
        cross_verifier=CrossVerifier(registry.verify_pairs()),
        judge=RelevanceJudge(
            _LegacyReviewer(), judge_version=JudgeVersion(model_id="legacy")
        ),
        relevance_store=store or RelevanceStore(),
        budget=ledger_budget.verify_budget_view(),
        verify_cache=VerificationCache(),
        acceptance_policy=RelevanceAcceptancePolicy(),
        active_relevance_runtime=_runtime(reviewer),
        budget_ledger=ledger_budget,
    )
    return deps, ledger_budget, search, verify


def _quota(filters=(), total_min=None):
    constraints = ()
    if total_min is not None:
        constraints = (
            CountConstraint(CountDimension.TOTAL, CountOperator.MIN, total_min),
        )
    return QuotaSpec(
        QuotaKind.RETRIEVAL,
        QuotaScope.NEW_ONLY,
        eligibility_filters=tuple(filters),
        count_constraints=constraints,
    )


def _batch(quota=None):
    quota = quota or _quota()
    return RetrievalBatch(
        batch_id="b1-test",
        task_fingerprint=CRITERIA.fingerprint(),
        quota=quota,
        verification_policy=VerificationPolicy.STRICT_FINAL_V1,
    )


def test_all_off_dispatches_directly_to_frozen_legacy(monkeypatch):
    import paper_agent.literature.retrieval.attempt as module

    sentinel = object()
    called = []

    def fake(*args, **kwargs):
        called.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(module, "_run_attempt_legacy", fake)
    deps = object.__new__(AttemptDeps)
    deps.active_relevance_runtime = None
    assert module.run_attempt("q", CRITERIA, _quota(), object(), deps) is sentinel
    assert len(called) == 1


def test_b1_batches_verified_candidates_into_one_physical_llm_call():
    reviewer = _BatchReviewer()
    candidates = [_candidate(i) for i in range(4)]
    deps, ledger, _search, verify = _deps(candidates, reviewer)
    result = run_attempt("agents", CRITERIA, _quota(), _batch(), deps)
    assert [item.disposition for item in result.items] == [
        BatchDisposition.ACCEPT_NEW
    ] * 4
    assert reviewer.calls == 1
    assert reviewer.keys_per_call == [("c0", "c1", "c2", "c3")]
    assert verify.calls == 4
    assert ledger.llm_calls == 1
    assert result.throughput.relevance_items == 4
    assert result.throughput.relevance_windows == 1
    assert result.throughput.relevance_llm_calls == 1


def test_deterministic_filter_reject_does_not_fragment_survivor_window_or_order():
    reviewer = _BatchReviewer()
    candidates = [_candidate(0), _candidate(1, year=2010), _candidate(2)]
    deps, _ledger, _search, verify = _deps(candidates, reviewer)
    year_filter = EligibilityFilter(FilterField.YEAR, FilterOperator.GTE, 2020)
    result = run_attempt(
        "agents",
        CRITERIA,
        _quota((year_filter,)),
        _batch(),
        deps,
    )
    assert [item.disposition for item in result.items] == [
        BatchDisposition.ACCEPT_NEW,
        BatchDisposition.REJECT,
        BatchDisposition.ACCEPT_NEW,
    ]
    assert result.items[1].rejection_reasons == ("filter",)
    assert reviewer.keys_per_call == [("c0", "c2")]
    assert verify.calls == 2


def test_verify_reject_never_enters_batch_window():
    reviewer = _BatchReviewer()
    candidates = [_candidate(0), _candidate(1), _candidate(2)]
    deps, _ledger, _search, _verify = _deps(
        candidates, reviewer, missing={"10.1000/b1-1"}
    )
    result = run_attempt("agents", CRITERIA, _quota(), _batch(), deps)
    assert result.items[1].disposition is BatchDisposition.REJECT
    assert result.items[1].rejection_reasons == ("verify:unverified",)
    assert reviewer.keys_per_call == [("c0", "c2")]
    assert result.throughput.relevance_items == 2


def test_same_work_identity_is_reduced_before_exact_duplicate_classification():
    reviewer = _BatchReviewer()
    candidates = [_candidate(0, doi="10.1000/same"), _candidate(1, doi="10.1000/same")]
    deps, _ledger, _search, verify = _deps(candidates, reviewer)
    result = run_attempt("agents", CRITERIA, _quota(), _batch(), deps)
    assert result.items[0].disposition is BatchDisposition.ACCEPT_NEW
    assert result.items[1].disposition is BatchDisposition.REJECT
    assert result.items[1].rejection_reasons == ("batch_duplicate",)
    assert reviewer.calls == 1
    assert verify.calls == 1


def test_active_success_cache_skips_second_batch_call():
    reviewer = _BatchReviewer()
    store = RelevanceStore()
    candidates = [_candidate(0), _candidate(1)]
    deps, _ledger, _search, _verify = _deps(candidates, reviewer, store=store)
    first = run_attempt("agents", CRITERIA, _quota(), _batch(), deps)
    assert first.throughput.relevance_llm_calls == 1

    deps2, ledger2, _search2, _verify2 = _deps(candidates, reviewer, store=store)
    second = run_attempt("agents", CRITERIA, _quota(), _batch(), deps2)
    assert reviewer.calls == 1
    assert ledger2.llm_calls == 0
    assert second.throughput.relevance_items == 0
    assert all(item.disposition is BatchDisposition.ACCEPT_NEW for item in second.items)


def test_loop_does_not_double_charge_b1_physical_llm_calls():
    reviewer = _BatchReviewer()
    candidates = [_candidate(0), _candidate(1)]
    limits = _limits()
    deps, ledger, _search, _verify = _deps(candidates, reviewer, limits=limits)
    result = run_retrieval_loop(
        RetrievalRequest(
            quota=_quota(total_min=2),
            criteria=CRITERIA,
            initial_query="agents",
            task_fingerprint=CRITERIA.fingerprint(),
        ),
        deps=deps,
        limits=limits,
        planner=DeterministicQueryPlanner(),
        budget_ledger=ledger,
    )
    assert reviewer.calls == 1
    assert ledger.llm_calls == 1
    assert result.diagnostics["llm_calls"] == 1


def test_active_preflight_backfill_and_availability_share_hash_policy():
    source_reviewer = _BatchReviewer()
    deps, _ledger, _search, _verify = _deps([_candidate(0)], source_reviewer)
    attempt = run_attempt("agents", CRITERIA, _quota(), _batch(), deps)
    payload = attempt.items[0].proposed_record_snapshot.canonical_record_payload
    record = record_from_dict(__import__("json").loads(payload))

    reviewer = _BatchReviewer()
    runtime = _runtime(reviewer)
    store = RelevanceStore()
    budget = BudgetLedger(_limits(), clock=lambda: 0.0)
    prep = ensure_relevance_active(
        CRITERIA,
        [record],
        runtime=runtime,
        store=store,
        budget_ledger=budget,
    )
    assert prep.assessed == 1 and prep.errors == 0
    assert reviewer.calls == 1
    availability = assess_availability(
        _quota(total_min=1),
        CRITERIA,
        type("Ledger", (), {"all_records": lambda self: [record]})(),
        store,
        classifier=lambda rec: type("Lang", (), {"quota_bucket": "en", "confidence": "high"})(),
        judge_key=runtime.judge_key,
        content_hasher=runtime.content_hash,
    )
    assert availability.available_ids == (record.reference_id,)
