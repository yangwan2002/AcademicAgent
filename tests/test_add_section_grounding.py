"""add_section 事实依据与可观测性回归。

真实 E2E 曾在只有模型组件和一个结果概述的文档中，凭空写入“自我报告数据、
横断面设计、样本子集稳健性、混淆变量”等事实。本文件钉死：这类候选不得写入，
且 trace 必须能看到 generation_attempt / llm_call / gate 完整闭环。
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.gates.grounding_gate import AddSectionGroundingGate
from paper_agent.intents.add_section import execute_add_section
from paper_agent.intents.plans import AddSectionPlan
from paper_agent.observability.bus import EventBus, RunContext
from paper_agent.observability.events import Event, EventKind
from paper_agent.errors import OperationCancelled
from paper_agent.providers.llm.base import CancellationToken, LLMResponse, ToolCall
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.session.facts import SessionFacts
from paper_agent.session.file_session import FileBackedSession
from paper_agent.tools.citation import CitationVerifier


SOURCE = """# Introduction

The proposed method contains an encoder, a matcher, and a lightweight decoder.

# Results

The manuscript reports that the method improves the recorded result.

# Conclusion

The current conclusion is brief and does not explain limitations.
"""

HALLUCINATED = """## 结果解释

稳健性分析表明，核心变量之间的关系在不同样本子集中仍然成立，因此结果并非由极端值驱动。

## 局限性

本研究的数据收集主要依赖自我报告，可能受到回忆偏差影响。本研究采用横断面设计，无法确证变量之间的因果顺序。部分混淆变量虽然经过统计调节，仍可能留下残余干扰。这些限制共同影响了结论的推广。

## 未来工作

未来可以补充作者实际掌握的研究设计、数据来源和结果材料，再讨论可执行的后续方向。"""

GROUNDED = (
    "The current manuscript states that the proposed method contains an encoder, "
    "a matcher, and a lightweight decoder. Its Results section records an improved "
    "result, and its Conclusion explicitly says that the present discussion of "
    "limitations is brief. This new section therefore confines itself to those "
    "three statements. It does not introduce a new study design, data source, "
    "experiment, comparison, or mechanism. Stronger claims require additional "
    "material from the author."
)


class CaptureSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class CancelInsideGroundingVerifier:
    """Generation succeeds; the synchronous grounding verifier cancels then returns supported."""

    model_name = "cancel-inside-grounding-verifier"

    def __init__(self, token: CancellationToken) -> None:
        self.token = token
        self.calls = []

    def complete(self, messages, **opts):
        self.calls.append((messages, opts))
        if opts.get("tools"):
            self.token.cancel()
            return LLMResponse(
                content="",
                tool_calls=[_verdict(supported=True, reason="supported", claims=())],
            )
        return LLMResponse(content=GROUNDED)


def _session(tmp_path):
    source = tmp_path / "paper.md"
    source.write_text(SOURCE, encoding="utf-8")
    return FileBackedSession(str(source), workspace_root=str(tmp_path / "ws"))


def _plan() -> AddSectionPlan:
    return AddSectionPlan(
        title="Discussion",
        purpose="解释结果意义和研究局限",
        position="before:conclusion",
        section_type="discussion",
        structure_kind="desc",
        structure_desc="结果解释、局限性、未来工作",
    )


def _verdict(*, supported: bool, reason: str, claims=()) -> ToolCall:
    return ToolCall(
        id="ground-1",
        name="verify_add_section_grounding",
        arguments={
            "supported": supported,
            "reason_code": reason,
            "unsupported_claims": list(claims),
        },
    )


def _execute(tmp_path, llm, *, bus=None):
    session = _session(tmp_path)
    facts = SessionFacts(path=str(tmp_path / "facts.json"))
    retrieval = MockRetrievalProvider()
    result = execute_add_section(
        session,
        _plan(),
        facts,
        llm,
        retrieval_provider=retrieval,
        verifier=CitationVerifier(retrieval),
        confirm=lambda _value, _context: False,
        max_retry=0,
        bus=bus,
    )
    return result, session


def test_real_sample_methodology_hallucinations_are_rejected_deterministically():
    # 即使语义 verifier 想放行，高风险事实只出现在候选、不在材料中，也先被确定性拒绝。
    llm = MockLLMProvider(scripted=[_verdict(
        supported=True, reason="supported", claims=()
    )])
    result = AddSectionGroundingGate(llm).check(HALLUCINATED, SOURCE)

    assert not result.passed
    assert result.reason_code == "unsupported_facts"
    assert result.unsupported_count >= 2
    assert llm.calls == []


def test_real_sample_is_not_written_and_generation_gate_are_observable(tmp_path):
    sink = CaptureSink()
    bus = EventBus(sink, RunContext(run_id="grounding-run"))
    result, session = _execute(
        tmp_path, MockLLMProvider(scripted=[HALLUCINATED]), bus=bus
    )

    assert not result.ok
    assert session.read_working() == SOURCE
    assert "事实" in result.message and "摘要" in result.message

    kinds = [e.kind for e in sink.events]
    assert kinds.count(EventKind.GENERATION_ATTEMPT_START) == 1
    assert kinds.count(EventKind.GENERATION_ATTEMPT_END) == 1
    assert kinds.count(EventKind.LLM_CALL_START) == 1
    assert kinds.count(EventKind.LLM_CALL_END) == 1
    assert kinds.count(EventKind.GATE_START) == 1
    assert kinds.count(EventKind.GATE_RESULT) == 1
    spans = [e.data.get("name") for e in sink.events if e.kind is EventKind.SPAN_START]
    assert "generation_attempt" in spans
    assert "llm_call" in spans
    assert "gate" in spans
    gate = next(e for e in sink.events if e.kind is EventKind.GATE_RESULT)
    assert gate.data["passed"] is False
    assert gate.data["grounding_passed"] is False
    assert gate.data["grounding_unsupported_count"] >= 2


def test_grounded_candidate_sees_working_copy_and_can_commit(tmp_path):
    llm = MockLLMProvider(scripted=[
        GROUNDED,
        _verdict(supported=True, reason="supported", claims=()),
    ])
    result, session = _execute(tmp_path, llm)

    assert result.ok, result.message
    assert "# Discussion" in session.read_working()
    assert len(llm.calls) == 2
    # 生成与独立 verifier 使用同一份事实材料；不再只看到 title/purpose 模板。
    assert "encoder, a matcher" in llm.calls[0][1].content
    assert "encoder, a matcher" in llm.calls[1][1].content
    assert GROUNDED in llm.calls[1][1].content


def test_cancelled_while_grounding_verifier_returns_supported_never_commits(tmp_path):
    token = CancellationToken()
    llm = CancelInsideGroundingVerifier(token)
    sink = CaptureSink()
    bus = EventBus(sink, RunContext(run_id="grounding-cancel-run"))
    session = _session(tmp_path)
    facts = SessionFacts(path=str(tmp_path / "facts.json"))
    retrieval = MockRetrievalProvider()

    with pytest.raises(OperationCancelled):
        execute_add_section(
            session,
            _plan(),
            facts,
            llm,
            retrieval_provider=retrieval,
            verifier=CitationVerifier(retrieval),
            confirm=lambda _value, _context: False,
            max_retry=0,
            bus=bus,
            cancel_token=token,
        )

    assert session.read_working() == SOURCE
    assert len(llm.calls) == 2, "generation and grounding verifier should both complete"
    assert not any(e.kind is EventKind.GATE_RESULT for e in sink.events)

    attempt_end = next(
        e for e in sink.events if e.kind is EventKind.GENERATION_ATTEMPT_END
    )
    assert attempt_end.data["status"] == "cancelled"
    span_ends = {
        e.data.get("name"): e.data.get("span_state")
        for e in sink.events
        if e.kind is EventKind.SPAN_END
    }
    assert span_ends["gate"] == "cancelled"
    assert span_ends["generation_attempt"] == "cancelled"


def test_title_only_search_failure_does_not_recommend_repeating_search(tmp_path):
    session = _session(tmp_path)
    facts = SessionFacts(path=str(tmp_path / "facts.json"))
    retrieval = MockRetrievalProvider()
    plan = _plan()
    plan.want_search = True
    plan.search_query = "grounded discussion"

    result = execute_add_section(
        session,
        plan,
        facts,
        MockLLMProvider(scripted=[HALLUCINATED]),
        retrieval_provider=retrieval,
        verifier=CitationVerifier(retrieval),
        confirm=lambda _value, _context: False,
        max_retry=0,
    )

    assert not result.ok
    assert session.read_working() == SOURCE
    assert "题录" in result.message and "摘要" in result.message
    assert "先检索" not in result.message


def test_malformed_grounding_verdict_fails_closed(tmp_path):
    llm = MockLLMProvider(scripted=[GROUNDED, "{}"])
    result, session = _execute(tmp_path, llm)

    assert not result.ok
    assert session.read_working() == SOURCE
    assert "严格模式" in result.message
