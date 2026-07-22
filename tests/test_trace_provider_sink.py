"""streaming-and-trace Task 5 验收：Provider 调用级 EventSink 适配。

对应 tasks.md Task 5 / design §5 / Req 11.1, 11.2, 13.1：
- stream/complete 接受可选 event_sink（默认不传 → 逐字节不变）
- resilient 用 per-call event_sink 发 LLM_RETRY
- 跨调用不串（event_sink 不长存）
- 具体 provider 吸收 event_sink，不透传给底层 API
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.observability.events import Event, EventKind
from paper_agent.providers.llm.base import (
    CancellationToken,
    LLMProvider,
    LLMResponse,
    Message,
    StreamChunk,
)
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.llm.resilient import ResilientLLMProvider
from paper_agent.workspace.models import RetryPolicy


class CaptureSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event):
        self.events.append(event)


class APITimeoutError(Exception):
    """类名命中 resilient 的可重试白名单（_RETRYABLE_NAMES）。"""


class FlakyInner(LLMProvider):
    """前 n 次可重试失败，之后成功；记录底层收到的 opts（验证不透传 event_sink）。"""

    def __init__(self, fails: int) -> None:
        self.fails = fails
        self.calls = 0
        self.last_opts: dict | None = None

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        self.calls += 1
        self.last_opts = opts
        if self.calls <= self.fails:
            raise APITimeoutError("transient")
        return LLMResponse(content="ok")

    def stream(self, messages, *, cancel_token=None, **opts):
        self.calls += 1
        self.last_opts = opts
        if self.calls <= self.fails:
            raise APITimeoutError("transient")
        yield StreamChunk(kind="content", text="ok")


_FAST = RetryPolicy(max_retries=3, base_backoff=0.0, max_backoff=0.0, jitter=0.0)


# --- 默认零影响：mock 不传 event_sink 行为不变 ---

def test_mock_complete_ignores_event_sink():
    llm = MockLLMProvider(scripted=["hello"])
    sink = CaptureSink()
    resp = llm.complete([Message(role="user", content="x")], event_sink=sink)
    assert resp.content == "hello"
    assert sink.events == []  # mock 不发事件


def test_mock_stream_ignores_event_sink():
    llm = MockLLMProvider(scripted=["hello"])
    sink = CaptureSink()
    chunks = list(llm.stream([Message(role="user", content="x")], event_sink=sink))
    assert "".join(c.text for c in chunks) == "hello"
    assert sink.events == []


# --- resilient 用 per-call event_sink 发 LLM_RETRY ---

def test_resilient_complete_emits_retry_to_call_sink():
    inner = FlakyInner(fails=1)
    prov = ResilientLLMProvider(inner, policy=_FAST)
    sink = CaptureSink()
    resp = prov.complete([Message(role="user", content="x")], event_sink=sink)
    assert resp.content == "ok"
    retries = [e for e in sink.events if e.kind is EventKind.LLM_RETRY]
    assert len(retries) == 1
    # event_sink 不透传给底层 provider
    assert "event_sink" not in (inner.last_opts or {})


def test_resilient_stream_emits_retry_to_call_sink():
    inner = FlakyInner(fails=2)
    prov = ResilientLLMProvider(inner, policy=_FAST)
    sink = CaptureSink()
    chunks = list(prov.stream([Message(role="user", content="x")], event_sink=sink))
    assert "".join(c.text for c in chunks) == "ok"
    retries = [e for e in sink.events if e.kind is EventKind.LLM_RETRY]
    assert len(retries) == 2
    assert "event_sink" not in (inner.last_opts or {})


# --- 跨调用不串：event_sink 不长存 ---

def test_event_sink_not_persisted_across_calls():
    inner = FlakyInner(fails=1)
    prov = ResilientLLMProvider(inner, policy=_FAST)  # 构造期无 sink
    first = CaptureSink()
    prov.complete([Message(role="user", content="a")], event_sink=first)
    assert len([e for e in first.events if e.kind is EventKind.LLM_RETRY]) == 1

    # 第二次调用不传 event_sink，且底层再失败一次 → 不应发到上一次的 sink
    inner.calls = 0  # 重置，让其再失败一次
    before = len(first.events)
    prov.complete([Message(role="user", content="b")])  # 无 event_sink
    assert len(first.events) == before  # 上一次的 sink 未被复用


def test_constructor_sink_still_used_as_fallback():
    inner = FlakyInner(fails=1)
    ctor_sink = CaptureSink()
    prov = ResilientLLMProvider(inner, policy=_FAST, sink=ctor_sink)
    prov.complete([Message(role="user", content="x")])  # 不传 per-call sink
    assert len([e for e in ctor_sink.events if e.kind is EventKind.LLM_RETRY]) == 1


def test_call_sink_overrides_constructor_sink():
    inner = FlakyInner(fails=1)
    ctor_sink = CaptureSink()
    call_sink = CaptureSink()
    prov = ResilientLLMProvider(inner, policy=_FAST, sink=ctor_sink)
    prov.complete([Message(role="user", content="x")], event_sink=call_sink)
    assert len([e for e in call_sink.events if e.kind is EventKind.LLM_RETRY]) == 1
    assert ctor_sink.events == []  # per-call 优先
