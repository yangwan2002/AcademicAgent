"""streaming-and-trace Task 7 验收：SimpleTraceRecorder + Span/RunTrace + LocalJsonExporter。

对应 tasks.md Task 7 / design §7 / Property 7 / Req 7.x, 8.x, 9.x：
- SPAN_START/END 建 span 树父子正确
- provider/gate retry 分别计数
- trace.json 不含正文全文/密钥；LLM_DELTA 原文不入 trace
- 导出幂等
- 容忍残缺/乱序 span（orphan END、重复 END、缺 parent）不崩
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.observability.events import Event, EventKind
from paper_agent.observability.recorder import (
    LocalJsonExporter,
    SimpleTraceRecorder,
    TraceExporter,
)
from paper_agent.observability.sanitize import sanitize_messages, sanitize_text
from paper_agent.providers.llm.base import Message


def _span_start(span_id, parent="", name="", **envelope):
    return Event(
        kind=EventKind.SPAN_START,
        data={"name": name},
        span_id=span_id,
        parent_span_id=parent,
        **envelope,
    )


def _span_end(span_id, parent="", state="ended", duration_ms=1.0, name=""):
    return Event(
        kind=EventKind.SPAN_END,
        data={"name": name, "span_state": state},
        span_id=span_id,
        parent_span_id=parent,
        duration_ms=duration_ms,
    )


class CountingExporter(TraceExporter):
    def __init__(self):
        self.calls = 0
        self.last = None

    def export(self, trace):
        self.calls += 1
        self.last = trace
        return "mem"


# --- span 树父子 ---

def test_span_tree_parent_child():
    rec = SimpleTraceRecorder("run-1", exporter=CountingExporter())
    rec.emit(_span_start("run", "", "run"))
    rec.emit(_span_start("att", "run", "generation_attempt"))
    rec.emit(_span_start("call", "att", "llm_call"))
    rec.emit(_span_end("call", "att"))
    rec.emit(_span_end("att", "run"))
    rec.emit(_span_end("run", ""))

    root = rec.trace.root
    assert root is not None and root.name == "run"
    assert root.state == "ended"
    assert len(root.children) == 1
    att = root.children[0]
    assert att.name == "generation_attempt"
    assert att.children[0].name == "llm_call"
    assert att.children[0].state == "ended"


def test_started_but_not_ended_has_no_state():
    rec = SimpleTraceRecorder("run-1", exporter=CountingExporter())
    rec.emit(_span_start("run", "", "run"))
    assert rec.trace.root.state is None  # 未终结


# --- retry 分别计数 ---

def test_provider_and_gate_retries_counted_separately():
    rec = SimpleTraceRecorder("run-1", exporter=CountingExporter())
    rec.emit(_span_start("run", "", "run"))
    rec.emit(Event(kind=EventKind.LLM_RETRY, span_id="run"))
    rec.emit(Event(kind=EventKind.LLM_RETRY, span_id="run"))
    rec.emit(Event(kind=EventKind.GATE_RETRY, span_id="run"))
    assert rec.trace.provider_retries == 2
    assert rec.trace.gate_retries == 1


def test_attempts_counted():
    rec = SimpleTraceRecorder("run-1", exporter=CountingExporter())
    rec.emit(_span_start("run", "", "run"))
    rec.emit(Event(kind=EventKind.GENERATION_ATTEMPT_START, span_id="run", attempt=1))
    rec.emit(Event(kind=EventKind.GENERATION_ATTEMPT_START, span_id="run", attempt=2))
    assert rec.trace.attempts == 2


# --- 隐私：trace.json 不含正文/密钥；LLM_DELTA 原文不入 ---

def test_trace_json_privacy(tmp_path):
    api_key = "ada_800aac45e6a742489460d155234322a4"
    # 只在 LLM_DELTA 里出现的唯一标记：用来证明增量原文绝不落盘
    delta_only_marker = "增量原文唯一标记DELTA_ONLY_MARKER_XYZ"
    # 长正文（>500 字）：证明只留 ≤500 预览、全文不落盘
    long_body = "论文正文段落。" * 200  # 远超 500 字

    rec = SimpleTraceRecorder(
        "run-priv", exporter=LocalJsonExporter(base_dir=str(tmp_path))
    )
    rec.emit(_span_start("run", "", "run"))
    rec.emit(_span_start("call", "run", "llm_call", llm_call_id="c1"))
    # prompt 发布前已脱敏（SafePrompt），含密钥应被 redact
    rec.emit(
        Event(
            kind=EventKind.LLM_CALL_START,
            span_id="call",
            llm_call_id="c1",
            data={
                "model": "qwen",
                "call_mode": "stream",
                "prompt": sanitize_messages(
                    [Message(role="user", content=f"请润色 key={api_key}")]
                ),
            },
        )
    )
    # LLM_DELTA 携原始候选文字 —— 绝不能落盘
    rec.emit(
        Event(kind=EventKind.LLM_DELTA, span_id="call", llm_call_id="c1",
              data={"text": delta_only_marker, "chunk_index": 0})
    )
    rec.emit(
        Event(
            kind=EventKind.LLM_CALL_END,
            span_id="call",
            llm_call_id="c1",
            data={"status": "completed", "response": sanitize_text(long_body),
                  "delta_count": 1},
        )
    )
    rec.emit(_span_end("call", "run"))
    rec.emit(_span_end("run", ""))
    rec.finalize_best_effort(final_state={"run_status": "completed"})

    path = os.path.join(str(tmp_path), "run-priv", "trace.json")
    assert os.path.exists(path)
    raw = open(path, encoding="utf-8").read()

    # ① 密钥被剔除
    assert api_key not in raw
    # ② LLM_DELTA 原文（唯一标记）绝不落盘
    assert delta_only_marker not in raw
    # ③ 长正文只留 ≤500 预览，全文不落盘
    assert long_body not in raw
    doc = json.loads(raw)
    resp = doc["root"]["children"][0]["attributes"]["response"]
    assert len(resp["preview"]) <= 500
    assert resp["length"] == len(long_body)  # 记录了长度但不存全文
    assert len(resp["hash"]) == 64
    assert doc["run_status"] == "completed"
    assert doc["schema_version"] == 1


def test_finalize_is_idempotent():
    exp = CountingExporter()
    rec = SimpleTraceRecorder("run-1", exporter=exp)
    rec.emit(_span_start("run", "", "run"))
    rec.emit(_span_end("run", ""))
    rec.finalize_best_effort()
    rec.finalize_best_effort()
    rec.finalize_best_effort()
    assert exp.calls == 1  # 只导出一次


def test_export_failure_does_not_raise():
    class BoomExporter(TraceExporter):
        def export(self, trace):
            raise RuntimeError("disk full")

    rec = SimpleTraceRecorder("run-1", exporter=BoomExporter())
    rec.emit(_span_start("run", "", "run"))
    # 不抛异常
    rec.finalize_best_effort()


# --- 容忍残缺/乱序 span ---

def test_orphan_span_end_does_not_crash():
    rec = SimpleTraceRecorder("run-1", exporter=CountingExporter())
    rec.emit(_span_start("run", "", "run"))
    rec.emit(_span_end("ghost", "run"))  # 无对应 START
    assert any("orphan" in w for w in rec.warnings)
    # root 仍在，未崩溃
    assert rec.trace.root is not None


def test_duplicate_span_end_ignored():
    rec = SimpleTraceRecorder("run-1", exporter=CountingExporter())
    rec.emit(_span_start("run", "", "run"))
    rec.emit(_span_end("run", "", state="ended"))
    rec.emit(_span_end("run", "", state="failed"))  # 第二次应忽略
    assert rec.trace.root.state == "ended"  # 保留第一次
    assert any("duplicate span_end" in w for w in rec.warnings)


def test_missing_parent_attaches_to_root_with_warning():
    rec = SimpleTraceRecorder("run-1", exporter=CountingExporter())
    rec.emit(_span_start("run", "", "run"))
    rec.emit(_span_start("child", "nonexistent", "orphan_child"))
    assert any("missing_parent" in w for w in rec.warnings)
    # child 被暂挂到 root
    assert any(c.span_id == "child" for c in rec.trace.root.children)


def test_duplicate_attribute_keeps_first_and_warns():
    rec = SimpleTraceRecorder("run-1", exporter=CountingExporter())
    rec.emit(_span_start("run", "", "run"))
    rec.emit(_span_start("call", "run", "llm_call", llm_call_id="c1"))
    # 异常：同一 llm_call span 收到两次 LLM_CALL_END，status 不同
    rec.emit(Event(kind=EventKind.LLM_CALL_END, span_id="call",
                   data={"status": "completed", "delta_count": 3}))
    rec.emit(Event(kind=EventKind.LLM_CALL_END, span_id="call",
                   data={"status": "failed", "delta_count": 9}))
    call_span = rec.trace.root.children[0]
    # 保留第一次的权威结果，不被静默覆盖
    assert call_span.attributes["status"] == "completed"
    assert call_span.attributes["delta_count"] == 3
    assert any("duplicate attribute" in w for w in rec.warnings)


def test_disjoint_kinds_merge_without_warning():
    rec = SimpleTraceRecorder("run-1", exporter=CountingExporter())
    rec.emit(_span_start("run", "", "run"))
    rec.emit(_span_start("call", "run", "llm_call", llm_call_id="c1"))
    # 正常：START/END/USAGE 用互不相交的 key，合并不冲突、无 warning
    rec.emit(Event(kind=EventKind.LLM_CALL_START, span_id="call",
                   data={"model": "qwen", "call_mode": "stream"}))
    rec.emit(Event(kind=EventKind.LLM_CALL_END, span_id="call",
                   data={"status": "completed", "delta_count": 2}))
    rec.emit(Event(kind=EventKind.LLM_USAGE, span_id="call",
                   data={"prompt_tokens": 10, "completion_tokens": 5}))
    attrs = rec.trace.root.children[0].attributes
    assert attrs["model"] == "qwen"
    assert attrs["status"] == "completed"
    assert attrs["prompt_tokens"] == 10
    assert not any("duplicate attribute" in w for w in rec.warnings)


def test_gate_result_attached_to_gate_span():
    rec = SimpleTraceRecorder("run-1", exporter=CountingExporter())
    rec.emit(_span_start("run", "", "run"))
    rec.emit(_span_start("gate", "run", "gate"))
    rec.emit(
        Event(
            kind=EventKind.GATE_RESULT,
            span_id="gate",
            data={
                "passed": False,
                "violations": ["数字被改"],
                "candidate_id": "cand-1",
                "source_llm_call_id": "c1",
            },
        )
    )
    rec.emit(_span_end("gate", "run"))
    gate_span = rec.trace.root.children[0]
    assert gate_span.name == "gate"
    assert gate_span.candidate_id == "cand-1"
    assert gate_span.attributes["passed"] is False
    assert gate_span.attributes["violations"] == ["数字被改"]
