"""intent-planning Task 9 验收：PreExtractor 公共框架。

覆盖 design D3/D4/Component§4：
- 调用态/字段态分离：user_absent ≠ unavailable
- 四态失败：provider/parse/schema 失败标 call_status，槽位全 unavailable
- requirement 消费：consumed_requirement_ids 汇总自各 slot
- Fast_Path：产 newly_discovered_requirements（offset 程序定位）
- 整体失败兜底：Fast_Path 失败把整段原话记为 unparsed，不静默丢失
- 抽取器不填默认值（无 defaulted）；normalizer→normalized；validator→invalid
- structured_call 可插拔：prompt / tools 两模式
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.base import LLMProvider, LLMResponse, Message, ToolCall
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tasking.contracts import (
    AtomicRequirement,
    CallStatus,
    FieldStatus,
    RawOperation,
    SlotSource,
)
from paper_agent.tasking.extractors.base import PreExtractor, structured_call
from paper_agent.tasking.intent_spec import (
    BindingKind,
    ExecutionBinding,
    IntentSpec,
    SlotSpec,
)


# --- 测试用最小抽取器：两个槽位（一个带 normalizer，一个带 validator）---

def _b(target: str) -> ExecutionBinding:
    return ExecutionBinding(kind=BindingKind.PLAN_FIELD, target=target)


_TEST_SPEC = IntentSpec(
    intent=Intent.POLISH,
    slots={
        "section_id": SlotSpec(
            "section_id", required=True, must_ask=True,
            execution_binding=_b("PolishPlan.section_id"),
        ),
        "target_ext": SlotSpec(
            "target_ext", required=True,
            normalizer=lambda v: {"word": ".docx", "pdf": ".pdf"}.get(str(v).lower(), v),
            execution_binding=_b("ConvertPlan.target_ext"),
        ),
        "limit": SlotSpec(
            "limit", required=False,
            validator=lambda v: isinstance(v, int) and v > 0,
            execution_binding=_b("X.limit"),
        ),
    },
)


class _TestExtractor(PreExtractor):
    @property
    def intent(self) -> Intent:
        return Intent.POLISH

    def _spec(self) -> IntentSpec:
        return _TEST_SPEC


class _ToolExtractor(_TestExtractor):
    """显式给一个最小 schema（默认由 IntentSpec 自动生成）。"""

    def _tool_schema(self):
        return {"function": {"name": "extract", "parameters": {}}}


class _FailingLLM(LLMProvider):
    def complete(self, messages, **opts):
        raise RuntimeError("network down")

    def stream(self, messages, *, cancel_token=None, **opts):
        yield from ()


def _mock(obj) -> MockLLMProvider:
    return MockLLMProvider(scripted=[json.dumps(obj, ensure_ascii=False)])


_OP = RawOperation(intent=Intent.POLISH, verbatim="把引言润色一下，转成 word")


# --- 调用态/字段态分离 ---

def test_user_absent_is_not_unavailable():
    # section_id 明确给出；target_ext 用户没说 → user_absent（不是 unavailable）
    ex = _TestExtractor(_mock({
        "slots": {"section_id": {"value": "sec:intro", "status": "present"}},
    }))
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.call_status is CallStatus.SUCCESS
    assert res.slots["section_id"].field_status is FieldStatus.PRESENT
    assert res.slots["target_ext"].field_status is FieldStatus.USER_ABSENT
    assert res.slots["limit"].field_status is FieldStatus.USER_ABSENT


def test_provider_failure_marks_all_slots_unavailable():
    ex = _TestExtractor(_FailingLLM())
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.call_status is CallStatus.PROVIDER_FAILED
    assert res.error_category == "RuntimeError"
    for sv in res.slots.values():
        assert sv.field_status is FieldStatus.UNAVAILABLE


def test_parse_failure_marks_call_status():
    ex = _TestExtractor(MockLLMProvider(scripted=["not json at all"]))
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.call_status is CallStatus.PARSE_FAILED
    assert all(s.field_status is FieldStatus.UNAVAILABLE for s in res.slots.values())


def test_schema_failure_when_slots_not_dict():
    ex = _TestExtractor(_mock({"slots": "oops"}))
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.call_status is CallStatus.SCHEMA_FAILED
    assert all(s.field_status is FieldStatus.UNAVAILABLE for s in res.slots.values())


# --- requirement 消费 ---

def test_consumed_requirement_ids_collected_from_slots():
    ex = _TestExtractor(_mock({
        "slots": {
            "section_id": {"value": "sec:intro", "status": "present",
                           "requirement_ids": ["r1"]},
            "target_ext": {"value": "word", "status": "present",
                           "requirement_ids": ["r2"]},
        },
    }))
    res = ex.pre_extract(_OP, canonical_requirements=(
        AtomicRequirement(requirement_id="r1", verbatim="润色引言"),
        AtomicRequirement(requirement_id="r2", verbatim="转成 word"),
    ))
    assert set(res.consumed_requirement_ids) == {"r1", "r2"}
    # analyzer 路径：不重复造 requirement
    assert res.newly_discovered_requirements == ()


# --- normalizer / validator ---

def test_normalizer_marks_source_normalized():
    ex = _TestExtractor(_mock({
        "slots": {"target_ext": {"value": "word", "status": "present"}},
    }))
    res = ex.pre_extract(_OP, canonical_requirements=())
    sv = res.slots["target_ext"]
    assert sv.value == ".docx"
    assert sv.field_status is FieldStatus.PRESENT
    assert sv.source is SlotSource.NORMALIZED


def test_validator_failure_marks_invalid():
    ex = _TestExtractor(_mock({
        "slots": {"limit": {"value": -3, "status": "present"}},
    }))
    res = ex.pre_extract(_OP, canonical_requirements=())
    sv = res.slots["limit"]
    assert sv.field_status is FieldStatus.INVALID


def test_extractor_never_produces_defaulted():
    # 用户没说的字段 → user_absent，绝不被抽取器填默认（默认值是 PlanBuilder 的事）
    ex = _TestExtractor(_mock({"slots": {}}))
    res = ex.pre_extract(_OP, canonical_requirements=())
    for sv in res.slots.values():
        assert sv.source is not SlotSource.DEFAULTED


# --- Fast_Path 产 requirement ---

def test_fast_path_produces_discovered_requirements_with_offsets():
    ex = _TestExtractor(_mock({
        "slots": {"section_id": {"value": "sec:intro", "status": "present"}},
        "newly_discovered_requirements": [
            {"verbatim": "转成 word", "category": "target"},
        ],
    }))
    res = ex.pre_extract(_OP, canonical_requirements=None)  # None → Fast_Path
    assert len(res.newly_discovered_requirements) == 1
    req = res.newly_discovered_requirements[0]
    assert req.verbatim == "转成 word"
    # offset 由程序在 operation.verbatim 里定位
    assert _OP.verbatim[req.source_start:req.source_end] == "转成 word"
    assert req.ambiguous_span is False


def test_analyzer_path_does_not_discover_requirements():
    ex = _TestExtractor(_mock({
        "slots": {"section_id": {"value": "sec:intro", "status": "present"}},
        "newly_discovered_requirements": [{"verbatim": "转成 word"}],
    }))
    # canonical 非 None → analyzer 路径，忽略 newly_discovered
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.newly_discovered_requirements == ()


# --- 整体失败兜底（Fast_Path）---

def test_fast_path_failure_fallback_unparsed_requirement():
    ex = _TestExtractor(_FailingLLM())
    res = ex.pre_extract(_OP, canonical_requirements=None)
    assert res.call_status is CallStatus.PROVIDER_FAILED
    assert len(res.newly_discovered_requirements) == 1
    fb = res.newly_discovered_requirements[0]
    assert fb.category == "unparsed"
    assert fb.verbatim == _OP.verbatim  # 整段原话不静默丢失


def test_analyzer_path_failure_has_no_fallback_requirement():
    # analyzer 路径失败：canonical 已存在，未消费者由 PlanBuilder 进 unmapped，
    # 抽取器不再造 unparsed 兜底（避免重复）。
    ex = _TestExtractor(_FailingLLM())
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.newly_discovered_requirements == ()


# --- structured_call 可插拔：tools 模式 ---

def test_structured_call_without_schema_parses_content_json():
    # 无 tool_schema → 退化为解析正文 JSON
    llm = _mock({"slots": {"section_id": {"value": "x", "status": "present"}}})
    out = structured_call(llm, [Message(role="user", content="hi")])
    assert out.call_status is CallStatus.SUCCESS
    assert out.payload["slots"]["section_id"]["value"] == "x"


def test_structured_call_tools_falls_back_to_content_json():
    # tools 模式但模型回了正文 JSON（未走工具）→ 兜底解析，不塌成失败
    llm = _mock({"slots": {"section_id": {"value": "y", "status": "present"}}})
    out = structured_call(
        llm, [Message(role="user", content="hi")],
        tool_schema={"function": {"name": "extract", "parameters": {}}},
    )
    assert out.call_status is CallStatus.SUCCESS
    assert out.payload["slots"]["section_id"]["value"] == "y"


def test_structured_call_tools_mode_reads_tool_arguments():
    call = ToolCall(id="c1", name="extract",
                    arguments={"slots": {"section_id": {"value": "sec:intro",
                                                        "status": "present"}}})
    llm = MockLLMProvider(scripted=[[call]])
    ex = _ToolExtractor(llm)
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.call_status is CallStatus.SUCCESS
    assert res.slots["section_id"].value == "sec:intro"


def test_structured_call_tools_mode_no_call_and_no_json_is_parse_failed():
    # 既没发 tool_call、正文也不是 JSON → parse_failed（尝试解析且失败）
    llm = MockLLMProvider(scripted=["just text, no tool call"])
    out = structured_call(
        llm, [Message(role="user", content="hi")],
        tool_schema={"function": {"name": "extract", "parameters": {}}},
    )
    assert out.call_status is CallStatus.PARSE_FAILED


def test_structured_call_tool_args_not_dict_is_schema_failed():
    from paper_agent.providers.llm.base import ToolCall

    llm = MockLLMProvider(scripted=[[ToolCall(id="c", name="extract", arguments="oops")]])
    out = structured_call(
        llm, [Message(role="user", content="hi")],
        tool_schema={"function": {"name": "extract", "parameters": {}}},
    )
    assert out.call_status is CallStatus.SCHEMA_FAILED


# --- requirement 消费关系由程序推断（FC 扁平 schema 不带 per-slot ids）---

def test_consumption_inferred_when_model_omits_requirement_ids():
    """真机回归：扁平 FC payload 没有 requirement_ids，消费关系须由程序补全。

    否则每条 requirement 都会被判「未消费」→ 全部落 unmapped（用户会被反复追问
    明明已经理解了的东西）。
    """
    ex = _TestExtractor(_mock({
        "section_id": "sec:intro",
        "target_ext": "docx",
    }))
    reqs = (
        AtomicRequirement(requirement_id="r1", verbatim="sec:intro"),
        AtomicRequirement(requirement_id="r2", verbatim="docx"),
    )
    res = ex.pre_extract(_OP, canonical_requirements=reqs)
    assert res.call_status is CallStatus.SUCCESS
    assert set(res.consumed_requirement_ids) == {"r1", "r2"}
    # 各自挂到对应槽位上
    assert "r1" in res.slots["section_id"].requirement_ids


def test_explicit_requirement_ids_take_precedence():
    ex = _TestExtractor(_mock({
        "slots": {
            "section_id": {"value": "sec:intro", "status": "present",
                           "requirement_ids": ["r9"]},
        },
    }))
    reqs = (AtomicRequirement(requirement_id="r9", verbatim="完全不匹配的原话"),)
    res = ex.pre_extract(_OP, canonical_requirements=reqs)
    assert res.slots["section_id"].requirement_ids == ("r9",)
    assert "r9" in res.consumed_requirement_ids


def test_unmatched_requirement_stays_unconsumed():
    ex = _TestExtractor(_mock({"section_id": "sec:intro"}))
    reqs = (
        AtomicRequirement(requirement_id="r1", verbatim="sec:intro"),
        AtomicRequirement(requirement_id="r2", verbatim="帮我订机票"),
    )
    res = ex.pre_extract(_OP, canonical_requirements=reqs)
    assert "r1" in res.consumed_requirement_ids
    assert "r2" not in res.consumed_requirement_ids   # 无处可去 → 由 PlanBuilder 落 unmapped


# --- P0：schema 的 required 不得等同业务 required ---

def test_tool_schema_has_no_required_fields():
    """业务必填 ≠ 协议必填。

    若把 required_slots 放进 JSON schema 的 required，模型就**无法合法省略**
    section_id，只能猜一个/给空串 —— 缺口永远不会暴露给 Clarifier，整套澄清机制
    被架空（曾导致 zero_ask 虚高）。
    """
    from paper_agent.tasking.extractors.base import build_tool_schema

    schema = build_tool_schema(_TEST_SPEC, "测试")
    assert schema["function"]["parameters"]["required"] == []
    # 业务必填仍在 spec 层保留（供 PlanBuilder/Clarifier 用）
    assert "section_id" in _TEST_SPEC.required_slots()


def test_omitted_required_slot_stays_user_absent_for_clarifier():
    # 模型省略 section_id（schema 允许）→ user_absent → 交给 Clarifier 追问
    ex = _TestExtractor(_mock({"target_ext": "docx"}))
    res = ex.pre_extract(_OP, canonical_requirements=())
    assert res.slots["section_id"].field_status is FieldStatus.USER_ABSENT
    assert res.slots["section_id"].value is None      # 没有被迫编造


def test_analyzer_schema_allows_empty_operations():
    """请求整体不属于五意图时 operations 应可为空，不该被 required 逼着硬凑。"""
    from paper_agent.tasking.operation_analyzer import _ANALYZE_TOOL

    assert _ANALYZE_TOOL["function"]["parameters"]["required"] == []
