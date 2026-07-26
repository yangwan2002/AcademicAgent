"""intent-planning Task 3 验收：业务事件进入 trace.json + 原文不落盘。

- 新增 EventKind 值稳定、不新增重复生命周期事件（无 TURN/OPERATION_START/END）
- 业务事件经 Recorder 落入 trace.json
- OPERATION_RESULT 可见 status；PLAN_RESULT 可见 plan_status
- 澄清等文本经 Sanitizer（SafeText）→ 原文不落盘
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.observability.events import Event, EventKind
from paper_agent.observability.recorder import LocalJsonExporter, SimpleTraceRecorder
from paper_agent.observability.sanitize import sanitize_text


def _span_start(span_id, parent="", name=""):
    return Event(kind=EventKind.SPAN_START, data={"name": name},
                 span_id=span_id, parent_span_id=parent)


def _span_end(span_id, parent="", state="ended"):
    return Event(kind=EventKind.SPAN_END, data={"name": "", "span_state": state},
                 span_id=span_id, parent_span_id=parent, duration_ms=1.0)


def _all_events_in(node):
    """递归收集 span 树里所有 attributes['events']。"""
    out = list(node.get("attributes", {}).get("events", []))
    for c in node.get("children", []):
        out.extend(_all_events_in(c))
    return out


# --- EventKind 存在且值稳定 ---

def test_new_business_kinds_present_and_stable():
    assert EventKind.OPERATION_ANALYSIS.value == "operation_analysis"
    assert EventKind.PRE_EXTRACT.value == "pre_extract"
    assert EventKind.PLAN_VALIDATION.value == "plan_validation"
    assert EventKind.CLARIFICATION_QA.value == "clarification_qa"
    assert EventKind.UNMAPPED_RESOLUTION.value == "unmapped_resolution"
    assert EventKind.PLAN_CONFIRMED.value == "plan_confirmed"
    assert EventKind.PLAN_REJECTED.value == "plan_rejected"
    assert EventKind.PLAN_CANCELLED.value == "plan_cancelled"
    assert EventKind.OPERATION_RESULT.value == "operation_result"
    assert EventKind.PLAN_RESULT.value == "plan_result"


def test_no_duplicate_lifecycle_kinds():
    # 复用 WORKFLOW_START/END=Turn、SPAN_START/END=Operation，不新增重复生命周期
    for absent in ("TURN_START", "TURN_END", "OPERATION_START", "OPERATION_END"):
        assert not hasattr(EventKind, absent), absent


# --- 业务事件进 trace.json ---

def test_business_events_reach_trace_json(tmp_path):
    rec = SimpleTraceRecorder("run-biz", exporter=LocalJsonExporter(base_dir=str(tmp_path)))
    rec.emit(_span_start("run", "", "run"))
    rec.emit(Event(kind=EventKind.OPERATION_ANALYSIS, span_id="run",
                   data={"operation_count": 2}))
    rec.emit(Event(kind=EventKind.PLAN_VALIDATION, span_id="run",
                   data={"result": "ready"}))
    rec.emit(_span_start("op1", "run", "operation:op1"))
    rec.emit(Event(kind=EventKind.OPERATION_RESULT, span_id="op1",
                   data={"operation_id": "op1", "status": "succeeded"}))
    rec.emit(_span_end("op1", "run"))
    rec.emit(Event(kind=EventKind.PLAN_RESULT, span_id="run",
                   data={"plan_status": "done"}))
    rec.emit(_span_end("run", ""))
    rec.finalize_best_effort(final_state={"run_status": "completed"})

    doc = json.loads(open(os.path.join(str(tmp_path), "run-biz", "trace.json"),
                          encoding="utf-8").read())
    events = _all_events_in(doc["root"])
    kinds = [e["kind"] for e in events]
    assert "operation_analysis" in kinds
    assert "plan_validation" in kinds
    assert "operation_result" in kinds
    assert "plan_result" in kinds
    # OPERATION_RESULT 能看出 status
    opres = next(e for e in events if e["kind"] == "operation_result")
    assert opres["data"]["status"] == "succeeded"
    # PLAN_RESULT 能看出 plan_status
    planres = next(e for e in events if e["kind"] == "plan_result")
    assert planres["data"]["plan_status"] == "done"


# --- 原文不落盘 ---

def test_clarification_text_sanitized_not_persisted(tmp_path):
    # 长文本（>500 字）：证明只留 ≤500 预览、全文不落盘
    secret = "用户回答机密内容。" * 100
    rec = SimpleTraceRecorder("run-priv", exporter=LocalJsonExporter(base_dir=str(tmp_path)))
    rec.emit(_span_start("run", "", "run"))
    # 发布方（Task 17）负责脱敏：answer 已是 SafeText
    rec.emit(Event(kind=EventKind.CLARIFICATION_QA, span_id="run",
                   data={"question": "润色到什么程度？", "answer": sanitize_text(secret)}))
    rec.emit(_span_end("run", ""))
    rec.finalize_best_effort()

    raw = open(os.path.join(str(tmp_path), "run-priv", "trace.json"), encoding="utf-8").read()
    assert secret not in raw          # 全文不落盘（只留 ≤500 预览）
    doc = json.loads(raw)
    ev = _all_events_in(doc["root"])[0]
    ans = ev["data"]["answer"]
    assert set(ans.keys()) == {"hash", "preview", "length"}   # 只留 SafeText 结构
    assert len(ans["hash"]) == 64
    assert len(ans["preview"]) <= 500
    assert ans["length"] == len(secret)
