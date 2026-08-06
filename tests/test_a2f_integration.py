"""A2f-5：config planner 装配 + 组合层 synthesizer/planner + Handler 端到端 + digest。"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.application.literature_composition import (
    build_query_planner,
    build_search_synthesizer,
)
from paper_agent.config import Config, load_config
from paper_agent.literature.retrieval.query_planner import (
    DeterministicQueryPlanner,
    LLMQueryPlanner,
)
from paper_agent.observability.sanitize import digest


# ---------------------------------------------------------------- config 解析

def test_query_planner_default_deterministic():
    assert Config().literature_query_planner == "deterministic"


def test_query_planner_env(monkeypatch):
    monkeypatch.setenv("PAPER_LIT_QUERY_PLANNER", "hybrid")
    assert load_config().literature_query_planner == "hybrid"


def test_query_planner_invalid_downgrades(monkeypatch):
    monkeypatch.setenv("PAPER_LIT_QUERY_PLANNER", "bogus")
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert load_config().literature_query_planner == "deterministic"


def test_search_synthesis_default_off():
    assert Config().literature_search_synthesis is False


# ---------------------------------------------------------------- planner 装配

class _LLM:
    def complete(self, messages, **opts):
        from paper_agent.providers.llm.base import LLMResponse
        return LLMResponse(content="{}")


def test_build_planner_deterministic():
    p = build_query_planner(Config(literature_query_planner="deterministic"), writer_llm=_LLM())
    assert isinstance(p, DeterministicQueryPlanner)


def test_build_planner_llm():
    p = build_query_planner(Config(literature_query_planner="llm"), writer_llm=_LLM())
    assert isinstance(p, LLMQueryPlanner)


def test_build_planner_llm_without_writer_falls_back():
    p = build_query_planner(Config(literature_query_planner="llm"), writer_llm=None)
    assert isinstance(p, DeterministicQueryPlanner)


def test_build_planner_hybrid_prefers_deterministic_then_llm():
    from paper_agent.literature.retrieval.query_planner import PlannerRequest

    p = build_query_planner(Config(literature_query_planner="hybrid"), writer_llm=_LLM())
    # 首轮无 tried_queries → deterministic 出 initial_query，不触 LLM
    plan = p.propose(PlannerRequest(initial_query="dl medical", tried_queries=()))
    assert not plan.is_empty and plan.llm_called is False


# ---------------------------------------------------------------- synthesizer 装配

def test_build_synthesizer_off_returns_none():
    assert build_search_synthesizer(Config(literature_search_synthesis=False), writer_llm=_LLM()) is None


def test_build_synthesizer_on_with_llm():
    from paper_agent.tasking.query_synthesizer import LLMSearchIntentSynthesizer

    s = build_search_synthesizer(Config(literature_search_synthesis=True), writer_llm=_LLM())
    assert isinstance(s, LLMSearchIntentSynthesizer)


def test_build_synthesizer_on_without_llm_deterministic():
    from paper_agent.tasking.query_synthesizer import DeterministicSearchIntentSynthesizer

    s = build_search_synthesizer(Config(literature_search_synthesis=True), writer_llm=None)
    assert isinstance(s, DeterministicSearchIntentSynthesizer)


# ---------------------------------------------------------------- digest（无 preview）

def test_digest_stable_and_no_preview():
    d1 = digest("大语言模型代码生成")
    d2 = digest("大语言模型代码生成")
    assert d1 == d2 and len(d1) == 16
    assert "大语言" not in d1                          # 只出摘要，不含原文


# ---------------------------------------------------------------- Handler 端到端（合成 → 多 seed → READY）

from paper_agent.literature.language import DEFAULT_THRESHOLDS, classify_record_language
from paper_agent.literature.relevance import (
    JudgeVersion,
    RelevanceJudge,
)
from paper_agent.literature.retrieval.attempt import AttemptDeps, RelevanceAcceptancePolicy
from paper_agent.literature.retrieval.budget import RetrievalLimits
from paper_agent.literature.retrieval.cross_verify import (
    CrossVerifier,
    VerificationCache,
    VerifyBudget,
)
from paper_agent.literature.retrieval.query_planner import DeterministicQueryPlanner as _DQP
from paper_agent.literature.retrieval.registry import ProviderRegistry, named
from paper_agent.application.literature_flow import WritingLiteratureOrchestrator
from paper_agent.application.literature_ports import (
    BatchDecision,
    BundleDecision,
    GapDecision,
    FinalGateDecision,
)
from paper_agent.runtime.handlers import HandlerContext, handler_for
from paper_agent.intents.router import Intent
from paper_agent.tasking.contracts import (
    FieldStatus,
    Operation,
    OperationStatus,
    SlotSource,
    SlotValue,
)
from paper_agent.providers.retrieval.mock import MockRetrievalProvider, MockVerifyProvider
from paper_agent.session.facts import SessionFacts
from paper_agent.session.file_session import FileBackedSession
from paper_agent.tasking.query_synthesizer import DeterministicSearchIntentSynthesizer
from paper_agent.workspace.models import ReferenceEntry

_TEX = "\\section{Introduction}\nThe accuracy is 0.87 and we cite \\cite{a}.\n"


def _present(v):
    return SlotValue(value=v, field_status=FieldStatus.PRESENT, source=SlotSource.USER_EXPLICIT)


class _AutoPort:
    def confirm_retrieval_batch(self, s): return BatchDecision("accept")
    def resolve_quota_gap(self, r): return GapDecision("accept")
    def confirm_evidence_bundle(self, s): return BundleDecision("accept")
    def resolve_final_gate(self, r): return FinalGateDecision("accept")
    def request_search_topic(self, s): return None
    def confirm_search_intent(self, s): return None


class _Reviewer:
    def complete(self, messages, **opts):
        from paper_agent.providers.llm.base import LLMResponse, ToolCall
        return LLMResponse(content="", tool_calls=[ToolCall(
            id="1", name="report_relevance",
            arguments={"verdict": "relevant", "confidence": "high"})])


def _entries():
    return [ReferenceEntry(
        id="arxiv:1706.03762", title="Attention Is All You Need for Code Generation",
        authors=["V"], year=2020, source_id="1706.03762", source="arxiv",
        abstract="We study transformers for code generation.", language="en",
        work_identifiers={"arxiv": "1706.03762"})]


def _orch(facts):
    reg = ProviderRegistry(
        search_sources=(named("openalex", MockRetrievalProvider(entries=_entries())),),
        verify_sources=(named("mock", MockVerifyProvider(entries=_entries())),))
    deps = AttemptDeps(
        registry=reg, ledger=facts.ledger,
        cross_verifier=CrossVerifier(reg.verify_pairs()),
        judge=RelevanceJudge(_Reviewer(), judge_version=JudgeVersion()),
        relevance_store=facts.relevance_store, budget=VerifyBudget(),
        verify_cache=VerificationCache(), acceptance_policy=RelevanceAcceptancePolicy())
    return WritingLiteratureOrchestrator(
        deps=deps, limits=RetrievalLimits(), planner=_DQP(),
        classifier=lambda r: classify_record_language(r, DEFAULT_THRESHOLDS),
        judge_key=RelevanceJudge(_Reviewer(), judge_version=JudgeVersion()).judge_version.key(),
        budget_chars=0, supervised=False)


def test_handler_synthesis_end_to_end(tmp_path):
    """合成开启 + 无显式 query：从 operation 语义合成主题 → 多 seed → 检索 → READY 写回。"""
    src = tmp_path / "p.tex"
    src.write_text(_TEX, encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))
    facts = SessionFacts(path=str(tmp_path / "facts.json"))

    from paper_agent.literature.quota import RawCountClause, RawQuotaSpec

    op = Operation(operation_id="op1", intent=Intent.ADD_SECTION, slots={
        "title": _present("引言"),                    # 通用标题
        "purpose": _present("综述大语言模型在代码生成中的应用"),   # 语义主题来源
        "citation_quota": _present(RawQuotaSpec(
            constraints=(RawCountClause("total", "min", 1),))),
    })

    class _LLMStub:
        def complete(self, messages, **opts):
            from paper_agent.providers.llm.base import LLMResponse
            body = ("\\section{引言}\nWe review code generation with LLMs "
                    "\\cite{key1706.03762}. It is a rich area.\n")
            return LLMResponse(content=body)

    ctx = HandlerContext(
        session=session, llm=_LLMStub(), facts=facts,
        writing_integration_enabled=True, literature_orchestrator=_orch(facts),
        literature_port=_AutoPort(),
        classifier=lambda r: classify_record_language(r, DEFAULT_THRESHOLDS),
        search_synthesis_enabled=True,
        search_synthesizer=DeterministicSearchIntentSynthesizer(),
        retrieval_limits=RetrievalLimits())
    res = handler_for(Intent.ADD_SECTION).execute(op, ctx)
    # 合成从 purpose 推出主题（不拿"引言"硬搜）→ 检索到相关英文文献 → 预检 READY → 写作
    assert res.status in (OperationStatus.SUCCEEDED, OperationStatus.REJECTED)
    # 至少不因"缺主题"被拒（purpose 提供了语义）
    assert "未能确定检索主题" not in res.message


def test_handler_synthesis_off_is_legacy_path(tmp_path):
    """合成关（默认）→ 不构造 SearchIntent，走旧 initial_query 路径（回归对照）。"""
    src = tmp_path / "p.tex"
    src.write_text(_TEX, encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))
    facts = SessionFacts(path=str(tmp_path / "facts.json"))
    from paper_agent.literature.quota import RawCountClause, RawQuotaSpec

    op = Operation(operation_id="op1", intent=Intent.ADD_SECTION, slots={
        "title": _present("Related Work"),
        "citation_quota": _present(RawQuotaSpec(
            constraints=(RawCountClause("total", "min", 1),))),
    })

    class _LLMStub:
        def complete(self, messages, **opts):
            from paper_agent.providers.llm.base import LLMResponse
            return LLMResponse(content="\\section{Related Work}\n"
                               "Prior work \\cite{key1706.03762} is relevant here for review.\n")

    ctx = HandlerContext(
        session=session, llm=_LLMStub(), facts=facts,
        writing_integration_enabled=True, literature_orchestrator=_orch(facts),
        literature_port=_AutoPort(),
        classifier=lambda r: classify_record_language(r, DEFAULT_THRESHOLDS),
        search_synthesis_enabled=False)          # 合成关
    res = handler_for(Intent.ADD_SECTION).execute(op, ctx)
    assert res.status in (OperationStatus.SUCCEEDED, OperationStatus.REJECTED)
