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

from paper_agent.tasking.contracts import AnalyzeResult, DispositionStatus, Task_Plan
from paper_agent.tasking.plan_builder import operation_constraints, operation_quota


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
    # --- unmapped 账本核对（2026-08-01 新增）------------------------------
    # `GoldenCase.expected_unmapped` 此前是**死数据**：load_golden 读进来，
    # score_case 从不检查。于是「哪些要求应该落 unmapped」这条标注不进任何指标。
    # 真机代价：用户说「参考文献10，英文8，中文2」，配额槽把后两条认领了、
    # 漏报了第一条，系统于是追问「有一条要求我没能安排进任何操作：参考文献10」
    # —— 值其实已在配额里，纯记账漏项。silent_loss 抓不到它（那条确实进了
    # unmapped，账本是平的），只有跟人工标注对账才能发现。
    unmapped_expected: int = 0             # 标注应落 unmapped 的条数
    unmapped_missing: int = 0              # 标注应落 unmapped 却没落（危险：静默接受）
    unmapped_spurious: int = 0             # 标注不该落却落了（扰民：无谓追问）
    spurious_texts: tuple[str, ...] = ()   # 多余的 unmapped 原文，便于定位


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
        # 配额是嵌套 dict，下面的 str/list 分支取不到 —— 用与确认摘要同一个渲染器，
        # 否则「引用 10 篇」这类标注约束在评测里恒判漏。
        texts.extend(operation_quota(op.slots))
        for sv in op.slots.values():
            if sv.verbatim:
                texts.append(sv.verbatim)
            if isinstance(sv.value, str):
                texts.append(sv.value)
            elif isinstance(sv.value, (list, tuple)):
                texts.extend(str(x) for x in sv.value)
            elif isinstance(sv.value, dict):
                # clause 自带的用户原话（配额等结构化槽位）
                for clauses in sv.value.values():
                    if isinstance(clauses, (list, tuple)):
                        texts.extend(
                            str(c["verbatim"]) for c in clauses
                            if isinstance(c, dict) and c.get("verbatim")
                        )
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
    # 项目级事实（「课题是 X」）落 facts.claims 而非槽位：**是**已消费，不算静默丢失。
    project_memory_ids = {
        d.requirement_id for d in plan.dispositions
        if d.status is DispositionStatus.PROJECT_MEMORY
    }
    silent = 0
    for r in analyze.requirements:
        accounted = (
            r.requirement_id in consumed_ids
            or r.requirement_id in project_memory_ids
            or _fuzzy_in(r.verbatim, unmapped_texts)
        )
        if not accounted:
            silent += 1

    # unmapped 账本与人工标注对账（两个方向都要查）
    missing_unmapped = sum(
        1 for exp in case.expected_unmapped if not _fuzzy_in(exp, unmapped_texts)
    )
    spurious = tuple(
        actual_text for actual_text in unmapped_texts
        if not _fuzzy_in(actual_text, case.expected_unmapped)
    )

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
        unmapped_expected=len(case.expected_unmapped),
        unmapped_missing=missing_unmapped,
        unmapped_spurious=len(spurious),
        spurious_texts=spurious,
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
    # unmapped 账本核对
    unmapped_missing_total: int = 0     # 应落未落（危险：系统静默接受了做不到的事）
    unmapped_spurious_total: int = 0    # 不该落却落了（扰民：用户被问已满足的要求）
    spurious_cases: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    per_case: list[CaseScore] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "n_cases": self.n_cases,
            "operation_split_accuracy": round(self.operation_split_accuracy, 4),
            "single_intent_accuracy": round(self.single_intent_accuracy, 4),
            "constraint_recall": round(self.constraint_recall, 4),
            "requirement_miss_rate": round(self.requirement_miss_rate, 4),
            "silent_loss_total": self.silent_loss_total,
            "unmapped_missing_total": self.unmapped_missing_total,
            "unmapped_spurious_total": self.unmapped_spurious_total,
            "spurious_cases": [
                {"case_id": cid, "texts": list(texts)}
                for cid, texts in self.spurious_cases
            ],
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
        unmapped_missing_total=sum(s.unmapped_missing for s in scores),
        unmapped_spurious_total=sum(s.unmapped_spurious for s in scores),
        spurious_cases=[
            (s.case_id, s.spurious_texts) for s in scores if s.spurious_texts
        ],
        per_case=list(scores),
    )


__all__ = [
    "GoldenCase", "CaseScore", "Metrics",
    "load_golden", "score_case", "aggregate",
]

# 注：此处**曾有**一套 ClarificationScore / ClarificationMetrics /
# score_clarification / aggregate_clarification 的旧实现（字段为
# needed_clarification / repeat_ask_count / unnecessary），与文件末尾 Task 18
# 那套同名。Python 后定义者遮盖前者 ⇒ 旧的一套是**死代码**，runner 调的一直是
# 末尾那套（签名 `score_clarification(case_id, *, asks, rounds, pre_clarify_plan)`）。
# 2026-08-01 删除。教训：新版指标要**替换**旧定义，追加同名类只会留下一个
# 永不执行、却会误导读者的第二真相源。


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
