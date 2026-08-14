"""只读问答的观测埋点（P-4 补齐）——事件真的进 trace.json，且问句原话不落盘。

**为什么必须走真 Coordinator 而不是 spy 掉 bus**：`recorder.py` 的
`_record_event` 有一张显式 kind 白名单，不在表里的事件会被静默丢弃。
只断言"publish 被调了 3 次"会在漏加白名单时照样绿 —— 那正是这次要修的
盲区的形状（观测里「发了事件」≠「trace 里有」）。所以这里跑完整 run 并
**读落盘的 trace.json**。

隐私口径与 A2f 检索一致：问句是整句用户原话，只出 hash 与长度。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_agent.application.ask_context import (
    READ_SECTION_MAX_CHARS,
    capture,
)
from paper_agent.application.ask_loop import AskStopReason, run_ask
from paper_agent.coordinator import Coordinator
from paper_agent.observability.events import EventKind
from paper_agent.providers.llm.base import LLMResponse, ToolCall

_DOC = """# 1 引言
空地协同的视觉关联需求日益突出 [1]。

# 2 方法
本文提出的方法基于跨视角匹配。
"""


class _FakeSession:
    def __init__(self, text: str = _DOC, ext: str = "md") -> None:
        self._text = text
        self.ext = ext

    def read_working(self) -> str:
        return self._text


class _ScriptedLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def complete(self, messages, **opts):
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="（脚本用尽）")


def _tool_resp(name, args=None, cid="c1"):
    return LLMResponse(
        content="", tool_calls=[ToolCall(id=cid, name=name, arguments=args or {})]
    )


def _all_events_in(node) -> list:
    """递归收集 span 树里所有 attributes['events']（同 test_planning_trace_events）。"""
    out = list(node.get("attributes", {}).get("events", []))
    for c in node.get("children", []):
        out.extend(_all_events_in(c))
    return out


def _run_with_trace(tmp_path, question, llm, session=None, **kw):
    """跑一次带 trace 的问答，返回 (AskResult, trace.json 里的事件列表, 原始 doc)。"""
    ctx = capture(session or _FakeSession())
    coordinator = Coordinator(enable_trace=True, runs_dir=str(tmp_path))
    report = coordinator.run(lambda bus, ct: run_ask(question, ctx, llm, bus=bus, **kw))
    path = os.path.join(str(tmp_path), report.run_id, "trace.json")
    doc = json.loads(open(path, encoding="utf-8").read())
    return report.value, _all_events_in(doc["root"]), doc


# ---- 事件种类存在且值稳定 ----


def test_ask_kinds_present_and_stable():
    assert EventKind.ASK_TURN_START.value == "ask_turn_start"
    assert EventKind.ASK_TOOL_CALL.value == "ask_tool_call"
    assert EventKind.ASK_TURN_END.value == "ask_turn_end"


# ---- 事件进 trace.json ----


def test_ask_events_reach_trace_json(tmp_path):
    """三种事件都要落盘。白名单漏一个，这条就红。"""
    llm = _ScriptedLLM([
        _tool_resp("list_sections"),
        _tool_resp("read_section", {"section_id": "1_引言"}, cid="c2"),
        LLMResponse(content="引言讲空地协同的需求。"),
    ])
    res, events, _ = _run_with_trace(tmp_path, "引言写了什么", llm)

    assert res.ok
    kinds = [e["kind"] for e in events]
    assert "ask_turn_start" in kinds
    assert "ask_tool_call" in kinds
    assert "ask_turn_end" in kinds


def test_tool_calls_are_individually_traceable(tmp_path):
    """每次工具调用一条事件，且能看出调了哪个、第几轮。

    "它查了什么"是这条通路最主要的观测需求：react 的路径由模型决定，
    不逐次记录就只能看到一个总数。
    """
    llm = _ScriptedLLM([
        _tool_resp("list_sections"),
        _tool_resp("doc_stats", cid="c2"),
        LLMResponse(content="共 2 章。"),
    ])
    _, events, _ = _run_with_trace(tmp_path, "有几章", llm)

    calls = [e for e in events if e["kind"] == "ask_tool_call"]
    assert [c["data"]["tool"] for c in calls] == ["list_sections", "doc_stats"]
    assert [c["data"]["iteration"] for c in calls] == [1, 2]


def test_turn_end_carries_stop_reason_and_counts(tmp_path):
    llm = _ScriptedLLM([
        _tool_resp("list_sections"),
        LLMResponse(content="共 2 章。"),
    ])
    res, events, _ = _run_with_trace(tmp_path, "有几章", llm)

    end = next(e for e in events if e["kind"] == "ask_turn_end")
    assert end["data"]["stop_reason"] == "answered"
    assert end["data"]["ok"] is True
    assert end["data"]["llm_calls"] == res.llm_calls == 2
    assert end["data"]["tool_calls"] == ["list_sections"]


# ---- 终态归因 ----


def test_guardrail_stop_is_visible_in_trace(tmp_path):
    """护栏终态必须能从 trace 看出来 —— 否则「答得不全」事后无法归因。

    这是护栏第 3 条（超限不报 answered）在观测面的对应物：结果对象里
    分得清，trace 里也必须分得清。
    """
    llm = _ScriptedLLM([_tool_resp("list_sections", cid=f"c{i}") for i in range(6)])
    res, events, _ = _run_with_trace(tmp_path, "有几章", llm, max_iterations=2)

    assert res.stop_reason is AskStopReason.ITERATION_LIMIT
    end = next(e for e in events if e["kind"] == "ask_turn_end")
    assert end["data"]["stop_reason"] == "iteration_limit"
    assert end["data"]["ok"] is False


def test_provider_failure_visible_in_trace(tmp_path):
    class _Boom:
        def complete(self, messages, **opts):
            raise RuntimeError("接口挂了")

    res, events, _ = _run_with_trace(tmp_path, "有几章", _Boom())

    assert res.stop_reason is AskStopReason.PROVIDER_FAILED
    end = next(e for e in events if e["kind"] == "ask_turn_end")
    assert end["data"]["stop_reason"] == "provider_failed"
    assert "RuntimeError" in end["data"]["error"]


def test_dispatch_failure_marked_on_the_tool_event(tmp_path):
    """dispatch 失败不是终态（错误回灌让模型改），但要在那条工具事件上留痕。"""
    llm = _ScriptedLLM([
        _tool_resp("read_section", {"section_id": "不存在的章"}),
        LLMResponse(content="没有这一章。"),
    ])
    res, events, _ = _run_with_trace(tmp_path, "第九章写了什么", llm)

    assert res.ok           # 循环照常收敛
    call = next(e for e in events if e["kind"] == "ask_tool_call")
    assert call["data"]["failed"] is True
    end = next(e for e in events if e["kind"] == "ask_turn_end")
    assert end["data"]["stop_reason"] == "answered"


def test_truncation_is_visible_in_trace(tmp_path):
    """截断是降级，必须可见 —— 事后要能看出模型只读了半章就下了断言。"""
    long_doc = "# 1 引言\n" + ("这是一段很长的正文。" * (READ_SECTION_MAX_CHARS // 5))
    llm = _ScriptedLLM([
        _tool_resp("read_section", {"section_id": "1_引言"}),
        LLMResponse(content="引言很长。"),
    ])
    _, events, _ = _run_with_trace(
        tmp_path, "引言写了什么", llm, session=_FakeSession(long_doc)
    )

    call = next(e for e in events if e["kind"] == "ask_tool_call")
    assert call["data"]["truncated"] is True
    assert call["data"]["result_len"] > 0


def test_short_result_not_marked_truncated(tmp_path):
    """反面：没截断时不能标 truncated，否则这个字段等于恒真、没有信息。"""
    llm = _ScriptedLLM([
        _tool_resp("read_section", {"section_id": "1_引言"}),
        LLMResponse(content="引言很短。"),
    ])
    _, events, _ = _run_with_trace(tmp_path, "引言写了什么", llm)

    call = next(e for e in events if e["kind"] == "ask_tool_call")
    assert call["data"]["truncated"] is False


# ---- 隐私：问句原话不落盘 ----


def test_question_text_never_reaches_trace(tmp_path):
    """整句问句是用户原话，只出 hash 与长度。

    断言方式刻意扫**整份 trace.json 文本**而不是只看那一个事件的 data：
    原话可能从别的字段泄出去（`message` 形参、span attrs），只查一处会假绿。
    """
    secret = "我们实验室的 K9 项目里那个未公开的传感器标定问题"
    llm = _ScriptedLLM([LLMResponse(content="文档里看不出来。")])
    _, events, doc = _run_with_trace(tmp_path, secret, llm)

    blob = json.dumps(doc, ensure_ascii=False)
    assert secret not in blob
    # 连片段都不能有（避免"只截断了尾巴"式的假通过）。
    assert "K9 项目" not in blob

    start = next(e for e in events if e["kind"] == "ask_turn_start")
    assert start["data"]["question_len"] == len(secret)
    assert len(start["data"]["question_hash"]) == 16      # digest 默认长度


def test_argument_values_not_traced_only_keys(tmp_path):
    """工具参数只出键名：`section_id` 的值是文档内容派生物（标题 slug）。"""
    llm = _ScriptedLLM([
        _tool_resp("read_section", {"section_id": "1_引言"}),
        LLMResponse(content="好。"),
    ])
    _, events, _ = _run_with_trace(tmp_path, "引言写了什么", llm)

    call = next(e for e in events if e["kind"] == "ask_tool_call")
    assert call["data"]["argument_keys"] == ["section_id"]
    assert "1_引言" not in json.dumps(call["data"], ensure_ascii=False)


# ---- 观测缺失不改变业务行为 ----


def test_no_bus_behaves_identically():
    """不传 bus 时行为逐字节不变 —— 只读通路不能因观测装配缺失而失效。"""
    ctx = capture(_FakeSession())
    llm = _ScriptedLLM([
        _tool_resp("list_sections"),
        LLMResponse(content="共 2 章。"),
    ])
    res = run_ask("有几章", ctx, llm)

    assert res.ok
    assert res.answer == "共 2 章。"
    assert res.tool_calls == ["list_sections"]


def test_broken_bus_does_not_break_the_answer():
    """bus 自身抛异常也不能拖垮问答（观测永不改变业务可见行为）。"""
    class _AngryBus:
        def publish(self, *a, **k):
            raise RuntimeError("观测炸了")

        def span(self, *a, **k):
            raise RuntimeError("span 也炸了")

    ctx = capture(_FakeSession())
    llm = _ScriptedLLM([LLMResponse(content="共 2 章。")])
    res = run_ask("有几章", ctx, llm, bus=_AngryBus())

    assert res.ok and res.answer == "共 2 章。"
