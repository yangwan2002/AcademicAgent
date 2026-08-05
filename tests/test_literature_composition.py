"""写作前文献编排器组合装配 + 控制台文献 port + MockVerifyProvider（接线层）。

覆盖:flag 关不装配、mock 离线端到端可达 READY、非 SEARCH 源明确不可用、
port 非交互不误接受、MockVerifyProvider 三态。
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.application.console_literature_port import ConsoleLiteratureInteractor
from paper_agent.application.literature_composition import (
    CompositionUnavailable,
    build_writing_orchestrator,
)
from paper_agent.application.literature_flow import FlowState, WritingEvidenceRequest
from paper_agent.config import Config
from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
)
from paper_agent.literature.relevance import RelevanceCriteria
from paper_agent.providers.retrieval.capabilities import FetchStatus
from paper_agent.providers.retrieval.mock import MockRetrievalProvider, MockVerifyProvider
from paper_agent.session.facts import SessionFacts


class _Reviewer:
    """stub reviewer：恒判 relevant/high（走 tool call，同 test_literature_flow）。"""

    def complete(self, messages, **opts):
        from paper_agent.providers.llm.base import LLMResponse, ToolCall

        return LLMResponse(content="", tool_calls=[ToolCall(
            id="1", name="report_relevance",
            arguments={"verdict": "relevant", "confidence": "high"})])


def _cfg(**over):
    base = dict(literature_writing_integration=True, retrieval_provider="mock")
    base.update(over)
    return Config(**base)


def _quota(total_min=1):
    return QuotaSpec(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED,
                     count_constraints=(CountConstraint(CountDimension.TOTAL,
                                                        CountOperator.MIN, total_min),))


# ---------------------------------------------------------------- flag 门控

def test_returns_none_when_integration_disabled(tmp_path):
    facts = SessionFacts(path=str(tmp_path / "f.json"))
    orch = build_writing_orchestrator(
        Config(literature_writing_integration=False, retrieval_provider="mock"),
        writer_llm=_Reviewer(), retrieval_provider=MockRetrievalProvider(), facts=facts)
    assert orch is None


def test_non_search_provider_raises(tmp_path):
    facts = SessionFacts(path=str(tmp_path / "f.json"))
    with pytest.raises(CompositionUnavailable):
        build_writing_orchestrator(
            _cfg(retrieval_provider="api"),
            writer_llm=_Reviewer(), retrieval_provider=MockRetrievalProvider(), facts=facts)


# ---------------------------------------------------------------- A2e：真实 verify 源注入

def test_mock_registry_verify_sources_unchanged(tmp_path):
    from paper_agent.application.literature_composition import _build_registry

    reg = _build_registry(_cfg(retrieval_provider="mock"), MockRetrievalProvider())
    assert [np.name for np in reg.verify_sources] == ["mock"]


def test_real_registry_includes_crossref_openalex_arxiv(tmp_path, monkeypatch):
    from paper_agent.application.literature_composition import _build_registry
    from paper_agent.providers.retrieval.openalex import OpenAlexRetrievalProvider

    monkeypatch.delenv("S2_API_KEY", raising=False)
    reg = _build_registry(_cfg(retrieval_provider="openalex"), OpenAlexRetrievalProvider())
    names = [np.name for np in reg.verify_sources]
    assert names == ["crossref", "openalex", "arxiv"]      # 无 key → 不含 s2
    assert [np.name for np in reg.search_sources] == ["openalex"]


def test_real_registry_includes_s2_when_key_present(tmp_path, monkeypatch):
    from paper_agent.application.literature_composition import _build_registry
    from paper_agent.providers.retrieval.openalex import OpenAlexRetrievalProvider

    monkeypatch.setenv("S2_API_KEY", "x")
    reg = _build_registry(_cfg(retrieval_provider="openalex"), OpenAlexRetrievalProvider())
    assert "semantic_scholar" in [np.name for np in reg.verify_sources]


# ---------------------------------------------------------------- 离线端到端

def test_mock_offline_reaches_ready_to_write(tmp_path):
    """mock search + MockVerifyProvider 离线走通检索→核验→相关性→Bundle。

    样例须带摘要:无摘要 → 相关性 basis=title_only → 严格策略拒(正确领域行为)。
    """
    from paper_agent.workspace.models import ReferenceEntry

    entries = [
        ReferenceEntry(
            id="arxiv:1706.03762", title="Attention Is All You Need",
            authors=["Vaswani"], year=2017, source_id="1706.03762", source="arxiv",
            abstract="We propose the Transformer, based solely on attention mechanisms.",
            work_identifiers={"arxiv": "1706.03762"}),
    ]
    facts = SessionFacts(path=str(tmp_path / "f.json"))
    orch = build_writing_orchestrator(
        _cfg(), writer_llm=_Reviewer(),
        retrieval_provider=MockRetrievalProvider(entries=entries), facts=facts)
    assert orch is not None

    class _AutoPort:
        def confirm_retrieval_batch(self, s): return _D("accept")
        def resolve_quota_gap(self, r): return _D("accept")
        def confirm_evidence_bundle(self, s): return _D("accept")
        def resolve_final_gate(self, r): return _D("accept")

    req = WritingEvidenceRequest(
        criteria=RelevanceCriteria(topic="attention transformer"),
        citation_quota=_quota(1), initial_query="attention", operation_id="op1")
    out = orch.run(req, facts=facts, port=_AutoPort())
    assert out.state is FlowState.READY_TO_WRITE
    assert out.bundle is not None and out.bundle.entries
    assert len(facts.ledger) >= 1                    # 候选已入库


class _D:
    def __init__(self, action):
        self.action = action
        self.note = ""


# ------------------------------------------------- 证据集预算可配（X-10 回归）

def test_bundle_budget_defaults_to_config(tmp_path):
    """不传 budget_chars → 取 config 值,**不再**是硬编码 8000。"""
    facts = SessionFacts(path=str(tmp_path / "f.json"))
    orch = build_writing_orchestrator(
        _cfg(literature_bundle_budget_chars=31337), writer_llm=_Reviewer(),
        retrieval_provider=MockRetrievalProvider(), facts=facts)
    assert orch._budget_chars == 31337


def test_bundle_budget_explicit_arg_wins(tmp_path):
    """显式传值优先于 config（0=不限,dogfood runner 与测试依赖此路径）。"""
    facts = SessionFacts(path=str(tmp_path / "f.json"))
    orch = build_writing_orchestrator(
        _cfg(literature_bundle_budget_chars=31337), writer_llm=_Reviewer(),
        retrieval_provider=MockRetrievalProvider(), facts=facts, budget_chars=0)
    assert orch._budget_chars == 0


def test_bundle_budget_default_fits_ten_real_world_records(tmp_path):
    """默认预算须装得下「引言 10 篇」这一常见要求。

    锚点是真机实测（2026-08-02,10 条 OpenAlex 记录,平均 1521 字符/条,
    合计 15213）。旧默认 8000 只装 5 条 ⇒ `total=10` 恒定被
    CONTEXT_BUDGET_INSUFFICIENT 毙掉,操作 rejected。
    本测试守护「默认值不得回退到装不下 10 条」。
    """
    from paper_agent.config import Config

    per_record_chars = 1521
    assert Config().literature_bundle_budget_chars >= per_record_chars * 10


def test_bundle_budget_env_override(monkeypatch):
    """PAPER_LIT_BUNDLE_BUDGET_CHARS 可覆盖;非法值降级为默认;0 合法（不限）。"""
    from paper_agent.config import Config, load_config

    monkeypatch.setenv("PAPER_LIT_BUNDLE_BUDGET_CHARS", "50000")
    assert load_config().literature_bundle_budget_chars == 50000

    monkeypatch.setenv("PAPER_LIT_BUNDLE_BUDGET_CHARS", "0")
    assert load_config().literature_bundle_budget_chars == 0

    monkeypatch.setenv("PAPER_LIT_BUNDLE_BUDGET_CHARS", "not-a-number")
    assert (load_config().literature_bundle_budget_chars
            == Config().literature_bundle_budget_chars)

    monkeypatch.setenv("PAPER_LIT_BUNDLE_BUDGET_CHARS", "-5")
    assert (load_config().literature_bundle_budget_chars
            == Config().literature_bundle_budget_chars)


# ---------------------------------------------------------------- MockVerifyProvider 三态

def test_mock_verify_found_and_not_found():
    v = MockVerifyProvider()
    ok = v.fetch_metadata("arxiv", "1706.03762")
    assert ok.status is FetchStatus.FOUND and ok.candidate is not None
    miss = v.fetch_metadata("doi", "10.9999/nope")
    assert miss.status is FetchStatus.NOT_FOUND_AT_SOURCE   # 明确未命中，非 provider 错误


def test_mock_verify_bibliographic_empty_on_no_match():
    from paper_agent.providers.retrieval.capabilities import BibliographicQuery

    v = MockVerifyProvider()
    assert v.search_bibliographic(BibliographicQuery(title="totally unrelated xyz")) == []
    hits = v.search_bibliographic(BibliographicQuery(title="Attention Is All You Need"))
    assert hits and "Attention" in hits[0].title


# ---------------------------------------------------------------- 控制台 port 非交互纪律

def _port(answers):
    it = iter(answers)
    return ConsoleLiteratureInteractor(ask=lambda _p: next(it), out=lambda _m: None)


def test_port_yes_accepts():
    assert _port(["y"]).confirm_retrieval_batch("s").action == "accept"
    assert _port(["y"]).confirm_evidence_bundle("s").action == "accept"
    assert _port(["y"]).resolve_quota_gap("r").action == "accept"
    assert _port(["y"]).resolve_final_gate("r").action == "accept"


def test_port_no_rejects_or_cancels():
    assert _port(["n"]).confirm_retrieval_batch("s").action == "reject"
    assert _port(["whatever"]).resolve_quota_gap("r").action == "cancel"
    assert _port(["no"]).resolve_final_gate("r").action == "cancel"


def test_port_abort_never_accepts():
    """Ctrl-D/中止 → needs_input，绝不误判为接受放宽配额。"""
    def _eof(_p):
        raise EOFError

    port = ConsoleLiteratureInteractor(ask=_eof, out=lambda _m: None)
    assert port.resolve_final_gate("r").action == "needs_input"
    assert port.confirm_retrieval_batch("s").action == "needs_input"

    quit_port = _port([":quit"])
    assert quit_port.resolve_quota_gap("r").action == "needs_input"
