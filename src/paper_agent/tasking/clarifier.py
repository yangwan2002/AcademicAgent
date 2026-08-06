"""Clarifier + resolve_unmapped + Confirmation（Task 17）。

三段用户交互，全部通过注入的 `PlanningInteractor` 与 I/O 解耦（便于测试与复用）：

- **Clarifier**：只补缺口——(required 或 must_ask) 且 field_status ∈
  {user_absent, ambiguous, invalid, unavailable} 的槽位；一次问清所有缺口、≤2 轮；
  回填标 `user_confirmed`（经 normalizer/validator）；`unavailable` 用「自动理解暂时
  失败」的措辞。**已明确给出的字段绝不追问**。
- **resolve_unmapped**：对每条未映射要求强制处置——补充说明 / 忽略 / 取消；全部处置
  后计划才可 ready。
- **Confirmation**：呈现执行序摘要，返回 确认 / 否决 / 修改。`require_confirmation`
  默认 True。

Clarifier/confirm 不做静态章节校验（那是 validator 的事，由规划循环在回填后重跑），
只负责「把缺的字段补上、把 unmapped 处置掉、拿到确认决定」。计划 frozen，回填走
`dataclasses.replace` 产新计划。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Protocol

from paper_agent.intents.router import Intent
from paper_agent.tasking.contracts import (
    DispositionStatus,
    FieldStatus,
    SlotSource,
    SlotValue,
    Task_Plan,
)
from paper_agent.tasking.intent_spec import SlotSpec, spec_for
from paper_agent.tasking.plan_builder import (
    operation_constraints,
    operation_goal,
    operation_quota,
    operation_target,
)

try:  # 观测事件（可选，None-safe）
    from paper_agent.observability.events import EventKind
except Exception:  # noqa: BLE001
    EventKind = None  # type: ignore


# --- 交互协议与决定对象 -----------------------------------------------------

@dataclass
class UnmappedDisposition:
    action: str          # "supplement" | "ignore" | "cancel"
    text: str = ""


@dataclass
class ConfirmDecision:
    kind: str            # "accept" | "reject" | "modify"
    text: str = ""


class PlanningInteractor(Protocol):
    """规划期的三类用户交互（控制台/测试各自实现）。"""

    def ask_field(self, prompt: str) -> str:
        """澄清追问一个缺失字段，返回用户答案文本（空串=未提供）。"""
        ...

    def resolve_unmapped(self, item: str) -> UnmappedDisposition:
        """对一条未映射要求给出处置。"""
        ...

    def confirm_plan(self, summary: str) -> ConfirmDecision:
        """看执行序摘要后确认/否决/修改。"""
        ...


# --- Clarifier -------------------------------------------------------------

_GAP_STATUSES = (
    FieldStatus.USER_ABSENT, FieldStatus.AMBIGUOUS,
    FieldStatus.INVALID, FieldStatus.UNAVAILABLE,
)


@dataclass
class ClarifyOutcome:
    plan: Task_Plan
    resolved: bool
    rounds_used: int = 0


def _is_gap(slot_spec: SlotSpec, sv: SlotValue | None) -> bool:
    """该槽位是否构成需追问的缺口。

    - required/must_ask 且非 present → 缺口（既有语义）。
    - 可选槽标记 `clarify_if_user_invalid` 且**用户显式给出**但 INVALID/AMBIGUOUS →
      也追问（A2d 条件式澄清）；USER_ABSENT/UNAVAILABLE 的可选槽仍不追问。
    """
    if slot_spec.required or slot_spec.must_ask:
        if sv is None:
            return True
        return sv.field_status in _GAP_STATUSES and sv.field_status is not FieldStatus.PRESENT
    # 可选槽：仅在条件式澄清标记下，且用户显式给了无法用的值时追问
    if getattr(slot_spec, "clarify_if_user_invalid", False) and sv is not None:
        return sv.field_status in (FieldStatus.INVALID, FieldStatus.AMBIGUOUS)
    return False


def _recompute_missing(plan: Task_Plan) -> list[str]:
    missing: list[str] = []
    for op in plan.operations:
        spec = spec_for(op.intent)
        if spec is None:
            continue
        for name, slot_spec in spec.slots.items():
            if _is_gap(slot_spec, op.slots.get(name)):
                missing.append(f"{op.operation_id}:{name}")
    return missing


@dataclass(frozen=True)
class _Question:
    """一个可澄清槽位的人性化问法（不泄露内部槽名）。"""

    label: str                                   # 人类可读标签
    ask: str                                     # 自然问句（含选项）
    choices: dict[str, str] | None = None        # 枚举：用户输入 → 规范值


# 五意图所有 required/must_ask 槽位的问法目录（复用 interview 语气）
_STRUCTURE_CHOICES = {
    "a": "desc", "描述": "desc", "描述式": "desc", "desc": "desc",
    "b": "list", "列表": "list", "列表式": "list", "list": "list",
    "c": "mimic", "模仿": "mimic", "模仿式": "mimic", "mimic": "mimic",
}
_QUESTIONS: dict[tuple[Intent, str], _Question] = {
    (Intent.POLISH, "section_id"): _Question("章节", "要润色哪一章？（章节名或序号）"),
    (Intent.REWRITE, "section_id"): _Question("章节", "要改写哪一章？（章节名）"),
    (Intent.REWRITE, "direction"): _Question(
        "改动方向", "希望往哪个方向改？（如 补充论证 / 调整结构 / 更换叙述角度）"),
    (Intent.ADD_SECTION, "title"): _Question("章节标题", "新章节的标题是？"),
    (Intent.ADD_SECTION, "purpose"): _Question("章节目的", "这一章想达到什么目的？（一句话）"),
    (Intent.ADD_SECTION, "structure_kind"): _Question(
        "章节内部结构", "这一章内部怎么组织？(a) 描述式  (b) 列表式  (c) 模仿式",
        choices=_STRUCTURE_CHOICES),
    (Intent.CONVERT_FORMAT, "target_ext"): _Question(
        "目标格式", "要转换成什么格式？（docx / latex / markdown）"),
    (Intent.FORMAT_DOCX, "operations"): _Question(
        "排版要求", "需要哪些排版？（如 正文宋体小四、标题居中、双栏、三线表）"),
}


def _parse_format_ops(text: str):
    """惰性导入 parse_format_ops，避免 planning 导入链吃上 python-docx。"""
    from paper_agent.intents.format_parser import parse_format_ops

    return parse_format_ops(text)


def _fill_slot(intent: Intent, slot_name: str, slot_spec: SlotSpec, answer: str) -> SlotValue:
    """把澄清答案回填为结构化 user_confirmed 槽位。

    - 枚举槽（如 structure_kind）：按选项映射自然语言 → 规范值。
    - format_docx.operations：走确定性 `parse_format_ops` → list[FormatOp]。
    - 其余字符串槽：normalizer/validator。
    空答案 → user_absent（下一轮再问）；无法结构化 → invalid（再问，措辞提示无效）。
    """
    raw = answer.strip()
    if not raw:
        return SlotValue(field_status=FieldStatus.USER_ABSENT)

    q = _QUESTIONS.get((intent, slot_name))

    # 1) 枚举槽：选项映射
    if q is not None and q.choices is not None:
        mapped = q.choices.get(raw.lower()) or q.choices.get(raw)
        if mapped is None and slot_spec.validator is not None and _safe_validate(slot_spec, raw):
            mapped = raw  # 用户直接给了规范值
        if mapped is None:
            return SlotValue(value=raw, field_status=FieldStatus.INVALID)
        return SlotValue(value=mapped, field_status=FieldStatus.PRESENT,
                         source=SlotSource.USER_CONFIRMED)

    # 2) 排版操作：确定性解析成受限 list[FormatOp]
    if intent is Intent.FORMAT_DOCX and slot_name == "operations":
        try:
            ops = _parse_format_ops(raw)
        except Exception:  # noqa: BLE001
            ops = []
        if not ops:
            return SlotValue(value=raw, field_status=FieldStatus.INVALID)
        return SlotValue(value=ops, field_status=FieldStatus.PRESENT,
                         source=SlotSource.USER_CONFIRMED)

    # 3) 普通字符串槽：normalizer → validator
    val: object = raw
    if slot_spec.normalizer is not None:
        try:
            val = slot_spec.normalizer(val)
        except Exception:  # noqa: BLE001
            return SlotValue(value=raw, field_status=FieldStatus.INVALID)
    status = FieldStatus.PRESENT
    if slot_spec.validator is not None and not _safe_validate(slot_spec, val):
        status = FieldStatus.INVALID
    return SlotValue(
        value=val, field_status=status,
        source=SlotSource.USER_CONFIRMED if status is FieldStatus.PRESENT else None,
    )


def _safe_validate(slot_spec: SlotSpec, val) -> bool:
    try:
        return bool(slot_spec.validator(val))
    except Exception:  # noqa: BLE001
        return False


def _clarify_prompt(intent: Intent, slot_name: str, sv: SlotValue | None) -> str:
    """人性化追问（不泄露内部槽名/repr）。"""
    q = _QUESTIONS.get((intent, slot_name))
    label = q.label if q is not None else slot_name
    base = q.ask if q is not None else f"请提供{label}"
    if sv is not None and sv.field_status is FieldStatus.UNAVAILABLE:
        return f"自动理解暂时失败，我直接和你确认——{base}"
    if sv is not None and sv.field_status is FieldStatus.INVALID:
        return f"刚才的{label}我没法用，请重新指定——{base}"
    if sv is not None and sv.field_status is FieldStatus.AMBIGUOUS:
        return f"{label}不太确定，请明确一下——{base}"
    return base


def clarify(
    plan: Task_Plan,
    *,
    ask,
    max_rounds: int = 2,
    bus=None,
) -> ClarifyOutcome:
    """补齐缺口（≤max_rounds 轮）；已给字段不问、一次问清所有缺口。"""
    rounds = 0
    for _ in range(max_rounds):
        missing = _recompute_missing(plan)
        if not missing:
            return ClarifyOutcome(plan=replace(plan, missing_fields=()), resolved=True,
                                  rounds_used=rounds)
        rounds += 1
        ops_by_id = {op.operation_id: op for op in plan.operations}
        for fieldkey in missing:
            op_id, slot_name = fieldkey.split(":", 1)
            op = ops_by_id[op_id]
            spec = spec_for(op.intent)
            slot_spec = spec.slots[slot_name]
            prompt = _clarify_prompt(op.intent, slot_name, op.slots.get(slot_name))
            answer = ask(prompt)
            sv = _fill_slot(op.intent, slot_name, slot_spec, answer)
            _publish(bus, "CLARIFICATION_QA", operation_id=op_id, slot=slot_name,
                     field_status=sv.field_status.value)
            new_slots = dict(op.slots)
            new_slots[slot_name] = sv
            # slots 是唯一真相源：展示字段按需实时派生，无需在此同步任何副本。
            ops_by_id[op_id] = replace(op, slots=new_slots)
        plan = replace(plan, operations=tuple(ops_by_id.values()))

    remaining = _recompute_missing(plan)
    return ClarifyOutcome(
        plan=replace(plan, missing_fields=tuple(remaining)),
        resolved=not remaining,
        rounds_used=rounds,
    )


# --- resolve_unmapped ------------------------------------------------------

@dataclass
class UnmappedOutcome:
    action: str              # "proceed" | "supplement" | "cancel"
    supplement_text: str = ""
    ignored: tuple[str, ...] = ()
    plan: Task_Plan | None = None


def resolve_unmapped(plan: Task_Plan, interactor: PlanningInteractor, *, bus=None) -> UnmappedOutcome:
    """对每条未映射要求强制处置：补充→整体走修订；取消→中止；否则忽略后 proceed。"""
    if not plan.unmapped_requirements:
        return UnmappedOutcome(action="proceed", plan=plan)

    supplements: list[str] = []
    ignored: list[str] = []
    for item in plan.unmapped_requirements:
        disp = interactor.resolve_unmapped(item)
        _publish(bus, "UNMAPPED_RESOLUTION", action=disp.action)
        if disp.action == "cancel":
            return UnmappedOutcome(action="cancel")
        if disp.action == "supplement":
            supplements.append(disp.text or item)
        else:  # ignore
            ignored.append(item)

    if supplements:
        return UnmappedOutcome(action="supplement", supplement_text="；".join(supplements))
    # 全部忽略 → 账本记 ignored（不是凭空消失），清空 unmapped 视图后 proceed
    ignored_set = {_norm_verbatim(x) for x in ignored}
    new_disp = tuple(
        replace(d, status=DispositionStatus.IGNORED)
        if (d.status is DispositionStatus.UNMAPPED
            and _norm_verbatim(d.verbatim) in ignored_set)
        else d
        for d in plan.dispositions
    )
    return UnmappedOutcome(
        action="proceed",
        ignored=tuple(ignored),
        plan=replace(plan, unmapped_requirements=(), dispositions=new_disp),
    )


def _norm_verbatim(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).lower()


# --- Confirmation ----------------------------------------------------------

def summarize_plan(plan: Task_Plan) -> str:
    """执行序摘要（供用户确认；展示用户级标识，非文档原文）。"""
    lines = [f"计划包含 {len(plan.operations)} 个操作（按执行序）："]
    for i, op in enumerate(plan.operations, 1):
        # 展示字段一律从 slots 实时派生（唯一真相源，永不陈旧）
        bits = [op.intent.value]
        target = operation_target(op.intent, op.slots)
        if target:
            bits.append(f"目标={target}")
        goal = operation_goal(op.slots)
        if goal:
            bits.append(f"目标描述={goal}")
        constraints = operation_constraints(op.slots)
        if constraints:
            bits.append("约束=" + "/".join(constraints))
        quota = operation_quota(op.slots)
        if quota:
            bits.append("引用配额=" + "，".join(quota))
        lines.append(f"  {i}. " + "，".join(bits))
    if plan.unmapped_requirements:
        lines.append("未映射（需处置）：" + "；".join(plan.unmapped_requirements))
    if plan.reorder_suggestions:
        lines.append(f"（有 {len(plan.reorder_suggestions)} 条重排建议）")
    return "\n".join(lines)


def confirm_plan(
    plan: Task_Plan,
    interactor: PlanningInteractor,
    *,
    require_confirmation: bool = True,
    bus=None,
) -> ConfirmDecision:
    if not require_confirmation:
        _publish(bus, "PLAN_CONFIRMED", auto=True)
        return ConfirmDecision(kind="accept")
    decision = interactor.confirm_plan(summarize_plan(plan))
    if decision.kind == "reject":
        _publish(bus, "PLAN_REJECTED")
    elif decision.kind == "accept":
        _publish(bus, "PLAN_CONFIRMED", auto=False)
    return decision


# --- 观测（None-safe）------------------------------------------------------

def _publish(bus, kind_name: str, **payload) -> None:
    if bus is None or EventKind is None:
        return
    kind = getattr(EventKind, kind_name, None)
    if kind is None:
        return
    try:
        bus.publish(kind, **payload)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "PlanningInteractor", "UnmappedDisposition", "ConfirmDecision",
    "ClarifyOutcome", "UnmappedOutcome",
    "clarify", "resolve_unmapped", "confirm_plan", "summarize_plan",
]
