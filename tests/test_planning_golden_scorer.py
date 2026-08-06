"""intent-planning Task 15 验收：golden 打分器（确定性，不联网）。

只测**打分器与 runner 管线**的正确性——用手工构造的理解结果 / 脚本化 provider，
保证进默认套件、确定性、零真实 LLM 依赖。真机基线由 runner.py 单独跑。
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
from runner import run_case

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tasking.contracts import (
    AnalyzeResult,
    AtomicRequirement,
    CallStatus,
    ExtractionResult,
    FieldStatus,
    RawOperation,
    SlotSource,
    SlotValue,
)
from paper_agent.tasking.plan_builder import build_plan

_GOLDEN = os.path.join(_EVAL, "golden.json")


def _present(value, req_ids=()):
    return SlotValue(value=value, field_status=FieldStatus.PRESENT,
                     source=SlotSource.USER_EXPLICIT, requirement_ids=tuple(req_ids))


# --- golden 加载 ---

def test_golden_loads_with_fields():
    cases = scorer.load_golden(_GOLDEN)
    assert len(cases) >= 20
    for c in cases:
        assert c.id and c.message
        assert isinstance(c.expected_intents, tuple)
        assert isinstance(c.atomic_requirements, tuple)


def test_golden_ids_unique():
    cases = scorer.load_golden(_GOLDEN)
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))


# --- 打分器：完美样本 ---

def test_score_perfect_polish_case():
    case = next(c for c in scorer.load_golden(_GOLDEN) if c.id == "polish_single_numbers")
    reqs = (
        AtomicRequirement(requirement_id="r1", verbatim="把引言保守润色一下"),
        AtomicRequirement(requirement_id="r2", verbatim="别改里面的实验数字"),
    )
    ar = AnalyzeResult(
        call_status=CallStatus.SUCCESS,
        operations=(RawOperation(Intent.POLISH, "把引言保守润色一下"),),
        requirements=reqs,
    )
    plan = build_plan("t", [RawOperation(Intent.POLISH, "把引言保守润色一下")], [
        ExtractionResult(call_status=CallStatus.SUCCESS, slots={
            "section_id": _present("introduction", ["r1"]),
            "constraints": _present(["别改里面的实验数字"], ["r2"]),
        }),
    ], canonical_requirements=reqs)

    s = scorer.score_case(case, ar, plan)
    assert s.intent_seq_match is True
    assert s.constraints_hit == 1 and s.constraints_total == 1
    assert s.requirements_missed == 0 and s.requirements_total == 2
    assert s.silent_loss == 0


# --- 打分器：意图错 + requirement 漏 ---

def test_score_wrong_intent_and_missed_requirement():
    case = next(c for c in scorer.load_golden(_GOLDEN) if c.id == "polish_single_numbers")
    # Analyzer 只识别了一条 requirement，且把意图判成 convert
    reqs = (AtomicRequirement(requirement_id="r1", verbatim="把引言保守润色一下"),)
    ar = AnalyzeResult(
        call_status=CallStatus.SUCCESS,
        operations=(RawOperation(Intent.CONVERT_FORMAT, "把引言保守润色一下"),),
        requirements=reqs,
    )
    plan = build_plan("t", [RawOperation(Intent.CONVERT_FORMAT, "转换")], [
        ExtractionResult(call_status=CallStatus.SUCCESS, slots={
            "target_ext": _present("docx", ["r1"]),
        }),
    ], canonical_requirements=reqs)

    s = scorer.score_case(case, ar, plan)
    assert s.intent_seq_match is False           # convert != polish
    assert s.requirements_missed == 1            # "别改里面的实验数字" 漏识别
    assert s.silent_loss == 0                    # 已识别的 r1 有去向


# --- 聚合 ---

def test_aggregate_math():
    from paper_agent.tasking.contracts import AnalyzeResult as AR
    _cases = scorer.load_golden(_GOLDEN)
    # 造两个 CaseScore 手动聚合
    good = scorer.CaseScore("a", True, True, 1, 1, 2, 0, 0)
    bad = scorer.CaseScore("b", False, True, 0, 0, 2, 1, 0)
    m = scorer.aggregate([good, bad])
    assert m.n_cases == 2
    assert m.operation_split_accuracy == 0.5
    assert m.single_intent_accuracy == 0.5
    assert m.constraint_recall == 1.0            # 唯一有约束的样本命中
    assert m.requirement_miss_rate == 0.25       # 1 漏 / 4 标注
    assert m.silent_loss_total == 0
    assert AR is not None


# --- runner 管线（脚本化 provider，不联网）---

def test_runner_pipeline_with_scripted_provider():
    case = next(c for c in scorer.load_golden(_GOLDEN) if c.id == "polish_method")
    analyze_json = json.dumps({
        "operations": [{"intent": "polish", "verbatim": "润色一下方法部分",
                        "requirement_ids": ["r1", "r2"]}],
        "requirements": [
            {"requirement_id": "r1", "verbatim": "润色一下方法部分", "category": "target"},
            {"requirement_id": "r2", "verbatim": "让表述更清晰", "category": "goal"},
        ],
        "unmapped_text": [],
    }, ensure_ascii=False)
    extract_json = json.dumps({
        "slots": {"section_id": {"value": "method", "status": "present",
                                 "requirement_ids": ["r1"]},
                  "goal": {"value": "让表述更清晰", "status": "present",
                           "requirement_ids": ["r2"]}},
    }, ensure_ascii=False)
    llm = MockLLMProvider(scripted=[analyze_json, extract_json])

    ar, plan = run_case(case, llm)
    assert ar.call_status is CallStatus.SUCCESS
    assert [op.intent for op in plan.operations] == [Intent.POLISH]
    s = scorer.score_case(case, ar, plan)
    assert s.intent_seq_match is True
    assert s.silent_loss == 0
