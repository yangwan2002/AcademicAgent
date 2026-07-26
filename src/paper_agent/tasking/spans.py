"""verbatim → 原文 offset 定位（Task 2）。

原则：**不让 LLM 算字符位置**（Unicode/空格/重复文本极易错位）。LLM 只返回精确
`verbatim`，由本模块在原始文本里定位出 `(start, end)`。找不到或出现多个匹配时标
`ambiguous_span`（offset 不可信，但不丢失该 requirement）。

`locate_verbatim` 只做纯字符串定位，零依赖、可测。
"""

from __future__ import annotations

from dataclasses import replace

from paper_agent.tasking.contracts import AtomicRequirement


def locate_verbatim(text: str, verbatim: str) -> tuple[int, int, bool]:
    """在 `text` 中定位 `verbatim`，返回 `(start, end, ambiguous)`。

    - 唯一匹配：`(start, end, False)`，`end` 为结束位置（含尾，即 start+len）。
    - 空 verbatim / 找不到 / 出现多次：`(-1, -1, True)`——offset 不可信。
    """
    if not verbatim:
        return (-1, -1, True)
    first = text.find(verbatim)
    if first == -1:
        return (-1, -1, True)
    # 多匹配 → 无法确定指哪一处，标 ambiguous。
    if text.find(verbatim, first + 1) != -1:
        return (-1, -1, True)
    return (first, first + len(verbatim), False)


def locate_requirement(text: str, requirement: AtomicRequirement) -> AtomicRequirement:
    """据原文为一条 requirement 补全 offset；定位失败标 `ambiguous_span`。

    返回新的 frozen 实例（原实例不变）。
    """
    start, end, ambiguous = locate_verbatim(text, requirement.verbatim)
    return replace(
        requirement,
        source_start=start,
        source_end=end,
        ambiguous_span=ambiguous,
    )


def locate_requirements(
    text: str, requirements: tuple[AtomicRequirement, ...]
) -> tuple[AtomicRequirement, ...]:
    """批量为一组 requirement 定位 offset。"""
    return tuple(locate_requirement(text, r) for r in requirements)
