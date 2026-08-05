"""A2d-4 / Task 4：WritingLiteratureOrchestrator 状态机（stub loop/port/facts）。"""

from __future__ import annotations

from paper_agent.application.literature_flow import (
    FlowState,
    WritingEvidenceRequest,
    WritingLiteratureOrchestrator,
)
from paper_agent.application.literature_ports import (
    BatchDecision,
    BundleDecision,
    FinalGateDecision,
    GapDecision,
)
from paper_agent.literature.language import DEFAULT_THRESHOLDS, classify_record_language
from paper_agent.literature.models import ReferenceRecord
from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
)
from paper_agent.literature.relevance import (
    relevance_content_hash,
    JudgeVersion,
    RelevanceAssessment,
    RelevanceCriteria,
    RelevanceJudge,
    RelevanceStore,
)
from paper_agent.literature.retrieval.attempt import AttemptDeps, RelevanceAcceptancePolicy
from paper_agent.literature.retrieval.budget import RetrievalLimits
from paper_agent.literature.retrieval.cross_verify import (
    CrossVerifier,
    VerificationCache,
    VerifyBudget,
)
from paper_agent.literature.retrieval.query_planner import DeterministicQueryPlanner
from paper_agent.literature.retrieval.registry import ProviderRegistry, named
from paper_agent.literature.verification import EventKind, make_event
from paper_agent.providers.retrieval.capabilities import FetchResult, FetchStatus
from paper_agent.session.facts import SessionFacts
from paper_agent.workspace.models import RetrievalCandidate

_CRIT = RelevanceCriteria(topic="deep learning medical")
_JK = ""


# ---------------------------------------------------------------- stubs

class _SearchStub:
    def __init__(self, cands):
        self._c = cands

    def search(self, query, *, limit=10, filters=()):
        return list(self._c)


class _VerifyOK:
    def fetch_metadata(self, identifier_type, identifier):
        return FetchResult(FetchStatus.FOUND, candidate=RetrievalCandidate(
            id="v", title="", authors=[], year=None, source_id=identifier, source="crossref"))

    def search_bibliographic(self, query, *, limit=5):
        return []


class _Reviewer:
    def complete(self, messages, **opts):
        from paper_agent.providers.llm.base import LLMResponse, ToolCall
        return LLMResponse(content="", tool_calls=[ToolCall(
            id="1", name="report_relevance",
            arguments={"verdict": "relevant", "confidence": "high"})])


class _Port:
    def __init__(self, batch="accept", gap="accept", bundle="accept"):
        self._b, self._g, self._u = batch, gap, bundle
        self.calls = []

    def confirm_retrieval_batch(self, summary):
        self.calls.append("batch")
        return BatchDecision(self._b)

    def resolve_quota_gap(self, report):
        self.calls.append("gap")
        return GapDecision(self._g)

    def confirm_evidence_bundle(self, summary):
        self.calls.append("bundle")
        return BundleDecision(self._u)

    def resolve_final_gate(self, report):
        return FinalGateDecision("accept")


def _cand(doi):
    return RetrievalCandidate(
        id=f"c:{doi}", title="Deep Learning for Medical Imaging", authors=["A"], year=2022,
        source_id=doi, source="crossref",
        abstract="We study deep learning for medical imaging.",
        work_identifiers={"doi": doi})


def _rec(rid, numeric="1"):
    ev = make_event(EventKind.VERIFIED, source="crossref", identifier_type="doi",
                    identifier=f"10.1/{rid}", authority="final", checked_at="2026-01-01T00:00:00Z")
    return ReferenceRecord(
        reference_id=rid, title="Deep Learning for Medical Imaging",
        abstract="We study deep learning for medical imaging.", year=2022, language_hint="en",
        work_identifiers=(("doi", f"10.1/{rid}"),), numeric_aliases=(numeric,),
        bibkey=f"key{rid}", verification_events=(ev,))


def _classifier(rec):
    return classify_record_language(rec, DEFAULT_THRESHOLDS)


def _facts(tmp_path, records=(), *, with_relevance=True):
    f = SessionFacts(path=str(tmp_path / "facts.json"))
    for r in records:
        f.ledger.add_record(r)
        if with_relevance:
            f.relevance_store.put(RelevanceAssessment(
                r.reference_id, _CRIT.fingerprint(),
                relevance_content_hash(r.title, r.abstract), _JK,
                status="SUCCESS", verdict="relevant", confidence="high",
                basis="title_and_abstract"))
    return f


def _orch(candidates=(), *, supervised=True, facts_ledger=None):
    registry = ProviderRegistry(
        search_sources=(named("openalex", _SearchStub(candidates)),),
        verify_sources=(named("crossref", _VerifyOK()),),
    )
    deps = AttemptDeps(
        registry=registry, ledger=facts_ledger,
        cross_verifier=CrossVerifier(registry.verify_pairs()),
        judge=RelevanceJudge(_Reviewer(), judge_version=JudgeVersion()),
        relevance_store=RelevanceStore(), budget=VerifyBudget(),
        verify_cache=VerificationCache(),
        acceptance_policy=RelevanceAcceptancePolicy(),
    )
    return WritingLiteratureOrchestrator(
        deps=deps, limits=RetrievalLimits(max_iterations=2), planner=DeterministicQueryPlanner(),
        classifier=_classifier, supervised=supervised, judge_key=_JK, budget_chars=0,
    )


def _req(total_min=1, op="op1"):
    quota = QuotaSpec(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED,
                      count_constraints=(CountConstraint(CountDimension.TOTAL,
                                                         CountOperator.MIN, total_min),))
    return WritingEvidenceRequest(criteria=_CRIT, citation_quota=quota,
                                  initial_query="dl medical", operation_id=op)


# ---------------------------------------------------------------- 无缺口直达

def test_gap_zero_goes_ready_without_retrieval(tmp_path):
    facts = _facts(tmp_path, [_rec("r1")])
    orch = _orch(facts_ledger=facts.ledger)
    port = _Port()
    out = orch.run(_req(total_min=1), facts=facts, port=port)
    assert out.state is FlowState.READY_TO_WRITE
    assert "batch" not in port.calls              # 不触发检索确认
    assert out.bundle is not None


# ---------------------------------------------------------------- 有缺口 → 检索 → 确认 → commit

def test_gap_triggers_retrieval_and_commit(tmp_path):
    facts = _facts(tmp_path)
    orch = _orch([_cand("10.1/new")], facts_ledger=facts.ledger)
    port = _Port(batch="accept")
    out = orch.run(_req(total_min=1), facts=facts, port=port)
    assert "batch" in port.calls
    assert len(facts.ledger) >= 1                 # commit 后入库
    assert out.state in (FlowState.READY_TO_WRITE, FlowState.CANCELLED,
                         FlowState.NEEDS_GAP_DECISION)


def test_batch_reject_cancels_and_ledger_unchanged(tmp_path):
    facts = _facts(tmp_path)
    orch = _orch([_cand("10.1/new")], facts_ledger=facts.ledger)
    out = orch.run(_req(total_min=1), facts=facts, port=_Port(batch="reject"))
    assert out.state is FlowState.CANCELLED
    assert len(facts.ledger) == 0                 # Ledger 逐字节不变


# ---------------------------------------------------------------- 残缺 gap 决策

def test_remaining_gap_accept_creates_deviation(tmp_path):
    facts = _facts(tmp_path)
    # 检索源无候选 → 检索后仍无可用 → NEEDS_GAP_DECISION → accept
    orch = _orch([], facts_ledger=facts.ledger)
    out = orch.run(_req(total_min=3), facts=facts, port=_Port(gap="accept"))
    assert out.state is FlowState.READY_TO_WRITE
    assert out.deviations and out.deviations[0].effective_lower_bound == 0
    assert out.deviations[0].original_lower_bound == 3


def test_remaining_gap_cancel(tmp_path):
    facts = _facts(tmp_path)
    orch = _orch([], facts_ledger=facts.ledger)
    out = orch.run(_req(total_min=3), facts=facts, port=_Port(gap="cancel"))
    assert out.state is FlowState.CANCELLED


# ---------------------------------------------------------------- 非交互不默认接受

def test_no_port_returns_needs_and_never_accepts(tmp_path):
    facts = _facts(tmp_path)
    orch = _orch([_cand("10.1/new")], facts_ledger=facts.ledger)
    out = orch.run(_req(total_min=1), facts=facts, port=None)
    assert out.state is FlowState.NEEDS_BATCH_CONFIRMATION
    assert len(facts.ledger) == 0                 # 未提交
    assert out.checkpoint is not None
    assert facts.get_literature_flow(out.checkpoint.flow_id) is not None


def test_needs_input_decision_pauses(tmp_path):
    facts = _facts(tmp_path)
    orch = _orch([_cand("10.1/new")], facts_ledger=facts.ledger)
    out = orch.run(_req(total_min=1), facts=facts, port=_Port(batch="needs_input"))
    assert out.state is FlowState.NEEDS_BATCH_CONFIRMATION
    assert len(facts.ledger) == 0


# ---------------------------------------------------------------- supervised bundle 确认

def test_supervised_requires_bundle_confirmation(tmp_path):
    facts = _facts(tmp_path, [_rec("r1")])
    orch = _orch(facts_ledger=facts.ledger, supervised=True)
    port = _Port(bundle="accept")
    out = orch.run(_req(total_min=1), facts=facts, port=port)
    assert "bundle" in port.calls and out.state is FlowState.READY_TO_WRITE


def test_bundle_reject_cancels(tmp_path):
    facts = _facts(tmp_path, [_rec("r1")])
    orch = _orch(facts_ledger=facts.ledger, supervised=True)
    out = orch.run(_req(total_min=1), facts=facts, port=_Port(bundle="reject"))
    assert out.state is FlowState.CANCELLED


def test_unsupervised_skips_bundle_confirmation(tmp_path):
    facts = _facts(tmp_path, [_rec("r1")])
    orch = _orch(facts_ledger=facts.ledger, supervised=False)
    port = _Port()
    out = orch.run(_req(total_min=1), facts=facts, port=port)
    assert "bundle" not in port.calls and out.state is FlowState.READY_TO_WRITE


# ---------------------------------------------------------------- 恢复

def test_resume_rejects_fingerprint_mismatch(tmp_path):
    facts = _facts(tmp_path)
    orch = _orch([_cand("10.1/new")], facts_ledger=facts.ledger)
    paused = orch.run(_req(total_min=1, op="op1"), facts=facts, port=None)
    fid = paused.checkpoint.flow_id
    # 换 operation_id → 作用域不一致 → 拒绝恢复
    out = orch.resume(fid, _req(total_min=1, op="OTHER_OP"), facts=facts, port=_Port())
    assert out.state is FlowState.CANCELLED
    assert "不一致" in out.message


def test_resume_stale_search_intent_rejected(tmp_path):
    """A2f：checkpoint 冻结过 SearchIntent，恢复时 intent 不一致（或缺失）→ STALE 拒绝。"""
    from paper_agent.literature.retrieval.search_intent import SearchIntent, SearchSeed

    def _req_si(seeds, op="op1"):
        quota = QuotaSpec(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED,
                          count_constraints=(CountConstraint(CountDimension.TOTAL,
                                                             CountOperator.MIN, 1),))
        si = SearchIntent(semantic_topic="deep learning medical imaging",
                          seeds=tuple(SearchSeed(text=s) for s in seeds))
        return WritingEvidenceRequest(criteria=_CRIT, citation_quota=quota,
                                      initial_query="", operation_id=op, search_intent=si)

    facts = _facts(tmp_path)
    orch = _orch([_cand("10.1/new")], facts_ledger=facts.ledger)
    paused = orch.run(_req_si(["seed alpha"]), facts=facts, port=None)
    fid = paused.checkpoint.flow_id
    assert paused.checkpoint.strategy_hash                      # 冻结了 strategy_hash
    # 恢复时给不同 seed 的 intent → strategy_hash 变 → STALE
    out = orch.resume(fid, _req_si(["seed beta different"]), facts=facts, port=_Port())
    assert out.state is FlowState.CANCELLED and "STALE" in out.message


def test_resume_same_search_intent_ok(tmp_path):
    """相同冻结 intent（同 seeds）恢复不触发 STALE。"""
    from paper_agent.literature.retrieval.search_intent import SearchIntent, SearchSeed

    def _req_si(op="op1"):
        quota = QuotaSpec(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED,
                          count_constraints=(CountConstraint(CountDimension.TOTAL,
                                                             CountOperator.MIN, 1),))
        si = SearchIntent(semantic_topic="deep learning medical imaging",
                          seeds=(SearchSeed(text="seed alpha"),))
        return WritingEvidenceRequest(criteria=_CRIT, citation_quota=quota,
                                      initial_query="", operation_id=op, search_intent=si)

    facts = _facts(tmp_path)
    orch = _orch([_cand("10.1/new")], facts_ledger=facts.ledger)
    paused = orch.run(_req_si(), facts=facts, port=None)
    out = orch.resume(paused.checkpoint.flow_id, _req_si(), facts=facts, port=_Port())
    assert "STALE" not in out.message                          # 同一 intent 不 STALE


def test_resume_missing_flow(tmp_path):
    facts = _facts(tmp_path)
    orch = _orch(facts_ledger=facts.ledger)
    out = orch.resume("nope", _req(), facts=facts, port=_Port())
    assert out.state is FlowState.CANCELLED
