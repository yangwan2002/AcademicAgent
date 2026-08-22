"""intent-planning Task 13 验收：PlanBuilder + build_revision。

- 保序：Operation 顺序 == 用户序，绝不自动改序
- 默认值来自 IntentSpec（source=defaulted），非 LLM
- Slot 消费粒度：requirement 落到具体绑定槽位才算已消费
- 无绑定槽位 → 其 requirement 落 unmapped（不静默丢失）
- required/must_ask 缺口进 missing_fields；可选缺口填默认
- reorder：add_section 位置前向引用产建议但不改序
- build_revision：不丢原请求、不含已完成 op、版本+1、血缘保留
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.intent import Intent
from paper_agent.tasking.contracts import (
    AtomicRequirement,
    CallStatus,
    ExtractionResult,
    FieldStatus,
    PlanRevisionRequest,
    RawOperation,
    RevisionReason,
    SlotSource,
    SlotValue,
)
from paper_agent.tasking.plan_builder import (
    build_plan,
    build_revision,
    operation_target,
)


def _present(value, req_ids=()):
    return SlotValue(value=value, field_status=FieldStatus.PRESENT,
                     source=SlotSource.USER_EXPLICIT, requirement_ids=tuple(req_ids))


def _ext(slots, *, consumed=(), discovered=()):
    return ExtractionResult(
        call_status=CallStatus.SUCCESS, slots=slots,
        consumed_requirement_ids=tuple(consumed),
        newly_discovered_requirements=tuple(discovered),
    )


# --- 保序 ---

def test_operations_preserve_user_order():
    raws = [
        RawOperation(intent=Intent.POLISH, verbatim="润色引言"),
        RawOperation(intent=Intent.CONVERT_FORMAT, verbatim="转成 docx"),
    ]
    exts = [
        _ext({"section_id": _present("sec:intro")}),
        _ext({"target_ext": _present("docx")}),
    ]
    plan = build_plan("turn1", raws, exts, canonical_requirements=())
    assert [op.intent for op in plan.operations] == [Intent.POLISH, Intent.CONVERT_FORMAT]


# --- 默认值来自 spec ---

def test_optional_slots_filled_with_spec_defaults():
    raws = [RawOperation(intent=Intent.POLISH, verbatim="润色引言")]
    exts = [_ext({"section_id": _present("sec:intro")})]
    plan = build_plan("t", raws, exts, canonical_requirements=())
    op = plan.operations[0]
    # goal 用户没说 → 填 spec 默认「保守润色」，source=defaulted
    assert op.slots["goal"].value == "保守润色"
    assert op.slots["goal"].source is SlotSource.DEFAULTED
    # constraints 默认空 list
    assert op.slots["constraints"].value == []
    assert op.slots["constraints"].source is SlotSource.DEFAULTED


def test_present_value_not_overwritten_by_default():
    raws = [RawOperation(intent=Intent.POLISH, verbatim="润色引言")]
    exts = [_ext({"section_id": _present("sec:intro"), "goal": _present("大幅精简")})]
    plan = build_plan("t", raws, exts, canonical_requirements=())
    assert plan.operations[0].slots["goal"].value == "大幅精简"
    assert plan.operations[0].slots["goal"].source is SlotSource.USER_EXPLICIT


# --- 缺口 ---

def test_required_missing_goes_to_missing_fields():
    raws = [RawOperation(intent=Intent.POLISH, verbatim="润色一下")]
    exts = [_ext({})]  # section_id 缺失（required+must_ask）
    plan = build_plan("t", raws, exts, canonical_requirements=())
    assert any(m.endswith(":section_id") for m in plan.missing_fields)
    # 缺口不填默认，保持非 present
    assert plan.operations[0].slots["section_id"].field_status is FieldStatus.USER_ABSENT


# --- Slot 消费粒度 + unmapped ---

def test_requirement_consumed_by_slot_not_unmapped():
    reqs = (
        AtomicRequirement(requirement_id="r1", verbatim="润色引言"),
        AtomicRequirement(requirement_id="r2", verbatim="别改数字"),
    )
    raws = [RawOperation(intent=Intent.POLISH, verbatim="润色引言，别改数字")]
    exts = [_ext({
        "section_id": _present("sec:intro", req_ids=["r1"]),
        "constraints": _present(["不改数字"], req_ids=["r2"]),
    })]
    plan = build_plan("t", raws, exts, canonical_requirements=reqs)
    assert plan.unmapped_requirements == ()


def test_unconsumed_requirement_goes_unmapped():
    reqs = (
        AtomicRequirement(requirement_id="r1", verbatim="润色引言"),
        AtomicRequirement(requirement_id="r2", verbatim="翻译成英文"),  # 没被任何槽消费
    )
    raws = [RawOperation(intent=Intent.POLISH, verbatim="润色引言")]
    exts = [_ext({"section_id": _present("sec:intro", req_ids=["r1"])})]
    plan = build_plan("t", raws, exts, canonical_requirements=reqs)
    assert "翻译成英文" in plan.unmapped_requirements


def test_slot_not_in_spec_reqs_go_unmapped():
    # 抽取器产出了一个不在 IntentSpec 里的槽位（无执行绑定）→ 其 requirement 落 unmapped
    reqs = (AtomicRequirement(requirement_id="rX", verbatim="加个水印"),)
    raws = [RawOperation(intent=Intent.POLISH, verbatim="润色引言，加个水印")]
    exts = [_ext({
        "section_id": _present("sec:intro"),
        "bogus_slot": _present("watermark", req_ids=["rX"]),  # spec 里没有
    })]
    plan = build_plan("t", raws, exts, canonical_requirements=reqs)
    assert "加个水印" in plan.unmapped_requirements
    # bogus_slot 不进 Operation
    assert "bogus_slot" not in plan.operations[0].slots


def test_analyzer_unmapped_text_carried():
    raws = [RawOperation(intent=Intent.POLISH, verbatim="润色引言")]
    exts = [_ext({"section_id": _present("sec:intro")})]
    plan = build_plan("t", raws, exts, canonical_requirements=(),
                      unmapped_text=["顺便帮我泡杯咖啡"])
    assert "顺便帮我泡杯咖啡" in plan.unmapped_requirements


# --- Fast_Path 发现的 requirement 参与消费判定 ---

def test_fast_path_discovered_requirements_consumed():
    discovered = (AtomicRequirement(requirement_id="d1", verbatim="转成 docx"),)
    raws = [RawOperation(intent=Intent.CONVERT_FORMAT, verbatim="转成 docx")]
    exts = [_ext({"target_ext": _present("docx", req_ids=["d1"])},
                 discovered=discovered)]
    plan = build_plan("t", raws, exts, canonical_requirements=None)  # Fast_Path
    assert plan.unmapped_requirements == ()


# --- reorder（不改序）---

def test_add_section_forward_reference_suggests_reorder_without_reordering():
    raws = [
        RawOperation(intent=Intent.ADD_SECTION, verbatim="加 Discussion 放 Conclusion 前"),
        RawOperation(intent=Intent.ADD_SECTION, verbatim="加 Conclusion"),
    ]
    exts = [
        _ext({"title": _present("Discussion"), "purpose": _present("讨论"),
              "structure_kind": _present("desc"),
              "position": _present("before:conclusion")}),
        _ext({"title": _present("Conclusion"), "purpose": _present("总结"),
              "structure_kind": _present("desc")}),
    ]
    plan = build_plan("t", raws, exts, canonical_requirements=())
    # 顺序未被改动（保序）；展示标识从 slots 实时派生
    assert [operation_target(op.intent, op.slots) for op in plan.operations] == [
        "Discussion", "Conclusion"
    ]
    # 但给出重排建议
    assert len(plan.reorder_suggestions) == 1
    sug = plan.reorder_suggestions[0]
    # 建议顺序把 Conclusion（创建者）前移到 Discussion 之前
    conclusion_id = plan.operations[1].operation_id
    discussion_id = plan.operations[0].operation_id
    assert sug.suggested_order.index(conclusion_id) < sug.suggested_order.index(discussion_id)


def test_no_reorder_when_no_forward_reference():
    raws = [RawOperation(intent=Intent.ADD_SECTION, verbatim="加 Discussion 放末尾")]
    exts = [_ext({"title": _present("Discussion"), "purpose": _present("讨论"),
                  "structure_kind": _present("desc"), "position": _present("end")})]
    plan = build_plan("t", raws, exts, canonical_requirements=())
    assert plan.reorder_suggestions == ()


# --- build_revision ---

def test_build_revision_keeps_original_and_bumps_version():
    # 初始计划：只 polish
    raws0 = [RawOperation(intent=Intent.POLISH, verbatim="润色引言")]
    exts0 = [_ext({"section_id": _present("sec:intro")})]
    plan0 = build_plan("turnA", raws0, exts0, canonical_requirements=())

    # 修订「再转成 docx」：调用方重新分析 → 含原始 polish + 新 convert（不丢原请求）
    req = PlanRevisionRequest(previous_plan=plan0, revision_text="再转成 docx",
                              revision_reason=RevisionReason.SUPPLEMENT)
    raws1 = [
        RawOperation(intent=Intent.POLISH, verbatim="润色引言"),
        RawOperation(intent=Intent.CONVERT_FORMAT, verbatim="转成 docx"),
    ]
    exts1 = [_ext({"section_id": _present("sec:intro")}), _ext({"target_ext": _present("docx")})]
    new_plan = build_revision(plan0, req, raws1, exts1, canonical_requirements=())

    assert [op.intent for op in new_plan.operations] == [Intent.POLISH, Intent.CONVERT_FORMAT]
    assert new_plan.plan_version == 2
    assert new_plan.plan_id == plan0.plan_id           # 血缘保留
    assert new_plan.source_turn_id == "turnA"


def test_build_revision_excludes_completed_ops():
    # 初始计划：add_section + polish；假设 add_section 已完成
    raws0 = [
        RawOperation(intent=Intent.ADD_SECTION, verbatim="加 Discussion"),
        RawOperation(intent=Intent.POLISH, verbatim="润色引言"),
    ]
    exts0 = [
        _ext({"title": _present("Discussion"), "purpose": _present("讨论"),
              "structure_kind": _present("desc")}),
        _ext({"section_id": _present("sec:intro")}),
    ]
    plan0 = build_plan("turnB", raws0, exts0, canonical_requirements=())
    _done_id = plan0.operations[0].operation_id
    remaining_id = plan0.operations[1].operation_id

    # needs_replan：只重规划未执行的 polish
    req = PlanRevisionRequest(previous_plan=plan0, revision_text="章节失效重规划",
                              revision_reason=RevisionReason.NEEDS_REPLAN)
    raws1 = [RawOperation(intent=Intent.POLISH, verbatim="润色引言")]
    exts1 = [_ext({"section_id": _present("sec:intro")})]
    new_plan = build_revision(plan0, req, raws1, exts1, canonical_requirements=(),
                              remaining_operation_ids=[remaining_id])

    assert len(new_plan.operations) == 1
    assert new_plan.operations[0].intent is Intent.POLISH
    # 不含已完成 add_section
    assert all(op.intent is not Intent.ADD_SECTION for op in new_plan.operations)
    assert new_plan.plan_version == 2


# --- dispositions 账本（结构化处置记录）---

def test_dispositions_record_mapped_with_operation_and_slot():
    from paper_agent.tasking.contracts import DispositionStatus

    reqs = (
        AtomicRequirement(requirement_id="r1", verbatim="润色引言"),
        AtomicRequirement(requirement_id="r2", verbatim="别改数字"),
    )
    raws = [RawOperation(intent=Intent.POLISH, verbatim="润色引言，别改数字")]
    exts = [_ext({
        "section_id": _present("sec:intro", req_ids=["r1"]),
        "constraints": _present(["不改数字"], req_ids=["r2"]),
    })]
    plan = build_plan("t", raws, exts, canonical_requirements=reqs)

    by_id = {d.requirement_id: d for d in plan.dispositions}
    assert by_id["r1"].status is DispositionStatus.MAPPED
    assert by_id["r1"].operation_id == plan.operations[0].operation_id
    assert by_id["r1"].slot_names == ("section_id",)
    assert by_id["r2"].slot_names == ("constraints",)
    assert plan.unmapped_requirements == ()


def test_dispositions_record_unmapped():
    from paper_agent.tasking.contracts import DispositionStatus

    reqs = (
        AtomicRequirement(requirement_id="r1", verbatim="润色引言"),
        AtomicRequirement(requirement_id="r2", verbatim="翻译成英文"),
    )
    raws = [RawOperation(intent=Intent.POLISH, verbatim="润色引言")]
    exts = [_ext({"section_id": _present("sec:intro", req_ids=["r1"])})]
    plan = build_plan("t", raws, exts, canonical_requirements=reqs)

    by_id = {d.requirement_id: d for d in plan.dispositions}
    assert by_id["r2"].status is DispositionStatus.UNMAPPED
    assert by_id["r2"].operation_id is None
    assert by_id["r2"].verbatim == "翻译成英文"
    # 派生视图与账本一致
    assert plan.unmapped_requirements == ("翻译成英文",)


def test_dispositions_mark_unparsed_fallback():
    from paper_agent.tasking.contracts import DispositionStatus

    reqs = (AtomicRequirement(requirement_id="r1", verbatim="整段原话",
                              category="unparsed"),)
    plan = build_plan("t", [], [], canonical_requirements=reqs)
    assert plan.dispositions[0].status is DispositionStatus.UNPARSED


def test_dispositions_ignore_phantom_requirement_ids():
    """模型引用了不存在的 requirement id → 不污染账本（该 req 仍算 unmapped）。"""
    from paper_agent.tasking.contracts import DispositionStatus

    reqs = (AtomicRequirement(requirement_id="r1", verbatim="润色引言"),)
    raws = [RawOperation(intent=Intent.POLISH, verbatim="润色引言")]
    exts = [_ext({"section_id": _present("sec:intro", req_ids=["r_ghost"])})]
    plan = build_plan("t", raws, exts, canonical_requirements=reqs)
    assert plan.dispositions[0].status is DispositionStatus.UNMAPPED


# --- P0：needs_replan 只重规划未执行步骤，绝不重跑已成功的 ---

def test_build_revision_truncates_to_remaining_operations():
    """重规划的输入是**原始请求**，理解层会重新拆出全部步骤；
    必须按 remaining_operation_ids 截断，否则已成功的 polish 会被再执行一次。
    """
    raws0 = [
        RawOperation(intent=Intent.POLISH, verbatim="润色引言"),
        RawOperation(intent=Intent.ADD_SECTION, verbatim="加 Discussion"),
    ]
    exts0 = [
        _ext({"section_id": _present("sec:intro")}),
        _ext({"title": _present("Discussion"), "purpose": _present("讨论"),
              "structure_kind": _present("desc")}),
    ]
    plan0 = build_plan("turnC", raws0, exts0, canonical_requirements=())
    remaining_id = plan0.operations[1].operation_id      # 只剩 add_section 没做

    # 重规划：理解层基于原始请求又拆出了两个操作
    req = PlanRevisionRequest(previous_plan=plan0, revision_text="原始请求：…",
                              revision_reason=RevisionReason.NEEDS_REPLAN)
    new_plan = build_revision(plan0, req, raws0, exts0, canonical_requirements=(),
                              remaining_operation_ids=[remaining_id])

    assert len(new_plan.operations) == 1                 # 截断到未执行步骤数
    assert new_plan.operations[0].intent is Intent.ADD_SECTION
    assert new_plan.plan_version == 2


def test_build_revision_without_remaining_ids_keeps_all():
    raws = [RawOperation(intent=Intent.POLISH, verbatim="润色引言")]
    exts = [_ext({"section_id": _present("sec:intro")})]
    plan0 = build_plan("t", raws, exts, canonical_requirements=())
    req = PlanRevisionRequest(previous_plan=plan0, revision_text="改改",
                              revision_reason=RevisionReason.USER_MODIFY)
    new_plan = build_revision(plan0, req, raws, exts, canonical_requirements=())
    assert len(new_plan.operations) == 1
