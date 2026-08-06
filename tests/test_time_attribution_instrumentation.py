"""时间归因埋点的落盘契约。

为回答「该先上 B1（批量判相关性）还是 B2（先判相关性、只核验 survivor）」，
必须能把时间预算拆成 **relevance LLM** 与 **verify 网络** 两块。这要求两类事件
带着耗时**落进 trace**，而 recorder 有多处白名单会静默丢字段 —— 故按
`test_trace_deep_instrumentation.py` 的两层写法：事件层 + 落盘层各断言一次。
"""

from __future__ import annotations

import json

from paper_agent.observability.bus import EventBus, RunContext
from paper_agent.observability.current import ambient_publish, use_bus
from paper_agent.observability.events import EventKind
from paper_agent.observability.health import ObservabilityHealth
from paper_agent.observability.recorder import SimpleTraceRecorder


class _Capture:
    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)


def _bus(sink) -> EventBus:
    return EventBus(sink, RunContext("run_attr"), ObservabilityHealth())


def _trace_blob(events) -> str:
    recorder = SimpleTraceRecorder("r_attr")
    for event in events:
        recorder.emit(event)
    return json.dumps(recorder.trace.root.to_dict(), ensure_ascii=False)


def test_verify_fetch_carries_elapsed_into_trace():
    """`VERIFY_FETCH` 必须带 `elapsed_ms` 落盘 —— verify 是 B2 要省掉的那部分，
    不出耗时就只能靠猜。"""
    cap = _Capture()
    with use_bus(_bus(cap)):
        with _bus(cap).span("retrieval_attempt"):
            ambient_publish(
                "VERIFY_FETCH", provider="crossref", id_type="doi",
                status="verified", elapsed_ms=812.5, cached=False,
            )

    hits = [e for e in cap.events if e.kind is EventKind.VERIFY_FETCH]
    assert len(hits) == 1
    assert hits[0].data["elapsed_ms"] == 812.5

    blob = _trace_blob(cap.events)
    assert "verify_fetch" in blob, "被 recorder 白名单挡掉"
    assert "812.5" in blob, "耗时字段没落盘 ⇒ 无法做时间归因"


def test_llm_call_end_carries_label_into_trace():
    """`LLM_CALL_END` 必须带 `label` 落盘。

    label（judge_relevance / plan_query / …）此前**只存在于 span 名**里，
    事件 attributes 中没有 ⇒ 按用途给耗时分桶时读不到。这正是 recorder
    固定键白名单的第二次翻车，故单独锁住。
    """
    cap = _Capture()
    bus = _bus(cap)
    with use_bus(bus):
        # span 名故意**不含** label：否则 `"judge_relevance" in blob` 会匹配到
        # span 名而非 attributes，测试通过但白名单漏项照样漏（首版就这么假绿过）。
        with bus.span("llm_call"):
            ambient_publish(
                "LLM_CALL_END", status="completed",
                latency_ms=4210.0, label="judge_relevance",
            )

    recorder = SimpleTraceRecorder("r_attr")
    for event in cap.events:
        recorder.emit(event)
    root = recorder.trace.root.to_dict()
    assert root["name"] == "llm_call", f"span 树结构变了：{root['name']}"
    attrs = root["attributes"]
    assert attrs.get("label") == "judge_relevance", \
        f"label 被白名单丢了 ⇒ 耗时无法按用途分桶；实得 {attrs}"
    assert attrs.get("latency_ms") == 4210.0


def test_cached_verify_reports_zero_elapsed():
    """缓存命中要显式标 `cached=True` 且不计耗时，否则会把省下的往返算进成本。"""
    cap = _Capture()
    with use_bus(_bus(cap)):
        with _bus(cap).span("retrieval_attempt"):
            ambient_publish(
                "VERIFY_FETCH", provider="openalex", id_type="openalex_work",
                status="verified", elapsed_ms=0.0, cached=True,
            )

    hit = [e for e in cap.events if e.kind is EventKind.VERIFY_FETCH][0]
    assert hit.data["cached"] is True
    assert hit.data["elapsed_ms"] == 0.0
