"""Static_Plan_Validator（Task 14）：计划期投影校验。

在**执行前**对整份计划做确定性静态校验，产出 `ValidationOutcome{result, issues,
missing_fields, reorder_suggestions}`。校验维度（design D6/D1/#11）：

- **投影章节（Projected_Sections）**：不查「现在是否存在」，查「按用户序执行到该步时
  应存在的集合」= 当前章节 ∪ **前序** add_section 将创建的章节。于是「先加再改」不被
  误拒，而「改一个谁都没创建的章节」在计划期就被拦（section_not_found）。
- **add_section 去重**：新增一个投影里已存在的章节 → section_exists（应改用 rewrite）。
- **Convert 终止性（D1/#11）**：convert_format 必须是计划最后一步；其后存在**任何**
  操作 → convert_not_terminal（分两轮）。同格式转换（目标==当前）→ convert_same_format。
- **format_docx 能力**：只作用于 docx 工件；当前非 docx → format_requires_docx。
- **约束冲突**：format_docx 的 op 列表里同一 (name, scope) 给了互斥取值 →
  conflicting_constraints。
- **意图支持性 / 必填**：不支持的意图 → unsupported_intent；required/must_ask 缺口
  （来自 PlanBuilder 的 `missing_fields`）→ 结果 needs_input。

只做**确定性**校验，不调 LLM、不改计划、不改序（重排建议由 PlanBuilder 收集、此处透传）。
"""

from __future__ import annotations

from collections.abc import Sequence

from paper_agent.intents.intent import Intent
from paper_agent.session.section_index import (
    match_key,
    slugify,
    supports_sections,
)
from paper_agent.tasking.contracts import (
    FieldStatus,
    Operation,
    Task_Plan,
    ValidationIssue,
    ValidationOutcome,
)
from paper_agent.tasking.intent_spec import spec_for

# --- 问题码 ---
SECTION_NOT_FOUND = "section_not_found"
SECTION_EXISTS = "section_exists"
CONVERT_NOT_TERMINAL = "convert_not_terminal"
CONVERT_SAME_FORMAT = "convert_same_format"
FORMAT_REQUIRES_DOCX = "format_requires_docx"
FORMAT_LACKS_SECTIONS = "format_lacks_sections"
CONFLICTING_CONSTRAINTS = "conflicting_constraints"
UNSUPPORTED_INTENT = "unsupported_intent"

_EDIT_INTENTS = (Intent.POLISH, Intent.REWRITE)
#: 需要按章节定位的意图。格式解析不出章节时，这些意图的**根因是格式**，
#: 报 section_not_found 会把用户引向「章节名写错了吗」这条错误的排查方向。
_SECTION_INTENTS = (Intent.POLISH, Intent.REWRITE, Intent.ADD_SECTION)


def _norm(value: object) -> str:
    """章节标识归一：取最后一个 ':' 段（去 sec: 前缀）再 slugify。"""
    s = str(value).strip()
    if ":" in s:
        s = s.split(":")[-1]
    return slugify(s)


def _norm_ext(value: object) -> str:
    return str(value).strip().lower().lstrip(".")


def _matches(value: object, projected: set[str]) -> bool:
    """章节是否落在投影集里。判据与 `SectionIndex.find` **共用** `match_key`。

    此前这里是一份独立实现（注释写「呼应 SectionIndex.find」——呼应但不共用），
    于是两边可以各自漂移。真机 2026-08-01：LLM 填 `section_id="引言部分"`，
    文档章节是「1 引言」（slug `1_引言`），双向包含两个方向都不成立
    （`引言部分` ⊄ `1_引言`，`1_引言` ⊄ `引言部分`），计划期误报 section_not_found。
    根因是**章节编号前缀**，由 `match_key` 剥离。

    注意 `projected` 是**已归一的扁平集合**（含 id 与标题，还含前序 add_section
    将创建的标题），拿不到 (id, title) 配对，故不能直接调 `anchor_match_rank`；
    但去编号这一关键步骤共用同一函数，不会再分叉。
    """
    key = _norm(value)
    if not key:
        return False
    if key in projected:
        return True
    if any(key in p or p in key for p in projected):
        return True
    mk = match_key(value)
    if not mk:
        return False
    return any(
        pk and (pk == mk or pk in mk or mk in pk)
        for pk in (match_key(p) for p in projected)
    )


def _position_ref(op: Operation) -> str | None:
    """从 add_section 的 position 槽解析被引用的章节标识（after:/before:）。"""
    sv = op.slots.get("position")
    if sv is None or not isinstance(sv.value, str):
        return None
    pos = sv.value
    for prefix in ("after:", "before:"):
        if pos.startswith(prefix):
            return pos[len(prefix):]
    return None


def _slot_value(op: Operation, name: str):
    sv = op.slots.get(name)
    if sv is not None and sv.field_status is FieldStatus.PRESENT:
        return sv.value
    return None


def validate_plan(
    plan: Task_Plan,
    *,
    current_sections: Sequence[str] = (),
    current_format: str = "",
) -> ValidationOutcome:
    """对计划做投影静态校验，返回 ValidationOutcome。

    `current_sections`：执行前已存在的章节标识（id 或标题）。
    `current_format`：当前工作副本格式（docx/tex/md）；空表示未知，相关检查跳过。
    """
    issues: list[ValidationIssue] = []
    ops = plan.operations

    known = {_norm(s) for s in current_sections if str(s).strip()}
    # 全量将创建集合（用于 add_section 位置引用的存在性；顺序问题由 reorder 建议处理）
    all_created = {
        _norm(_slot_value(op, "title"))
        for op in ops
        if op.intent is Intent.ADD_SECTION and _slot_value(op, "title")
    }
    full_projected = known | all_created

    projected = set(known)  # 前序投影：随执行序累积
    last_index = len(ops) - 1

    for i, op in enumerate(ops):
        if spec_for(op.intent) is None:
            issues.append(ValidationIssue(op.operation_id, UNSUPPORTED_INTENT,
                                          f"不支持的意图：{op.intent.value}"))
            continue

        # 格式能力先于章节存在性：docx 等格式解析不出章节，投影集合必然为空，
        # 若不先在此拦下，下面会报 section_not_found —— 把根因说成表象。
        # `current_format` 为空表示未知 → 沿用「相关检查跳过」的既有语义。
        if (op.intent in _SECTION_INTENTS and current_format
                and not supports_sections(current_format)):
            issues.append(ValidationIssue(
                op.operation_id, FORMAT_LACKS_SECTIONS,
                f"当前文档格式 .{_norm_ext(current_format)} 解析不出章节结构，"
                f"而 {op.intent.value} 需要按章节定位。"
                "请先用 convert_format 转成 .md 或 .tex（转换须单独一轮），"
                "或直接以 .md/.tex 文件载入。",
            ))
            continue    # 归因已明确，不再叠加 section_not_found 等次生诊断

        if op.intent in _EDIT_INTENTS:
            target = _slot_value(op, "section_id")
            if target is not None and not _matches(target, projected):
                issues.append(ValidationIssue(
                    op.operation_id, SECTION_NOT_FOUND,
                    f"计划期找不到要修改的章节：{target}",
                ))

        elif op.intent is Intent.ADD_SECTION:
            title = _slot_value(op, "title")
            if title is not None and _matches(title, projected):
                issues.append(ValidationIssue(
                    op.operation_id, SECTION_EXISTS,
                    f"章节「{title}」已存在，应改用 rewrite。",
                ))
            ref = _position_ref(op)
            if ref and not _matches(ref, full_projected):
                issues.append(ValidationIssue(
                    op.operation_id, SECTION_NOT_FOUND,
                    f"插入位置引用了不存在的章节：{ref}",
                ))
            if title is not None:
                projected.add(_norm(title))

        elif op.intent is Intent.CONVERT_FORMAT:
            if i != last_index:
                issues.append(ValidationIssue(
                    op.operation_id, CONVERT_NOT_TERMINAL,
                    "格式转换必须是计划的最后一步；其后仍有操作，请分两轮完成。",
                ))
            tgt = _slot_value(op, "target_ext")
            if current_format and tgt and _norm_ext(tgt) == _norm_ext(current_format):
                issues.append(ValidationIssue(
                    op.operation_id, CONVERT_SAME_FORMAT,
                    f"目标格式与当前一致（{current_format}），无需转换。",
                ))

        elif op.intent is Intent.FORMAT_DOCX:
            if current_format and _norm_ext(current_format) != "docx":
                issues.append(ValidationIssue(
                    op.operation_id, FORMAT_REQUIRES_DOCX,
                    f"排版仅作用于 docx（当前 {current_format}）；请先转换为 docx。",
                ))
            issues.extend(_format_conflicts(op))

    if issues:
        result = "rejected"
    elif plan.missing_fields:
        result = "needs_input"
    else:
        result = "ready"

    return ValidationOutcome(
        result=result,
        issues=tuple(issues),
        missing_fields=tuple(plan.missing_fields),
        reorder_suggestions=tuple(plan.reorder_suggestions),
    )


def _format_conflicts(op: Operation) -> list[ValidationIssue]:
    """检测 format_docx 的 op 列表内互斥取值（同 name+scope 不同 value）。"""
    sv = op.slots.get("operations")
    if sv is None or sv.field_status is not FieldStatus.PRESENT or not isinstance(sv.value, (list, tuple)):
        return []
    seen: dict[tuple, object] = {}
    conflicts: list[ValidationIssue] = []
    for fop in sv.value:
        name = getattr(fop, "name", None)
        scope = getattr(fop, "scope", None)
        value = getattr(fop, "value", None)
        if name is None:
            continue
        key = (name, scope)
        if key in seen and seen[key] != value:
            conflicts.append(ValidationIssue(
                op.operation_id, CONFLICTING_CONSTRAINTS,
                f"排版操作冲突：{name}（{scope}）同时要求 {seen[key]!r} 与 {value!r}。",
            ))
        else:
            seen[key] = value
    return conflicts


__all__ = [
    "validate_plan",
    "SECTION_NOT_FOUND", "SECTION_EXISTS", "CONVERT_NOT_TERMINAL",
    "FORMAT_LACKS_SECTIONS",
    "CONVERT_SAME_FORMAT", "FORMAT_REQUIRES_DOCX", "CONFLICTING_CONSTRAINTS",
    "UNSUPPORTED_INTENT",
]
