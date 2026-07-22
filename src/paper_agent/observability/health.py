"""可观测健康状态（ObservabilityHealth）。

放在独立中立模块，避免 `bus.py`（EventBus 置 `pipeline_failed`）与
`fanout.py`（FanOutSink 置 `trace_collection_failed/renderer_failed`）互相
import 造成循环依赖。

四个维度分层：
- `pipeline_failed`：EventBus 自身故障（stamp / deep_freeze / sequence 分配等，
  发生在下游之前）。
- `trace_collection_failed`：TraceRecorder 下游 emit 抛异常。
- `renderer_failed`：ConsoleRenderer 下游 emit 抛异常。
- `warnings`：人类可读的降级/告警摘要（不含敏感 payload）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ObservabilityHealth:
    pipeline_failed: bool = False
    trace_collection_failed: bool = False
    renderer_failed: bool = False
    warnings: list[str] = field(default_factory=list)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)
