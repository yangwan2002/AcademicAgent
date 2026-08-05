"""B2 serial reorder: relevance before strict verification, flag-off safe."""

from __future__ import annotations

import re

import pytest

from paper_agent.application.literature_composition import build_writing_orchestrator
from paper_agent.config import Config, load_config
from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import candidate_to_record
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
    _run_attempt_b2_verify_first_reference,
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
from paper_agent.literature.retrieval.loop import RetrievalRequest, run_retrieval_loop
from paper_agent.literature.retrieval.query_planner import DeterministicQueryPlanner
from paper_agent.literature.retrieval.registry import ProviderRegistry, named
from paper_agent.literature.retrieval.window_packer import (
    BatchInputBudget,
    WindowLimits,
)
from paper_agent.literature.verification import VerificationPolicy
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.providers.retrieval.capabilities import FetchResult, FetchStatus
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.session.facts import SessionFacts
from paper_agent.workspace.models import RetrievalCandidate


CRITERIA = RelevanceCriteria(topic="academic writing evidence agents")


def _limits():
    return RetrievalLimits(
        max_iterations=4,
        max_provider_calls=100,
        max_search_calls=20,
        max_verify_calls=50,
        max_discovery_calls=10,
        max_llm_calls=20,
        max_candidates=100,
        provider_time_budget_s=100,
    )


def _candidate(index, *, doi=None, abstract=None, year=2024):
    identifier = doi or f"10.2000/b2-{index}"
    return RetrievalCandidate(
        id=f"candidate:{index}",
        title=f"Academic Evidence Agent {index}",
        authors=["Author"],
        year=year,
        source_id=identifier,
        source="openalex",
        abstract=(
            f"Evidence-grounded academic writing agent study {index}."
            if abstract is None else abstract
        ),
        language="en",
        work_identifiers={"doi": identifier},
    )


class _Search:
    def __init__(self, candidates):
        self.candidates = list(candidates)

    def search(self, query, *, limit=10, filters=()):
        return self.candidates[:limit]


class _Verify:
    def __init__(self, *, events=None, missing=()):
        self.events = events if events is not None else []
        self.missing = set(missing)
        self.calls = 0

    def fetch_metadata(self, identifier_type, identifier):
        self.calls += 1
        self.events.append(("verify", identifier))
        status = (
            FetchStatus.NOT_FOUND_AT_SOURCE
            if identifier in self.missing else FetchStatus.FOUND
        )
        found = RetrievalCandidate(
            id=f"verified:{identifier}",
            title="",
            authors=[],
            year=None,
            source_id=identifier,
            source="crossref",
        )
        return FetchResult(status, candidate=found if status is FetchStatus.FOUND else None)

    def search_bibliographic(self, query, *, limit=5):
        return []


class _PerItemReviewer:
    def __init__(self, verdict="relevant", *, events=None):
        self.verdict = verdict
        self.events = events if events is not None else []
        self.calls = 0
        self.last_user = ""

    def complete(self, messages, **opts):
        self.calls += 1
        self.events.append(("judge", self.calls))
        self.last_user = "\n".join(
            message.content for message in messages if message.role == "user"
        )
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(
                id=str(self.calls),
                name="report_relevance",
                arguments={"verdict": self.verdict, "confidence": "high"},
            )],
        )


class _BatchReviewer:
    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.calls = 0

    def complete(self, messages, **opts):
        self.calls += 1
        user = "\n".join(
            message.content for message in messages if message.role == "user"
        )
        keys = re.findall(r"candidate_key=(c\d+)", user)
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(
                id=str(self.calls),
                name="report_relevance_batch",
                arguments={"results": [
                    {
                        "candidate_key": key,
                        "verdict": self.verdicts.get(key, "relevant"),
                        "confidence": "high",
                    }
                    for key in keys
                ]},
            )],
        )


def _runtime(reviewer):
    policy = RelevanceTextPolicy(version="b2-test-text-v1")
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
        window_limits=WindowLimits(batch_size=8, input_budget=budget),
    )


def _deps(
    candidates,
    reviewer,
    *,
    ledger=None,
    missing=(),
    events=None,
    batch_runtime=None,
):
    search = _Search(candidates)
    verify = _Verify(events=events, missing=missing)
    registry = ProviderRegistry(
        search_sources=(named("openalex", search),),
        verify_sources=(named("crossref", verify),),
    )
    budget = BudgetLedger(_limits(), clock=lambda: 0.0)
    return AttemptDeps(
        registry=registry,
        ledger=ledger or ReferenceLedger(),
        cross_verifier=CrossVerifier(registry.verify_pairs()),
        judge=RelevanceJudge(
            reviewer, judge_version=JudgeVersion(provider="stub", model_id="single")
        ),
        relevance_store=RelevanceStore(),
        budget=budget.verify_budget_view(),
        verify_cache=VerificationCache(),
        active_relevance_runtime=batch_runtime,
        budget_ledger=budget,
        pipeline_reorder_enabled=True,
    ), verify, budget


def _quota(*, filters=(), total_min=None):
    constraints = () if total_min is None else (
        CountConstraint(CountDimension.TOTAL, CountOperator.MIN, total_min),
    )
    return QuotaSpec(
        QuotaKind.RETRIEVAL,
        QuotaScope.NEW_ONLY,
        eligibility_filters=tuple(filters),
        count_constraints=constraints,
    )


def _batch():
    return RetrievalBatch(
        batch_id="b2",
        task_fingerprint=CRITERIA.fingerprint(),
        quota=_quota(),
        verification_policy=VerificationPolicy.STRICT_FINAL_V1,
    )


def test_b2_unrelated_candidate_never_calls_strict_verifier():
    reviewer = _PerItemReviewer("unrelated")
    deps, verify, budget = _deps([_candidate(0)], reviewer)
    result = run_attempt("agents", CRITERIA, _quota(), _batch(), deps)
    assert reviewer.calls == budget.llm_calls == 1
    assert verify.calls == 0
    assert result.items[0].disposition is BatchDisposition.REJECT
    assert result.items[0].rejection_reasons == ("relevance:unrelated",)


def test_b2_deterministic_filter_skips_judge_and_verifier():
    reviewer = _PerItemReviewer()
    year_filter = EligibilityFilter(FilterField.YEAR, FilterOperator.GTE, 2030)
    deps, verify, budget = _deps([_candidate(0, year=2020)], reviewer)
    result = run_attempt(
        "agents", CRITERIA, _quota(filters=(year_filter,)), _batch(), deps
    )
    assert reviewer.calls == budget.llm_calls == verify.calls == 0
    assert result.items[0].rejection_reasons == ("filter",)


def test_b2_relevance_precedes_verify_and_strict_success_accepts():
    events = []
    reviewer = _PerItemReviewer(events=events)
    deps, verify, _budget = _deps([_candidate(0)], reviewer, events=events)
    result = run_attempt("agents", CRITERIA, _quota(), _batch(), deps)
    assert [kind for kind, _ in events] == ["judge", "verify"]
    assert verify.calls == 1
    assert result.items[0].disposition is BatchDisposition.ACCEPT_NEW


def test_b2_relevant_but_unverified_is_honestly_rejected():
    candidate = _candidate(0)
    reviewer = _PerItemReviewer()
    deps, verify, _budget = _deps(
        [candidate], reviewer, missing={"10.2000/b2-0"}
    )
    result = run_attempt("agents", CRITERIA, _quota(), _batch(), deps)
    assert reviewer.calls == verify.calls == 1
    assert result.items[0].disposition is BatchDisposition.REJECT
    assert result.items[0].rejection_reasons == ("verify:unverified",)


def test_b1_b2_batches_relevance_then_verifies_only_survivors():
    candidates = [_candidate(0), _candidate(1), _candidate(2)]
    reviewer = _BatchReviewer({"c1": "unrelated"})
    runtime = _runtime(reviewer)
    deps, verify, budget = _deps(
        candidates, _PerItemReviewer(), batch_runtime=runtime
    )
    result = run_attempt("agents", CRITERIA, _quota(), _batch(), deps)
    assert reviewer.calls == budget.llm_calls == 1
    assert verify.calls == 2
    assert [item.disposition for item in result.items] == [
        BatchDisposition.ACCEPT_NEW,
        BatchDisposition.REJECT,
        BatchDisposition.ACCEPT_NEW,
    ]
    assert result.throughput.relevance_items == 3
    assert result.throughput.relevance_windows == 1


def test_b2_same_work_selects_richer_representative_before_judging():
    sparse = _candidate(0, doi="10.2000/same", abstract="")
    rich = _candidate(
        1,
        doi="10.2000/same",
        abstract="Richer evidence about retrieval, verification, and citation.",
    )
    reviewer = _PerItemReviewer()
    deps, verify, _budget = _deps([sparse, rich], reviewer)
    result = run_attempt("agents", CRITERIA, _quota(), _batch(), deps)
    assert reviewer.calls == verify.calls == 1
    assert "Richer evidence" in reviewer.last_user
    assert result.items[0].rejection_reasons == ("batch_duplicate",)
    assert result.items[1].disposition is BatchDisposition.ACCEPT_NEW


def test_b2_reuse_judges_ledger_subject_and_skips_verification():
    existing_candidate = _candidate(
        0,
        doi="10.2000/existing",
        abstract="Authoritative ledger abstract used as the relevance subject.",
    )
    ledger = ReferenceLedger()
    ledger.add_record(candidate_to_record(existing_candidate))
    incoming = _candidate(0, doi="10.2000/existing", abstract="")
    reviewer = _PerItemReviewer()
    deps, verify, _budget = _deps([incoming], reviewer, ledger=ledger)
    result = run_attempt("agents", CRITERIA, _quota(), _batch(), deps)
    assert "Authoritative ledger abstract" in reviewer.last_user
    assert verify.calls == 0
    assert result.items[0].disposition is BatchDisposition.REUSE_EXISTING
    assert (
        result.items[0].relevance.reference_content_hash
        == deps.judge.content_hash(
            existing_candidate.title, existing_candidate.abstract
        )[2]
    )


def test_b2_config_and_snapshot_are_independent_from_batch(monkeypatch, tmp_path):
    assert Config().literature_pipeline_reorder_enabled is False
    monkeypatch.setenv("PAPER_LIT_PIPELINE_REORDER_ENABLED", "true")
    assert load_config().literature_pipeline_reorder_enabled is True

    config = Config(
        literature_writing_integration=True,
        literature_pipeline_reorder_enabled=True,
        retrieval_provider="mock",
    )
    facts = SessionFacts(path=str(tmp_path / "facts.json"))
    reviewer = _PerItemReviewer()
    orchestrator = build_writing_orchestrator(
        config,
        writer_llm=reviewer,
        retrieval_provider=MockRetrievalProvider(),
        facts=facts,
    )
    assert orchestrator._deps.pipeline_reorder_enabled is True
    assert orchestrator._deps.active_relevance_runtime is None
    assert orchestrator._throughput_policy["flags"] == {
        "batch": False,
        "reorder": True,
        "concurrency": False,
    }


def test_b2_loop_charges_per_item_llm_once_not_again_outside_attempt():
    reviewer = _PerItemReviewer()
    deps, verify, budget = _deps([_candidate(0)], reviewer)
    quota = _quota(total_min=1)
    result = run_retrieval_loop(
        RetrievalRequest(
            quota=quota,
            criteria=CRITERIA,
            initial_query="agents",
            task_fingerprint=CRITERIA.fingerprint(),
        ),
        deps=deps,
        limits=_limits(),
        planner=DeterministicQueryPlanner(),
        budget_ledger=budget,
    )
    assert reviewer.calls == budget.llm_calls == 1
    assert verify.calls == budget.verify_calls == 1
    assert result.diagnostics["llm_calls"] == 1


@pytest.mark.parametrize(
    ("verdict", "missing"),
    [
        ("unrelated", ()),
        ("relevant", ("10.2000/b2-0",)),
        ("relevant", ()),
    ],
)
def test_b2_reorder_matches_same_subject_verify_first_reference(verdict, missing):
    candidate = _candidate(0)
    reordered_deps, reordered_verify, _ = _deps(
        [candidate], _PerItemReviewer(verdict), missing=missing
    )
    reference_deps, reference_verify, _ = _deps(
        [candidate], _PerItemReviewer(verdict), missing=missing
    )
    reordered = run_attempt(
        "agents", CRITERIA, _quota(), _batch(), reordered_deps
    )
    reference = _run_attempt_b2_verify_first_reference(
        "agents", CRITERIA, _quota(), _batch(), reference_deps
    )

    def semantic(result):
        return [
            (item.disposition, item.rejection_reasons, item.target_reference_id)
            for item in result.items
        ]

    assert semantic(reordered) == semantic(reference)
    assert reordered_verify.calls <= reference_verify.calls
    if verdict == "unrelated":
        assert reordered_verify.calls == 0
        assert reference_verify.calls == 1
