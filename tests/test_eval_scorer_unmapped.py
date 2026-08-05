"""golden 打分器的 unmapped 账本核对（确定性，零 LLM，可进默认套件）。

背景（2026-08-01）：`GoldenCase.expected_unmapped` 此前是**死数据** ——
`load_golden` 读进来，`score_case` 从不检查。于是「哪些要求应该落 unmapped」
这条人工标注不进任何指标。

真机代价：用户说「参考文献10，英文8，中文2」，配额槽认领了后两条、漏报第一条，
系统追问「有一条要求我没能安排进任何操作：参考文献10」—— 值其实已在配额里。
`silent_loss` 抓不到它（那条确实进了 unmapped，账本是平的），
只有**跟人工标注对账**才能发现。

两个方向都要查，且严重性不同：
- `unmapped_missing`：标注应落却没落 → 系统**静默接受**了它做不到的事（危险）
- `unmapped_spurious`：标注不该落却落了 → 用户被追问已满足的要求（扰民）
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT / "src"), str(_ROOT / "tests" / "eval" / "intent_planning")):
    if p not in sys.path:
        sys.path.insert(0, p)

import scorer  # noqa: E402

from paper_agent.intents.router import Intent                        # noqa: E402
from paper_agent.tasking.contracts import (                          # noqa: E402
    AnalyzeResult, AtomicRequirement, CallStatus, ExtractionResult,
    FieldStatus, RawOperation, SlotSource, SlotValue,
)
from paper_agent.tasking.plan_builder import build_plan              # noqa: E402

GOLDEN = _ROOT / "tests" / "eval" / "intent_planning" / "golden.json"


def _req(rid: str, text: str) -> AtomicRequirement:
    return AtomicRequirement(requirement_id=rid, verbatim=text)


def _slot(value, req_ids=()):
    return SlotValue(value=value, field_status=FieldStatus.PRESENT,
                     source=SlotSource.USER_EXPLICIT,
                     requirement_ids=tuple(req_ids))


def _case(**kw) -> scorer.GoldenCase:
    base = dict(
        id="c", message="m", current_sections=(), current_format="md",
        expected_intents=("rewrite",), expected_constraints=(),
        atomic_requirements=(), expected_unmapped=(),
    )
    base.update(kw)
    return scorer.GoldenCase(**base)


def _run(reqs, slots, *, expected_unmapped=()):
    """跑 build_plan 并打分。`slots` 为 rewrite 的槽位字典。"""
    ar = AnalyzeResult(
        call_status=CallStatus.SUCCESS,
        operations=(RawOperation(Intent.REWRITE, "改写引言"),),
        requirements=tuple(reqs),
    )
    ext = ExtractionResult(call_status=CallStatus.SUCCESS, slots=slots)
    plan = build_plan("t", ar.operations, [ext], ar.requirements)
    case = _case(
        atomic_requirements=tuple(r.verbatim for r in reqs),
        expected_unmapped=tuple(expected_unmapped),
    )
    return scorer.score_case(case, ar, plan), plan


# ============================================ 真机 bug 的最小复现


def test_regression_quota_underclaim_is_flagged_as_spurious():
    """配额槽认领 2 条、漏报 1 条 → 必须记为 spurious（标注说不该有 unmapped）。"""
    reqs = [_req("r1", "参考文献10"), _req("r2", "英文8"), _req("r3", "中文2")]
    slots = {
        "section_id": _slot("引言"),
        "direction": _slot("完善"),
        # LLM 只报了 r2/r3，漏了 r1 —— 这就是真机发生的事
        "citation_quota": _slot({"total": 10, "en": 8, "zh": 2}, ["r2", "r3"]),
    }
    score, plan = _run(reqs, slots, expected_unmapped=())

    assert "参考文献10" in plan.unmapped_requirements    # 现象
    assert score.silent_loss == 0                        # silent_loss 抓不到
    assert score.unmapped_spurious == 1                  # 新指标抓到了
    assert "参考文献10" in score.spurious_texts


def test_full_claim_produces_no_spurious():
    """三条都认领 → 零 unmapped、零 spurious。这是期望状态。"""
    reqs = [_req("r1", "参考文献10"), _req("r2", "英文8"), _req("r3", "中文2")]
    slots = {
        "section_id": _slot("引言"),
        "direction": _slot("完善"),
        "citation_quota": _slot({"total": 10, "en": 8, "zh": 2}, ["r1", "r2", "r3"]),
    }
    score, plan = _run(reqs, slots, expected_unmapped=())
    assert plan.unmapped_requirements == ()
    assert score.unmapped_spurious == 0
    assert score.unmapped_missing == 0


# ============================================ 另一个方向：应落未落


def test_expected_unmapped_not_appearing_is_missing():
    """标注「订机票」应落 unmapped，实际却被某槽认领 → 静默接受，必须报。"""
    reqs = [_req("r1", "改写引言"), _req("r2", "订一张去北京的机票")]
    slots = {
        "section_id": _slot("引言", ["r1"]),
        "direction": _slot("更连贯", ["r2"]),   # 幻觉认领：把订机票当成改写方向
    }
    score, _plan = _run(reqs, slots, expected_unmapped=("订一张去北京的机票",))
    assert score.unmapped_missing == 1
    assert score.unmapped_expected == 1


def test_expected_unmapped_appearing_is_clean():
    reqs = [_req("r1", "改写引言"), _req("r2", "订一张去北京的机票")]
    slots = {"section_id": _slot("引言", ["r1"]), "direction": _slot("更连贯", ["r1"])}
    score, plan = _run(reqs, slots, expected_unmapped=("订一张去北京的机票",))
    assert "订一张去北京的机票" in plan.unmapped_requirements
    assert score.unmapped_missing == 0
    assert score.unmapped_spurious == 0


# ============================================ 聚合与序列化


def test_aggregate_sums_and_lists_offending_cases():
    s1 = scorer.CaseScore("a", True, True, 0, 0, 0, 0, 0,
                          unmapped_spurious=2, spurious_texts=("x", "y"))
    s2 = scorer.CaseScore("b", True, True, 0, 0, 0, 0, 0, unmapped_missing=1)
    m = scorer.aggregate([s1, s2])
    assert m.unmapped_spurious_total == 2
    assert m.unmapped_missing_total == 1
    assert m.spurious_cases == [("a", ("x", "y"))]
    d = m.as_dict()
    assert d["unmapped_spurious_total"] == 2
    assert d["spurious_cases"] == [{"case_id": "a", "texts": ["x", "y"]}]
    json.dumps(d, ensure_ascii=False)      # 基线要能序列化


# ============================================ golden 集本身的守护


def test_golden_loads_and_has_real_cases():
    cases = scorer.load_golden(GOLDEN)
    ids = {c.id for c in cases}
    # 2026-08-01 补入的真实形态用例，不得被后续编辑悄悄删掉
    for required in (
        "real_quota_rambling_intro",
        "real_quota_explicit_sentence",
        "real_numbered_section_with_suffix_word",
        "real_improve_existing_section_is_rewrite",
    ):
        assert required in ids, f"golden 缺少真实形态用例：{required}"


def test_golden_covers_citation_quota():
    """A2d 的核心槽位必须有 golden 覆盖 —— 此前为零。"""
    cases = scorer.load_golden(GOLDEN)
    with_quota = [
        c for c in cases
        if any("参考文献" in r or "篇" in r for r in c.atomic_requirements)
    ]
    assert len(with_quota) >= 3


def test_golden_covers_numbered_section_titles():
    """真实文档标题带编号（「1 引言」）。此前 golden 全是干净章节名。"""
    cases = scorer.load_golden(GOLDEN)
    numbered = [
        c for c in cases
        if any(s[:1].isdigit() for s in c.current_sections)
    ]
    assert len(numbered) >= 4


def test_golden_covers_rambling_phrasing():
    """真实用户会写口语铺垫长句，而不是「润色引言」这种命令式短句。"""
    cases = scorer.load_golden(GOLDEN)
    long_cases = [c for c in cases if len(c.message) >= 40]
    assert len(long_cases) >= 2


# ============================================ 死代码不得复活


def test_no_duplicate_clarification_definitions():
    """scorer.py 曾有两套同名澄清指标（后者遮盖前者，前者是死代码）。

    守住不再复现：同名类只能定义一次。
    """
    src = (_ROOT / "tests" / "eval" / "intent_planning" / "scorer.py").read_text(
        encoding="utf-8"
    )
    assert src.count("class ClarificationScore") == 1
    assert src.count("class ClarificationMetrics") == 1
    assert src.count("def score_clarification") == 1
    assert src.count("def aggregate_clarification") == 1


def test_runner_signature_still_matches_scorer():
    """runner 调的是关键字签名那一套；删死代码后不得错位。"""
    import inspect

    sig = inspect.signature(scorer.score_clarification)
    assert set(sig.parameters) >= {"case_id", "asks", "rounds", "pre_clarify_plan"}
