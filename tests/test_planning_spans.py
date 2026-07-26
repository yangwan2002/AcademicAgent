"""intent-planning Task 2 验收：verbatim offset 定位。

- 中文/空格/重复文本定位正确
- 多匹配 / 未命中 → ambiguous_span
- 定位结果回填 AtomicRequirement
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.tasking.contracts import AtomicRequirement
from paper_agent.tasking.spans import (
    locate_requirement,
    locate_requirements,
    locate_verbatim,
)


# --- locate_verbatim ---

def test_unique_match_chinese():
    text = "请润色引言，不要修改实验数字，再转成 docx。"
    start, end, ambiguous = locate_verbatim(text, "不要修改实验数字")
    assert ambiguous is False
    assert text[start:end] == "不要修改实验数字"


def test_match_with_spaces():
    text = "convert to  double column word"
    start, end, ambiguous = locate_verbatim(text, "double column")
    assert ambiguous is False
    assert text[start:end] == "double column"


def test_not_found_is_ambiguous():
    start, end, ambiguous = locate_verbatim("润色引言", "转成 docx")
    assert (start, end, ambiguous) == (-1, -1, True)


def test_multiple_match_is_ambiguous():
    # "润色" 出现两次 → 无法确定指哪处
    text = "先润色引言，再润色结论"
    start, end, ambiguous = locate_verbatim(text, "润色")
    assert ambiguous is True
    assert (start, end) == (-1, -1)


def test_empty_verbatim_is_ambiguous():
    assert locate_verbatim("任意文本", "") == (-1, -1, True)


def test_offset_is_exact_including_repeats_elsewhere():
    text = "abc 目标段落 xyz 目标段落"
    # verbatim 唯一（带上下文）时能精确定位
    start, end, ambiguous = locate_verbatim(text, "abc 目标段落")
    assert ambiguous is False and text[start:end] == "abc 目标段落"


# --- locate_requirement 回填 ---

def test_locate_requirement_fills_offset():
    text = "请润色引言，不要修改实验数字。"
    req = AtomicRequirement(requirement_id="r1", verbatim="不要修改实验数字")
    out = locate_requirement(text, req)
    assert out.ambiguous_span is False
    assert text[out.source_start:out.source_end] == "不要修改实验数字"
    # 原实例不变（frozen 返回新对象）
    assert req.source_start == -1


def test_locate_requirement_marks_ambiguous_on_miss():
    req = AtomicRequirement(requirement_id="r1", verbatim="不存在的要求")
    out = locate_requirement("润色引言", req)
    assert out.ambiguous_span is True
    assert (out.source_start, out.source_end) == (-1, -1)


def test_locate_requirements_batch():
    text = "润色引言，保留引用"
    reqs = (
        AtomicRequirement(requirement_id="r1", verbatim="润色引言"),
        AtomicRequirement(requirement_id="r2", verbatim="保留引用"),
        AtomicRequirement(requirement_id="r3", verbatim="没提到的"),
    )
    out = locate_requirements(text, reqs)
    assert out[0].ambiguous_span is False
    assert out[1].ambiguous_span is False
    assert out[2].ambiguous_span is True
