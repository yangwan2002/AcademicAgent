"""A2f-4：自适应多 seed Loop + UUID batch + 共享预算 + charge_llm 修复 + STALE 恢复。"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.literature.language import DEFAULT_THRESHOLDS, classify_record_language
from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
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
from paper_agent.literature.retrieval.attempt import AttemptDeps, RelevanceAcceptancePolicy
from paper_agent.literature.retrieval.budget import BudgetLedger, RetrievalLimits
from paper_agent.literature.retrieval.cross_verify import (
    CrossVerifier,
    VerificationCache,
    VerifyBudget,
)
from paper_agent.literature.retrieval.loop import (
    RetrievalRequest,
    run_retrieval_loop,
)
from paper_agent.literature.retrieval.query_planner import (
    DeterministicQueryPlanner,
    PlannerRequest,
    SearchPlan,
)
from paper_agent.literature.retrieval.registry import ProviderRegistry, named
from paper_agent.literature.retrieval.search_intent import (
    SearchIntent,
    SearchSeed,
)
from paper_agent.providers.retrieval.capabilities import FetchResult, FetchStatus
from paper_agent.workspace.models import RetrievalCandidate

_CRIT = RelevanceCriteria(topic="deep learning")


# ---------------------------------------------------------------- RetrievalRequest 兼容

def _si(seeds):
    return SearchIntent(semantic_topic="deep learning for medical imaging",
                        seeds=tuple(seeds))


def test_request_conflict_construct_fails():
    it = _si([SearchSeed(text="alpha keyword")])
    with pytest.raises(ValueError):
        RetrievalRequest(quota=_quota(), criteria=_CRIT,
                         initial_query="different query", search_intent=it)


def test_request_initial_query_equal_first_seed_ok():
    it = _si([SearchSeed(text="alpha keyword")])
    req = RetrievalRequest(quota=_quota(), criteria=_CRIT,
                           initial_query="alpha keyword", search_intent=it)
    assert len(req.effective_seeds()) == 1


def test_request_legacy_no_seeds():
    req = RetrievalRequest(quota=_quota(), criteria=_CRIT, initial_query="dl")
    assert req.effective_seeds() == ()


# ---------------------------------------------------------------- stubs

class _SearchStub:
    """按 query 返回候选；记录被搜过的 query。"""

    def __init__(self, by_query):
        self._by_query = by_query          # query → [candidate]
        self.queries: list[str] = []

    def search(self, query, *, limit=10, filters=()):
        self.queries.append(query)
        return list(self._by_query.get(query, []))


class _VerifyOK:
    def fetch_metadata(self, identifier_type, identifier):
        return FetchResult(FetchStatus.FOUND, candidate=RetrievalCandidate(
            id="v", title="Deep Learning for Medical Imaging", authors=[], year=2022,
            source_id=identifier, source="crossref"))

    def search_bibliographic(self, query, *, limit=5):
        return []


class _Reviewer:
    def complete(self, messages, **opts):
        from paper_agent.providers.llm.base import LLMResponse, ToolCall
        return LLMResponse(content="", tool_calls=[ToolCall(
            id="1", name="report_relevance",
            arguments={"verdict": "relevant", "confidence": "high"})])


def _cand(doi, title="Deep Learning for Medical Imaging"):
    return RetrievalCandidate(
        id=f"c:{doi}", title=title, authors=["A"], year=2022, source_id=doi,
        source="crossref", abstract="We study deep learning for medical imaging.",
        work_identifiers={"doi": doi})


def _classifier(rec):
    return classify_record_language(rec, DEFAULT_THRESHOLDS)


def _quota(total_min=1):
    return QuotaSpec(QuotaKind.RETRIEVAL, QuotaScope.NEW_ONLY,
                     count_constraints=(CountConstraint(CountDimension.TOTAL,
                                                        CountOperator.MIN, total_min),))


def _deps(search_stub):
    from paper_agent.literature.ledger import ReferenceLedger

    registry = ProviderRegistry(
        search_sources=(named("openalex", search_stub),),
        verify_sources=(named("crossref", _VerifyOK()),),
    )
    return AttemptDeps(
        registry=registry, ledger=ReferenceLedger(),
        cross_verifier=CrossVerifier(registry.verify_pairs()),
        judge=RelevanceJudge(_Reviewer(), judge_version=JudgeVersion()),
        relevance_store=RelevanceStore(), budget=VerifyBudget(),
        verify_cache=VerificationCache(), acceptance_policy=RelevanceAcceptancePolicy(),
    )


class _CountingPlanner:
    """记录 propose 被调用次数；返回空计划（不续搜）。"""

    def __init__(self):
        self.calls = 0

    def propose(self, request: PlannerRequest) -> SearchPlan:
        self.calls += 1
        return SearchPlan(strategy_reason="none", llm_called=False)


# ---------------------------------------------------------------- 首个 seed 满足即停

def test_first_seed_satisfies_stops_before_remaining():
    stub = _SearchStub({"seed one": [_cand("10.1/a")], "seed two": [_cand("10.1/b")]})
    deps = _deps(stub)
    planner = _CountingPlanner()
    req = RetrievalRequest(
        quota=_quota(total_min=1), criteria=_CRIT, task_fingerprint=_CRIT.fingerprint(),
        search_intent=_si([SearchSeed(text="seed one"), SearchSeed(text="seed two")]))
    res = run_retrieval_loop(req, deps=deps, limits=RetrievalLimits(), planner=planner)
    assert res.stop_reason.value == "quota_satisfied"
    assert stub.queries == ["seed one"]              # 第 2 个 seed 未跑
    assert planner.calls == 0                        # 已满足，不进 planner


# ---------------------------------------------------------------- 多 seed 都跑后仍不足 → planner

def test_all_seeds_then_planner_continuation():
    stub = _SearchStub({"seed one": [], "seed two": []})   # seed 都无候选
    deps = _deps(stub)
    planner = _CountingPlanner()
    req = RetrievalRequest(
        quota=_quota(total_min=1), criteria=_CRIT, task_fingerprint=_CRIT.fingerprint(),
        search_intent=_si([SearchSeed(text="seed one"), SearchSeed(text="seed two")]))
    run_retrieval_loop(req, deps=deps, limits=RetrievalLimits(), planner=planner)
    assert "seed one" in stub.queries and "seed two" in stub.queries
    assert planner.calls >= 1                         # seed 用尽仍缺 → 进 planner 续搜


# ---------------------------------------------------------------- UUID batch 身份

def test_batch_id_is_uuid_not_query_hash():
    stub = _SearchStub({"seed one": [_cand("10.1/a")]})
    req = RetrievalRequest(quota=_quota(1), criteria=_CRIT,
                           task_fingerprint=_CRIT.fingerprint(),
                           search_intent=_si([SearchSeed(text="seed one")]))
    res = run_retrieval_loop(req, deps=_deps(stub), limits=RetrievalLimits(),
                             planner=_CountingPlanner())
    assert res.batch.batch_id.startswith("batch_")
    # 两次同 query 的 loop → 不同 batch id（UUID）
    res2 = run_retrieval_loop(
        RetrievalRequest(quota=_quota(1), criteria=_CRIT,
                         task_fingerprint=_CRIT.fingerprint(),
                         search_intent=_si([SearchSeed(text="seed one")])),
        deps=_deps(stub), limits=RetrievalLimits(), planner=_CountingPlanner())
    assert res.batch.batch_id != res2.batch.batch_id


# ---------------------------------------------------------------- 共享预算

def test_shared_budget_ledger_accumulates():
    stub = _SearchStub({"seed one": [_cand("10.1/a")]})
    shared = BudgetLedger(RetrievalLimits())
    shared.charge_llm()                               # 模拟合成阶段已计一次
    req = RetrievalRequest(quota=_quota(1), criteria=_CRIT,
                           task_fingerprint=_CRIT.fingerprint(),
                           search_intent=_si([SearchSeed(text="seed one")]))
    run_retrieval_loop(req, deps=_deps(stub), limits=RetrievalLimits(),
                       planner=_CountingPlanner(), budget_ledger=shared)
    assert shared.llm_calls >= 1 and shared.search_calls >= 1   # 同一 ledger 汇入


# ---------------------------------------------------------------- charge_llm 修复

def test_deterministic_planner_does_not_charge_llm():
    """seed 用尽 + DeterministicQueryPlanner 续搜 → 不虚记 LLM 调用（除相关性调用）。"""
    stub = _SearchStub({"seed one": []})
    deps = _deps(stub)
    shared = BudgetLedger(RetrievalLimits(max_iterations=2, max_no_progress_rounds=1))
    req = RetrievalRequest(quota=_quota(1), criteria=_CRIT,
                           task_fingerprint=_CRIT.fingerprint(),
                           search_intent=_si([SearchSeed(text="seed one")]))
    run_retrieval_loop(req, deps=deps, limits=RetrievalLimits(max_iterations=2,
                       max_no_progress_rounds=1), planner=DeterministicQueryPlanner(),
                       budget_ledger=shared)
    # seed 无候选 → 无相关性调用；Deterministic planner 不记 LLM → llm_calls 应为 0
    assert shared.llm_calls == 0
