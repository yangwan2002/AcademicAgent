"""Coordinator：单次运行（一条用户消息）的观测生命周期唯一负责人。

职责（design D5）：
- 每次运行新建 `run_id / RunContext / EventBus(+FanOutSink+下游)`；无下游时用
  `NullEventBus`（零开销）。
- `WORKFLOW_START` → `with bus.span("run")` 包裹业务 → 最外层 `finally` **发恰一次
  `WORKFLOW_END`** 并**直接**调一次 `recorder.finalize_best_effort(final_state)`
  （唯一最终化点，幂等；不依赖 WORKFLOW_END 送达）。
- 取消（`OperationCancelled`）是**唯一捕获点**：置 `run_status="cancelled"` + 发一次
  `RUN_CANCELLED`，不外抛（REPL 可继续）。普通异常 → `run_status="failed"` + 脱敏
  error，同样不外抛（不拖垮 REPL）。
- 观测失败绝不拖垮业务：`trace_collection_status` 据 health 计算。

业务函数签名：`fn(bus, cancel_token) -> value`。`value` 可选实现
`run_status_hint()/working_copy_status/diff_summary` 供最终状态汇总。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from paper_agent.errors import OperationCancelled
from paper_agent.observability.bus import EventBus, NullEventBus, RunContext
from paper_agent.observability.events import EventKind
from paper_agent.observability.fanout import FanOutSink
from paper_agent.observability.health import ObservabilityHealth
from paper_agent.observability.current import use_bus
from paper_agent.observability.recorder import (
    LocalJsonExporter,
    SimpleTraceRecorder,
)
from paper_agent.observability.sanitize import sanitize_text


@dataclass
class RunReport:
    run_id: str
    value: Any = None
    run_status: str = "completed"          # completed | cancelled | failed
    working_copy_status: str = "unchanged"
    diff_summary: str = ""
    error: dict | None = None
    trace_collection_status: str = "not_requested"
    health: ObservabilityHealth = field(default_factory=ObservabilityHealth)
    recorder: SimpleTraceRecorder | None = None


class Coordinator:
    """运行编排器：把观测生命周期与业务解耦，业务只管在 `fn` 里做事、发事件。"""

    def __init__(
        self,
        *,
        enable_trace: bool = False,
        enable_stream: bool = False,
        renderer=None,
        runs_dir: str = "runs",
    ) -> None:
        self.enable_trace = enable_trace
        self.enable_stream = enable_stream
        self._renderer = renderer
        self._runs_dir = runs_dir

    @property
    def observing(self) -> bool:
        """是否有任何下游（决定用真 EventBus 还是 NullEventBus）。"""
        return self.enable_trace or self._renderer is not None

    def run(
        self,
        fn: Callable[[Any, Any], Any],
        *,
        cancel_token=None,
    ) -> RunReport:
        run_id = uuid4().hex
        health = ObservabilityHealth()
        recorder: SimpleTraceRecorder | None = None

        if self.observing:
            fan = FanOutSink(health=health)
            if self._renderer is not None:
                fan.add_renderer(self._renderer)
            if self.enable_trace:
                recorder = SimpleTraceRecorder(
                    run_id, exporter=LocalJsonExporter(self._runs_dir)
                )
                fan.add_recorder(recorder)
            bus = EventBus(fan, RunContext(run_id), health)
        else:
            bus = NullEventBus()

        report = RunReport(run_id=run_id, health=health, recorder=recorder)

        bus.publish(EventKind.WORKFLOW_START)
        try:
            # `use_bus` 把 bus 登记为 run 作用域的 ambient bus，供显式传不到的深层
            # 埋点取用（`structured_call` 等）。退出必还原，生命周期严格等于本次 run。
            with use_bus(bus), bus.span("run"):
                report.value = fn(bus, cancel_token)
                self._absorb_value(report)
        except OperationCancelled:
            report.run_status = "cancelled"
            bus.publish(EventKind.RUN_CANCELLED)
        except Exception as exc:  # noqa: BLE001 - Coordinator 不让业务异常拖垮 REPL
            report.run_status = "failed"
            report.error = {
                "category": type(exc).__name__,
                "preview": sanitize_text(str(exc)).preview,
            }
        finally:
            report.trace_collection_status = (
                "not_requested"
                if recorder is None
                else "failed"
                if (health.pipeline_failed or health.trace_collection_failed)
                else "completed"
            )
            final_state = {
                "run_status": report.run_status,
                "working_copy_status": report.working_copy_status,
                "trace_collection_status": report.trace_collection_status,
                "diff_summary": report.diff_summary,
                "error": report.error,
            }
            # 恰一次 WORKFLOW_END（唯一发布点）。
            bus.publish(EventKind.WORKFLOW_END, **final_state)
            # 唯一最终化点：最终状态直接传入，不依赖 WORKFLOW_END 送达（幂等）。
            if recorder is not None:
                recorder.finalize_best_effort(final_state=final_state)
        return report

    @staticmethod
    def _absorb_value(report: RunReport) -> None:
        """从业务返回值提炼最终状态（尽力而为，鸭子类型，绝不抛）。"""
        value = report.value
        try:
            ok = getattr(value, "ok", None)
            if ok is True:
                report.working_copy_status = "updated"
            diff = getattr(value, "diff", None)
            if isinstance(diff, str) and diff:
                # 只留摘要（行数），不落全文 diff。
                report.diff_summary = f"{diff.count(chr(10)) + 1} 行变更"
        except Exception:  # noqa: BLE001
            pass


__all__ = ["Coordinator", "RunReport"]
