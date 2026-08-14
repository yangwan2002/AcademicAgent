"""规则分析（Task 5）：判定一条消息能否走 Fast_Path。

现有 `Router.route()` 只按优先级返回**一个** Intent（"先润色再转 Word" 会被截断成
convert_format），所以不能仅凭「返回了一个 intent」就当无歧义。这里改为**扫描全部
候选意图**，并检测序列连接词与未覆盖文本，产出 `Rule_Analysis`。

Fast_Path 条件（全满足才走）：恰一个候选 + 无序列信号 + 无未覆盖文本。
（«目标动作是否清晰» 由后续 PreExtractor 的必填槽位校验兜底，此处不重复判定。）

只复用 router 的关键词表，零 LLM、零副作用。
"""

from __future__ import annotations

import re

from paper_agent.intents.router import (
    Intent,
    _ADD_BATCH_KW,
    _ADD_SECTION_KW,
    _CONVERT_KW,
    _FORMAT_KW,
    _FORMAT_OP_KW,
    _POLISH_KW,
    _REWRITE_KW,
)
from paper_agent.tasking.contracts import Rule_Analysis

# 表示「多件事」的序列连接词——出现即倾向多操作、不走 Fast_Path。
_SEQUENCE_SIGNALS = [
    "然后", "再", "接着", "并且", "同时", "以及", "还要", "顺便", "之后",
    "先", "最后", "另外",
    "then", "and then", "also", "after that", "next",
]

# 意图 → 关键词列表（复用 router 的表；convert 额外看目标格式词）。
_INTENT_KEYWORDS: list[tuple[Intent, list[str]]] = [
    (Intent.ADD_SECTION_BATCH, _ADD_BATCH_KW),
    (Intent.ADD_SECTION, _ADD_SECTION_KW),
    (Intent.CONVERT_FORMAT, _CONVERT_KW + list(_FORMAT_KW.keys())),
    (Intent.FORMAT_DOCX, _FORMAT_OP_KW),
    (Intent.POLISH, _POLISH_KW),
    (Intent.REWRITE, _REWRITE_KW),
]

_TOKEN = re.compile(r"[A-Za-z]+|[\u4e00-\u9fff]")


def _matched(low: str, keywords: list[str]) -> list[str]:
    return [kw for kw in keywords if kw.lower() in low]


def rule_analyze(text: str) -> Rule_Analysis:
    """扫描全部候选意图 + 序列信号 + 未覆盖文本，产出 Rule_Analysis。"""
    low = text.lower()

    candidates: list[Intent] = []
    matched_signals: dict[str, tuple[str, ...]] = {}
    for intent, keywords in _INTENT_KEYWORDS:
        hits = _matched(low, keywords)
        if hits:
            candidates.append(intent)
            matched_signals[intent.value] = tuple(hits)

    has_sequence = any(sig.lower() in low for sig in _SEQUENCE_SIGNALS)

    # 未覆盖文本：整条消息里既没命中任何意图关键词、也没命中序列信号时视为「有未覆盖」。
    # 用一个粗粒度信号：若没有任何候选，则整句都算未覆盖。
    uncovered: tuple[str, ...] = () if candidates else (text.strip(),)

    unambiguous = (
        len(candidates) == 1
        and not has_sequence
        and not uncovered
    )

    return Rule_Analysis(
        candidates=tuple(candidates),
        matched_signals=matched_signals,
        has_sequence_signal=has_sequence,
        uncovered_text=uncovered,
        unambiguous=unambiguous,
    )


def is_fast_path(analysis: Rule_Analysis) -> bool:
    """Fast_Path 判定：恰一候选 + 无序列信号 + 无未覆盖文本。"""
    return (
        len(analysis.candidates) == 1
        and not analysis.has_sequence_signal
        and not analysis.uncovered_text
    )


__all__ = ["rule_analyze", "is_fast_path"]
