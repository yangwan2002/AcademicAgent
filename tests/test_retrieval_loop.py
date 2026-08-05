"""A2c-4：预算 / QueryPlanner / RetrievalLoop（stub 离线）。"""

from __future__ import annotations

import ast
import pathlib

import pytest

from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
)
from paper_agent.literature.relevance import JudgeVersion, RelevanceCriteria, RelevanceJudge, RelevanceStore
from paper_agent.literature.retrieval.attempt import AttemptDeps, RelevanceAcceptancePolicy
from paper_agent.literature.retrieval.budget import (
    BudgetLedger,
    CancelToken,
    RetrievalLimits,
    StopReason,
)
from paper_agent.literature.retrieval.cross_verify import CrossVerifier, VerificationCache, VerifyBudget
from paper_agent.literature.retrieval.loop import RetrievalRequest, run_retrieval_loop
from paper_agent.literature.retrieval.query_planner import (
    DeterministicQueryPlanner,
    LLMQueryPlanner,
    PlannerRequest,
    SearchPlan,
)
from paper_agent.literature.retrieval.registry import ProviderRegistry, named
from paper_agent.providers.retrieval.capabilities import FetchResult, FetchStatus
from paper_agent.workspace.models import RetrievalCandidate


# ============================================================ budget

def test_budget_dimensions_and_verify_view():
    lim = RetrievalLimits(max_search_calls=2, max_verify_calls=1)
    b = BudgetLedger(lim, clock=lambda: 0.0)
    assert b.can_continue()
    b.charge_search(2)
    assert "max_search_calls" in b.exhausted_dimensions()
    assert not b.can_continue()
    # verify_budget_view 的 charge 汇入 ledger（不双重记账）
    b2 = BudgetLedger(RetrievalLimits(max_verify_calls=2), clock=lambda: 0.0)
    view = b2.verify_budget_view()
    view.charge()
    assert b2.verify_calls == 1


def test_budget_time_exhaustion():
    t = {"v": 0.0}
    lim = RetrievalLimits(provider_time_budget_s=5.0)
    b = BudgetLedger(lim, clock=lambda: t["v"])
    assert b.can_continue()
    t["v"] = 6.0
    assert "provider_time_budget_s" in b.exhausted_dimensions()


# ============================================================ query planner

def test_deterministic_planner_first_and_expand():
    p = DeterministicQueryPlanner()
    first = p.propose(PlannerRequest(initial_query="dl medical"))
    assert first.queries == ("dl medical",)
    exp = p.propose(PlannerRequest(initial_query="dl medical", topic="dl medical",
                                   gap_dimensions=("language.zh",), tried_queries=("dl medical",)))
    assert any("中文" in q for q in exp.queries)


class _PlannerLLM:
    def complete(self, messages, **opts):
        from paper_agent.providers.llm.base import LLMResponse, ToolCall
        return LLMResponse(content="", tool_calls=[ToolCall(
            id="1", name="propose_search_plan",
            arguments={"queries": ["q2"], "source_names": ["openalex"], "strategy_reason": "r"})])


def test_llm_planner_structured():
    plan = LLMQueryPlanner(_PlannerLLM()).propose(PlannerRequest(initial_query="q"))
    assert plan.queries == ("q2",)


def test_llm_planner_failure_empty():
    class _Bad:
        def complete(self, messages, **opts):
            from paper_agent.providers.llm.base import LLMResponse
            return LLMResponse(content="not json", tool_calls=[])
    plan = LLMQueryPlanner(_Bad()).propose(PlannerRequest(initial_query="q"))
    assert plan.is_empty                            # → Loop 用 fallback


# ============================================================ loop stubs

class _SearchStub:
    def __init__(self, by_query, *, fail=False):
        self._by_query = by_query
        self._fail = fail

    def search(self, query, *, limit=10, filters=()):
        if self._fail:
            from paper_agent.providers.retrieval.base import RetrievalUnavailable
            raise RetrievalUnavailable("down")
        return list(self._by_query.get(query, []))


class _VerifyOK:
    def fetch_metadata(self, identifier_type, identifier):
        return FetchResult(FetchStatus.FOUND, candidate=RetrievalCandidate(
            id="v", title="", authors=[], year=None, source_id=identifier, source="crossref"))

    def search_bibliographic(self, query, *, limit=5):
        return []


class _Reviewer:
    def __init__(self, verdict="relevant"):
        self._v = verdict

    def complete(self, messages, **opts):
        from paper_agent.providers.llm.base import LLMResponse, ToolCall
        return LLMResponse(content="", tool_calls=[ToolCall(
            id="1", name="report_relevance", arguments={"verdict": self._v, "confidence": "high"})])


class _FixedPlanner:
    """按预置每轮返回查询（测试多轮）。"""
    def __init__(self, plans):
        self._plans = list(plans)
        self._i = 0

    def propose(self, request):
        if self._i < len(self._plans):
            q = self._plans[self._i]
            self._i += 1
            return SearchPlan(queries=(q,), strategy_reason="fixed")
        return SearchPlan()


def _cand(doi, title="Deep Learning for Medical Diagnosis"):
    return RetrievalCandidate(
        id=f"c:{doi}", title=title, authors=["A"], year=2022, source_id=doi, source="crossref",
        abstract="We study deep learning for medical image diagnosis.",
        work_identifiers={"doi": doi})


def _deps(by_query, *, reviewer=None, ledger=None):
    registry = ProviderRegistry(
        search_sources=(named("openalex", _SearchStub(by_query)),),
        verify_sources=(named("crossref", _VerifyOK()),),
    )
    return AttemptDeps(
        registry=registry,
        ledger=ledger if ledger is not None else ReferenceLedger(),
        cross_verifier=CrossVerifier(registry.verify_pairs()),
        judge=RelevanceJudge(reviewer or _Reviewer(), judge_version=JudgeVersion(model_id="stub")),
        relevance_store=RelevanceStore(),
        budget=VerifyBudget(),
        verify_cache=VerificationCache(),
        acceptance_policy=RelevanceAcceptancePolicy(),
    )


def _quota(total_min=2, scope=QuotaScope.NEW_ONLY, constraints=None):
    cons = constraints if constraints is not None else (
        CountConstraint(CountDimension.TOTAL, CountOperator.MIN, total_min),
    )
    return QuotaSpec(QuotaKind.RETRIEVAL, scope, count_constraints=cons)


def _clock():
    t = {"v": 0.0}
    return lambda: t["v"]


# ============================================================ loop behaviour

def test_loop_satisfied_first_round():
    by_query = {"q": [_cand("10.1/a"), _cand("10.2/b")]}
    req = RetrievalRequest(quota=_quota(total_min=2), criteria=RelevanceCriteria(topic="dl"),
                           initial_query="q")
    res = run_retrieval_loop(req, deps=_deps(by_query), limits=RetrievalLimits(),
                             planner=DeterministicQueryPlanner(), clock=_clock())
    assert res.stop_reason is StopReason.QUOTA_SATISFIED
    assert res.batch.status.value == "staged"
    assert res.gaps == ()


def test_loop_multi_round_then_satisfied():
    by_query = {"q": [_cand("10.1/a")], "q2": [_cand("10.2/b")]}
    req = RetrievalRequest(quota=_quota(total_min=2), criteria=RelevanceCriteria(topic="dl"),
                           initial_query="q")
    planner = _FixedPlanner(["q2"])
    res = run_retrieval_loop(req, deps=_deps(by_query), limits=RetrievalLimits(),
                             planner=planner, clock=_clock())
    assert res.stop_reason is StopReason.QUOTA_SATISFIED
    assert res.diagnostics["iterations"] >= 2


def test_loop_only_max_quota_does_not_start():
    q = _quota(constraints=(CountConstraint(CountDimension.TOTAL, CountOperator.MAX, 5),))
    req = RetrievalRequest(quota=q, criteria=RelevanceCriteria(topic="dl"), initial_query="q")
    res = run_retrieval_loop(req, deps=_deps({"q": []}), limits=RetrievalLimits(),
                             planner=DeterministicQueryPlanner(), clock=_clock())
    assert res.stop_reason is StopReason.QUOTA_SATISFIED
    assert res.diagnostics.get("reason") == "no_lower_bound"


def test_loop_budget_exhausted():
    by_query = {"q": [_cand("10.1/a")]}
    req = RetrievalRequest(quota=_quota(total_min=99), criteria=RelevanceCriteria(topic="dl"),
                           initial_query="q")
    limits = RetrievalLimits(max_iterations=1)
    res = run_retrieval_loop(req, deps=_deps(by_query), limits=limits,
                             planner=_FixedPlanner(["q2", "q3"]), clock=_clock())
    assert res.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert "max_iterations" in res.diagnostics["exhausted_dimensions"]


def test_loop_no_progress():
    by_query = {"q": [_cand("10.1/a")]}      # 每轮都只这一篇，凑不够 total_min=3
    req = RetrievalRequest(quota=_quota(total_min=3), criteria=RelevanceCriteria(topic="dl"),
                           initial_query="q")
    # planner 反复给同主题查询但无新命中
    planner = _FixedPlanner(["q", "q", "q", "q"])
    limits = RetrievalLimits(max_iterations=10, max_no_progress_rounds=2)
    res = run_retrieval_loop(req, deps=_deps(by_query), limits=limits, planner=planner, clock=_clock())
    assert res.stop_reason in (StopReason.NO_PROGRESS, StopReason.SOURCE_EXHAUSTED)


def test_loop_cancelled():
    by_query = {"q": [_cand("10.1/a")]}
    req = RetrievalRequest(quota=_quota(total_min=5), criteria=RelevanceCriteria(topic="dl"),
                           initial_query="q")
    token = CancelToken()
    token.cancel()
    res = run_retrieval_loop(req, deps=_deps(by_query), limits=RetrievalLimits(),
                             planner=DeterministicQueryPlanner(), clock=_clock(), cancel_token=token)
    assert res.stop_reason is StopReason.CANCELLED


def test_loop_all_sources_unavailable():
    registry = ProviderRegistry(
        search_sources=(named("openalex", _SearchStub({}, fail=True)),),
        verify_sources=(named("crossref", _VerifyOK()),),
    )
    deps = _deps({"q": []})
    deps.registry = registry
    req = RetrievalRequest(quota=_quota(total_min=2), criteria=RelevanceCriteria(topic="dl"),
                           initial_query="q")
    res = run_retrieval_loop(req, deps=deps, limits=RetrievalLimits(),
                             planner=DeterministicQueryPlanner(), clock=_clock())
    assert res.stop_reason is StopReason.ALL_SOURCES_UNAVAILABLE


def test_loop_unguaranteeable_language_dim():
    by_query = {"q": [_cand("10.1/a")]}      # 只 1 篇英文，中文维度凑不够
    q = _quota(constraints=(
        CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1),
        CountConstraint(CountDimension.LANGUAGE_ZH, CountOperator.MIN, 3),
    ))
    req = RetrievalRequest(quota=q, criteria=RelevanceCriteria(topic="dl"), initial_query="q")
    res = run_retrieval_loop(req, deps=_deps(by_query),
                             limits=RetrievalLimits(max_iterations=2, max_no_progress_rounds=1),
                             planner=_FixedPlanner(["q", "q"]), clock=_clock())
    assert "language.zh" in res.unguaranteeable


def test_loop_trace_sink_privacy_safe():
    by_query = {"q": [_cand("10.1/a"), _cand("10.2/b")]}
    records = []
    req = RetrievalRequest(quota=_quota(total_min=2), criteria=RelevanceCriteria(topic="secret topic xyz"),
                           initial_query="secret query phrase")
    run_retrieval_loop(req, deps=_deps(by_query), limits=RetrievalLimits(),
                       planner=DeterministicQueryPlanner(), clock=_clock(),
                       trace_sink=records.append)
    assert records
    blob = str(records)
    assert "secret query phrase" not in blob        # 原始 query 不落
    assert all("query_hash" in r and "candidates" in r for r in records)


def test_loop_does_not_mutate_quota_or_fingerprint():
    by_query = {"q": [_cand("10.1/a"), _cand("10.2/b")]}
    quota = _quota(total_min=2)
    crit = RelevanceCriteria(topic="dl")
    fp_before = crit.fingerprint()
    filters_before = quota.eligibility_filters
    cons_before = quota.count_constraints
    req = RetrievalRequest(quota=quota, criteria=crit, initial_query="q", task_fingerprint=fp_before)
    run_retrieval_loop(req, deps=_deps(by_query), limits=RetrievalLimits(),
                       planner=DeterministicQueryPlanner(), clock=_clock())
    assert crit.fingerprint() == fp_before
    assert quota.eligibility_filters is filters_before
    assert quota.count_constraints is cons_before


# ============================================================ 结构验收

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "paper_agent"


def _imports(rel):
    tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("mod", [
    "literature/retrieval/budget.py",
    "literature/retrieval/query_planner.py",
    "literature/retrieval/loop.py",
])
def test_a2c4_no_tasking_config(mod):
    imports = _imports(mod)
    assert not any(m.startswith("paper_agent.tasking") for m in imports), (mod, imports)
    assert "paper_agent.config" not in imports, (mod, imports)


def test_loop_not_import_facts_or_discovery():
    imports = _imports("literature/retrieval/loop.py")
    assert "paper_agent.session.facts" not in imports
    # 不接 Discovery（属 A2c-5）：不 import discovery 模块
    assert "paper_agent.literature.retrieval.discovery" not in imports
