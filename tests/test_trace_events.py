"""streaming-and-trace Task 1 验收：Event 信封 + 深度不可变 + serialize + 统一 EventKind。

对应 tasks.md Task 1：
- frozen=True + 新增信封字段
- deep_freeze（dict→MappingProxyType、list→tuple、set→frozenset 递归）
- __post_init__ 冻结 data
- serialize_event 可 json.dumps
- 新增 EventKind 值稳定、既有 kind 值不变、既有构造可用
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from types import MappingProxyType

from paper_agent.observability.events import (
    Event,
    EventKind,
    NullSink,
    CallbackSink,
    SENSITIVE_EPHEMERAL_KINDS,
    deep_freeze,
    serialize_event,
)


# --- 向后兼容：既有构造方式（如 resilient.py）依然可用 ---

def test_legacy_construction_still_works():
    ev = Event(
        kind=EventKind.LLM_RETRY,
        message="重试中",
        data={"attempt": 1, "delay": 0.5, "error_type": "rate_limit"},
    )
    assert ev.kind is EventKind.LLM_RETRY
    assert ev.message == "重试中"
    assert ev.data["attempt"] == 1
    # 信封字段有安全默认值
    assert ev.event_id == ""
    assert ev.sequence == -1
    assert ev.schema_version == 1
    assert ev.attempt is None


def test_default_data_is_independent_per_instance():
    a = Event(kind=EventKind.WORKFLOW_START)
    b = Event(kind=EventKind.WORKFLOW_START)
    # 两个默认 data 不应是同一对象（default_factory 而非共享可变默认）
    assert a.data is not b.data


# --- kind 值稳定（trace 落盘/日志依赖这些字符串值）---

def test_kind_values_stable():
    assert EventKind.WORKFLOW_START.value == "workflow_start"
    assert EventKind.WORKFLOW_END.value == "workflow_end"
    assert EventKind.LLM_DELTA.value == "llm_delta"
    assert EventKind.LLM_THINKING.value == "llm_thinking"
    assert EventKind.LLM_USAGE.value == "llm_usage"
    assert EventKind.LLM_RETRY.value == "llm_retry"


def test_removed_v1_legacy_kinds_absent():
    # V2 不再使用的 V1 遗留种类应已删除，避免误用
    for name in (
        "PHASE",
        "AGENT_START",
        "AGENT_LOG",
        "ITERATION",
        "REVIEW_SCORES",
        "LLM_REQUEST",
        "LLM_RESPONSE",
        "DEGRADATION",
        "EXPORT_ASSET",
        "SPAN",
    ):
        assert not hasattr(EventKind, name), name


def test_new_kinds_present():
    for name in (
        "SPAN_START",
        "SPAN_END",
        "RETRIEVAL_START",
        "RETRIEVAL_END",
        "GENERATION_ATTEMPT_START",
        "GENERATION_ATTEMPT_END",
        "GATE_START",
        "GATE_RESULT",
        "GATE_RETRY",
        "LLM_CALL_START",
        "LLM_CALL_END",
        "RUN_CANCELLED",
        "WORKING_COPY_UPDATE",
        "WORKSPACE_COMMIT",
        "SOURCE_SAVE",
    ):
        assert hasattr(EventKind, name), name


def test_sensitive_ephemeral_set():
    assert EventKind.LLM_DELTA in SENSITIVE_EPHEMERAL_KINDS
    assert EventKind.LLM_THINKING in SENSITIVE_EPHEMERAL_KINDS
    assert EventKind.GATE_RESULT not in SENSITIVE_EPHEMERAL_KINDS


# --- 不可变性：Event 本身与 data（含嵌套）都不可改 ---

def test_event_is_frozen():
    ev = Event(kind=EventKind.WORKFLOW_START, message="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.message = "y"  # type: ignore[misc]


def test_data_top_level_immutable():
    ev = Event(kind=EventKind.WORKFLOW_START, data={"x": 1})
    assert isinstance(ev.data, MappingProxyType)
    with pytest.raises(TypeError):
        ev.data["x"] = 2  # type: ignore[index]


def test_data_nested_list_becomes_tuple_and_immutable():
    ev = Event(kind=EventKind.GATE_RESULT, data={"violations": ["a", "b"]})
    assert isinstance(ev.data["violations"], tuple)
    with pytest.raises(AttributeError):
        ev.data["violations"].append("c")  # type: ignore[attr-defined]


def test_data_nested_dict_immutable():
    ev = Event(kind=EventKind.LLM_USAGE, data={"usage": {"in": 1, "out": 2}})
    assert isinstance(ev.data["usage"], MappingProxyType)
    with pytest.raises(TypeError):
        ev.data["usage"]["in"] = 99  # type: ignore[index]


def test_data_set_becomes_frozenset():
    ev = Event(kind=EventKind.WORKFLOW_START, data={"tags": {"x", "y"}})
    assert isinstance(ev.data["tags"], frozenset)


def test_mutating_source_dict_after_construction_does_not_affect_event():
    src = {"nested": {"k": [1, 2]}}
    ev = Event(kind=EventKind.WORKFLOW_START, data=src)
    src["nested"]["k"].append(3)
    src["new"] = 1
    # Event 持有的是深度冻结的独立快照
    assert ev.data["nested"]["k"] == (1, 2)
    assert "new" not in ev.data


# --- deep_freeze 直接测试 ---

def test_deep_freeze_idempotent():
    once = deep_freeze({"a": [1, {"b": {2, 3}}]})
    twice = deep_freeze(once)
    assert twice == once


def test_deep_freeze_leaves_scalars():
    assert deep_freeze("s") == "s"
    assert deep_freeze(5) == 5
    assert deep_freeze(None) is None


# --- serialize_event 可被 json.dumps ---

def test_serialize_event_json_dumpable():
    ev = Event(
        kind=EventKind.GATE_RESULT,
        message="校验完成",
        data={"violations": ["数字被改", "引用缺失"], "usage": {"in": 10}},
        trace_id="t1",
        span_id="s1",
        event_id="e1",
        sequence=3,
        generation_id="g1",
        attempt=0,
        llm_call_id="c1",
    )
    d = serialize_event(ev)
    # kind 转为字符串值
    assert d["kind"] == "gate_result"
    # 嵌套结构转回普通 list/dict
    assert d["data"]["violations"] == ["数字被改", "引用缺失"]
    assert d["data"]["usage"] == {"in": 10}
    assert d["sequence"] == 3
    assert d["attempt"] == 0
    # 可以直接 json 序列化，且中文不转义时可读
    text = json.dumps(d, ensure_ascii=False)
    assert "gate_result" in text
    # 往返
    loaded = json.loads(text)
    assert loaded["data"]["violations"] == ["数字被改", "引用缺失"]


def test_serialize_includes_all_envelope_fields():
    ev = Event(kind=EventKind.SPAN_START)
    d = serialize_event(ev)
    for key in (
        "kind",
        "message",
        "data",
        "trace_id",
        "span_id",
        "parent_span_id",
        "ts",
        "duration_ms",
        "event_id",
        "sequence",
        "schema_version",
        "generation_id",
        "attempt",
        "llm_call_id",
        "source_event_id",
    ):
        assert key in d, key


# --- 盖章通过 replace 产生新实例（不破坏不可变性）---

def test_replace_produces_new_stamped_instance():
    ev = Event(kind=EventKind.WORKFLOW_START, data={"x": 1})
    stamped = dataclasses.replace(ev, event_id="e42", sequence=7)
    assert stamped.event_id == "e42"
    assert stamped.sequence == 7
    # 原实例未变
    assert ev.event_id == ""
    assert ev.sequence == -1
    # replace 后 data 仍然是冻结的
    assert isinstance(stamped.data, MappingProxyType)


# --- sink 基础行为不变 ---

def test_null_and_callback_sink():
    NullSink().emit(Event(kind=EventKind.WORKFLOW_START))  # 不抛错
    seen = []
    CallbackSink(seen.append).emit(Event(kind=EventKind.WORKFLOW_START, message="hi"))
    assert len(seen) == 1
    assert seen[0].message == "hi"
