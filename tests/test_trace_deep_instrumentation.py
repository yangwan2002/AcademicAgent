"""深层埋点验收：`structured_call` / 检索 Loop / OPERATION_RESULT.message / TurnMetrics.load。

背景（backlog 第七节「能力写对了，接线送不到」的观测层形态）：`literature/` 整个目录
零 bus 接线 ⇒ 真机一次 1057 秒的 run 只有 2 个 span，其中 945 秒的 `operation:*` 是
`attributes={} children=[]`；四次 rejected/failed 的原因一次都没进 trace。

本文件按「注入原 bug 应变红」写（见 backlog 测试纪律）：
- 去掉 `structured_call` 的 span → `test_structured_call_emits_span` 红；
- 去掉 `_run_one` 的 span/事件 → `test_retrieval_attempt_emits_span` 红；
- `_emit_op_result` 不再发 message → `test_operation_result_carries_message` 红；
- 删掉 `TurnMetrics.load` → 三个 load 测试红。
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.observability.bus import EventBus, NullEventBus, RunContext
from paper_agent.observability.current import (
    ambient_publish,
    ambient_span,
    current_bus,
    use_bus,
)
from paper_agent.observability.events import EventKind
from paper_agent.observability.health import ObservabilityHealth
from paper_agent.observability.turn_metrics import TurnMetrics
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.providers.llm.structured import CallStatus, structured_call


class Capture:
    """收集所有事件的 EventSink。"""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def kinds(self):
        return [e.kind for e in self.events]

    def span_names(self):
        return [
            e.data.get("name")
            for e in self.events
            if e.kind is EventKind.SPAN_START
        ]

    def of_kind(self, kind):
        return [e for e in self.events if e.kind is kind]


def _bus(capture):
    return EventBus(capture, RunContext("run_test"), ObservabilityHealth())


_TOOL = {
    "type": "function",
    "function": {
        "name": "report_relevance",
        "description": "t",
        "parameters": {"type": "object", "properties": {}},
    },
}


class _ToolLLM:
    """回一次合法 tool_call 的 provider。"""

    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload if payload is not None else {"ok": True}
        self._raise = raise_exc
        self.calls = 0

    def complete(self, messages, **kw):
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="report_relevance", arguments=self._payload)],
        )


# --- ambient bus 基础语义 ---------------------------------------------------


def test_ambient_bus_absent_outside_run():
    """run 外没有 ambient bus；埋点必须安静退化，不抛。"""
    assert current_bus() is None
    with ambient_span("x") as span:
        assert span is None
    ambient_publish("RETRIEVAL_START", query_hash="h")   # 不抛即通过


def test_use_bus_restores_on_exception():
    """异常路径也必须还原 —— 否则 bus 泄漏到下一次 run，造成串 run。"""
    capture = Capture()
    bus = _bus(capture)
    try:
        with use_bus(bus):
            assert current_bus() is bus
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert current_bus() is None


def test_use_bus_nests():
    outer, inner = _bus(Capture()), _bus(Capture())
    with use_bus(outer):
        with use_bus(inner):
            assert current_bus() is inner
        assert current_bus() is outer
    assert current_bus() is None


def test_null_bus_span_is_noop():
    """NullEventBus 下不得产生事件，也不得抛。"""
    with use_bus(NullEventBus()):
        with ambient_span("llm_call:x"):
            pass


# --- 切口 1：structured_call 的 span ---------------------------------------


def test_structured_call_emits_span():
    """span 名带工具名，且结果经 LLM_CALL_END 记到 span 上。"""
    capture = Capture()
    llm = _ToolLLM({"verdict": "relevant"})
    with use_bus(_bus(capture)):
        res = structured_call(llm, [], tool_schema=_TOOL)

    assert res.call_status is CallStatus.SUCCESS
    assert res.payload == {"verdict": "relevant"}
    assert "llm_call:report_relevance" in capture.span_names()

    ends = capture.of_kind(EventKind.LLM_CALL_END)
    assert len(ends) == 1
    assert ends[0].data["status"] == "success"
    assert ends[0].data["provider_attempts"] == 1
    assert isinstance(ends[0].data["latency_ms"], float)
    assert "error" not in ends[0].data       # 成功不写 error: null


def test_structured_call_span_records_failure():
    """provider 抛 → 仍要有 span，且 status/error 落到事件上（这是失败归因的关键）。"""
    capture = Capture()
    llm = _ToolLLM(raise_exc=TimeoutError("slow"))
    with use_bus(_bus(capture)):
        res = structured_call(llm, [], tool_schema=_TOOL)

    assert res.call_status is CallStatus.PROVIDER_FAILED
    assert "llm_call:report_relevance" in capture.span_names()
    end = capture.of_kind(EventKind.LLM_CALL_END)[0]
    assert end.data["status"] == "provider_failed"
    assert end.data["error"] == "TimeoutError"


def test_structured_call_without_bus_behaves_identically():
    """无 bus 时行为与埋点前逐字一致（观测绝不改变业务结果）。"""
    llm = _ToolLLM({"a": 1})
    res = structured_call(llm, [], tool_schema=_TOOL)
    assert res.call_status is CallStatus.SUCCESS
    assert res.payload == {"a": 1}
    assert llm.calls == 1


def test_structured_call_plain_json_labels_span():
    """无 tool_schema 的退化路径也要可见，名字标成 plain_json。"""

    class _PlainLLM:
        def complete(self, messages, **kw):
            return LLMResponse(content='{"k": "v"}')

    capture = Capture()
    with use_bus(_bus(capture)):
        res = structured_call(_PlainLLM(), [])
    assert res.payload == {"k": "v"}
    assert "llm_call:plain_json" in capture.span_names()


def test_structured_call_span_nests_under_current_span():
    """span 必须挂在当前 span 下，否则 trace 树是平的、看不出归属。"""
    capture = Capture()
    bus = _bus(capture)
    with use_bus(bus):
        with bus.span("operation:op_1"):
            structured_call(_ToolLLM(), [], tool_schema=_TOOL)

    starts = {e.data.get("name"): e for e in capture.of_kind(EventKind.SPAN_START)}
    outer = starts["operation:op_1"]
    inner = starts["llm_call:report_relevance"]
    assert inner.parent_span_id == outer.span_id


# --- 切口 2：检索 Loop 的 attempt span -------------------------------------


def _loop_pieces():
    """复用 test_retrieval_loop 的 stub 装配（避免重复一套 provider 假件）。"""
    from tests.test_retrieval_loop import _cand, _deps, _quota
    return _cand, _deps, _quota


def _run_loop(capture=None, *, total_min=2):
    from paper_agent.literature.relevance import RelevanceCriteria
    from paper_agent.literature.retrieval.loop import RetrievalRequest, run_retrieval_loop
    from paper_agent.literature.retrieval.budget import RetrievalLimits
    from paper_agent.literature.retrieval.query_planner import DeterministicQueryPlanner

    _cand, _deps, _quota = _loop_pieces()
    by_query = {"q": [_cand("10.1/a"), _cand("10.2/b")]}
    req = RetrievalRequest(
        quota=_quota(total_min=total_min),
        criteria=RelevanceCriteria(topic="dl"),
        initial_query="q",
    )

    def _go():
        return run_retrieval_loop(
            req, deps=_deps(by_query), limits=RetrievalLimits(),
            planner=DeterministicQueryPlanner(), clock=lambda: 0.0,
        )

    if capture is None:
        return _go()
    with use_bus(_bus(capture)):
        return _go()


def test_retrieval_attempt_emits_span():
    """每次 attempt 一个 span + RETRIEVAL_START/END —— 此前生产路径完全不可见。"""
    capture = Capture()
    res = _run_loop(capture)

    assert res.stop_reason.value == "quota_satisfied"
    names = capture.span_names()
    assert any(n.startswith("retrieval_attempt:") for n in names), names
    assert capture.of_kind(EventKind.RETRIEVAL_START)
    assert capture.of_kind(EventKind.RETRIEVAL_END)


def test_retrieval_start_does_not_leak_query_text():
    """隐私（D15）：查询原文绝不进 trace，只有 hash 与长度。"""
    capture = Capture()
    _run_loop(capture)
    start = capture.of_kind(EventKind.RETRIEVAL_START)[0]
    assert start.data["query_len"] == 1
    assert start.data["query_hash"] != "q"
    assert "q" not in str(start.data["query_hash"])


def test_retrieval_start_end_share_query_hash():
    """START 与 END 必须同 hash，否则关联不上同一次 attempt。

    首版用了 `sanitize.digest`（redact+sha256）而 `_trace_record` 用裸 sha1，
    两端出不同 hash —— 静默失效，故锁住。
    """
    capture = Capture()
    _run_loop(capture)
    start = capture.of_kind(EventKind.RETRIEVAL_START)[0]
    end = capture.of_kind(EventKind.RETRIEVAL_END)[0]
    assert start.data["query_hash"] == end.data["query_hash"]


def test_retrieval_end_carries_counters():
    """RETRIEVAL_END 必须带上 `_trace_record` 的计数（这是「时间花在哪」的答案）。"""
    capture = Capture()
    _run_loop(capture)
    data = capture.of_kind(EventKind.RETRIEVAL_END)[0].data
    for key in ("candidates", "accept_new", "relevance_calls", "search_calls", "elapsed_s"):
        assert key in data, key
    assert data["candidates"] >= 1


def test_retrieval_llm_spans_nest_under_attempt():
    """相关性判定的 llm_call span 必须落在 attempt span 内 —— 归因的关键。"""
    capture = Capture()
    _run_loop(capture)
    starts = capture.of_kind(EventKind.SPAN_START)
    by_id = {e.span_id: e.data.get("name") for e in starts}
    llm_spans = [e for e in starts if str(e.data.get("name", "")).startswith("llm_call:")]
    assert llm_spans, "相关性判定应产生 llm_call span"
    parents = {by_id.get(e.parent_span_id) for e in llm_spans}
    assert any(str(p).startswith("retrieval_attempt:") for p in parents), parents


def test_retrieval_loop_unchanged_without_bus():
    """无 bus 时 Loop 结果与埋点前一致。"""
    res = _run_loop(None)
    assert res.stop_reason.value == "quota_satisfied"
    assert res.batch.status.value == "staged"


def test_retrieval_trace_sink_still_called():
    """既有 `trace_sink` 注入点不得被埋点取代（dogfood runner 仍依赖它）。"""
    from paper_agent.literature.relevance import RelevanceCriteria
    from paper_agent.literature.retrieval.loop import RetrievalRequest, run_retrieval_loop
    from paper_agent.literature.retrieval.budget import RetrievalLimits
    from paper_agent.literature.retrieval.query_planner import DeterministicQueryPlanner

    _cand, _deps, _quota = _loop_pieces()
    records = []
    run_retrieval_loop(
        RetrievalRequest(quota=_quota(total_min=2),
                         criteria=RelevanceCriteria(topic="dl"), initial_query="q"),
        deps=_deps({"q": [_cand("10.1/a"), _cand("10.2/b")]}),
        limits=RetrievalLimits(), planner=DeterministicQueryPlanner(),
        clock=lambda: 0.0, trace_sink=records.append,
    )
    assert records and "query_hash" in records[0]


# --- 端到端：属性要真的落进 trace.json -------------------------------------


def _trace_json(tmp_path, fn):
    """跑一次真 Coordinator，返回落盘的 trace.json。"""
    from paper_agent.coordinator import Coordinator

    Coordinator(enable_trace=True, runs_dir=str(tmp_path)).run(lambda bus, ct: fn())
    hits = [
        os.path.join(root, f)
        for root, _, files in os.walk(str(tmp_path))
        for f in files
        if f == "trace.json"
    ]
    assert hits, "未落盘 trace.json"
    with open(hits[0], encoding="utf-8") as fh:
        return json.load(fh)


def _flatten(span, out=None):
    out = [] if out is None else out
    out.append(span)
    for child in span.get("children", []):
        _flatten(child, out)
    return out


def test_llm_span_attributes_reach_trace_json(tmp_path):
    """光发事件不够 —— recorder 白名单丢过字段。这里断言属性真的进了文件。

    注入原 bug（recorder 只取固定四键）→ provider_attempts 不在 attributes，红。
    """
    llm = _ToolLLM({"v": 1})
    trace = _trace_json(tmp_path, lambda: structured_call(llm, [], tool_schema=_TOOL))

    spans = {s["name"]: s for s in _flatten(trace["root"])}
    assert "llm_call:report_relevance" in spans, list(spans)
    attrs = spans["llm_call:report_relevance"]["attributes"]
    assert attrs["status"] == "success"
    assert attrs["provider_attempts"] == 1
    assert "latency_ms" in attrs
    assert "error" not in attrs


def test_retrieval_spans_reach_trace_json(tmp_path):
    """检索 attempt 在 trace.json 里必须有独立 span，且 llm_call 挂在它下面。

    这正是真机上缺的那棵子树：945 秒的 operation span 曾是 children=[]。
    """
    trace = _trace_json(tmp_path, lambda: _run_loop(None))
    spans = _flatten(trace["root"])
    attempts = [s for s in spans if s["name"].startswith("retrieval_attempt:")]
    assert attempts, [s["name"] for s in spans]
    nested = _flatten(attempts[0])
    assert any(s["name"].startswith("llm_call:") for s in nested), [s["name"] for s in nested]


def test_generator_llm_call_end_still_recorded(tmp_path):
    """回归：recorder 改动不得影响 generator 的 LLM_CALL_END（response/delta_count）。"""
    from paper_agent.observability.recorder import SimpleTraceRecorder
    from paper_agent.observability.sanitize import sanitize_text

    capture = Capture()
    bus = _bus(capture)
    with bus.span("llm_call"):
        bus.publish(
            EventKind.LLM_CALL_END,
            status="completed",
            response=sanitize_text("hello"),
            delta_count=3,
        )

    recorder = SimpleTraceRecorder("r1")
    for event in capture.events:
        recorder.emit(event)

    spans = _flatten(recorder.trace.root.to_dict())
    target = [s for s in spans if s["name"] == "llm_call"][0]
    assert target["attributes"]["status"] == "completed"
    assert target["attributes"]["delta_count"] == 3
    assert target["attributes"]["response"]["preview"] == "hello"


# --- 切口 3：OPERATION_RESULT 带 message ------------------------------------


def test_operation_result_carries_message():
    """`OperationResult.message` 必须进事件（脱敏后），否则 rejected 无从归因。"""
    from paper_agent.runtime.plan_executor import _emit_op_result
    from paper_agent.tasking.contracts import OperationResult, OperationStatus

    capture = Capture()
    res = OperationResult(
        operation_id="op_1",
        status=OperationStatus.REJECTED,
        message="Gate 违反：引用配额未满足（需要 10，实得 3）",
    )
    _emit_op_result(_bus(capture), res)

    events = capture.of_kind(EventKind.OPERATION_RESULT)
    assert len(events) == 1
    msg = events[0].data["result_message"]
    assert "引用配额未满足" in msg.preview
    assert msg.length > 0


def test_operation_result_message_is_redacted():
    """message 走脱敏：密钥不得进 trace。"""
    from paper_agent.runtime.plan_executor import _emit_op_result
    from paper_agent.tasking.contracts import OperationResult, OperationStatus

    capture = Capture()
    _emit_op_result(
        _bus(capture),
        OperationResult(
            operation_id="op_1",
            status=OperationStatus.FAILED,
            message="failed with api_key=sk-abcdef1234567890abcdef",
        ),
    )
    preview = capture.of_kind(EventKind.OPERATION_RESULT)[0].data["result_message"].preview
    assert "sk-abcdef1234567890abcdef" not in preview


def test_operation_result_message_key_avoids_publish_collision():
    """键名必须是 `result_message`：`publish(kind, message="", **data)` 把 `message`
    当自己的形参，用它会静默落进 `Event.message`（str）而非 `Event.data`，
    SafeText 被 str 化、preview/length 全丢。这是踩过的坑，锁住。"""
    from paper_agent.runtime.plan_executor import _emit_op_result
    from paper_agent.tasking.contracts import OperationResult, OperationStatus

    capture = Capture()
    _emit_op_result(
        _bus(capture),
        OperationResult(operation_id="op_1", status=OperationStatus.REJECTED, message="原因"),
    )
    event = capture.of_kind(EventKind.OPERATION_RESULT)[0]
    assert "result_message" in event.data
    assert event.message == ""          # 没被挤进信封的 message 字段


def test_operation_result_empty_message_is_safe():
    from paper_agent.runtime.plan_executor import _emit_op_result
    from paper_agent.tasking.contracts import OperationResult, OperationStatus

    capture = Capture()
    _emit_op_result(
        _bus(capture),
        OperationResult(operation_id="op_1", status=OperationStatus.SUCCEEDED),
    )
    assert capture.of_kind(EventKind.OPERATION_RESULT)[0].data["result_message"].length == 0


# --- 切口 4：TurnMetrics.load ----------------------------------------------


def test_turn_metrics_roundtrip(tmp_path):
    """save→load 保住累积值 —— 此前只有 save，每次启动清零。"""
    path = str(tmp_path / "turn_metrics.json")
    m = TurnMetrics()
    m.record_turn()
    m.record_served_by("active")
    m.record_plan_status("failed")
    m.record_fallback("TimeoutError")
    m.record_cancelled()
    assert m.save(path)

    back = TurnMetrics.load(path)
    assert back.turns == 1
    assert back.served_by == {"active": 1}
    assert back.plan_status == {"failed": 1}
    assert back.fallback_to_legacy == {"TimeoutError": 1}
    assert back.cancelled == 1
    assert back.fallback_count == 1
    assert back.attribution_is_clean is False


def test_turn_metrics_load_accumulates_across_sessions(tmp_path):
    """第二次会话在第一次基础上继续累加（B-4 要的正是跨会话计数）。"""
    path = str(tmp_path / "turn_metrics.json")
    first = TurnMetrics()
    first.record_turn()
    first.record_served_by("active")
    first.save(path)

    second = TurnMetrics.load(path)
    second.record_turn()
    second.record_served_by("active")
    second.save(path)

    assert TurnMetrics.load(path).turns == 2
    assert TurnMetrics.load(path).served_by == {"active": 2}


def test_turn_metrics_load_missing_and_corrupt(tmp_path):
    """不存在/损坏/结构异常 → 全零，绝不抛（不能因计数文件阻塞 REPL 启动）。"""
    assert TurnMetrics.load(str(tmp_path / "nope.json")).turns == 0

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert TurnMetrics.load(str(bad)).turns == 0

    wrong = tmp_path / "wrong.json"
    wrong.write_text('["a list"]', encoding="utf-8")
    assert TurnMetrics.load(str(wrong)).turns == 0

    weird = tmp_path / "weird.json"
    weird.write_text(
        json.dumps({"turns": "many", "served_by": {"active": "lots"}, "shadow": 5}),
        encoding="utf-8",
    )
    loaded = TurnMetrics.load(str(weird))
    assert loaded.turns == 0
    assert loaded.served_by == {}
    assert loaded.shadow_matches_legacy == 0


def test_turn_metrics_load_rejects_bool_counts(tmp_path):
    """bool 是 int 的子类：`turns: true` 不得被当成 1。"""
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"turns": True, "cancelled": True}), encoding="utf-8")
    loaded = TurnMetrics.load(str(path))
    assert loaded.turns == 0
    assert loaded.cancelled == 0
