"""P1 验收：#10 理解阶段事件、#8 tool_choice 协议适配与重试收窄、#9 预算生效。"""

from __future__ import annotations

import json
import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.base import LLMProvider, Message
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tasking.budget import (
    BudgetExceeded,
    PlanningBudget,
    budget_from_config,
)
from paper_agent.tasking.clarifier import ConfirmDecision, UnmappedDisposition
from paper_agent.tasking.contracts import ConfirmedPlan, PlanningExit, PlanStatus
from paper_agent.tasking.planning_loop import run_planning_loop


class _It:
    def __init__(self, confirm=None):
        self._confirm = list(confirm or [])

    def ask_field(self, prompt):
        return ""

    def resolve_unmapped(self, item):
        return UnmappedDisposition("ignore")

    def confirm_plan(self, summary):
        return self._confirm.pop(0) if self._confirm else ConfirmDecision("accept")


class _RecordingBus:
    def __init__(self):
        self.events: list[tuple] = []

    def publish(self, kind, **payload):
        self.events.append((getattr(kind, "value", str(kind)), payload))

    def span(self, name):
        class _S:
            def __enter__(s):
                return s

            def __exit__(s, *e):
                return False
        return _S()

    def kinds(self):
        return [k for k, _ in self.events]


def _analyze(ops, unmapped=()):
    return json.dumps({
        "operations": [{"intent": i, "verbatim": v, "requirement_ids": []}
                       for i, v in ops],
        "requirements": [], "unmapped_text": list(unmapped),
    }, ensure_ascii=False)


def _extract(slots):
    return json.dumps(
        {k: v for k, v in slots.items()}, ensure_ascii=False)


# ============ #10：active 的 trace 必须能看到理解阶段 ============

def test_planning_loop_publishes_understanding_events():
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色引言")]),
        _extract({"section_id": "introduction"}),
    ])
    bus = _RecordingBus()
    out = run_planning_loop("润色引言", llm=llm, interactor=_It(),
                            current_sections=["introduction"], current_format="tex",
                            bus=bus)
    assert isinstance(out, ConfirmedPlan)
    kinds = bus.kinds()
    assert "operation_analysis" in kinds
    assert "pre_extract" in kinds
    assert "plan_validation" in kinds


def test_analysis_failure_is_visible_in_events():
    """解析失败时 trace 不能是空的——否则无从排查（#10 的动机）。"""
    llm = MockLLMProvider(scripted=["这不是 JSON"])
    bus = _RecordingBus()
    out = run_planning_loop("润色引言", llm=llm, interactor=_It(),
                            current_sections=["introduction"], current_format="tex",
                            bus=bus)
    assert isinstance(out, PlanningExit)
    analysis = [p for k, p in bus.events if k == "operation_analysis"][0]
    assert analysis["call_status"] == "parse_failed"
    assert analysis["error_category"]          # 能看到失败原因类别


def test_pre_extract_event_reports_call_status():
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色引言")]),
        _extract({"section_id": "introduction"}),
    ])
    bus = _RecordingBus()
    run_planning_loop("润色引言", llm=llm, interactor=_It(),
                      current_sections=["introduction"], current_format="tex", bus=bus)
    pre = [p for k, p in bus.events if k == "pre_extract"][0]
    assert pre["intent"] == "polish"
    assert pre["call_status"] == "success"


# ============ #8：tool_choice 由 provider 适配；重试只针对参数不支持 ============

def test_structured_call_passes_forced_tool_name_not_tool_choice():
    from paper_agent.tasking.extractors.base import structured_call

    seen = {}

    class _Spy(LLMProvider):
        def complete(self, messages, **opts):
            seen.update(opts)
            return MockLLMProvider(scripted=['{"a":1}'])._decide(messages)

        def stream(self, messages, *, cancel_token=None, **opts):
            yield from ()

    structured_call(_Spy(), [Message(role="user", content="x")],
                    tool_schema={"function": {"name": "extract_polish"}})
    assert seen.get("forced_tool_name") == "extract_polish"
    assert "tool_choice" not in seen        # 协议形态不由统一层拼


def test_retry_only_on_unsupported_parameter():
    from paper_agent.tasking.extractors.base import structured_call

    calls = {"n": 0}

    class _Unsupported(LLMProvider):
        def complete(self, messages, **opts):
            calls["n"] += 1
            if "forced_tool_name" in opts:
                raise TypeError("unexpected keyword argument 'forced_tool_name'")
            return MockLLMProvider(scripted=['{"a":1}'])._decide(messages)

        def stream(self, messages, *, cancel_token=None, **opts):
            yield from ()

    out = structured_call(_Unsupported(), [Message(role="user", content="x")],
                          tool_schema={"function": {"name": "f"}})
    assert calls["n"] == 2                  # 降级重试一次
    assert out.payload == {"a": 1}


def test_no_retry_on_timeout_or_rate_limit():
    """超时/限流不该重试（重复请求 + 延迟翻倍），交给 Resilient 层退避。"""
    from paper_agent.tasking.extractors.base import structured_call

    calls = {"n": 0}

    class _Timeout(LLMProvider):
        def complete(self, messages, **opts):
            calls["n"] += 1
            raise TimeoutError("request timed out")

        def stream(self, messages, *, cancel_token=None, **opts):
            yield from ()

    out = structured_call(_Timeout(), [Message(role="user", content="x")],
                          tool_schema={"function": {"name": "f"}})
    assert calls["n"] == 1                  # 只调一次
    assert out.call_status.value == "provider_failed"


def test_anthropic_provider_maps_forced_tool_name():
    from paper_agent.providers.llm.anthropic_provider import _to_anthropic_tools

    tools = _to_anthropic_tools([{"function": {"name": "f", "parameters": {}}}])
    assert tools[0]["name"] == "f"
    assert "input_schema" in tools[0]       # Anthropic 形态


# ============ #9：预算真正生效 ============

def test_input_too_long_rejected_without_llm_call():
    llm = MockLLMProvider(scripted=[_analyze([("polish", "x")])])
    budget = PlanningBudget(max_input_chars=10)
    out = run_planning_loop("这是一条非常非常长的消息" * 10, llm=llm, interactor=_It(),
                            current_sections=[], current_format="tex", budget=budget)
    assert isinstance(out, PlanningExit)
    assert out.status is PlanStatus.REJECTED
    assert "过长" in out.message
    assert llm.calls == []                  # 一次 provider 调用都没浪费


def test_too_many_operations_rejected_before_extraction():
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "a"), ("polish", "b"), ("polish", "c")]),
    ])
    budget = PlanningBudget(max_operations=2)
    out = run_planning_loop("三件事", llm=llm, interactor=_It(),
                            current_sections=["a"], current_format="tex", budget=budget)
    assert isinstance(out, PlanningExit)
    assert "3 个操作" in out.message
    assert len(llm.calls) == 1              # 只跑了 Analyzer，没进抽槽


def test_llm_call_budget_enforced():
    budget = PlanningBudget(max_llm_calls=1)
    with budget.measured():
        pass
    with pytest.raises(BudgetExceeded) as ei:
        with budget.measured():
            pass
    assert ei.value.kind == "too_many_llm_calls"


def test_provider_time_budget_counts_only_real_calls():
    budget = PlanningBudget(provider_time_budget_s=0.01)
    with budget.measured():
        pass
    budget.record_llm_call(1.0)             # 模拟一次慢调用
    with pytest.raises(BudgetExceeded) as ei:
        with budget.measured():
            pass
    assert ei.value.kind == "provider_time_exhausted"
    # 人机交互等待不计入：预算只在 measured() 内累加
    assert budget.provider_elapsed_s >= 1.0


def test_failed_call_still_consumes_budget():
    """失败调用也要计费，否则失败重试可无限进行。"""
    budget = PlanningBudget(max_llm_calls=5)
    with pytest.raises(RuntimeError):
        with budget.measured():
            raise RuntimeError("boom")
    assert budget.llm_calls == 1


def test_budget_from_config_reads_all_limits():
    from paper_agent.config import Config

    cfg = Config(
        planning_max_input_chars=123, planning_max_operations=4,
        planning_max_llm_calls=7, planning_provider_time_budget_s=42.0,
    )
    b = budget_from_config(cfg)
    assert (b.max_input_chars, b.max_operations, b.max_llm_calls,
            b.provider_time_budget_s) == (123, 4, 7, 42.0)


def test_budget_shared_across_revisions_in_active(tmp_path):
    """一个 Turn 一份预算：修订不会重置调用计数（否则 3 次修订 × N 抽取器失控）。"""
    from paper_agent.application.turn_service import TurnService
    from paper_agent.config import Config
    from paper_agent.runtime.handlers import HandlerContext
    from paper_agent.session.file_session import FileBackedSession

    src = tmp_path / "p.tex"
    src.write_text("\\section{Introduction}\nbody\n", encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))
    # 预算只允许 1 次 LLM 调用 → Analyzer 跑完即耗尽，抽槽前就被拦
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色引言")]),
        _extract({"section_id": "introduction"}),
    ])
    cfg = Config(intent_planning_mode="active", planning_max_llm_calls=1)
    svc = TurnService(config=cfg, llm=llm,
                      handler_context=HandlerContext(session=session, llm=llm),
                      interactor=_It())
    out = svc.handle_turn("润色引言", session=session, facts=None, legacy=lambda: None)
    assert out.planning_exit is not None
    assert "上限" in out.planning_exit.message
