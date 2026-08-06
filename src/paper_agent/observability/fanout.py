"""FanOutSink：把同一个 frozen Event 分发给零个或多个下游 sink。

隔离但不静默：某个下游 `emit` 抛异常时，捕获→上报（**只打 sink 类名 / EventKind /
event_id / 异常类型，绝不打 payload**，防论文正文/密钥经错误日志外泄）→按下游
角色置 `ObservabilityHealth.trace_collection_failed / renderer_failed`→继续转发
其余下游。零下游时接受事件但不转发（Req 1.3）。

`deliver(event) -> (delivered_count, failed_count)` 供 `EventBus` 得到精确的每下游
投递计数（组装 `DeliveryResult`）。`emit(event)` 满足 `EventSink` 协议（丢弃计数）。

下游按「角色」注册：`role ∈ {"recorder", "renderer", "other"}`，决定异常时置哪个
health 维度。EventBus 自身（下游之前）的故障由 EventBus 记 `pipeline_failed`，不在此。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from paper_agent.observability.events import Event, EventSink
from paper_agent.observability.health import ObservabilityHealth


@dataclass
class _Downstream:
    sink: EventSink
    role: str  # "recorder" | "renderer" | "other"


class FanOutSink(EventSink):
    """同步 fan-out 分发器；实现 EventSink，供 EventBus 作为唯一下游。"""

    def __init__(
        self,
        downstreams: list[EventSink] | None = None,
        health: ObservabilityHealth | None = None,
    ) -> None:
        self._downstreams: list[_Downstream] = []
        self.health = health or ObservabilityHealth()
        for d in downstreams or []:
            self.add(d)

    def add(self, sink: EventSink, role: str = "other") -> None:
        self._downstreams.append(_Downstream(sink=sink, role=role))

    def add_recorder(self, sink: EventSink) -> None:
        self.add(sink, role="recorder")

    def add_renderer(self, sink: EventSink) -> None:
        self.add(sink, role="renderer")

    def deliver(self, event: Event) -> tuple[int, int]:
        """转发给每个下游；返回 (成功数, 失败数)。异常隔离、不外抛。"""
        delivered = 0
        failed = 0
        for d in self._downstreams:
            try:
                d.sink.emit(event)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - 观测不拖垮业务
                failed += 1
                self._report(d, event, exc)
        return (delivered, failed)

    def emit(self, event: Event) -> None:  # EventSink 协议
        self.deliver(event)

    def _report(self, d: _Downstream, event: Event, exc: Exception) -> None:
        """上报下游异常：只含类型信息，绝不含 payload。"""
        if d.role == "recorder":
            self.health.trace_collection_failed = True
        elif d.role == "renderer":
            self.health.renderer_failed = True
        sink_name = type(d.sink).__name__
        kind = getattr(event.kind, "value", str(event.kind))
        detail = (
            f"downstream {sink_name} failed on {kind} "
            f"(event_id={event.event_id}): {type(exc).__name__}"
        )
        self.health.add_warning(detail)
        # 只打类型信息到 stderr，不打 payload。
        # T201 在此豁免（见行尾 noqa）：可观测层自身的兜底出口 —— 此处正是
        # "下游 sink 已失败"的分支，不能再经事件总线上报（会递归回同一个坏
        # sink），只能直写 stderr。
        print(f"[observability] {detail}", file=sys.stderr)  # noqa: T201
