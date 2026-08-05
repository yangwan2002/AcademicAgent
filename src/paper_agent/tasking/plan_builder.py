"""PlanBuilder + build_revision（Task 13）：理解链路收口。

把 Operation_Analyzer 的有序 `RawOperation` 与各 PreExtractor 的 `ExtractionResult`
合并成一份有序、可执行的 `Task_Plan`。核心纪律：

- **严格保序（D0）**：Operation 顺序 == 用户表达顺序，绝不自动改序；能力前置不可行
  只**收集** `reorder_suggestions`（由用户在确认阶段决定是否改），本模块不动顺序。
- **默认值由此填（D10），不由 LLM**：抽取器只报「用户说了什么」；缺失的可选槽由
  PlanBuilder 按 IntentSpec 的 `default_factory` 填入，`source=defaulted`；缺失的
  required/must_ask 槽记入 `missing_fields`（留给 Clarifier）。
- **requirement 唯一真相源 + 可验证零静默丢失（D4）**：canonical requirements 来自
  Analyzer（analyzer 路径）或各抽取器的 `newly_discovered_requirements`（Fast_Path）；
  按 **Slot 消费粒度**统计——只有落到某个 in-spec 且**有 execution_binding** 的槽位
  的 requirement 才算已消费；未消费者进 `unmapped_requirements`。
- **execution_binding 校验（D10 #5 / Property 10）**：无绑定的槽位（执行器不消费）
  不进 Operation，其引用的 requirement 自然不计入 consumed → 落 unmapped，杜绝
  「理解层看似理解了、执行器却完全没用」的静默丢失。

`build_revision` 供 Task 17 的有界规划循环调用：基于旧计划重建新版本（plan_version+1、
保留 plan_id/source_turn_id 血缘），**不丢原请求、不含已完成 op**（只重规划传入的
remaining 工作）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace

from paper_agent.intents.router import Intent
from paper_agent.session.section_index import slugify
from paper_agent.tasking.contracts import (
    AtomicRequirement,
    DispositionStatus,
    ExtractionResult,
    FieldStatus,
    Operation,
    OperationStatus,
    PlanRevisionRequest,
    PlanStatus,
    RawOperation,
    ReorderSuggestion,
    RequirementDisposition,
    SlotSource,
    SlotValue,
    Task_Plan,
)
from paper_agent.tasking.intent_spec import IntentSpec, SlotSpec, spec_for

# 承载「实际取值」的字段态（用于判断槽位是否已被填上值）。
_VALUED = (FieldStatus.PRESENT, FieldStatus.AMBIGUOUS, FieldStatus.INVALID)

# 每个意图的「主目标」槽位（仅用于 Operation.target 展示/trace；执行读 slots）。
_TARGET_SLOT = {
    Intent.POLISH: "section_id",
    Intent.REWRITE: "section_id",
    Intent.ADD_SECTION: "title",
    Intent.CONVERT_FORMAT: "target_ext",
}


def new_plan_id() -> str:
    return f"plan_{uuid.uuid4().hex[:8]}"


def _new_op_id() -> str:
    return f"op_{uuid.uuid4().hex[:8]}"


# --- 槽位定稿 ---------------------------------------------------------------

def _finalize_slot(slot_spec: SlotSpec, extracted: SlotValue | None) -> tuple[SlotValue, bool]:
    """定稿单个槽位，返回 `(SlotValue, is_gap)`。

    - present（抽取器已给值/已规范化）→ 原样保留。
    - 非 present：
      - required 或 must_ask → 是缺口（gap=True），保留抽取器状态供 Clarifier；不填默认。
      - 可选 → 按 IntentSpec 填默认值（source=defaulted），不追问。
    """
    if extracted is not None and extracted.field_status is FieldStatus.PRESENT:
        return extracted, False

    if slot_spec.required or slot_spec.must_ask:
        gap = extracted if extracted is not None else SlotValue(field_status=FieldStatus.USER_ABSENT)
        return gap, True

    # 可选槽位：填默认（确定性，非 LLM）
    if slot_spec.default_factory is not None:
        val = slot_spec.default()
        if slot_spec.normalizer is not None:
            try:
                val = slot_spec.normalizer(val)
            except Exception:  # noqa: BLE001
                pass
        return SlotValue(value=val, field_status=FieldStatus.PRESENT, source=SlotSource.DEFAULTED), False

    # 可选且无默认（罕见）→ 保持缺省态，不作缺口
    keep = extracted if extracted is not None else SlotValue(field_status=FieldStatus.USER_ABSENT)
    return keep, False


def operation_target(intent: Intent, slots: dict[str, SlotValue]) -> str:
    """从槽位实时派生 Operation.target（展示/trace 用；执行读 slots）。"""
    name = _TARGET_SLOT.get(intent)
    if name:
        sv = slots.get(name)
        if sv is not None and sv.value is not None:
            return str(sv.value)
    return ""


def operation_goal(slots: dict[str, SlotValue]) -> str:
    """从槽位实时派生「目标描述」（展示/trace 用）。"""
    for key in ("goal", "direction"):
        sv = slots.get(key)
        if sv is not None and sv.field_status is FieldStatus.PRESENT and sv.value:
            return str(sv.value)
    return ""


def operation_constraints(slots: dict[str, SlotValue]) -> tuple[str, ...]:
    """从槽位实时派生约束列表（展示/trace 用；执行读 slots["constraints"]）。"""
    sv = slots.get("constraints")
    if sv is None or sv.field_status is not FieldStatus.PRESENT:
        return ()
    if isinstance(sv.value, (list, tuple)):
        return tuple(str(x) for x in sv.value)
    return ()


#: 配额维度/操作符 → 中文标签（仅供确认摘要展示）。
_QUOTA_DIM_LABELS = {"total": "共", "language.zh": "中文", "language.en": "英文"}
_QUOTA_OP_LABELS = {
    "bare": "", "exact": "恰好", "min": "至少", "max": "最多",
    "range": "", "approx": "约",
}


def operation_quota(slots: dict[str, SlotValue]) -> tuple[str, ...]:
    """从 `citation_quota` 槽派生**人可读**的配额摘要（X-11）。

    此前确认摘要完全不显示配额 —— 系统让用户「据此核对」，却不显示要核对的东西：
    真机上「10 篇、英 8、中 2」在摘要里一个字都没有。
    """
    sv = slots.get("citation_quota")
    if sv is None or sv.field_status is not FieldStatus.PRESENT:
        return ()
    raw = sv.value
    if not isinstance(raw, dict):
        return ()

    out: list[str] = []
    for c in raw.get("constraints") or ():
        if not isinstance(c, dict):
            continue
        dim = _QUOTA_DIM_LABELS.get(str(c.get("dimension", "")), str(c.get("dimension", "")))
        op = _QUOTA_OP_LABELS.get(str(c.get("operator", "")), "")
        val = c.get("value")
        if isinstance(val, (list, tuple)) and len(val) == 2:
            val_s = f"{val[0]}-{val[1]}"
        elif val is None:
            continue
        else:
            val_s = str(val)
        out.append(f"{dim}{op}{val_s}篇")

    for f in raw.get("filters") or ():
        if not isinstance(f, dict):
            continue
        field, op, val = str(f.get("field", "")), str(f.get("operator", "")), f.get("value")
        if field == "year" and op == "recent_years" and val is not None:
            out.append(f"近{val}年")
        elif field == "year" and op == "gte" and val is not None:
            out.append(f"{val}年后")
        elif field == "year" and op == "lte" and val is not None:
            out.append(f"{val}年前")
        elif field == "language" and val is not None:
            langs = val if isinstance(val, (list, tuple)) else [val]
            names = "/".join(_QUOTA_DIM_LABELS.get(f"language.{x}", str(x)) for x in langs)
            out.append(f"限{names}")
        elif field == "reference_type" and val is not None:
            types = val if isinstance(val, (list, tuple)) else [val]
            out.append("限" + "/".join(str(x) for x in types))
    return tuple(out)


def _build_operation(
    raw: RawOperation,
    extraction: ExtractionResult,
    canonical_ids: set[str],
) -> tuple[Operation | None, list[str], list[str]]:
    """据 spec 定稿一个 Operation，返回 `(op, consumed_ids, missing_fields)`。

    无 spec（不支持的意图）→ op=None（调用方将其原话入 unmapped_text）。
    """
    spec = spec_for(raw.intent)
    if spec is None:
        return None, [], []

    op_id = _new_op_id()
    final_slots: dict[str, SlotValue] = {}
    consumed: list[str] = []
    missing: list[str] = []

    for name, slot_spec in spec.slots.items():
        # execution_binding 校验：无绑定的槽位执行器不消费 → 不纳入 Operation，
        # 其引用的 requirement 因此不计入 consumed，自然落 unmapped。
        if slot_spec.execution_binding is None:
            continue
        extracted = extraction.slots.get(name)
        final, is_gap = _finalize_slot(slot_spec, extracted)
        final_slots[name] = final
        for rid in final.requirement_ids:
            if rid in canonical_ids:
                consumed.append(rid)
        if is_gap:
            missing.append(f"{op_id}:{name}")

    op = Operation(
        operation_id=op_id,
        intent=raw.intent,
        slots=final_slots,
        requirement_ids=tuple(dict.fromkeys(consumed)),
        status=OperationStatus.PENDING,
    )
    return op, consumed, missing


# --- reorder 能力模拟（不自动改序）-----------------------------------------

def _collect_reorder_suggestions(operations: Sequence[Operation]) -> tuple[ReorderSuggestion, ...]:
    """v1 能力前置模拟：仅检测 add_section 的**位置前向引用**。

    若某 add_section 的 position 引用了一个由**更靠后**的 add_section 才创建的章节
    （如「把 Discussion 放 Conclusion 前」而 Conclusion 也在本计划里、排在其后创建），
    则该顺序执行期不可行——但**不自动改序**，只给出建议顺序供用户确认。

    其余能力校验（章节是否已存在、convert 终止性、格式工件依赖）属投影校验，
    留给 Static_Plan_Validator（Task 14）。
    """
    # 各 add_section 将创建的 section id → 首个创建它的 op 下标
    creator_index: dict[str, int] = {}
    for i, op in enumerate(operations):
        if op.intent is Intent.ADD_SECTION:
            title_sv = op.slots.get("title")
            if title_sv is not None and title_sv.value:
                creator_index.setdefault(slugify(str(title_sv.value)), i)

    suggestions: list[ReorderSuggestion] = []
    order = [op.operation_id for op in operations]
    for i, op in enumerate(operations):
        if op.intent is not Intent.ADD_SECTION:
            continue
        pos_sv = op.slots.get("position")
        if pos_sv is None or not isinstance(pos_sv.value, str):
            continue
        pos = pos_sv.value
        ref = None
        if pos.startswith("after:"):
            ref = pos[len("after:"):]
        elif pos.startswith("before:"):
            ref = pos[len("before:"):]
        if not ref:
            continue
        ref_key = slugify(ref.split(":")[-1])
        creator = creator_index.get(ref_key)
        if creator is not None and creator > i:
            # 建议顺序：把创建被引用章节的 op 前移到本 op 之前
            new_order = [oid for oid in order if oid != operations[creator].operation_id]
            insert_at = new_order.index(op.operation_id)
            new_order.insert(insert_at, operations[creator].operation_id)
            here = operation_target(op.intent, op.slots)
            there = operation_target(
                operations[creator].intent, operations[creator].slots
            )
            suggestions.append(ReorderSuggestion(
                reason=(
                    f"「{here}」要放在「{there}」旁，"
                    f"但后者在本计划里排在其后创建；建议先创建被引用章节。"
                ),
                suggested_order=tuple(new_order),
            ))
    return tuple(suggestions)


# --- 主入口 -----------------------------------------------------------------

def build_plan(
    source_turn_id: str,
    raw_operations: Sequence[RawOperation],
    extractions: Sequence[ExtractionResult],
    canonical_requirements: Sequence[AtomicRequirement] | None = None,
    *,
    unmapped_text: Sequence[str] = (),
    plan_id: str | None = None,
    plan_version: int = 1,
    project_fact_requirement_ids: Sequence[str] = (),
) -> Task_Plan:
    """合并操作与抽取结果 → 有序 Task_Plan（保序、填默认、统计 unmapped）。

    `raw_operations` 与 `extractions` 一一对应、同序。`canonical_requirements` 为
    Analyzer 路径的 canonical 要求；Fast_Path 传 None，要求来自各抽取器的
    `newly_discovered_requirements`。
    """
    if len(raw_operations) != len(extractions):
        raise ValueError("raw_operations 与 extractions 必须一一对应")

    # 汇总 canonical requirements（analyzer 路径 + Fast_Path 发现），按 id 去重
    canon: dict[str, AtomicRequirement] = {}
    for r in (canonical_requirements or ()):
        canon.setdefault(r.requirement_id, r)
    for ext in extractions:
        for r in ext.newly_discovered_requirements:
            canon.setdefault(r.requirement_id, r)
    canonical_ids = set(canon.keys())

    operations: list[Operation] = []
    consumed_all: set[str] = set()
    missing_all: list[str] = []
    leftover_text = list(unmapped_text)

    for raw, ext in zip(raw_operations, extractions):
        op, consumed, missing = _build_operation(raw, ext, canonical_ids)
        if op is None:
            leftover_text.append(raw.verbatim)
            continue
        operations.append(op)
        consumed_all.update(consumed)
        missing_all.extend(missing)

    # 每条 requirement 的去向账本（保序）：被某具体槽位消费 → mapped，否则 unmapped。
    dispositions = _build_dispositions(
        canon, operations, set(project_fact_requirement_ids)
    )
    unmapped_reqs = [
        d.verbatim for d in dispositions if d.status is DispositionStatus.UNMAPPED
    ]
    unmapped_all = tuple(unmapped_reqs) + tuple(leftover_text)

    reorder = _collect_reorder_suggestions(operations)

    return Task_Plan(
        plan_id=plan_id or new_plan_id(),
        source_turn_id=source_turn_id,
        operations=tuple(operations),
        dispositions=dispositions,
        unmapped_requirements=unmapped_all,
        missing_fields=tuple(missing_all),
        reorder_suggestions=reorder,
        status=PlanStatus.DRAFT,
        plan_version=plan_version,
    )


def _build_dispositions(
    canon: dict[str, AtomicRequirement],
    operations: Sequence[Operation],
    project_fact_ids: set[str] | None = None,
) -> tuple[RequirementDisposition, ...]:
    """为每条 requirement 记录去向：被哪个操作的哪些槽位消费，或未映射。

    覆盖判定按 **Slot 消费粒度**（D4/Property 2）：只有落进某个具体
    `SlotValue.requirement_ids` 才算 mapped；「仅挂在 Operation 上」不算。

    例外是 `project_fact_ids`（X-11）：项目级陈述（「课题是 X」）**没有**对应槽位——
    它属于整篇论文而非单次操作，落 `facts.claims`。这类记 PROJECT_MEMORY，
    算已消费，不弹 unmapped。槽位消费优先（同一条既进槽又进 claims 时记 mapped）。
    """
    pf_ids = project_fact_ids or set()
    # requirement_id → (operation_id, [slot_name...])
    consumers: dict[str, tuple[str, list[str]]] = {}
    for op in operations:
        for slot_name, sv in op.slots.items():
            for rid in sv.requirement_ids:
                if rid not in canon:
                    continue          # 引用了不存在的 id → 忽略（防幻觉污染账本）
                if rid in consumers:
                    consumers[rid][1].append(slot_name)
                else:
                    consumers[rid] = (op.operation_id, [slot_name])

    out: list[RequirementDisposition] = []
    for rid, req in canon.items():
        if req.category == "unparsed":
            # 理解整体失败的兜底原话：留痕但不算已处理
            out.append(RequirementDisposition(
                requirement_id=rid, status=DispositionStatus.UNPARSED,
                verbatim=req.verbatim,
            ))
            continue
        hit = consumers.get(rid)
        if hit is None and rid in pf_ids:
            out.append(RequirementDisposition(
                requirement_id=rid, status=DispositionStatus.PROJECT_MEMORY,
                verbatim=req.verbatim,
            ))
        elif hit is None:
            out.append(RequirementDisposition(
                requirement_id=rid, status=DispositionStatus.UNMAPPED,
                verbatim=req.verbatim,
            ))
        else:
            op_id, slot_names = hit
            out.append(RequirementDisposition(
                requirement_id=rid, status=DispositionStatus.MAPPED,
                verbatim=req.verbatim, operation_id=op_id,
                slot_names=tuple(dict.fromkeys(slot_names)),
            ))
    return tuple(out)


def build_revision(
    previous_plan: Task_Plan,
    revision_request: PlanRevisionRequest,
    raw_operations: Sequence[RawOperation],
    extractions: Sequence[ExtractionResult],
    canonical_requirements: Sequence[AtomicRequirement] | None = None,
    *,
    remaining_operation_ids: Sequence[str] | None = None,
    unmapped_text: Sequence[str] = (),
    project_fact_requirement_ids: Sequence[str] = (),
) -> Task_Plan:
    """基于旧计划重建新版本（Task 17 有界规划循环调用）。

    - **不丢原请求**：`raw_operations`/`extractions` 由调用方对「旧计划摘要 + 修订文本」
      重新分析得到（含原始意图），本函数据其整体重建，不把修订当全新请求。
    - **不含已完成 op**：`remaining_operation_ids` 非空时，新计划的操作数被**截断**到
      未执行步骤的数量（防止理解层把原始请求整体重新拆出已完成的步骤，导致重跑已成功
      的润色/新增章节，Property 11）。
    - 版本与血缘：`plan_version = previous.plan_version + 1`，保留 `plan_id` 与
      `source_turn_id`。
    """
    plan = build_plan(
        previous_plan.source_turn_id,
        raw_operations,
        extractions,
        canonical_requirements,
        unmapped_text=unmapped_text,
        plan_id=previous_plan.plan_id,
        plan_version=previous_plan.plan_version + 1,
        project_fact_requirement_ids=project_fact_requirement_ids,
    )
    if remaining_operation_ids:
        # 只保留与「未执行步骤数」相符的尾部操作：重规划的输入是原始请求，理解层可能
        # 重新拆出全部步骤，但已成功的那些绝不能再执行一次。
        keep = len(tuple(remaining_operation_ids))
        if keep < len(plan.operations):
            plan = replace(plan, operations=plan.operations[-keep:])
    return plan


__all__ = [
    "build_plan", "build_revision", "new_plan_id",
    "operation_target", "operation_goal", "operation_constraints",
    "operation_quota",
]
