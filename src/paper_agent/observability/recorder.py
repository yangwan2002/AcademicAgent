"""SimpleTraceRecorder + Span / RunTrace + Trace_Exporter（薄版，observability/recorder.py）。

下游 EventSink 之一：依 `SPAN_START/SPAN_END` 建 span 树（凭信封的 span_id/
parent_span_id 定父子），把 GATE_RESULT/LLM_CALL_*/ATTEMPT_* 等挂到对应 span，
统计 provider/gate retry，结束时经 `Trace_Exporter` 原子落一个 `trace.json`。

**容忍残缺/乱序 Span（关键）**：观测错误不拖垮业务 → 可能丢事件。Recorder 绝不
能因一个事件缺失/乱序而崩溃，否则引发更多观测失败：
- 只有 END 无 START → 建 orphan span + warning；
- 只有 START 无 END → state=None（未终结）；
- parent 不存在 → 暂挂 root + missing_parent warning；
- 重复 END → 忽略第二次 + warning。

**隐私（Property 7）**：`SENSITIVE_EPHEMERAL_KINDS`（LLM_DELTA/LLM_THINKING）的
**原文一律不持久化**（renderer 负责实时显示，recorder 只字不写入 trace.json）；
其余文本字段由发布方在发布前经 Sanitizer 转 SafeText/SafePrompt，recorder 只存
其 hash/preview/length。

**v1 不做**：Event_Journal / durability / fsync / delta-response 一致性校验（→ P1）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from paper_agent.observability.events import (
    SENSITIVE_EPHEMERAL_KINDS,
    Event,
    EventKind,
)
from paper_agent.observability.sanitize import SafePrompt, SafeText


def _plain(value):
    """把任意值转为 json 可序列化的普通结构（含 SafeText/SafePrompt→dict、解冻）。"""
    if isinstance(value, (SafeText, SafePrompt)):
        return {"hash": value.hash, "preview": value.preview, "length": value.length}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_plain(v) for v in value]
    return value


@dataclass
class Span:
    span_id: str
    parent_span_id: str = ""
    name: str = ""
    generation_id: str = ""
    attempt: int | None = None
    llm_call_id: str = ""
    candidate_id: str = ""
    start_ts: float = 0.0
    end_ts: float | None = None
    duration_ms: float | None = None
    state: str | None = None            # None(未终结) | ended | cancelled | failed
    attributes: dict = field(default_factory=dict)
    children: list["Span"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "generation_id": self.generation_id,
            "attempt": self.attempt,
            "llm_call_id": self.llm_call_id,
            "candidate_id": self.candidate_id,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_ms": self.duration_ms,
            "state": self.state,
            "attributes": _plain(self.attributes),
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class RunTrace:
    trace_id: str
    schema_version: int = 1
    started_at: float = 0.0
    ended_at: float | None = None
    root: Span | None = None
    attempts: int = 0
    gate_retries: int = 0
    provider_retries: int = 0
    run_status: str = "completed"          # completed | failed | cancelled
    quality_status: str = "normal"         # normal | degraded
    working_copy_status: str = "unchanged"  # unchanged | updated | rolled_back
    trace_collection_status: str = "completed"  # not_requested | completed | failed
    diff_summary: str = ""
    error: dict | None = None
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "schema_version": self.schema_version,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "attempts": self.attempts,
            "gate_retries": self.gate_retries,
            "provider_retries": self.provider_retries,
            "run_status": self.run_status,
            "quality_status": self.quality_status,
            "working_copy_status": self.working_copy_status,
            "trace_collection_status": self.trace_collection_status,
            "diff_summary": self.diff_summary,
            "error": _plain(self.error) if self.error else None,
            "warnings": list(self.warnings),
            "root": self.root.to_dict() if self.root else None,
        }


class TraceExporter(ABC):
    """导出 RunTrace 的接口抽象（Req 9）；本功能仅提供本地 JSON 实现。"""

    @abstractmethod
    def export(self, trace: RunTrace) -> str:
        ...


class LocalJsonExporter(TraceExporter):
    """把 RunTrace 原子写到本地 `runs/<run_id>/trace.json`（UTF-8，tmp+os.replace）。"""

    def __init__(self, base_dir: str = "runs") -> None:
        self._base = base_dir

    def export(self, trace: RunTrace) -> str:
        directory = os.path.join(self._base, trace.trace_id)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "trace.json")
        tmp = path + ".tmp"
        data = json.dumps(trace.to_dict(), ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)  # 原子替换：全有或全无
        return path


class SimpleTraceRecorder:
    """实现 EventSink：消费事件构建 span 树，finalize 时落 trace.json。"""

    def __init__(self, run_id: str, exporter: TraceExporter | None = None) -> None:
        self.run_id = run_id
        self.trace = RunTrace(trace_id=run_id, started_at=time.time())
        self._spans: dict[str, Span] = {}
        self._exporter = exporter or LocalJsonExporter()
        self._finalized = False

    @property
    def warnings(self) -> list:
        return self.trace.warnings

    # --- EventSink ---

    def emit(self, event: Event) -> None:
        # 双保险：即便上游 FanOutSink 已隔离，recorder 自身也绝不因单事件崩溃。
        try:
            self._handle(event)
        except Exception as exc:  # noqa: BLE001
            self.trace.warnings.append(
                f"recorder error on {getattr(event.kind, 'value', event.kind)}: "
                f"{type(exc).__name__}"
            )

    def _handle(self, event: Event) -> None:
        kind = event.kind
        if kind in SENSITIVE_EPHEMERAL_KINDS:
            return  # LLM_DELTA / LLM_THINKING 原文一律不持久化
        if kind is EventKind.SPAN_START:
            self._on_span_start(event)
        elif kind is EventKind.SPAN_END:
            self._on_span_end(event)
        elif kind is EventKind.GENERATION_ATTEMPT_START:
            self.trace.attempts += 1
            self._attach(event, {"attempt": event.attempt})
        elif kind is EventKind.GENERATION_ATTEMPT_END:
            self._attach(event, {"status": event.data.get("status")})
        elif kind is EventKind.GATE_RETRY:
            self.trace.gate_retries += 1
        elif kind is EventKind.LLM_RETRY:
            self.trace.provider_retries += 1
        elif kind is EventKind.LLM_CALL_START:
            self._attach(
                event,
                {
                    "model": event.data.get("model"),
                    "call_mode": event.data.get("call_mode"),
                    "prompt": event.data.get("prompt"),
                },
            )
        elif kind is EventKind.LLM_CALL_END:
            self._attach(
                event,
                {
                    "status": event.data.get("status"),
                    "response": event.data.get("response"),
                    "delta_count": event.data.get("delta_count"),
                    "error": event.data.get("error"),
                },
            )
        elif kind is EventKind.LLM_USAGE:
            self._attach(
                event,
                {
                    "prompt_tokens": event.data.get("prompt_tokens"),
                    "completion_tokens": event.data.get("completion_tokens"),
                    "latency_ms": event.data.get("latency_ms"),
                    "usage_status": event.data.get("usage_status"),
                },
            )
        elif kind is EventKind.GATE_RESULT:
            span = self._span_for(event)
            if span is not None:
                cid = event.data.get("candidate_id")
                if cid and not span.candidate_id:
                    span.candidate_id = str(cid)
                self._merge_attrs(
                    span,
                    {
                        "passed": event.data.get("passed"),
                        "violations": event.data.get("violations"),
                        "candidate_id": cid,
                        "source_llm_call_id": event.data.get("source_llm_call_id"),
                    },
                )
        elif kind in (
            EventKind.ROUTE_DECISION,
            EventKind.INTERVIEW_QA,
            EventKind.RETRIEVAL_START,
            EventKind.RETRIEVAL_END,
            EventKind.GATE_START,
            EventKind.WORKING_COPY_UPDATE,
            EventKind.WORKSPACE_COMMIT,
            EventKind.SOURCE_SAVE,
            EventKind.RUN_CANCELLED,
            # --- intent-planning 业务事件（挂到当前 span 的 events 列表）---
            EventKind.OPERATION_ANALYSIS,
            EventKind.PRE_EXTRACT,
            EventKind.PLAN_VALIDATION,
            EventKind.CLARIFICATION_QA,
            EventKind.UNMAPPED_RESOLUTION,
            EventKind.PROJECT_FACT_RECORDED,
            EventKind.PLAN_CONFIRMED,
            EventKind.PLAN_REJECTED,
            EventKind.PLAN_CANCELLED,
            EventKind.OPERATION_RESULT,
            EventKind.PLAN_RESULT,
        ):
            self._record_event(event)

    # --- span 树构建 ---

    def _on_span_start(self, event: Event) -> None:
        sid = event.span_id
        if not sid:
            self.trace.warnings.append("span_start without span_id")
            return
        if sid in self._spans:
            self.trace.warnings.append(f"duplicate span_start {sid}")
            return
        span = Span(
            span_id=sid,
            parent_span_id=event.parent_span_id,
            name=str(event.data.get("name", "")),
            generation_id=event.generation_id,
            attempt=event.attempt,
            llm_call_id=event.llm_call_id,
            start_ts=event.ts,
            attributes=_plain(event.data.get("attributes") or {}),
        )
        self._spans[sid] = span
        parent = self._spans.get(event.parent_span_id) if event.parent_span_id else None
        if event.parent_span_id and parent is None:
            self.trace.warnings.append(
                f"missing_parent {event.parent_span_id} for {sid}"
            )
        if parent is not None:
            parent.children.append(span)
        elif self.trace.root is None and not event.parent_span_id:
            self.trace.root = span
        elif self.trace.root is not None:
            self.trace.root.children.append(span)  # orphan 暂挂 root
        else:
            self.trace.root = span

    def _on_span_end(self, event: Event) -> None:
        sid = event.span_id
        span = self._spans.get(sid)
        if span is None:
            # orphan END：建占位 span 挂到 root
            span = Span(
                span_id=sid,
                parent_span_id=event.parent_span_id,
                name=str(event.data.get("name", "<orphan>")),
            )
            self._spans[sid] = span
            if self.trace.root is not None and self.trace.root is not span:
                self.trace.root.children.append(span)
            else:
                self.trace.root = span
            self.trace.warnings.append(f"orphan span_end {sid}")
        if span.state is not None:
            self.trace.warnings.append(f"duplicate span_end {sid}")
            return
        span.state = event.data.get("span_state")
        span.end_ts = event.ts
        span.duration_ms = event.duration_ms

    # --- 挂载 ---

    def _span_for(self, event: Event) -> Span | None:
        span = self._spans.get(event.span_id)
        if span is None:
            span = self.trace.root
        if span is None:
            self.trace.warnings.append(
                f"no span for {getattr(event.kind, 'value', event.kind)}"
            )
        return span

    def _attach(self, event: Event, attrs: dict) -> None:
        span = self._span_for(event)
        if span is None:
            return
        self._merge_attrs(span, attrs)

    def _merge_attrs(self, span: Span, attrs: dict) -> None:
        """首写为准合并属性：已存在且值不同则记 warning、保留第一次（与重复 END 一致）。

        正常 v1 拓扑下同 span 同 key 不会写两次；此处只为防御异常/重复事件时
        静默覆盖、丢失第一次的权威结果。
        """
        for key, value in _plain(attrs).items():
            if key in span.attributes and span.attributes[key] != value:
                self.trace.warnings.append(
                    f"duplicate attribute '{key}' on span {span.span_id}; keeping first"
                )
                continue
            span.attributes[key] = value

    def _record_event(self, event: Event) -> None:
        span = self._span_for(event)
        if span is None:
            return
        events = span.attributes.setdefault("events", [])
        events.append(
            {
                "kind": getattr(event.kind, "value", str(event.kind)),
                "data": _plain(dict(event.data)),
            }
        )

    # --- 最终化 ---

    def finalize_best_effort(self, final_state: dict | None = None) -> None:
        """幂等：合并最终状态并导出一次 trace.json；导出失败单独记 stderr，不外抛。"""
        if self._finalized:
            return
        self._finalized = True
        self.trace.ended_at = time.time()
        if final_state:
            for key in (
                "run_status",
                "quality_status",
                "working_copy_status",
                "trace_collection_status",
                "diff_summary",
            ):
                if final_state.get(key) is not None:
                    setattr(self.trace, key, final_state[key])
            if final_state.get("error"):
                self.trace.error = _plain(final_state["error"])
        try:
            self._exporter.export(self.trace)
        except Exception as exc:  # noqa: BLE001 - 导出失败不拖垮业务
            print(
                f"[observability] trace export failed: {type(exc).__name__}",
                file=sys.stderr,
            )
