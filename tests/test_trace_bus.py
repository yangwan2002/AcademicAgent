"""streaming-and-trace Task 3 验收：RunContext + EventBus + NullEventBus + SpanScope + OperationCancelled。

对应 tasks.md Task 3 / design D0/D1/D2/D4 / Property 8/9/10/11/13：
- 强制覆盖权威信封（provider 旧值仅存 source_event_id）
- scope 退出自动恢复；嵌套 span 父子正确
- span 三终态含取消；SPAN_END 异常仍弹栈
- NullEventBus.span().cancel() 不报错；publish 内部异常不外抛
- DeliveryResult 语义
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.errors import OperationCancelled
from paper_agent.observability.bus import (
    DeliveryResult,
    EventBus,
    NullEventBus,
    NullSpanScope,
    RunContext,
)
from paper_agent.observability.events import Event, EventKind
from paper_agent.observability.health import ObservabilityHealth


class CaptureSink:
    """记录收到的所有事件。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class BoomSink:
    """emit 永远抛异常，用于验证隔离。"""

    def emit(self, event: Event) -> None:
        raise RuntimeError("downstream boom")


def _bus(sink=None, health=None):
    ctx = RunContext(run_id="run-1")
    return EventBus(sink or CaptureSink(), ctx, health)


# --- 信封盖章：强制覆盖 ---

def test_publish_stamps_authoritative_envelope():
    sink = CaptureSink()
    bus = _bus(sink)
    bus.publish(EventKind.WORKFLOW_START, message="开始")
    ev = sink.events[0]
    assert ev.trace_id == "run-1"
    assert ev.event_id != ""
    assert ev.sequence == 0
    assert ev.ts > 0


def test_sequence_monotonic():
    sink = CaptureSink()
    bus = _bus(sink)
    for _ in range(3):
        bus.publish(EventKind.WORKFLOW_START)
    assert [e.sequence for e in sink.events] == [0, 1, 2]


def test_provider_envelope_is_force_overridden_and_source_kept():
    # provider 直接 emit 一个带旧信封的事件（模拟串 run）
    sink = CaptureSink()
    bus = _bus(sink)
    foreign = Event(
        kind=EventKind.LLM_RETRY,
        message="retry",
        trace_id="OTHER-RUN",
        event_id="foreign-eid",
        sequence=999,
    )
    bus.emit(foreign)
    ev = sink.events[0]
    # 权威信封被强制覆盖
    assert ev.trace_id == "run-1"
    assert ev.sequence == 0
    assert ev.event_id != "foreign-eid"
    # provider 旧 event_id 仅作为 source_event_id 留存
    assert ev.source_event_id == "foreign-eid"


def test_stamp_reflects_relation_scopes():
    sink = CaptureSink()
    bus = _bus(sink)
    with bus.generation_scope("gen-1"):
        with bus.attempt_scope(2):
            with bus.llm_call_scope("call-9"):
                bus.publish(EventKind.LLM_CALL_START)
    ev = sink.events[0]
    assert ev.generation_id == "gen-1"
    assert ev.attempt == 2
    assert ev.llm_call_id == "call-9"


def test_scopes_restore_on_exit():
    bus = _bus()
    assert bus.ctx.generation_id == ""
    with bus.generation_scope("g"):
        assert bus.ctx.generation_id == "g"
        with bus.attempt_scope(1):
            assert bus.ctx.attempt == 1
        assert bus.ctx.attempt is None
    assert bus.ctx.generation_id == ""


# --- span 树：父子关系 ---

def test_nested_span_parent_child():
    sink = CaptureSink()
    bus = _bus(sink)
    with bus.span("run"):
        with bus.span("generation_attempt"):
            pass
    starts = [e for e in sink.events if e.kind is EventKind.SPAN_START]
    ends = [e for e in sink.events if e.kind is EventKind.SPAN_END]
    run_start, attempt_start = starts[0], starts[1]
    # 根 span 无父
    assert run_start.parent_span_id == ""
    # 子 span 的父是根 span
    assert attempt_start.parent_span_id == run_start.span_id
    # 每个 START 都有对应 END，且栈已清空
    assert len(starts) == 2 and len(ends) == 2
    assert bus.ctx.current_span_id == ""


def test_span_terminal_state_ended():
    sink = CaptureSink()
    bus = _bus(sink)
    with bus.span("run"):
        pass
    end = [e for e in sink.events if e.kind is EventKind.SPAN_END][0]
    assert end.data["span_state"] == "ended"
    assert end.duration_ms is not None


def test_span_terminal_state_failed_on_exception():
    sink = CaptureSink()
    bus = _bus(sink)
    with pytest.raises(ValueError):
        with bus.span("run"):
            raise ValueError("boom")
    end = [e for e in sink.events if e.kind is EventKind.SPAN_END][0]
    assert end.data["span_state"] == "failed"
    # 异常仍向上传播、栈已弹出
    assert bus.ctx.current_span_id == ""


def test_span_terminal_state_cancelled_on_operation_cancelled():
    sink = CaptureSink()
    bus = _bus(sink)
    with pytest.raises(OperationCancelled):
        with bus.span("run"):
            raise OperationCancelled()
    end = [e for e in sink.events if e.kind is EventKind.SPAN_END][0]
    assert end.data["span_state"] == "cancelled"


def test_span_explicit_cancel():
    sink = CaptureSink()
    bus = _bus(sink)
    with bus.span("run") as s:
        s.cancel()
    end = [e for e in sink.events if e.kind is EventKind.SPAN_END][0]
    assert end.data["span_state"] == "cancelled"


def test_cancel_propagates_through_nested_spans_all_cancelled():
    sink = CaptureSink()
    bus = _bus(sink)
    with pytest.raises(OperationCancelled):
        with bus.span("run"):
            with bus.span("generation_attempt"):
                with bus.span("llm_call"):
                    raise OperationCancelled()
    ends = [e for e in sink.events if e.kind is EventKind.SPAN_END]
    assert len(ends) == 3
    assert all(e.data["span_state"] == "cancelled" for e in ends)


def test_span_end_publish_failure_still_pops_stack():
    # 让 SPAN_END 的转发抛异常，验证栈仍被弹出（finally pop）
    class EndBoomSink:
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            if event.kind is EventKind.SPAN_END:
                raise RuntimeError("end boom")

    sink = EndBoomSink()
    bus = _bus(sink)
    # SPAN_END 转发失败被 EventBus 隔离，不外抛；栈仍弹出
    with bus.span("run"):
        pass
    assert bus.ctx.current_span_id == ""


# --- 防御式：观测异常不外抛 ---

def test_publish_does_not_propagate_downstream_exception():
    health = ObservabilityHealth()
    bus = _bus(BoomSink(), health)
    # 不抛异常
    res = bus.publish(EventKind.WORKFLOW_START)
    assert res.enabled is True
    assert res.delivered_count == 0
    assert res.failed_count == 1


def test_pipeline_failure_recorded_when_stamp_breaks():
    # 通过让 ctx.next_sequence 抛异常来模拟 stamp 阶段失败
    class BadCtx(RunContext):
        def next_sequence(self):
            raise RuntimeError("seq boom")

    health = ObservabilityHealth()
    bus = EventBus(CaptureSink(), BadCtx(run_id="r"), health)
    res = bus.publish(EventKind.WORKFLOW_START)
    assert res.delivered_count == 0 and res.failed_count == 0
    assert health.pipeline_failed is True


# --- DeliveryResult 语义 ---

def test_delivery_result_delivered_property():
    assert DeliveryResult(True, 2, 0).delivered is True
    assert DeliveryResult(True, 0, 2).delivered is False


def test_delivery_counts_success():
    bus = _bus(CaptureSink())
    res = bus.publish(EventKind.WORKFLOW_START)
    assert res.enabled is True and res.delivered_count == 1 and res.failed_count == 0


# --- NullEventBus ---

def test_null_bus_disabled_delivery():
    nb = NullEventBus()
    res = nb.publish(EventKind.WORKFLOW_START)
    assert res.enabled is False
    assert nb.as_sink_or_none() is None


def test_null_bus_span_is_noop_and_cancel_safe():
    nb = NullEventBus()
    span = nb.span("run")
    assert isinstance(span, NullSpanScope)
    with nb.span("run") as s:
        s.cancel()       # 不报错
        s.degrade("x")   # 不报错


def test_null_bus_scopes_are_nullcontext():
    nb = NullEventBus()
    with nb.generation_scope("g"):
        with nb.attempt_scope(1):
            with nb.llm_call_scope("c"):
                pass  # 不报错
