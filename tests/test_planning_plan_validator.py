"""intent-planning Task 14 验收：Static_Plan_Validator。

- 投影章节：先加再改不误拒；改不存在章节计划期即拦
- add_section 重复已存在章节 → section_exists
- convert 终止性：convert 后有 op → 不可执行；同格式 → 拒
- format_docx 需 docx；format 冲突标记
- 必填缺口 → needs_input；全清 → ready
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.intent import Intent
from paper_agent.tasking.contracts import (
    CallStatus,
    ExtractionResult,
    FieldStatus,
    RawOperation,
    SlotSource,
    SlotValue,
)
from paper_agent.tasking.plan_builder import build_plan
from paper_agent.tasking.plan_validator import (
    CONFLICTING_CONSTRAINTS,
    CONVERT_NOT_TERMINAL,
    CONVERT_SAME_FORMAT,
    FORMAT_REQUIRES_DOCX,
    SECTION_EXISTS,
    SECTION_NOT_FOUND,
    validate_plan,
)


def _present(value):
    return SlotValue(value=value, field_status=FieldStatus.PRESENT, source=SlotSource.USER_EXPLICIT)


def _ext(slots):
    return ExtractionResult(call_status=CallStatus.SUCCESS, slots=slots)


def _plan(pairs, **kw):
    """pairs: list[(RawOperation, slots dict)] → Task_Plan。"""
    raws = [p[0] for p in pairs]
    exts = [_ext(p[1]) for p in pairs]
    return build_plan("t", raws, exts, canonical_requirements=(), **kw)


def _codes(outcome):
    return {i.code for i in outcome.issues}


# --- 投影章节 ---

def test_edit_existing_section_ok():
    plan = _plan([(RawOperation(Intent.POLISH, "润色引言"),
                   {"section_id": _present("introduction")})])
    out = validate_plan(plan, current_sections=["introduction", "conclusion"],
                        current_format="tex")
    assert out.result == "ready"


def test_edit_nonexistent_section_rejected_at_plan_time():
    plan = _plan([(RawOperation(Intent.POLISH, "润色 FooBar"),
                   {"section_id": _present("FooBar")})])
    out = validate_plan(plan, current_sections=["introduction"], current_format="tex")
    assert out.result == "rejected"
    assert SECTION_NOT_FOUND in _codes(out)


def test_add_then_edit_not_falsely_rejected():
    # 先加 Discussion，再润色 Discussion —— 投影集里前序已创建，不应误拒
    plan = _plan([
        (RawOperation(Intent.ADD_SECTION, "加 Discussion"),
         {"title": _present("Discussion"), "purpose": _present("讨论"),
          "structure_kind": _present("desc")}),
        (RawOperation(Intent.POLISH, "润色 Discussion"),
         {"section_id": _present("Discussion")}),
    ])
    out = validate_plan(plan, current_sections=["introduction"], current_format="tex")
    assert out.result == "ready"
    assert SECTION_NOT_FOUND not in _codes(out)


def test_add_section_duplicate_existing_rejected():
    plan = _plan([(RawOperation(Intent.ADD_SECTION, "加 Introduction"),
                   {"title": _present("Introduction"), "purpose": _present("引入"),
                    "structure_kind": _present("desc")})])
    out = validate_plan(plan, current_sections=["introduction"], current_format="tex")
    assert out.result == "rejected"
    assert SECTION_EXISTS in _codes(out)


def test_add_section_position_ref_missing_rejected():
    plan = _plan([(RawOperation(Intent.ADD_SECTION, "加 Discussion 放 Ghost 前"),
                   {"title": _present("Discussion"), "purpose": _present("讨论"),
                    "structure_kind": _present("desc"),
                    "position": _present("before:ghost")})])
    out = validate_plan(plan, current_sections=["introduction"], current_format="tex")
    assert out.result == "rejected"
    assert SECTION_NOT_FOUND in _codes(out)


def test_add_section_position_ref_to_existing_ok():
    plan = _plan([(RawOperation(Intent.ADD_SECTION, "加 Discussion 放 Conclusion 前"),
                   {"title": _present("Discussion"), "purpose": _present("讨论"),
                    "structure_kind": _present("desc"),
                    "position": _present("before:conclusion")})])
    out = validate_plan(plan, current_sections=["introduction", "conclusion"],
                        current_format="tex")
    assert out.result == "ready"


# --- convert 终止性 ---

def test_convert_not_terminal_rejected():
    plan = _plan([
        (RawOperation(Intent.CONVERT_FORMAT, "转 docx"), {"target_ext": _present("docx")}),
        (RawOperation(Intent.POLISH, "润色引言"), {"section_id": _present("introduction")}),
    ])
    out = validate_plan(plan, current_sections=["introduction"], current_format="tex")
    assert out.result == "rejected"
    assert CONVERT_NOT_TERMINAL in _codes(out)


def test_convert_as_last_op_ok():
    plan = _plan([
        (RawOperation(Intent.POLISH, "润色引言"), {"section_id": _present("introduction")}),
        (RawOperation(Intent.CONVERT_FORMAT, "转 docx"), {"target_ext": _present("docx")}),
    ])
    out = validate_plan(plan, current_sections=["introduction"], current_format="tex")
    assert out.result == "ready"


def test_convert_same_format_rejected():
    plan = _plan([(RawOperation(Intent.CONVERT_FORMAT, "转 tex"),
                   {"target_ext": _present("tex")})])
    out = validate_plan(plan, current_sections=[], current_format="tex")
    assert out.result == "rejected"
    assert CONVERT_SAME_FORMAT in _codes(out)


# --- format_docx ---

def test_format_docx_requires_docx():
    plan = _plan([(RawOperation(Intent.FORMAT_DOCX, "正文宋体"),
                   {"operations": SlotValue(value=[], field_status=FieldStatus.PRESENT,
                                            source=SlotSource.USER_EXPLICIT)})])
    out = validate_plan(plan, current_sections=[], current_format="tex")
    assert out.result == "rejected"
    assert FORMAT_REQUIRES_DOCX in _codes(out)


def test_format_docx_on_docx_ok():
    from paper_agent.tools.docx_format import FormatOp
    ops = [FormatOp("set_font_family", "宋体", "body")]
    plan = _plan([(RawOperation(Intent.FORMAT_DOCX, "正文宋体"),
                   {"operations": _present(ops)})])
    out = validate_plan(plan, current_sections=[], current_format="docx")
    assert out.result == "ready"


def test_format_conflict_flagged():
    from paper_agent.tools.docx_format import FormatOp
    ops = [
        FormatOp("set_columns", 1, "all"),
        FormatOp("set_columns", 2, "all"),   # 冲突：同 name+scope 不同 value
    ]
    plan = _plan([(RawOperation(Intent.FORMAT_DOCX, "单栏又双栏"),
                   {"operations": _present(ops)})])
    out = validate_plan(plan, current_sections=[], current_format="docx")
    assert out.result == "rejected"
    assert CONFLICTING_CONSTRAINTS in _codes(out)


# --- 必填缺口 → needs_input ---

def test_missing_required_field_needs_input():
    # polish 缺 section_id（required+must_ask）→ missing_fields → needs_input
    plan = _plan([(RawOperation(Intent.POLISH, "润色一下"), {})])
    out = validate_plan(plan, current_sections=["introduction"], current_format="tex")
    assert out.result == "needs_input"
    assert any(m.endswith(":section_id") for m in out.missing_fields)


def test_reorder_suggestions_passed_through():
    # add_section 前向引用：PlanBuilder 产 reorder 建议，validator 透传
    plan = _plan([
        (RawOperation(Intent.ADD_SECTION, "加 Discussion 放 Summary 前"),
         {"title": _present("Discussion"), "purpose": _present("讨论"),
          "structure_kind": _present("desc"), "position": _present("before:summary")}),
        (RawOperation(Intent.ADD_SECTION, "加 Summary"),
         {"title": _present("Summary"), "purpose": _present("总结"),
          "structure_kind": _present("desc")}),
    ])
    out = validate_plan(plan, current_sections=["introduction"], current_format="tex")
    assert len(out.reorder_suggestions) == 1
