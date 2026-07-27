"""intent-planning 理解层 golden 打分器（Task 15）。

纯函数、确定性、零 LLM 依赖：给定一条 golden 标注 + 该样本的实际理解结果
（`AnalyzeResult` + `Task_Plan`），算出可测的理解层指标，并聚合。

度量（design Testing Strategy / D4 / Property 2）：
- **operation_split_accuracy**：实际操作意图序列 == 期望序列（严格顺序）。
- **single_intent_accuracy**：仅单操作样本上，意图判对率。
- **constraint_recall**：期望应捕获的约束里，实际落到某槽位/约束的比例。
- **requirement_miss_rate**：相对人工标注的原子要求，Analyzer 未识别的比例。
- **silent_loss**：运行时不变量——已识别的 requirement 必须「被某槽消费」或「进
  unmapped」，二者皆非即静默丢失；PlanBuilder 保证其恒为 0，此指标做守门校验。

打分只依赖数据结构，不联网，可进默认测试套件。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from paper_agent.tasking.contracts import AnalyzeResult, Task_Plan
from paper_agent.tasking.plan_builder import operation_constraints


# --- golden 数据模型 -------------------------------------------------------

@dataclass(frozen=True)
class GoldenCase:
    id: str
    message: str
    current_sections: tuple[str, ...]
    current_format: str
    expected_intents: tuple[str, ...]
    expected_constraints: tuple[str, ...]
    atomic_requirements: tuple[str, ...]
    expected_unmapped: tuple[str, ...] = ()
    expected_clarification: bool | None = None   # 标注：该样本是否本应需要澄清


def load_golden(path: str | Path) -> list[GoldenCase]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases: list[GoldenCase] = []
    for c in data["cases"]:
        exp = c["expected"]
        cases.append(GoldenCase(
            id=c["id"],
            message=c["message"],
            current_sections=tuple(c.get("current_sections", [])),
            current_format=c.get("current_format", ""),
            expected_intents=tuple(o["intent"] for o in exp.get("operations", [])),
            expected_constraints=tuple(exp.get("constraints", [])),
            atomic_requirements=tuple(exp.get("atomic_requirements", [])),
            expected_unmapped=tuple(exp.get("expected_unmapped", [])),
            expected_clarification=exp.get("expected_clarification"),
        ))
    return cases


# --- 单样本打分 ------------------------------------------------------------

@dataclass(frozen=True)
class CaseScore:
    case_id: str
    intent_seq_match: bool                 # 操作意图序列是否完全一致
    is_single: bool                        # 期望是否单操作
    constraints_total: int
    constraints_hit: int
    requirements_total: int
    requirements_missed: int
    silent_loss: int
    actual_intents: tuple[str, ...] = ()


def _norm(s: str) -> str:
    """归一：去空白、小写，便于模糊包含匹配（中英混排）。"""
    return re.sub(r"\s+", "", str(s)).lower()


def _fuzzy_in(needle: str, haystacks: Sequence[str]) -> bool:
    """needle 是否与某个 haystack 模糊匹配（双向子串包含）。"""
    n = _norm(needle)
    if not n:
        return False
    for h in haystacks:
        hn = _norm(h)
        if hn and (n in hn or hn in n):
            return True
    return False


def _plan_intents(plan: Task_Plan) -> tuple[str, ...]:
    return tuple(op.intent.value for op in plan.operations)


def _collect_constraint_texts(plan: Task_Plan) -> list[str]:
    """从计划里收集所有「可能承载约束」的文本：op.constraints + 各槽 verbatim/值。"""
    texts: list[str] = []
    for op in plan.operations:
        # slots 是唯一真相源；约束从 slots 实时派生（不再有 Operation.constraints 副本）
        texts.extend(operation_constraints(op.slots))
        for sv in op.slots.values():
            if sv.verbatim:
                texts.append(sv.verbatim)
            if isinstance(sv.value, str):
                texts.append(sv.value)
            elif isinstance(sv.value, (list, tuple)):
                texts.extend(str(x) for x in sv.value)
    return texts


def _identified_requirements(analyze: AnalyzeResult, plan: Task_Plan) -> list[str]:
    """已识别 requirement 的 verbatim 集合（analyzer + Fast_Path 发现）。"""
    verbatims = [r.verbatim for r in analyze.requirements]
    # Fast_Path：canonical 来自各 op 未在 analyze.requirements 时（此 runner 走 analyzer
    # 路径，通常为空），此处仅用 analyze.requirements 即可。
    return verbatims


def score_case(case: GoldenCase, analyze: AnalyzeResult, plan: Task_Plan) -> CaseScore:
    actual = _plan_intents(plan)
    intent_seq_match = actual == case.expected_intents
    is_single = len(case.expected_intents) == 1

    # 约束召回
    constraint_texts = _collect_constraint_texts(plan)
    hit = sum(1 for c in case.expected_constraints if _fuzzy_in(c, constraint_texts))

    # requirement 漏识别：标注的原子要求里，Analyzer 没识别出来的
    identified = [r.verbatim for r in analyze.requirements]
    missed = sum(1 for req in case.atomic_requirements if not _fuzzy_in(req, identified))

    # silent_loss：已识别 requirement 必须被消费或进 unmapped
    consumed_ids = {
        rid for op in plan.operations for sv in op.slots.values()
        for rid in sv.requirement_ids
    }
    unmapped_texts = list(plan.unmapped_requirements)
    silent = 0
    for r in analyze.requirements:
        accounted = (r.requirement_id in consumed_ids) or _fuzzy_in(r.verbatim, unmapped_texts)
        if not accounted:
            silent += 1

    return CaseScore(
        case_id=case.id,
        intent_seq_match=intent_seq_match,
        is_single=is_single,
        constraints_total=len(case.expected_constraints),
        constraints_hit=hit,
        requirements_total=len(case.atomic_requirements),
        requirements_missed=missed,
        silent_loss=silent,
        actual_intents=actual,
    )


# --- 聚合 ------------------------------------------------------------------

@dataclass
class Metrics:
    n_cases: int = 0
    operation_split_accuracy: float = 0.0
    single_intent_accuracy: float = 0.0
    constraint_recall: float = 0.0
    requirement_miss_rate: float = 0.0
    silent_loss_total: int = 0
    per_case: list[CaseScore] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "n_cases": self.n_cases,
            "operation_split_accuracy": round(self.operation_split_accuracy, 4),
            "single_intent_accuracy": round(self.single_intent_accuracy, 4),
            "constraint_recall": round(self.constraint_recall, 4),
            "requirement_miss_rate": round(self.requirement_miss_rate, 4),
            "silent_loss_total": self.silent_loss_total,
        }


def aggregate(scores: Sequence[CaseScore]) -> Metrics:
    n = len(scores)
    if n == 0:
        return Metrics()
    split_ok = sum(1 for s in scores if s.intent_seq_match)
    singles = [s for s in scores if s.is_single]
    single_ok = sum(1 for s in singles if s.intent_seq_match)
    c_total = sum(s.constraints_total for s in scores)
    c_hit = sum(s.constraints_hit for s in scores)
    r_total = sum(s.requirements_total for s in scores)
    r_missed = sum(s.requirements_missed for s in scores)
    return Metrics(
        n_cases=n,
        operation_split_accuracy=split_ok / n,
        single_intent_accuracy=(single_ok / len(singles)) if singles else 1.0,
        constraint_recall=(c_hit / c_total) if c_total else 1.0,
        requirement_miss_rate=(r_missed / r_total) if r_total else 0.0,
        silent_loss_total=sum(s.silent_loss for s in scores),
        per_case=list(scores),
    )


# --- 澄清相关指标（Task 18）------------------------------------------------

@dataclass(frozen=True)
class ClarificationScore:
    case_id: str
    needed_clarification: bool          # 计划是否存在 required/must_ask 缺口
    rounds: int                         # 澄清轮数（有 clarify_outcome 用其真实轮数）
    repeat_ask_count: int               # 追问了已 present 字段的次数（不变量应为 0）
    unnecessary: bool                   # 标注「本不该澄清」却需要澄清


def score_clarification(case: GoldenCase, plan: Task_Plan, clarify_outcome=None) -> ClarificationScore:
    """从（澄清前）计划与可选的 clarify 结果算澄清指标。

    - `needed_clarification`：plan.missing_fields 非空（这正是 clarify 会问的缺口；
      **计划确认不在此列**，确认不计入澄清追问）。
    - `repeat_ask_count`：missing_fields 里指向的槽位若其实已 present → 违反「只补缺口」
      不变量；正常恒为 0。
    - `unnecessary`：仅当标注 expected_clarification=False 却仍需澄清。
    """
    missing = plan.missing_fields
    needed = bool(missing)
    rounds = clarify_outcome.rounds_used if clarify_outcome is not None else (1 if needed else 0)

    repeat = 0
    ops = {op.operation_id: op for op in plan.operations}
    for field in missing:
        op_id, _, slot = field.partition(":")
        op = ops.get(op_id)
        if op is not None:
            sv = op.slots.get(slot)
            if sv is not None and getattr(sv.field_status, "value", None) == "present":
                repeat += 1

    unnecessary = (case.expected_clarification is False) and needed
    return ClarificationScore(
        case_id=case.id, needed_clarification=needed, rounds=rounds,
        repeat_ask_count=repeat, unnecessary=unnecessary,
    )


@dataclass
class ClarificationMetrics:
    n_cases: int = 0
    clarification_rate: float = 0.0            # 需要澄清的样本占比
    unnecessary_clarification_rate: float = 0.0  # 标注无需澄清却追问的占比
    avg_rounds: float = 0.0
    repeat_ask_total: int = 0                  # 重复询问 user_explicit 字段（应为 0）
    per_case: list[ClarificationScore] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "n_cases": self.n_cases,
            "clarification_rate": round(self.clarification_rate, 4),
            "unnecessary_clarification_rate": round(self.unnecessary_clarification_rate, 4),
            "avg_rounds": round(self.avg_rounds, 4),
            "repeat_ask_total": self.repeat_ask_total,
        }


def aggregate_clarification(scores: Sequence[ClarificationScore],
                            cases: Sequence[GoldenCase] | None = None) -> ClarificationMetrics:
    n = len(scores)
    if n == 0:
        return ClarificationMetrics()
    needed = sum(1 for s in scores if s.needed_clarification)
    # 不必要追问率：分母为「标注无需澄清」的样本
    flagged_none = 0
    if cases is not None:
        by_id = {c.id: c for c in cases}
        flagged_none = sum(1 for s in scores
                           if by_id.get(s.case_id) and by_id[s.case_id].expected_clarification is False)
    unnecessary = sum(1 for s in scores if s.unnecessary)
    return ClarificationMetrics(
        n_cases=n,
        clarification_rate=needed / n,
        unnecessary_clarification_rate=(unnecessary / flagged_none) if flagged_none else 0.0,
        avg_rounds=sum(s.rounds for s in scores) / n,
        repeat_ask_total=sum(s.repeat_ask_count for s in scores),
        per_case=list(scores),
    )


__all__ = [
    "GoldenCase", "CaseScore", "Metrics",
    "load_golden", "score_case", "aggregate",
    "ClarificationScore", "ClarificationMetrics",
    "score_clarification", "aggregate_clarification",
]


# --- 澄清相关指标（Task 18）------------------------------------------------
#
# 度量「只补缺口」这条产品承诺（design Property 4/5、Req 12.4）：
# - **unnecessary_ask_rate**：追问了「用户其实已明确给出」的字段的比例。
# - **avg_clarification_rounds**：平均澄清轮数。
# - **repeat_ask_rate**：user_explicit 字段被重复询问的比例（应为 0）。
# - **zero_ask_case_rate**：简单任务（信息写全）零澄清追问的样本占比。
#
# **计划确认不计入澄清追问**：确认（confirm_plan）是执行前的必要闸门，不是「没听懂
# 才问」。故 RecordingInteractor 分开计数 `field_asks` 与 `confirm_calls`，指标只用
# 前者。

from paper_agent.tasking.clarifier import (  # noqa: E402
    ConfirmDecision,
    UnmappedDisposition,
)
from paper_agent.tasking.contracts import FieldStatus, SlotSource  # noqa: E402


class RecordingInteractor:
    """记录型 interactor：统计字段追问 / 确认调用（二者分开）。"""

    def __init__(self, *, field_answers=None, unmapped=None, confirm=None):
        self._fields = list(field_answers or [])
        self._unmapped = list(unmapped or [])
        self._confirm = list(confirm or [])
        self.field_asks: list[str] = []      # 澄清追问（计入指标）
        self.confirm_calls: int = 0          # 计划确认（**不**计入澄清指标）
        self.unmapped_calls: int = 0

    def ask_field(self, prompt: str) -> str:
        self.field_asks.append(prompt)
        return self._fields.pop(0) if self._fields else ""

    def resolve_unmapped(self, item: str) -> UnmappedDisposition:
        self.unmapped_calls += 1
        return self._unmapped.pop(0) if self._unmapped else UnmappedDisposition("ignore")

    def confirm_plan(self, summary: str) -> ConfirmDecision:
        self.confirm_calls += 1
        return self._confirm.pop(0) if self._confirm else ConfirmDecision("accept")


@dataclass(frozen=True)
class ClarificationScore:
    case_id: str
    asks: int = 0                     # 字段追问次数（不含确认）
    rounds: int = 0                   # 澄清轮数
    unnecessary_asks: int = 0         # 追问了已明确给出的字段
    repeat_asks: int = 0              # user_explicit 字段被重复询问
    confirm_calls: int = 0            # 仅记录，不计入追问


def score_clarification(
    case_id: str,
    *,
    asks: Sequence[str],
    rounds: int,
    pre_clarify_plan,
    confirm_calls: int = 0,
) -> ClarificationScore:
    """据「澄清前的计划」判定哪些追问是不必要的。

    不必要 = 该槽位在澄清**之前**就已是 present（用户明确给出/已规范化），却仍被问。
    重复询问 = 被问的槽位在澄清前 source 为 user_explicit（用户原话里明说过）。
    """
    present_slots: set[tuple[str, str]] = set()
    explicit_slots: set[tuple[str, str]] = set()
    for op in pre_clarify_plan.operations:
        for name, sv in op.slots.items():
            if sv.field_status is FieldStatus.PRESENT:
                present_slots.add((op.operation_id, name))
                if sv.source is SlotSource.USER_EXPLICIT:
                    explicit_slots.add((op.operation_id, name))

    # 追问的槽位由 clarify 的 missing_fields 决定；此处按「澄清前缺口集合」反推：
    # 凡是 pre-plan 里 present 的槽位都不该被问，故不必要追问数 = 被问总数中落在
    # present 集合里的部分。clarify 只问 _recompute_missing 的结果，故理论应为 0。
    pre_missing = set()
    for fieldkey in pre_clarify_plan.missing_fields:
        op_id, _, slot = fieldkey.partition(":")
        pre_missing.add((op_id, slot))

    unnecessary = len(pre_missing & present_slots)
    repeat = len(pre_missing & explicit_slots)

    return ClarificationScore(
        case_id=case_id, asks=len(asks), rounds=rounds,
        unnecessary_asks=unnecessary, repeat_asks=repeat,
        confirm_calls=confirm_calls,
    )


@dataclass
class ClarificationMetrics:
    n_cases: int = 0
    total_asks: int = 0
    avg_clarification_rounds: float = 0.0
    unnecessary_ask_rate: float = 0.0
    repeat_ask_rate: float = 0.0
    zero_ask_case_rate: float = 0.0
    per_case: list[ClarificationScore] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "n_cases": self.n_cases,
            "total_asks": self.total_asks,
            "avg_clarification_rounds": round(self.avg_clarification_rounds, 4),
            "unnecessary_ask_rate": round(self.unnecessary_ask_rate, 4),
            "repeat_ask_rate": round(self.repeat_ask_rate, 4),
            "zero_ask_case_rate": round(self.zero_ask_case_rate, 4),
        }


def aggregate_clarification(scores: Sequence[ClarificationScore]) -> ClarificationMetrics:
    n = len(scores)
    if n == 0:
        return ClarificationMetrics()
    total_asks = sum(s.asks for s in scores)
    return ClarificationMetrics(
        n_cases=n,
        total_asks=total_asks,
        avg_clarification_rounds=sum(s.rounds for s in scores) / n,
        unnecessary_ask_rate=(sum(s.unnecessary_asks for s in scores) / total_asks)
        if total_asks else 0.0,
        repeat_ask_rate=(sum(s.repeat_asks for s in scores) / total_asks)
        if total_asks else 0.0,
        zero_ask_case_rate=sum(1 for s in scores if s.asks == 0) / n,
        per_case=list(scores),
    )


__all__ += [
    "RecordingInteractor", "ClarificationScore", "ClarificationMetrics",
    "score_clarification", "aggregate_clarification",
]
