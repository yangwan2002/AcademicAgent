"""运行上下文 + 单一事件总线 + Span 作用域（观测脊柱核心）。

- `RunContext`：单次运行的权威上下文——run_id、单调 sequence、span 栈、
  以及关联层级 `generation_id > attempt > llm_call_id`（design D1）。
- `EventBus`：唯一生产者，实现 `EventSink`。两入口 `publish(kind,**data)`（业务层）
  与 `emit(event)`（provider）都经 `_stamp` **强制覆盖**权威信封（不尊重旧值，
  防串 run，provider 旧 event_id 仅存 source_event_id）+ `deep_freeze`；**内部
  异常绝不外抛**（记 `ObservabilityHealth.pipeline_failed`），返回 `DeliveryResult`。
- `SpanScope`：进入先 push 再发 `SPAN_START`；退出先发 `SPAN_END` 再（`finally`）
  pop——即使 SPAN_END 投递失败也弹栈；终态：取消→cancelled、异常→failed、
  否则 `_state or ended`（design D2 / Property 9）。
- `NullEventBus`：关闭观测时的零开销实现；`span()` 返回 `NullSpanScope`，
  scope 返回 `nullcontext()`，`as_sink_or_none()`→None（provider 走原始签名）。

时长用 `time.monotonic()` 差；`ts` 仅供展示。REPL 单线程，sequence 无锁。
"""

from __future__ import annotations

import contextlib
import dataclasses
import time
import uuid
from dataclasses import dataclass, field

from paper_agent.errors import OperationCancelled
from paper_agent.observability.events import Event, EventKind, EventSink
from paper_agent.observability.health import ObservabilityHealth


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class DeliveryResult:
    """一次发布的投递结果，供调用方（如 Coordinator）判断送达情况。

    - `enabled=False`：观测未启用（NullEventBus）——无从谈论送达。
    - `enabled=True` 且 `delivered_count=0, failed_count=0`：EventBus 自身在下游
      之前就失败（stamp/freeze），无事件被转发。
    """

    enabled: bool
    delivered_count: int
    failed_count: int

    @property
    def delivered(self) -> bool:
        return self.delivered_count > 0


@dataclass
class RunContext:
    """单次运行的权威上下文（单线程 REPL）。"""

    run_id: str
    _sequence: int = -1
    _span_stack: list[str] = field(default_factory=list)
    generation_id: str = ""
    attempt: int | None = None
    llm_call_id: str = ""

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    @property
    def current_span_id(self) -> str:
        return self._span_stack[-1] if self._span_stack else ""

    @property
    def current_parent_span_id(self) -> str:
        return self._span_stack[-2] if len(self._span_stack) >= 2 else ""

    def push_span(self, span_id: str) -> None:
        self._span_stack.append(span_id)

    def pop_span(self, span_id: str) -> None:
        # 正常情况 span_id 在栈顶；即便乱序也稳妥移除，不抛错。
        if self._span_stack and self._span_stack[-1] == span_id:
            self._span_stack.pop()
        elif span_id in self._span_stack:
            self._span_stack.remove(span_id)


class SpanScope:
    """一个 span 的上下文管理器：push→SPAN_START…SPAN_END→pop(finally)。"""

    def __init__(self, bus: "EventBus", name: str, attrs: dict | None) -> None:
        self._bus = bus
        self._name = name
        self._attrs = attrs or {}
        self._state: str | None = None
        self._start_monotonic: float | None = None
        self.span_id = ""
        self.parent_id = ""

    def __enter__(self) -> "SpanScope":
        ctx = self._bus.ctx
        self.parent_id = ctx.current_span_id
        self.span_id = _new_id()
        ctx.push_span(self.span_id)                 # 先 push
        self._start_monotonic = time.monotonic()
        self._bus._publish_span(EventKind.SPAN_START, self._name, attrs=self._attrs)
        return self

    def cancel(self) -> None:
        """显式把该 span 终态标记为 cancelled（未抛异常时生效）。"""
        self._state = "cancelled"

    def degrade(self, warning: str | None = None) -> None:
        """记录一次降级告警（不改变 span 终态）。"""
        if warning:
            self._bus.health.add_warning(warning)

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and exc_type is not None and issubclass(exc_type, OperationCancelled):
            state = "cancelled"
        elif exc is not None:
            state = "failed"
        else:
            state = self._state or "ended"
        duration_ms = None
        if self._start_monotonic is not None:
            duration_ms = (time.monotonic() - self._start_monotonic) * 1000.0
        try:
            self._bus._publish_span(
                EventKind.SPAN_END,
                self._name,
                span_state=state,
                duration_ms=duration_ms,
            )
        finally:
            self._bus.ctx.pop_span(self.span_id)     # 无论 END 成败都弹栈
        return False                                  # 不吞异常（取消/错误继续向上）


class NullSpanScope:
    """关闭观测时的 no-op span：cancel/degrade 均无副作用。"""

    def __enter__(self) -> "NullSpanScope":
        return self

    def cancel(self) -> None:
        return None

    def degrade(self, warning: str | None = None) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class EventBus(EventSink):
    """唯一事件生产者：盖章 + fan-out + 防御式；实现 EventSink 供 provider emit。"""

    def __init__(
        self,
        downstream: EventSink,
        ctx: RunContext,
        health: ObservabilityHealth | None = None,
    ) -> None:
        self._sink = downstream
        self.ctx = ctx
        self.health = health or ObservabilityHealth()

    # --- 对外入口 ---

    def publish(self, kind: EventKind, message: str = "", **data) -> DeliveryResult:
        """业务层入口：按 kind + 关键字 data 发布事件。"""
        return self._dispatch(Event(kind=kind, message=message, data=data))

    def emit(self, event: Event) -> DeliveryResult:  # EventSink 协议（provider 入口）
        return self._dispatch(event)

    def as_sink_or_none(self) -> EventSink | None:
        """观测启用 → 返回自身作为 provider 的 event_sink。"""
        return self

    # --- span / scope ---

    def span(self, name: str, **attrs) -> SpanScope:
        return SpanScope(self, name, attrs)

    @contextlib.contextmanager
    def generation_scope(self, generation_id: str):
        prev = self.ctx.generation_id
        self.ctx.generation_id = generation_id
        try:
            yield generation_id
        finally:
            self.ctx.generation_id = prev

    @contextlib.contextmanager
    def attempt_scope(self, attempt: int):
        prev = self.ctx.attempt
        self.ctx.attempt = attempt
        try:
            yield attempt
        finally:
            self.ctx.attempt = prev

    @contextlib.contextmanager
    def llm_call_scope(self, llm_call_id: str):
        prev = self.ctx.llm_call_id
        self.ctx.llm_call_id = llm_call_id
        try:
            yield llm_call_id
        finally:
            self.ctx.llm_call_id = prev

    # --- 内部 ---

    def _publish_span(
        self,
        kind: EventKind,
        name: str,
        span_state: str | None = None,
        duration_ms: float | None = None,
        attrs: dict | None = None,
    ) -> DeliveryResult:
        data: dict = {"name": name}
        if span_state is not None:
            data["span_state"] = span_state
        if attrs:
            data["attributes"] = dict(attrs)
        return self._dispatch(Event(kind=kind, data=data, duration_ms=duration_ms))

    def _stamp(self, e: Event) -> Event:
        """强制覆盖权威信封；仅 kind/message/data/duration_ms 来自调用方。"""
        return dataclasses.replace(
            e,
            trace_id=self.ctx.run_id,
            event_id=_new_id(),
            sequence=self.ctx.next_sequence(),
            ts=time.time(),
            span_id=self.ctx.current_span_id,
            parent_span_id=self.ctx.current_parent_span_id,
            generation_id=self.ctx.generation_id,
            attempt=self.ctx.attempt,
            llm_call_id=self.ctx.llm_call_id,
            source_event_id=(e.event_id or e.source_event_id or ""),
        )

    def _dispatch(self, event: Event) -> DeliveryResult:
        try:
            stamped = self._stamp(event)
        except Exception as exc:  # noqa: BLE001 - 观测不拖垮业务
            self._mark_pipeline_failed(exc)
            return DeliveryResult(enabled=True, delivered_count=0, failed_count=0)
        delivered, failed = self._forward(stamped)
        return DeliveryResult(enabled=True, delivered_count=delivered, failed_count=failed)

    def _forward(self, event: Event) -> tuple[int, int]:
        sink = self._sink
        # FanOutSink（Task 4）暴露 deliver(event)->(delivered, failed)；否则按单 sink 处理。
        deliver = getattr(sink, "deliver", None)
        if callable(deliver):
            try:
                res = deliver(event)
            except Exception as exc:  # noqa: BLE001 - fanout 自身异常也不外抛
                self._mark_pipeline_failed(exc)
                return (0, 1)
            if isinstance(res, tuple):
                return res
            return (
                getattr(res, "delivered_count", 0),
                getattr(res, "failed_count", 0),
            )
        # 普通单 sink：隔离其异常，不外抛。
        try:
            sink.emit(event)
            return (1, 0)
        except Exception as exc:  # noqa: BLE001
            self.health.add_warning(f"downstream emit failed: {type(exc).__name__}")
            return (0, 1)

    def _mark_pipeline_failed(self, exc: Exception) -> None:
        self.health.pipeline_failed = True
        self.health.add_warning(f"pipeline failure: {type(exc).__name__}")


class NullEventBus(EventSink):
    """关闭观测时的零开销总线：所有入口 no-op，返回 enabled=False。"""

    def __init__(self) -> None:
        self.health = ObservabilityHealth()

    def publish(self, *args, **kwargs) -> DeliveryResult:
        return DeliveryResult(enabled=False, delivered_count=0, failed_count=0)

    def emit(self, *args, **kwargs) -> DeliveryResult:
        return DeliveryResult(enabled=False, delivered_count=0, failed_count=0)

    def as_sink_or_none(self) -> EventSink | None:
        return None

    def span(self, *args, **kwargs) -> NullSpanScope:
        return NullSpanScope()

    def generation_scope(self, *args, **kwargs):
        return contextlib.nullcontext()

    def attempt_scope(self, *args, **kwargs):
        return contextlib.nullcontext()

    def llm_call_scope(self, *args, **kwargs):
        return contextlib.nullcontext()
