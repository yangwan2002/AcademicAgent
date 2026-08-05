"""格式无章节索引时的归因正确性。

背景（真机 2026-08-01）：用户把 `.docx` 骨架喂给 chat.py 后请求改写引言，
得到 `rejected(section_not_found: 计划期找不到要修改的章节：引言部分)`。
诊断把**根因**（docx 解析不出章节）说成了**表象**（章节名不存在），
把用户引向「章节名是不是写错了」这条错误的排查路线。

本文件锁住三件事：
1. 不支持章节的格式上，章节类意图报 `format_lacks_sections`，**不再**报 section_not_found
2. 支持章节的格式行为逐字不变（不得因此改动而误伤 tex/md/docx）
3. `SECTIONABLE_EXTS` 是唯一真相源 —— chat.py 与 SectionIndex 不得各持一份

**docx 已改为支持章节**（就地编辑，见 test_docx_inplace.py）：working copy 是 docx
副本，标题按段落样式投影出 `#` 前缀，章节定位与 md 同一套。因此本文件里"不支持章节
的格式"改用 pdf/odt 举例 —— 这条守卫本身仍有价值，只是 docx 不再属于它管的范围。
"""

from __future__ import annotations

import pytest

from paper_agent.intents.router import Intent
from paper_agent.session.section_index import (
    SECTIONABLE_EXTS,
    SectionIndex,
    supports_sections,
)
from paper_agent.tasking.contracts import (
    FieldStatus,
    Operation,
    SlotValue,
    Task_Plan,
)
from paper_agent.tasking.plan_validator import (
    FORMAT_LACKS_SECTIONS,
    SECTION_NOT_FOUND,
    validate_plan,
)


def _op(intent: Intent, oid: str = "op1", **slots) -> Operation:
    return Operation(
        operation_id=oid,
        intent=intent,
        slots={
            k: SlotValue(value=v, field_status=FieldStatus.PRESENT)
            for k, v in slots.items()
        },
    )


def _plan(*ops: Operation) -> Task_Plan:
    return Task_Plan(plan_id="p1", source_turn_id="t1", operations=tuple(ops))


# ---------------------------------------------------------------- 真相源


def test_sectionable_exts_is_single_source_of_truth():
    assert SECTIONABLE_EXTS == frozenset({"tex", "md", "markdown", "docx"})
    # chat.py 必须复用同一对象，不得另立一份集合
    import scripts.chat as chat_mod

    assert chat_mod._TEXT_EXT is SECTIONABLE_EXTS


@pytest.mark.parametrize("ext", ["tex", "md", "markdown", ".TEX", "MD",
                                 "docx", ".DOCX"])
def test_supports_sections_true(ext):
    assert supports_sections(ext) is True


@pytest.mark.parametrize("ext", ["pdf", "doc", "odt", "", None])
def test_supports_sections_false(ext):
    assert supports_sections(ext) is False


def test_index_property_distinguishes_two_kinds_of_empty():
    """空 sections 有两种成因，property 必须能区分。"""
    # 格式不支持
    assert SectionIndex("", "pdf").supports_sections is False
    # 格式支持但文档确实没有标题
    empty_md = SectionIndex("just a paragraph, no headings", "md")
    assert empty_md.supports_sections is True
    assert empty_md.list() == []


# ---------------------------------------------------- 归因：docx 上的章节意图


@pytest.mark.parametrize(
    "intent, slots",
    [
        (Intent.REWRITE, {"section_id": "引言部分"}),
        (Intent.POLISH, {"section_id": "引言"}),
        (Intent.ADD_SECTION, {"title": "结论"}),
    ],
)
def test_unsupported_format_reports_format_not_missing_section(intent, slots):
    """真机场景的一般形态：不支持章节的格式 + 章节意图 → 报格式，不报章节缺失。

    当初触发它的是 docx；docx 现已支持章节，故改用 pdf 守同一条归因逻辑。
    """
    outcome = validate_plan(
        _plan(_op(intent, **slots)), current_sections=(), current_format="pdf"
    )
    codes = [i.code for i in outcome.issues]
    assert FORMAT_LACKS_SECTIONS in codes
    # 关键断言：不得叠加误导性的次生诊断
    assert SECTION_NOT_FOUND not in codes
    assert outcome.result == "rejected"


def test_message_points_at_format_and_names_the_way_out():
    outcome = validate_plan(
        _plan(_op(Intent.REWRITE, section_id="引言部分")),
        current_sections=(), current_format="pdf",
    )
    msg = next(i.message for i in outcome.issues if i.code == FORMAT_LACKS_SECTIONS)
    assert ".pdf" in msg           # 说清是哪个格式
    assert "rewrite" in msg        # 说清是哪个操作
    assert ".md" in msg            # 给出出路
    assert "章节" in msg


def test_ext_is_normalized_in_message():
    outcome = validate_plan(
        _plan(_op(Intent.REWRITE, section_id="x")),
        current_sections=(), current_format=".PDF",
    )
    msg = next(i.message for i in outcome.issues if i.code == FORMAT_LACKS_SECTIONS)
    assert ".pdf" in msg


# -------------------------------------------------------- 不误伤既有行为


def test_supported_format_missing_section_still_reports_section_not_found():
    """tex 上章节真的不存在时，仍应报 section_not_found（原行为不变）。"""
    outcome = validate_plan(
        _plan(_op(Intent.REWRITE, section_id="不存在的章节")),
        current_sections=("引言",), current_format="tex",
    )
    codes = [i.code for i in outcome.issues]
    assert SECTION_NOT_FOUND in codes
    assert FORMAT_LACKS_SECTIONS not in codes


def test_supported_format_existing_section_is_valid():
    outcome = validate_plan(
        _plan(_op(Intent.REWRITE, section_id="引言")),
        current_sections=("引言",), current_format="md",
    )
    assert [i.code for i in outcome.issues] == []


def test_unknown_format_skips_the_check():
    """`current_format` 为空 = 未知 → 沿用既有「相关检查跳过」语义，不得误报。"""
    outcome = validate_plan(
        _plan(_op(Intent.REWRITE, section_id="引言")),
        current_sections=("引言",), current_format="",
    )
    assert FORMAT_LACKS_SECTIONS not in [i.code for i in outcome.issues]


def test_docx_non_section_intents_unaffected():
    """format_docx 在 docx 上是**正当**操作，不得被这条检查波及。"""
    outcome = validate_plan(
        _plan(_op(Intent.FORMAT_DOCX)), current_sections=(), current_format="docx"
    )
    assert FORMAT_LACKS_SECTIONS not in [i.code for i in outcome.issues]


def test_docx_section_intents_now_allowed():
    """docx 上章节意图必须放行 —— 就地编辑之后它不再是"出路问题"。

    这条与文件开头那批用例互为镜像：守卫仍在，但 docx 已移出它的管辖范围。
    章节存在时不得报 format_lacks_sections，也不得报 section_not_found。
    """
    outcome = validate_plan(
        _plan(_op(Intent.REWRITE, section_id="引言")),
        current_sections=("引言",), current_format="docx",
    )
    codes = [i.code for i in outcome.issues]
    assert FORMAT_LACKS_SECTIONS not in codes
    assert SECTION_NOT_FOUND not in codes


def test_docx_convert_format_unaffected():
    """convert_format 仍应放行（用户可能确实想转格式）。"""
    outcome = validate_plan(
        _plan(_op(Intent.CONVERT_FORMAT, target_ext="md")),
        current_sections=(), current_format="docx",
    )
    assert FORMAT_LACKS_SECTIONS not in [i.code for i in outcome.issues]


def test_each_offending_op_reported_once():
    """多个章节意图 → 每个各报一次，便于用户看到全部受影响操作。"""
    outcome = validate_plan(
        _plan(
            _op(Intent.REWRITE, "op1", section_id="引言"),
            _op(Intent.ADD_SECTION, "op2", title="结论"),
        ),
        current_sections=(), current_format="pdf",
    )
    hits = [i for i in outcome.issues if i.code == FORMAT_LACKS_SECTIONS]
    assert {i.operation_id for i in hits} == {"op1", "op2"}
