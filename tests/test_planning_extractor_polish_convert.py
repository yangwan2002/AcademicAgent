"""intent-planning Task 10 验收：polish + convert 抽取器。

- 字段来源/状态正确（present/user_absent/normalized/invalid）
- requirement 消费到具体 slot
- convert 目标格式规范化（Word→docx）与非法拦截（→invalid）
- build_extractor 注册表可取到 polish/convert
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
from paper_agent.tasking.contracts import (
    AtomicRequirement,
    CallStatus,
    FieldStatus,
    RawOperation,
    SlotSource,
)
from paper_agent.tasking.extractors import build_extractor
from paper_agent.tasking.extractors.convert import ConvertExtractor
from paper_agent.tasking.extractors.polish import PolishExtractor


def _mock(obj) -> MockLLMProvider:
    return MockLLMProvider(scripted=[json.dumps(obj, ensure_ascii=False)])


_POLISH_OP = RawOperation(intent=Intent.POLISH, verbatim="把引言润色一下，别改数字")
_CONVERT_OP = RawOperation(intent=Intent.CONVERT_FORMAT, verbatim="转成 Word")


# --- 注册表 ---

def test_registry_returns_polish_and_convert():
    llm = MockLLMProvider()
    assert isinstance(build_extractor(Intent.POLISH, llm), PolishExtractor)
    assert isinstance(build_extractor(Intent.CONVERT_FORMAT, llm), ConvertExtractor)


def test_registry_unregistered_intent_returns_none():
    # 非规划五意图（如 UNCLEAR）不注册抽取器
    assert build_extractor(Intent.UNCLEAR, MockLLMProvider()) is None


# --- polish 字段状态/来源 ---

def test_polish_present_and_user_absent():
    ex = PolishExtractor(_mock({
        "slots": {
            "section_id": {"value": "sec:intro", "status": "present"},
            "constraints": {"value": ["不改数字"], "status": "present"},
        },
    }))
    res = ex.pre_extract(_POLISH_OP, canonical_requirements=())
    assert res.call_status is CallStatus.SUCCESS
    assert res.slots["section_id"].field_status is FieldStatus.PRESENT
    assert res.slots["section_id"].source is SlotSource.USER_EXPLICIT
    assert res.slots["constraints"].value == ["不改数字"]
    # goal 用户没说 → user_absent（默认值不在抽取器填）
    assert res.slots["goal"].field_status is FieldStatus.USER_ABSENT
    assert res.slots["goal"].value is None


def test_polish_requirement_consumed_to_slot():
    ex = PolishExtractor(_mock({
        "slots": {
            "section_id": {"value": "sec:intro", "status": "present",
                           "requirement_ids": ["r1"]},
            "constraints": {"value": ["不改数字"], "status": "present",
                            "requirement_ids": ["r2"]},
        },
    }))
    res = ex.pre_extract(_POLISH_OP, canonical_requirements=(
        AtomicRequirement(requirement_id="r1", verbatim="润色引言"),
        AtomicRequirement(requirement_id="r2", verbatim="别改数字"),
    ))
    assert set(res.consumed_requirement_ids) == {"r1", "r2"}
    assert res.slots["constraints"].requirement_ids == ("r2",)


# --- convert 规范化 / 非法 ---

def test_convert_normalizes_word_to_docx():
    ex = ConvertExtractor(_mock({
        "slots": {"target_ext": {"value": "Word", "status": "present"}},
    }))
    res = ex.pre_extract(_CONVERT_OP, canonical_requirements=())
    sv = res.slots["target_ext"]
    assert sv.value == "docx"
    assert sv.field_status is FieldStatus.PRESENT
    assert sv.source is SlotSource.NORMALIZED


def test_convert_canonical_value_stays_user_explicit():
    ex = ConvertExtractor(_mock({
        "slots": {"target_ext": {"value": "docx", "status": "present"}},
    }))
    res = ex.pre_extract(_CONVERT_OP, canonical_requirements=())
    sv = res.slots["target_ext"]
    assert sv.value == "docx"
    assert sv.source is SlotSource.USER_EXPLICIT


def test_convert_unknown_format_marked_invalid():
    ex = ConvertExtractor(_mock({
        "slots": {"target_ext": {"value": "pptx", "status": "present"}},
    }))
    res = ex.pre_extract(_CONVERT_OP, canonical_requirements=())
    sv = res.slots["target_ext"]
    assert sv.field_status is FieldStatus.INVALID


def test_convert_target_absent_is_user_absent():
    ex = ConvertExtractor(_mock({"slots": {}}))
    res = ex.pre_extract(_CONVERT_OP, canonical_requirements=())
    assert res.slots["target_ext"].field_status is FieldStatus.USER_ABSENT


# --- Fast_Path：convert 发现 requirement ---

def test_convert_fast_path_discovers_requirement():
    ex = ConvertExtractor(_mock({
        "slots": {"target_ext": {"value": "docx", "status": "present"}},
        "newly_discovered_requirements": [{"verbatim": "转成 Word", "category": "target"}],
    }))
    res = ex.pre_extract(_CONVERT_OP, canonical_requirements=None)
    assert len(res.newly_discovered_requirements) == 1
    assert res.newly_discovered_requirements[0].category == "target"
