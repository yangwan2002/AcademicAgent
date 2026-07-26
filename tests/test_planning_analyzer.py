"""intent-planning Task 6 验收：Operation_Analyzer。

- 多意图有序拆分
- AtomicRequirement 齐全 + offset 由程序定位
- 非法 intent 入 unmapped
- 四态失败可区分不塌陷（provider/parse/schema/success）
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.base import LLMProvider, LLMResponse, Message
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tasking.contracts import CallStatus
from paper_agent.tasking.operation_analyzer import analyze


def _mock(json_obj) -> MockLLMProvider:
    return MockLLMProvider(scripted=[json.dumps(json_obj, ensure_ascii=False)])


class _FailingLLM(LLMProvider):
    def complete(self, messages, **opts):
        raise RuntimeError("network down")

    def stream(self, messages, *, cancel_token=None, **opts):
        yield from ()


TEXT = "先润色引言，别改数字，再转成 docx"


# --- 多意图有序拆分 + requirement + offset ---

def test_multi_intent_ordered_split():
    out = analyze(TEXT, _mock({
        "operations": [
            {"intent": "polish", "verbatim": "润色引言", "requirement_ids": ["r1", "r2"]},
            {"intent": "convert_format", "verbatim": "转成 docx", "requirement_ids": []},
        ],
        "requirements": [
            {"requirement_id": "r1", "verbatim": "润色引言", "category": "target"},
            {"requirement_id": "r2", "verbatim": "别改数字", "category": "constraint"},
        ],
        "unmapped_text": [],
    }))
    assert out.call_status is CallStatus.SUCCESS
    assert [op.intent for op in out.operations] == [Intent.POLISH, Intent.CONVERT_FORMAT]
    assert out.operations[0].requirement_ids == ("r1", "r2")


def test_requirement_offsets_located_by_program():
    out = analyze(TEXT, _mock({
        "operations": [{"intent": "polish", "verbatim": "润色引言"}],
        "requirements": [
            {"requirement_id": "r2", "verbatim": "别改数字", "category": "constraint"},
        ],
        "unmapped_text": [],
    }))
    r = out.requirements[0]
    assert r.ambiguous_span is False
    assert TEXT[r.source_start:r.source_end] == "别改数字"


def test_requirement_not_in_text_marked_ambiguous():
    out = analyze(TEXT, _mock({
        "operations": [{"intent": "polish", "verbatim": "润色引言"}],
        "requirements": [
            {"requirement_id": "r9", "verbatim": "模型幻觉出的原话", "category": "other"},
        ],
        "unmapped_text": [],
    }))
    assert out.requirements[0].ambiguous_span is True


# --- 非法 intent 入 unmapped ---

def test_illegal_intent_goes_unmapped():
    out = analyze("做点什么", _mock({
        "operations": [
            {"intent": "translate", "verbatim": "翻译成英文"},   # 不支持
            {"intent": "polish", "verbatim": "润色"},
        ],
        "requirements": [],
        "unmapped_text": [],
    }))
    assert out.call_status is CallStatus.SUCCESS
    assert [op.intent for op in out.operations] == [Intent.POLISH]
    assert "翻译成英文" in out.unmapped_text


# --- 四态失败 ---

def test_provider_failed():
    out = analyze(TEXT, _FailingLLM())
    assert out.call_status is CallStatus.PROVIDER_FAILED
    assert out.operations == ()
    assert out.error_category == "RuntimeError"


def test_parse_failed_on_non_json():
    out = analyze(TEXT, MockLLMProvider(scripted=["这不是 JSON，只是普通文本"]))
    assert out.call_status is CallStatus.PARSE_FAILED


def test_schema_failed_on_bad_structure():
    # operations 不是 list → schema_failed
    out = analyze(TEXT, _mock({"operations": "oops"}))
    assert out.call_status is CallStatus.SCHEMA_FAILED


def test_schema_failed_on_missing_intent_key():
    out = analyze(TEXT, _mock({"operations": [{"verbatim": "缺 intent 键"}]}))
    assert out.call_status is CallStatus.SCHEMA_FAILED


def test_json_in_code_fence_parsed():
    fenced = "```json\n" + json.dumps({
        "operations": [{"intent": "polish", "verbatim": "润色"}],
        "requirements": [], "unmapped_text": [],
    }, ensure_ascii=False) + "\n```"
    out = analyze("润色", MockLLMProvider(scripted=[fenced]))
    assert out.call_status is CallStatus.SUCCESS
    assert out.operations[0].intent is Intent.POLISH
