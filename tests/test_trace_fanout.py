"""streaming-and-trace Task 4 验收：FanOutSink + ObservabilityHealth。

对应 tasks.md Task 4 / Req 1.1-1.5 / design Components§3：
- 多下游收到字段相等的同一 frozen Event
- 一个下游抛异常，其余仍收到；被上报（不含 payload）；health 置位
- 零下游接受不转发
- 与 EventBus 集成时 DeliveryResult 计数精确
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.observability.bus import EventBus, RunContext
from paper_agent.observability.events import Event, EventKind
from paper_agent.observability.fanout import FanOutSink
from paper_agent.observability.health import ObservabilityHealth


class CaptureSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class BoomSink:
    def emit(self, event: Event) -> None:
        raise RuntimeError("boom")


def _event(**kw) -> Event:
    return Event(kind=EventKind.WORKFLOW_START, **kw)


# --- fan-out 基础 ---

def test_forwards_same_event_to_all_downstreams():
    a, b = CaptureSink(), CaptureSink()
    fan = FanOutSink([a, b])
    ev = _event(message="hi", data={"x": 1})
    fan.emit(ev)
    assert len(a.events) == 1 and len(b.events) == 1
    # 同一对象、字段相等
    assert a.events[0] is b.events[0]
    assert a.events[0].message == "hi"


def test_zero_downstreams_accepts_without_forwarding():
    fan = FanOutSink([])
    # 不抛错
    d, f = fan.deliver(_event())
    assert d == 0 and f == 0


def test_deliver_counts_success():
    fan = FanOutSink([CaptureSink(), CaptureSink()])
    delivered, failed = fan.deliver(_event())
    assert delivered == 2 and failed == 0


# --- 隔离、上报、health ---

def test_one_downstream_boom_others_still_receive():
    good = CaptureSink()
    fan = FanOutSink()
    fan.add(BoomSink(), role="other")
    fan.add(good, role="other")
    delivered, failed = fan.deliver(_event())
    assert delivered == 1 and failed == 1
    assert len(good.events) == 1  # 其余下游仍收到


def test_recorder_failure_sets_trace_collection_failed():
    health = ObservabilityHealth()
    fan = FanOutSink(health=health)
    fan.add_recorder(BoomSink())
    fan.deliver(_event())
    assert health.trace_collection_failed is True
    assert health.renderer_failed is False


def test_renderer_failure_sets_renderer_failed():
    health = ObservabilityHealth()
    fan = FanOutSink(health=health)
    fan.add_renderer(BoomSink())
    fan.deliver(_event())
    assert health.renderer_failed is True
    assert health.trace_collection_failed is False


def test_report_warning_excludes_payload(capsys):
    health = ObservabilityHealth()
    fan = FanOutSink(health=health)
    fan.add_recorder(BoomSink())
    secret_event = _event(message="绝密论文正文", data={"body": "机密内容不应出现在日志"})
    fan.deliver(secret_event)
    # warning 只含类型信息，不含 payload
    assert health.warnings
    warning = health.warnings[0]
    assert "机密内容不应出现在日志" not in warning
    assert "绝密论文正文" not in warning
    assert "BoomSink" in warning
    # stderr 同样不含 payload
    err = capsys.readouterr().err
    assert "机密内容不应出现在日志" not in err
    assert "绝密论文正文" not in err


# --- 与 EventBus 集成：DeliveryResult 精确计数 ---

def test_eventbus_delivery_counts_via_fanout():
    good, bad = CaptureSink(), BoomSink()
    health = ObservabilityHealth()
    fan = FanOutSink(health=health)
    fan.add_recorder(good)
    fan.add_renderer(bad)
    bus = EventBus(fan, RunContext(run_id="r"), health)
    res = bus.publish(EventKind.WORKFLOW_START)
    assert res.enabled is True
    assert res.delivered_count == 1
    assert res.failed_count == 1
    # 下游收到的是被 EventBus 盖过章的事件
    assert good.events[0].trace_id == "r"
    assert good.events[0].sequence == 0
    assert health.renderer_failed is True


def test_eventbus_all_downstreams_ok():
    a, b = CaptureSink(), CaptureSink()
    health = ObservabilityHealth()
    fan = FanOutSink([a, b], health=health)
    bus = EventBus(fan, RunContext(run_id="r"), health)
    res = bus.publish(EventKind.WORKFLOW_START)
    assert res.delivered_count == 2 and res.failed_count == 0
    assert health.pipeline_failed is False
