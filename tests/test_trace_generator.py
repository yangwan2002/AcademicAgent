"""streaming-and-trace Task 8 验收：Generator 流式改造。

对应 tasks.md Task 8 / design §8 / Property 2/3/6/11：
- 默认路径等价（Property 6）
- 流式拼接==最终文本（Property 2）；thinking 不入 buffer（Property 3）
- 重试共享 gen_id / attempt 递增 / llm_call_id 每次新
- 恰一个 LLM_CALL_END（各终态）；Gate 拒绝=rejected 非 failed
- 取消一路 raise（OperationCancelled）
"""

from __future__ import annotations

import os
import sys
from typing import Iterator

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.errors import OperationCancelled
from paper_agent.gates.diff_gate import PolishGate
from paper_agent.generate.generator import run_polish
from paper_agent.observability.bus import EventBus, RunContext
from paper_agent.observability.events import Event, EventKind
from paper_agent.providers.llm.base import (
    CancellationToken,
    LLMProvider,
    LLMResponse,
    StreamChunk,
)
from paper_agent.providers.llm.mock import MockLLMProvider


ORIG = r"We got 0.87 AUROC \cite{a} versus 0.84, with $x^2$ terms."
GOOD = r"We achieved 0.87 AUROC \cite{a} compared to 0.84, using $x^2$ terms."
BAD = r"We achieved 0.99 AUROC \cite{a} compared to 0.84, using $x^2$ terms."


def _gate():
    return PolishGate("tex", max_char_change_ratio=0.30)


class CaptureSink:
    def __init__(self):
        self.events: list[Event] = []

    def emit(self, event):
        self.events.append(event)


def _bus_and_sink():
    sink = CaptureSink()
    bus = EventBus(sink, RunContext(run_id="run-1"))
    return bus, sink


def _kinds(sink):
    return [e.kind for e in sink.events]


# --- Property 6: 默认路径等价 ---

def test_default_path_equivalent_no_bus():
    llm = MockLLMProvider(scripted=[GOOD])
    out = run_polish(ORIG, "", llm, _gate(), max_retry=2)
    assert out.ok and out.attempts == 1 and out.text == GOOD.strip()


def test_default_retry_then_fail_unchanged():
    llm = MockLLMProvider(scripted=[BAD, BAD, BAD])
    out = run_polish(ORIG, "", llm, _gate(), max_retry=2)
    assert not out.ok and out.attempts == 3
    assert out.error_category == "validation_exhausted"


def test_default_path_calls_complete_not_stream():
    # Mock 记录调用；默认应走 complete（stream=False）
    llm = MockLLMProvider(scripted=[GOOD])
    run_polish(ORIG, "", llm, _gate(), max_retry=2)
    assert len(llm.calls) == 1  # 一次 complete


# --- 事件闭环（Property 11） ---

def test_events_emitted_with_bus_complete_mode():
    bus, sink = _bus_and_sink()
    llm = MockLLMProvider(scripted=[GOOD])
    out = run_polish(ORIG, "", llm, _gate(), max_retry=2, bus=bus)
    assert out.ok
    kinds = _kinds(sink)
    # 恰一个 LLM_CALL_START / LLM_CALL_END / ATTEMPT_START / ATTEMPT_END
    assert kinds.count(EventKind.LLM_CALL_START) == 1
    assert kinds.count(EventKind.LLM_CALL_END) == 1
    assert kinds.count(EventKind.GENERATION_ATTEMPT_START) == 1
    assert kinds.count(EventKind.GENERATION_ATTEMPT_END) == 1
    # call_mode=complete
    start = next(e for e in sink.events if e.kind is EventKind.LLM_CALL_START)
    assert start.data["call_mode"] == "complete"
    end = next(e for e in sink.events if e.kind is EventKind.LLM_CALL_END)
    assert end.data["status"] == "completed"
    # ATTEMPT_END accepted
    aend = next(e for e in sink.events if e.kind is EventKind.GENERATION_ATTEMPT_END)
    assert aend.data["status"] == "accepted"


def test_call_end_exactly_once_per_attempt_on_retry():
    bus, sink = _bus_and_sink()
    llm = MockLLMProvider(scripted=[BAD, GOOD])
    out = run_polish(ORIG, "", llm, _gate(), max_retry=2, bus=bus)
    assert out.ok and out.attempts == 2
    kinds = _kinds(sink)
    # 两次尝试 → 两个 START/END 闭环
    assert kinds.count(EventKind.LLM_CALL_START) == 2
    assert kinds.count(EventKind.LLM_CALL_END) == 2
    assert kinds.count(EventKind.GENERATION_ATTEMPT_START) == 2
    assert kinds.count(EventKind.GENERATION_ATTEMPT_END) == 2
    # 第二次尝试前发 GATE_RETRY
    assert kinds.count(EventKind.GATE_RETRY) == 1


def test_gate_reject_is_rejected_not_failed():
    bus, sink = _bus_and_sink()
    llm = MockLLMProvider(scripted=[BAD, BAD, BAD])
    out = run_polish(ORIG, "", llm, _gate(), max_retry=2, bus=bus)
    assert not out.ok
    ends = [e for e in sink.events if e.kind is EventKind.GENERATION_ATTEMPT_END]
    assert all(e.data["status"] == "rejected" for e in ends)
    # llm_call 本身是 completed（模型正常返回，只是没过 gate）
    call_ends = [e for e in sink.events if e.kind is EventKind.LLM_CALL_END]
    assert all(e.data["status"] == "completed" for e in call_ends)


# --- 关联层级 ---

def test_generation_id_shared_attempt_increments_call_id_new():
    bus, sink = _bus_and_sink()
    llm = MockLLMProvider(scripted=[BAD, GOOD])
    run_polish(ORIG, "", llm, _gate(), max_retry=2, bus=bus)
    starts = [e for e in sink.events if e.kind is EventKind.LLM_CALL_START]
    # 同一 generation_id 跨两次尝试共享
    assert starts[0].generation_id == starts[1].generation_id
    assert starts[0].generation_id != ""
    # attempt 递增
    assert starts[0].attempt == 1 and starts[1].attempt == 2
    # llm_call_id 每次新
    assert starts[0].llm_call_id != starts[1].llm_call_id


# --- 流式（Property 2/3） ---

class StreamingLLM(LLMProvider):
    """把 content 分多块流式产出，并夹带 thinking 块。"""

    def __init__(self, content: str, chunks: int = 3, thinking: str = "推理原文"):
        self._content = content
        self._chunks = chunks
        self._thinking = thinking
        self.calls = []

    def complete(self, messages, **opts):  # 不应被流式路径调用
        opts.pop("event_sink", None)
        return LLMResponse(content=self._content)

    def stream(self, messages, *, cancel_token=None, **opts) -> Iterator[StreamChunk]:
        opts.pop("event_sink", None)
        self.calls.append(messages)
        # 先来一段 thinking（不应进 buffer）
        if self._thinking:
            yield StreamChunk(kind="thinking", text=self._thinking)
        # content 切成若干块
        size = max(1, len(self._content) // self._chunks)
        for i in range(0, len(self._content), size):
            yield StreamChunk(kind="content", text=self._content[i:i + size])


def test_stream_concat_equals_final_and_passes_gate():
    bus, sink = _bus_and_sink()
    llm = StreamingLLM(GOOD, chunks=4)
    out = run_polish(ORIG, "", llm, _gate(), max_retry=2, bus=bus, stream=True)
    assert out.ok
    # 候选拼接 == 最终文本
    deltas = [e for e in sink.events if e.kind is EventKind.LLM_DELTA]
    joined = "".join(e.data["text"] for e in deltas)
    assert joined.strip() == GOOD.strip()
    assert out.text == GOOD.strip()
    # call_mode=stream
    start = next(e for e in sink.events if e.kind is EventKind.LLM_CALL_START)
    assert start.data["call_mode"] == "stream"


def test_thinking_not_in_candidate_buffer():
    bus, sink = _bus_and_sink()
    llm = StreamingLLM(GOOD, chunks=3, thinking="绝密推理链不应进候选")
    out = run_polish(ORIG, "", llm, _gate(), max_retry=2, bus=bus, stream=True)
    assert out.ok
    # thinking 原文不在候选文本里
    assert "绝密推理链不应进候选" not in (out.text or "")
    # thinking 事件无原文
    thinking = [e for e in sink.events if e.kind is EventKind.LLM_THINKING]
    assert thinking  # 有发出标记
    for e in thinking:
        assert "绝密推理链不应进候选" not in str(dict(e.data))


def test_delta_chunk_index_monotonic():
    bus, sink = _bus_and_sink()
    llm = StreamingLLM(GOOD, chunks=5)
    run_polish(ORIG, "", llm, _gate(), max_retry=2, bus=bus, stream=True)
    deltas = [e for e in sink.events if e.kind is EventKind.LLM_DELTA]
    idx = [e.data["chunk_index"] for e in deltas]
    assert idx == list(range(len(idx)))


# --- 超限：大声失败而非静默截断 ---

def test_stream_over_cap_fails_loudly_not_silent_truncation(monkeypatch):
    from paper_agent.generate import generator as gen_mod
    from paper_agent.providers.llm.base import LLMError

    # 把 delta 数上限压到 2，制造超限
    monkeypatch.setattr(gen_mod, "_MAX_DELTA_COUNT", 2)
    bus, sink = _bus_and_sink()
    llm = StreamingLLM(GOOD, chunks=6)  # 6 块 > 2
    with pytest.raises(LLMError):
        run_polish(ORIG, "", llm, _gate(), max_retry=0, bus=bus, stream=True)
    # LLM_CALL_END 报 failed（不是 completed），且恰一个
    call_ends = [e for e in sink.events if e.kind is EventKind.LLM_CALL_END]
    assert len(call_ends) == 1 and call_ends[0].data["status"] == "failed"
    # 越界内容未作为有效候选发出：LLM_DELTA 不超过上限
    deltas = [e for e in sink.events if e.kind is EventKind.LLM_DELTA]
    assert len(deltas) <= 2
    # llm_call span 记 failed
    span_ends = [e for e in sink.events if e.kind is EventKind.SPAN_END]
    assert any(e.data["span_state"] == "failed" for e in span_ends)


# --- 取消一路 raise ---

class CancelDuringStreamLLM(LLMProvider):
    def __init__(self, token: CancellationToken):
        self._token = token

    def complete(self, messages, **opts):
        opts.pop("event_sink", None)
        return LLMResponse(content=GOOD)

    def stream(self, messages, *, cancel_token=None, **opts):
        opts.pop("event_sink", None)
        yield StreamChunk(kind="content", text="部分")
        self._token.cancel()  # 产出一块后请求取消
        yield StreamChunk(kind="content", text="更多")


def test_cancel_raises_operation_cancelled_and_marks_call_cancelled():
    token = CancellationToken()
    bus, sink = _bus_and_sink()
    llm = CancelDuringStreamLLM(token)
    with pytest.raises(OperationCancelled):
        run_polish(ORIG, "", llm, _gate(), max_retry=2, bus=bus,
                   cancel_token=token, stream=True)
    # LLM_CALL_END(cancelled) 恰一个
    call_ends = [e for e in sink.events if e.kind is EventKind.LLM_CALL_END]
    assert len(call_ends) == 1 and call_ends[0].data["status"] == "cancelled"
    # ATTEMPT_END(cancelled)
    aend = [e for e in sink.events if e.kind is EventKind.GENERATION_ATTEMPT_END]
    assert len(aend) == 1 and aend[0].data["status"] == "cancelled"
    # 沿途 span 记 cancelled
    span_ends = [e for e in sink.events if e.kind is EventKind.SPAN_END]
    assert all(e.data["span_state"] == "cancelled" for e in span_ends)
