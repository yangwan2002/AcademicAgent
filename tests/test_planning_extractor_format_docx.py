"""intent-planning Task 12 验收：format_docx 抽取器。

- operations 输出受限 list[FormatOp]，只允许 REGISTRY 支持的类型
- 非法操作类型 → 整槽 invalid（保留原始供澄清，不静默丢弃）
- 合法 op 可转现有格式操作（可直接喂 apply_ops）
- 非法/未知 scope 归一为 all
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tasking.contracts import CallStatus, FieldStatus, RawOperation
from paper_agent.tasking.extractors import build_extractor
from paper_agent.tasking.extractors.format_docx import FormatDocxExtractor
from paper_agent.tools.docx_format import REGISTRY, FormatOp, apply_ops


def _mock(obj) -> MockLLMProvider:
    return MockLLMProvider(scripted=[json.dumps(obj, ensure_ascii=False)])


_OP = RawOperation(intent=Intent.FORMAT_DOCX, verbatim="正文宋体小四，标题居中，全文双栏")


def test_registry_returns_format_docx():
    assert isinstance(build_extractor(Intent.FORMAT_DOCX, MockLLMProvider()),
                      FormatDocxExtractor)


def test_legal_ops_convert_to_format_op_list():
    ex = FormatDocxExtractor(_mock({
        "slots": {
            "operations": {"status": "present", "value": [
                {"name": "set_font_family", "value": "宋体", "scope": "body"},
                {"name": "set_alignment", "value": "center", "scope": "headings"},
                {"name": "set_columns", "value": 2, "scope": "all"},
            ]},
        },
    }))
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.call_status is CallStatus.SUCCESS
    sv = res.slots["operations"]
    assert sv.field_status is FieldStatus.PRESENT
    ops = sv.value
    assert all(isinstance(o, FormatOp) for o in ops)
    assert [o.name for o in ops] == ["set_font_family", "set_alignment", "set_columns"]
    # 只用 REGISTRY 支持的类型
    assert all(o.name in REGISTRY for o in ops)


def test_illegal_op_type_marks_slot_invalid():
    ex = FormatDocxExtractor(_mock({
        "slots": {
            "operations": {"status": "present", "value": [
                {"name": "set_font_family", "value": "宋体", "scope": "body"},
                {"name": "insert_watermark", "value": "DRAFT", "scope": "all"},  # 非法
            ]},
        },
    }))
    res = ex.pre_extract(_OP, canonical_requirements=())
    sv = res.slots["operations"]
    assert sv.field_status is FieldStatus.INVALID
    # 原始值保留（供 Clarifier 回显问题），不静默丢弃
    assert isinstance(sv.value, list)
    assert any(o.get("name") == "insert_watermark" for o in sv.value)


def test_unknown_scope_normalized_to_all():
    ex = FormatDocxExtractor(_mock({
        "slots": {
            "operations": {"status": "present", "value": [
                {"name": "set_bold", "value": True, "scope": "footer"},  # 未知 scope
            ]},
        },
    }))
    res = ex.pre_extract(_OP, canonical_requirements=())
    sv = res.slots["operations"]
    assert sv.field_status is FieldStatus.PRESENT
    assert sv.value[0].scope == "all"


def test_operations_absent_is_user_absent():
    ex = FormatDocxExtractor(_mock({"slots": {}}))
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.slots["operations"].field_status is FieldStatus.USER_ABSENT
    assert res.slots["operations"].value is None


def test_operations_not_a_list_marked_invalid():
    ex = FormatDocxExtractor(_mock({
        "slots": {"operations": {"status": "present", "value": "宋体小四"}},
    }))
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.slots["operations"].field_status is FieldStatus.INVALID


def test_scope_slot_uses_generic_coercion():
    ex = FormatDocxExtractor(_mock({
        "slots": {
            "operations": {"status": "present", "value": [
                {"name": "set_bold", "value": True, "scope": "all"},
            ]},
            "scope": {"status": "present", "value": "document"},
        },
    }))
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.slots["scope"].value == "document"
    assert res.slots["scope"].field_status is FieldStatus.PRESENT
