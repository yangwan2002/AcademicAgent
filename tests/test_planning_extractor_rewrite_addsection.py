"""intent-planning Task 11 验收：rewrite + add_section 抽取器。

- 复杂槽位：检索三元组（want_search/search_query/search_limit）、结构化 structure
- search_query 非空 → want_search（派生，source=normalized，不覆盖用户明确值）
- 结构化 structure 可转现有 AddSectionPlan
- structure_kind 非法值 → invalid
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.plans import AddSectionPlan
from paper_agent.intents.intent import Intent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tasking.contracts import (
    CallStatus,
    FieldStatus,
    RawOperation,
    SlotSource,
)
from paper_agent.tasking.extractors import build_extractor
from paper_agent.tasking.extractors.add_section import AddSectionExtractor
from paper_agent.tasking.extractors.rewrite import RewriteExtractor


def _mock(obj) -> MockLLMProvider:
    return MockLLMProvider(scripted=[json.dumps(obj, ensure_ascii=False)])


_REWRITE_OP = RawOperation(
    intent=Intent.REWRITE, verbatim="改写相关工作，补充论证并引用最新文献"
)
_ADD_OP = RawOperation(
    intent=Intent.ADD_SECTION, verbatim="新增 Discussion 章节讨论结果意义与局限"
)


# --- 注册表 ---

def test_registry_returns_rewrite_and_add_section():
    llm = MockLLMProvider()
    assert isinstance(build_extractor(Intent.REWRITE, llm), RewriteExtractor)
    assert isinstance(build_extractor(Intent.ADD_SECTION, llm), AddSectionExtractor)


# --- rewrite 检索三元组 ---

def test_rewrite_search_query_derives_want_search():
    # 用户给了 search_query 但没显式 want_search → 推导 want_search=True
    ex = RewriteExtractor(_mock({
        "slots": {
            "section_id": {"value": "sec:related", "status": "present"},
            "direction": {"value": "补充论证", "status": "present"},
            "search_query": {"value": "diffusion models", "status": "present",
                             "requirement_ids": ["r1"]},
        },
    }))
    res = ex.pre_extract(_REWRITE_OP, canonical_requirements=())
    ws = res.slots["want_search"]
    assert ws.value is True
    assert ws.field_status is FieldStatus.PRESENT
    assert ws.source is SlotSource.NORMALIZED
    # 派生的 want_search 继承 query 的 requirement 消费
    assert ws.requirement_ids == ("r1",)


def test_rewrite_explicit_want_search_not_overridden():
    # 用户显式说不搜 → 即便有 query 也不覆盖
    ex = RewriteExtractor(_mock({
        "slots": {
            "section_id": {"value": "sec:related", "status": "present"},
            "direction": {"value": "补充论证", "status": "present"},
            "search_query": {"value": "x", "status": "present"},
            "want_search": {"value": False, "status": "present"},
        },
    }))
    res = ex.pre_extract(_REWRITE_OP, canonical_requirements=())
    assert res.slots["want_search"].value is False
    assert res.slots["want_search"].source is SlotSource.USER_EXPLICIT


def test_rewrite_no_query_leaves_want_search_absent():
    ex = RewriteExtractor(_mock({
        "slots": {
            "section_id": {"value": "sec:related", "status": "present"},
            "direction": {"value": "调整结构", "status": "present"},
        },
    }))
    res = ex.pre_extract(_REWRITE_OP, canonical_requirements=())
    # 无 query、无显式 want_search → user_absent（静态默认 False 留给 PlanBuilder）
    assert res.slots["want_search"].field_status is FieldStatus.USER_ABSENT


def test_rewrite_required_slots_absent_are_user_absent():
    ex = RewriteExtractor(_mock({"slots": {}}))
    res = ex.pre_extract(_REWRITE_OP, canonical_requirements=())
    assert res.call_status is CallStatus.SUCCESS
    assert res.slots["section_id"].field_status is FieldStatus.USER_ABSENT
    assert res.slots["direction"].field_status is FieldStatus.USER_ABSENT


# --- add_section 结构化 structure ---

def test_add_section_structured_structure_maps_to_plan():
    ex = AddSectionExtractor(_mock({
        "slots": {
            "title": {"value": "Discussion", "status": "present"},
            "purpose": {"value": "讨论结果意义与局限", "status": "present"},
            "structure_kind": {"value": "list", "status": "present"},
            "subsections": {"value": ["意义", "局限", "未来工作"], "status": "present"},
            "position": {"value": "before:sec:conclusion", "status": "present"},
        },
    }))
    res = ex.pre_extract(_ADD_OP, canonical_requirements=())
    assert res.call_status is CallStatus.SUCCESS
    # 结构化联合可直接构造现有 AddSectionPlan（字段兼容）
    plan = AddSectionPlan(
        title=res.slots["title"].value,
        purpose=res.slots["purpose"].value,
        structure_kind=res.slots["structure_kind"].value,
        structure_desc=res.slots["structure_desc"].value or "",
        subsections=res.slots["subsections"].value or [],
        position=res.slots["position"].value,
    )
    assert plan.structure_kind == "list"
    assert plan.subsections == ["意义", "局限", "未来工作"]
    assert plan.position == "before:sec:conclusion"


def test_add_section_invalid_structure_kind_marked_invalid():
    ex = AddSectionExtractor(_mock({
        "slots": {
            "title": {"value": "Discussion", "status": "present"},
            "purpose": {"value": "讨论", "status": "present"},
            "structure_kind": {"value": "flowchart", "status": "present"},  # 非法
        },
    }))
    res = ex.pre_extract(_ADD_OP, canonical_requirements=())
    assert res.slots["structure_kind"].field_status is FieldStatus.INVALID


def test_add_section_desc_structure():
    ex = AddSectionExtractor(_mock({
        "slots": {
            "title": {"value": "Discussion", "status": "present"},
            "purpose": {"value": "讨论", "status": "present"},
            "structure_kind": {"value": "desc", "status": "present"},
            "structure_desc": {"value": "意义 → 局限 → 展望", "status": "present"},
        },
    }))
    res = ex.pre_extract(_ADD_OP, canonical_requirements=())
    assert res.slots["structure_kind"].value == "desc"
    assert res.slots["structure_desc"].value == "意义 → 局限 → 展望"
    # 无 search_query → want_search 保持 user_absent（静态默认 False 留给 PlanBuilder）
    assert res.slots["want_search"].field_status is FieldStatus.USER_ABSENT


def test_add_section_search_query_derives_want_search():
    # add_section 现有 search_query 槽，同 rewrite：query 非空 → 推导 want_search
    ex = AddSectionExtractor(_mock({
        "slots": {
            "title": {"value": "Related Work", "status": "present"},
            "purpose": {"value": "综述相关工作", "status": "present"},
            "structure_kind": {"value": "desc", "status": "present"},
            "search_query": {"value": "graph neural networks", "status": "present"},
        },
    }))
    res = ex.pre_extract(_ADD_OP, canonical_requirements=())
    ws = res.slots["want_search"]
    assert ws.value is True
    assert ws.source is SlotSource.NORMALIZED
