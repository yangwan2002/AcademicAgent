"""intent-planning Task 5 验收：RuleAnalysis / Fast_Path 判定。

- 单意图无歧义 → unambiguous / is_fast_path
- "先润色再转 Word" → 多候选 + 序列信号 → 非 unambiguous / 非 fast_path
- 无关文本 → 无候选 + 未覆盖文本 → 非 fast_path
- 多候选但无序列信号也不走 fast_path
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.router import Intent
from paper_agent.tasking.rule_analysis import is_fast_path, rule_analyze


def test_single_intent_unambiguous_fast_path():
    ra = rule_analyze("帮我润色引言")
    assert ra.candidates == (Intent.POLISH,)
    assert ra.has_sequence_signal is False
    assert ra.uncovered_text == ()
    assert ra.unambiguous is True
    assert is_fast_path(ra) is True


def test_single_rewrite_unambiguous():
    ra = rule_analyze("改写方法这一节")
    assert ra.candidates == (Intent.REWRITE,)
    assert is_fast_path(ra) is True


def test_multi_intent_with_sequence_not_fast_path():
    ra = rule_analyze("先润色引言，再转成 Word")
    assert Intent.POLISH in ra.candidates
    assert Intent.CONVERT_FORMAT in ra.candidates
    assert ra.has_sequence_signal is True
    assert ra.unambiguous is False
    assert is_fast_path(ra) is False


def test_multi_candidate_without_sequence_still_not_fast_path():
    # "润色并改写"：两候选、无 "并且" 等序列词，但候选数>1 仍不走 fast_path
    ra = rule_analyze("润色并改写这段")
    assert len(ra.candidates) >= 2
    assert is_fast_path(ra) is False


def test_unrelated_text_has_uncovered_and_not_fast_path():
    ra = rule_analyze("今天天气怎么样")
    assert ra.candidates == ()
    assert ra.uncovered_text != ()
    assert ra.unambiguous is False
    assert is_fast_path(ra) is False


def test_matched_signals_recorded():
    ra = rule_analyze("润色引言")
    assert "polish" in ra.matched_signals
    assert "润色" in ra.matched_signals["polish"]


def test_convert_by_target_format_word():
    ra = rule_analyze("转成 docx")
    assert Intent.CONVERT_FORMAT in ra.candidates


def test_sequence_signal_english():
    ra = rule_analyze("polish intro and then convert to docx")
    assert ra.has_sequence_signal is True
    assert is_fast_path(ra) is False
