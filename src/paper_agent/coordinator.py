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

业务函数签名：`fn(bus, cancel_token) -> value`。需要声明 Working_Copy 终态时，
业务函数显式返回 `RunOutcome(value, working_copy_status=...)`；普通返回值一律按
只读/未改变处理。Coordinator 不再通过任意对象的 ``ok`` 属性猜测是否发生写入。
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


@dataclass(frozen=True)
class RunOutcome:
    """业务返回值及其显式的 Working_Copy 终态。

    ``ok`` 在项目里既表示“问答成功”，也表示“生成成功”，无法推出文档是否改变。
    这个信封只由真正拥有写入语义的应用编排层构造；Coordinator 只读取该类型，
    不再对任意业务对象做鸭子类型推断。
    """

    value: Any = None
    working_copy_status: str = "unchanged"
    changed_lines: int | None = None

    def __post_init__(self) -> None:
        if self.working_copy_status not in {"unchanged", "updated", "rolled_back"}:
            raise ValueError(
                "working_copy_status must be unchanged, updated, or rolled_back"
            )
        if self.changed_lines is not None and self.changed_lines < 0:
            raise ValueError("changed_lines must be non-negative")


class Coordinator:
    """运行编排器：把观测生命周期与业务解耦，业务只管在 `fn` 里做事、发事件。"""

    def __init__(
        self,
        *,
        enable_trace: bool = False,
        enable_stream: bool = False,
        renderer=None,
        runs_dir: str = "runs",
        enable_langfuse: bool = False,
    ) -> None:
        self.enable_trace = enable_trace
        self.enable_stream = enable_stream
        self._renderer = renderer
        self._runs_dir = runs_dir
        self.enable_langfuse = enable_langfuse

    def _build_exporter(self):
        """本地 JSON 恒在第一位；Langfuse 只作附加的第二路。

        Langfuse 未开、未装包、或 key 不全 → 直接退化为单路本地导出，
        不让可选依赖影响主路径。
        """
        local = LocalJsonExporter(self._runs_dir)
        if not self.enable_langfuse:
            return local
        try:
            from paper_agent.observability.langfuse_exporter import (
                CompositeExporter,
                LangfuseExporter,
            )

            remote = LangfuseExporter()
            if not remote.configured:
                return local
            return CompositeExporter(local, remote)
        except Exception:  # noqa: BLE001 - 可选依赖缺失绝不能拖垮 trace 落盘
            return local

    @property
    def observing(self) -> bool:
        """是否有任何下游（决定用真 EventBus 还是 NullEventBus）。"""
        return self.enable_trace or self._renderer is not None

    def run(
        self,
        fn: Callable[[Any, Any], Any],
        *,
        cancel_token=None,
        working_copy_probe: Callable[[], str] | None = None,
    ) -> RunReport:
        """运行一次业务旅程。

        ``working_copy_probe`` 是可选的只读、best-effort 终态探针。业务因取消或
        异常而来不及返回显式 ``RunOutcome`` 时，Coordinator 会在 ``finally`` 中
        调用它，避免已经发生的写入被误报为 ``unchanged``。显式 ``RunOutcome``
        始终优先，探针不会覆盖 ``updated`` / ``rolled_back`` 等业务结论。
        """
        run_id = uuid4().hex
        health = ObservabilityHealth()
        recorder: SimpleTraceRecorder | None = None
        has_explicit_working_copy_status = False

        if self.observing:
            fan = FanOutSink(health=health)
            if self._renderer is not None:
                fan.add_renderer(self._renderer)
            if self.enable_trace:
                recorder = SimpleTraceRecorder(run_id, exporter=self._build_exporter())
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
                value = fn(bus, cancel_token)
                has_explicit_working_copy_status = isinstance(value, RunOutcome)
                self._absorb_value(report, value)
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
            if working_copy_probe is not None and not has_explicit_working_copy_status:
                self._absorb_working_copy_probe(report, working_copy_probe)
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
    def _absorb_value(report: RunReport, value: Any) -> None:
        """吸收显式 ``RunOutcome``；普通值保持 ``unchanged``。

        不能再把 ``value.ok is True`` 当写入证据：``AskResult.ok`` 只表示回答正常
        收敛，曾因此把完全只读的 AskLoop 标成 ``working_copy_status=updated``。
        """
        if not isinstance(value, RunOutcome):
            report.value = value
            return
        report.value = value.value
        report.working_copy_status = value.working_copy_status
        if value.changed_lines is not None:
            report.diff_summary = f"{value.changed_lines} 行变更"

    @staticmethod
    def _absorb_working_copy_probe(
        report: RunReport,
        probe: Callable[[], str],
    ) -> None:
        """安全吸收只读终态探针；探针故障不得改变业务 run 的终态。"""
        try:
            status = probe()
        except Exception:  # noqa: BLE001 - 状态观测失败不能掩盖原始业务终态
            return
        if status in {"unchanged", "updated", "rolled_back"}:
            report.working_copy_status = status


__all__ = ["Coordinator", "RunOutcome", "RunReport"]
