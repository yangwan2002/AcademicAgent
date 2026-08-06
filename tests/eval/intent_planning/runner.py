"""intent-planning golden 跑分 runner（Task 15）。

对每条 golden 样本跑「analyze → pre_extract → build_plan」，再用 scorer 打分、聚合，
打印指标并写基线 JSON。

**默认不进 pytest**（真实 LLM 非确定、要联网、要花钱）。两种用法：
- 真机基线：`python tests/eval/intent_planning/runner.py`（从 .env 装配真实 provider）。
- 传入自定义 provider：`run_all(cases, llm)`（脚本化 provider 亦可，用于管线自测）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]
_SRC = _ROOT / "src"
for p in (str(_SRC), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from paper_agent.providers.llm.base import LLMProvider
from paper_agent.tasking.contracts import AnalyzeResult, CallStatus, Task_Plan
from paper_agent.tasking.extractors import build_extractor
from paper_agent.tasking.operation_analyzer import (
    analyze,
    requirements_for,
)
from paper_agent.tasking.plan_builder import build_plan

import scorer

GOLDEN_PATH = _HERE / "golden.json"
BASELINE_PATH = _HERE / "baseline.json"


def run_case(case: scorer.GoldenCase, llm: LLMProvider) -> tuple[AnalyzeResult, Task_Plan]:
    """对一条样本跑理解链路，返回 (AnalyzeResult, Task_Plan)。"""
    ar = analyze(case.message, llm)
    extractions = []
    n_ops = len(ar.operations)
    for op in ar.operations:
        ex = build_extractor(op.intent, llm)
        if ex is None:
            continue
        # **必须与生产路径走同一路由**，否则 golden 指标不反映真实行为。
        # backlog F-3 的教训：runner 此前直接调 assess_availability 不传策略，
        # 于是恒走 strict，真机改动在评测里完全看不出来。
        owned, _attr = requirements_for(op, ar.requirements, n_ops)
        extractions.append(ex.pre_extract(op, owned))
    # build_extractor 恒非 None（analyze 已过滤不支持意图），extractions 与 operations 对齐
    plan = build_plan(
        case.id, ar.operations, extractions, ar.requirements,
        unmapped_text=ar.unmapped_text,
        # 与生产路径一致：项目级事实的 requirement 记 PROJECT_MEMORY，不算 unmapped。
        project_fact_requirement_ids=tuple(
            rid for _k, _v, rids in ar.project_facts for rid in rids
        ),
    )
    return ar, plan


def run_all(cases, llm: LLMProvider, *, verbose: bool = False):
    """跑分并聚合；返回 (Metrics, ClarificationMetrics)。"""
    scores = []
    clar_scores = []
    for idx, case in enumerate(cases, 1):
        ar, plan = run_case(case, llm)
        if ar.call_status is not CallStatus.SUCCESS:
            # Analyzer 失败：记为空计划打分（意图不匹配、requirement 全漏），不塌陷
            plan = build_plan(case.id, (), (), ())
        s = scorer.score_case(case, ar, plan)
        scores.append(s)
        # 澄清指标：直接用真机产出的计划缺口判定「本轮会问几个字段」，
        # 无需再跑一遍 clarify（确认不计入追问）。
        gaps = list(plan.missing_fields)
        cs = scorer.score_clarification(
            case.id, asks=gaps, rounds=(1 if gaps else 0), pre_clarify_plan=plan,
        )
        clar_scores.append(cs)
        if verbose:
            flag = "OK " if s.intent_seq_match else "XX "
            status = ar.call_status.value
            clar = f"澄清{len(gaps)}" if gaps else "直通"
            print(f"[{idx}/{len(cases)}] {flag}{s.case_id} "
                  f"({status}) actual={list(s.actual_intents)} "
                  f"miss={s.requirements_missed}/{s.requirements_total} "
                  f"constr={s.constraints_hit}/{s.constraints_total} "
                  f"silent={s.silent_loss} {clar}",
                  flush=True)
    return scorer.aggregate(scores), scorer.aggregate_clarification(clar_scores)


def main() -> int:
    from paper_agent.config import load_config
    from paper_agent.providers.factory import build_llm_provider
    from paper_agent.utils.dotenv import load_dotenv

    load_dotenv()  # 先加载 .env，使 load_config() 能读到真实网关配置
    cases = scorer.load_golden(GOLDEN_PATH)
    print(f"装配 provider… 共 {len(cases)} 条样本，开始跑分（打真实模型）", flush=True)
    llm = build_llm_provider(load_config())
    metrics, clar = run_all(cases, llm, verbose=True)

    combined = {"understanding": metrics.as_dict(), "clarification": clar.as_dict()}
    print("\n=== intent-planning golden 基线 ===", flush=True)
    print(json.dumps(combined, ensure_ascii=False, indent=2), flush=True)

    BASELINE_PATH.write_text(
        json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n基线已写入 {BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --- 澄清指标 runner（Task 18）--------------------------------------------

def run_clarification_case(
    case: scorer.GoldenCase,
    llm: LLMProvider,
    *,
    field_answers=None,
) -> scorer.ClarificationScore:
    """跑「理解 → 建计划 → clarify」，度量澄清追问（确认不计入）。

    先拿到**澄清前**的计划（判定哪些追问不必要的依据），再跑 clarify 记录追问次数
    与轮数。不进确认阶段——确认不属于澄清追问。
    """
    from paper_agent.tasking.clarifier import clarify

    _ar, pre_plan = run_case(case, llm)
    it = scorer.RecordingInteractor(field_answers=field_answers)
    out = clarify(pre_plan, ask=it.ask_field, max_rounds=2)
    return scorer.score_clarification(
        case.id, asks=it.field_asks, rounds=out.rounds_used,
        pre_clarify_plan=pre_plan, confirm_calls=it.confirm_calls,
    )


def run_all_clarification(cases, llm: LLMProvider, *, verbose: bool = False):
    scores = []
    for idx, case in enumerate(cases, 1):
        s = run_clarification_case(case, llm)
        scores.append(s)
        if verbose:
            print(f"[{idx}/{len(cases)}] {s.case_id}: asks={s.asks} rounds={s.rounds} "
                  f"unnecessary={s.unnecessary_asks} repeat={s.repeat_asks}", flush=True)
    return scorer.aggregate_clarification(scores)
