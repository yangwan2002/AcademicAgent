"""intent-planning Task 18 验收：澄清相关指标（确定性，不联网）。

- 指标可算：不必要追问率 / 澄清轮数 / user_explicit 重复询问率 / 零追问样本率
- **计划确认不计入澄清追问**（confirm_calls 与 field_asks 分开统计）
- 简单任务（信息写全）零澄清追问
"""

from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
_EVAL = os.path.join(_ROOT, "tests", "eval", "intent_planning")
for _p in (_SRC, _EVAL):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scorer
from runner import run_clarification_case

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tasking.clarifier import (
    ConfirmDecision,
    clarify,
    confirm_plan,
)
from paper_agent.tasking.contracts import (
    CallStatus,
    ExtractionResult,
    FieldStatus,
    RawOperation,
    SlotSource,
    SlotValue,
)
from paper_agent.tasking.plan_builder import build_plan

_GOLDEN = os.path.join(_EVAL, "golden.json")


def _present(value):
    return SlotValue(value=value, field_status=FieldStatus.PRESENT,
                     source=SlotSource.USER_EXPLICIT)


def _ext(slots):
    return ExtractionResult(call_status=CallStatus.SUCCESS, slots=slots)


def _analyze_json(intent, verbatim):
    return json.dumps({
        "operations": [{"intent": intent, "verbatim": verbatim, "requirement_ids": []}],
        "requirements": [], "unmapped_text": [],
    }, ensure_ascii=False)


def _extract_json(slots):
    return json.dumps(
        {"slots": {k: {"value": v, "status": "present"} for k, v in slots.items()}},
        ensure_ascii=False)


# --- 零追问（信息写全）---

def test_complete_case_has_zero_asks():
    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色引言")],
                      [_ext({"section_id": _present("introduction")})],
                      canonical_requirements=())
    it = scorer.RecordingInteractor()
    out = clarify(plan, ask=it.ask_field)
    s = scorer.score_clarification("c", asks=it.field_asks, rounds=out.rounds_used,
                                   pre_clarify_plan=plan)
    assert s.asks == 0
    assert s.rounds == 0
    assert s.unnecessary_asks == 0


# --- 缺口才问，且不必要追问为 0 ---

def test_gap_case_asks_but_not_unnecessary():
    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色一下")],
                      [_ext({})], canonical_requirements=())
    it = scorer.RecordingInteractor(field_answers=["introduction"])
    out = clarify(plan, ask=it.ask_field)
    s = scorer.score_clarification("c", asks=it.field_asks, rounds=out.rounds_used,
                                   pre_clarify_plan=plan)
    assert s.asks == 1                 # 问了缺失的章节
    assert s.unnecessary_asks == 0     # 但不是「已给还问」
    assert s.repeat_asks == 0          # 也没重复问 user_explicit 字段


def test_present_field_never_counted_as_missing():
    # section_id 已明确给出 → 不在 missing_fields → 不必要追问/重复询问恒 0
    plan = build_plan("t", [RawOperation(Intent.REWRITE, "改写引言，补充论证")],
                      [_ext({"section_id": _present("introduction"),
                             "direction": _present("补充论证")})],
                      canonical_requirements=())
    assert plan.missing_fields == ()
    it = scorer.RecordingInteractor()
    out = clarify(plan, ask=it.ask_field)
    s = scorer.score_clarification("c", asks=it.field_asks, rounds=out.rounds_used,
                                   pre_clarify_plan=plan)
    assert (s.asks, s.unnecessary_asks, s.repeat_asks) == (0, 0, 0)


# --- 确认不计入澄清追问 ---

def test_confirmation_not_counted_as_clarification_ask():
    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色引言")],
                      [_ext({"section_id": _present("introduction")})],
                      canonical_requirements=())
    it = scorer.RecordingInteractor(confirm=[ConfirmDecision("accept")])
    out = clarify(plan, ask=it.ask_field)
    confirm_plan(plan, it)                       # 走了一次确认
    s = scorer.score_clarification("c", asks=it.field_asks, rounds=out.rounds_used,
                                   pre_clarify_plan=plan, confirm_calls=it.confirm_calls)
    assert it.confirm_calls == 1
    assert s.confirm_calls == 1
    assert s.asks == 0                           # 确认不算澄清追问


# --- 聚合 ---

def test_aggregate_clarification_math():
    a = scorer.ClarificationScore("a", asks=0, rounds=0)
    b = scorer.ClarificationScore("b", asks=2, rounds=1, unnecessary_asks=1, repeat_asks=1)
    m = scorer.aggregate_clarification([a, b])
    assert m.n_cases == 2
    assert m.total_asks == 2
    assert m.avg_clarification_rounds == 0.5
    assert m.unnecessary_ask_rate == 0.5      # 1 不必要 / 2 追问
    assert m.repeat_ask_rate == 0.5
    assert m.zero_ask_case_rate == 0.5


def test_aggregate_empty_is_safe():
    m = scorer.aggregate_clarification([])
    assert m.n_cases == 0
    assert m.as_dict()["total_asks"] == 0


# --- runner 管线（脚本化 provider，不联网）---

def test_clarification_runner_zero_ask_for_complete_message():
    case = next(c for c in scorer.load_golden(_GOLDEN) if c.id == "polish_method")
    llm = MockLLMProvider(scripted=[
        _analyze_json("polish", "润色一下方法部分"),
        _extract_json({"section_id": "method"}),
    ])
    s = run_clarification_case(case, llm)
    assert s.asks == 0                # 信息写全 → 零澄清追问
    assert s.unnecessary_asks == 0


def test_clarification_runner_asks_when_slot_missing():
    case = next(c for c in scorer.load_golden(_GOLDEN) if c.id == "polish_method")
    llm = MockLLMProvider(scripted=[
        _analyze_json("polish", "润色一下"),
        _extract_json({}),            # 抽取器没给 section_id
    ])
    s = run_clarification_case(case, llm, field_answers=["method"])
    assert s.asks == 1
    assert s.rounds == 1
    assert s.unnecessary_asks == 0
