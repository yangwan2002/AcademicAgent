"""A2b wave 2 / Task 4：嵌套 quota 的 JSON Schema 与槽位常量。

只验证 schema 生成与激活范围；配额解析逻辑在 test_literature_quota.py。
A2d-5 起该槽已挂入 REWRITE/ADD_SECTION（与 Handler 消费同批原子激活）。
"""

from __future__ import annotations

from paper_agent.intents.router import Intent
from paper_agent.tasking.extractors.base import build_tool_schema, _slot_property
from paper_agent.tasking.intent_spec import (
    CITATION_QUOTA_SLOT_SPEC,
    INTENT_SPECS,
    RAW_QUOTA_JSON_SCHEMA,
    SlotSpec,
    BindingKind,
    ExecutionBinding,
    spec_for,
)


# ---------------------------------------------------------------- override 生效

def test_override_slot_produces_nested_schema():
    prop = _slot_property(CITATION_QUOTA_SLOT_SPEC)
    assert prop["type"] == "object"
    # 嵌套 filters / constraints 数组结构被原样带出（非空 object）
    assert "filters" in prop["properties"]
    assert "constraints" in prop["properties"]
    assert prop["properties"]["filters"]["type"] == "array"
    assert prop["properties"]["constraints"]["items"]["type"] == "object"
    # 描述被补上（override 未自带 description 顶层时用 slot.description）
    assert "description" in prop


def test_override_enums_align_with_finalize_tokens():
    con_items = RAW_QUOTA_JSON_SCHEMA["properties"]["constraints"]["items"]
    dims = set(con_items["properties"]["dimension"]["enum"])
    ops = set(con_items["properties"]["operator"]["enum"])
    assert dims == {"total", "language.zh", "language.en"}
    assert {"bare", "exact", "min", "max", "range", "approx"} == ops
    filt_items = RAW_QUOTA_JSON_SCHEMA["properties"]["filters"]["items"]
    assert set(filt_items["properties"]["field"]["enum"]) == {"year", "reference_type", "language"}


# ---------------------------------------------------------------- 激活范围
# 按 A2d-5 预期变更更新：槽已与 Handler 消费同批原子激活（原断言「未挂 Active」失效）。

def test_quota_slot_active_only_on_rewrite_and_add_section():
    for intent in (Intent.REWRITE, Intent.ADD_SECTION):
        assert "citation_quota" in spec_for(intent).slots
    for intent in (Intent.POLISH, Intent.CONVERT_FORMAT, Intent.FORMAT_DOCX):
        assert "citation_quota" not in spec_for(intent).slots


def test_existing_specs_schema_unchanged_without_override():
    """无 override 的既有槽位 schema 逐字不变（string/array/boolean/integer）。"""
    # 用一个普通 string 槽验证 override 分支不干扰默认路径
    plain = SlotSpec(
        "x", required=False, value_type="string",
        description="d",
        execution_binding=ExecutionBinding(BindingKind.PLAN_FIELD, "X.x"),
    )
    prop = _slot_property(plain)
    assert prop == {"type": "string", "description": "d"}

    arr = SlotSpec(
        "tags", required=False, value_type="array",
        execution_binding=ExecutionBinding(BindingKind.PLAN_FIELD, "X.tags"),
    )
    assert _slot_property(arr) == {"type": "array", "items": {"type": "string"}}


def test_all_active_intents_build_schema_ok():
    """五意图 schema 仍可正常生成（override 改动不破坏既有路径）。"""
    for intent, spec in INTENT_SPECS.items():
        schema = build_tool_schema(spec, intent.value)
        assert schema["type"] == "function"
        assert schema["function"]["parameters"]["required"] == []
