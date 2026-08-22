"""streaming-and-trace Task 6 验收：ConsoleRenderer 流式渲染。

对应 tasks.md Task 6 / design §6 / Req 2.2, 3.4, 3.5, 5.1, 5.3, 4.4, 6.x：
- 候选标识；thinking 不现原文；违规呈现；候选作废提示；ANSI 被转义
"""

from __future__ import annotations

import io
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.observability.events import Event, EventKind
from paper_agent.observability.renderer import (
    ConsoleRenderer,
    escape_terminal_controls,
)


def _render(events, **kw) -> str:
    buf = io.StringIO()
    r = ConsoleRenderer(stream=buf, **kw)
    for e in events:
        r.emit(e)
    return buf.getvalue()


def _ev(kind, llm_call_id="", attempt=None, **data) -> Event:
    return Event(kind=kind, data=data, llm_call_id=llm_call_id, attempt=attempt)


# --- 候选标识（Req 2.2）---

def test_delta_shows_candidate_marker_before_first_chunk():
    out = _render(
        [
            _ev(EventKind.LLM_DELTA, llm_call_id="c1", text="Hello "),
            _ev(EventKind.LLM_DELTA, llm_call_id="c1", text="world"),
        ]
    )
    assert "[候选·校验中…]" in out
    # 标识只在首个增量前出现一次
    assert out.count("[候选·校验中…]") == 1
    assert "Hello world" in out


def test_delta_marker_per_call():
    out = _render(
        [
            _ev(EventKind.LLM_DELTA, llm_call_id="c1", text="a"),
            _ev(EventKind.LLM_DELTA, llm_call_id="c2", text="b"),
        ]
    )
    assert out.count("[候选·校验中…]") == 2


# --- thinking 不现原文（Req 5.1）---

def test_thinking_never_shows_raw_text_by_default():
    secret = "模型的私密推理链路不应出现"
    out = _render([_ev(EventKind.LLM_THINKING, llm_call_id="c1", text=secret)])
    assert secret not in out
    # 默认不开启：连提示也不显示
    assert out == ""


def test_thinking_shows_only_notice_when_enabled():
    secret = "私密推理"
    out = _render(
        [
            _ev(EventKind.LLM_THINKING, llm_call_id="c1", text=secret),
            _ev(EventKind.LLM_THINKING, llm_call_id="c1", text=secret),
        ],
        show_thinking=True,
    )
    assert secret not in out
    assert "[正在分析…]" in out
    # 每个 call 只提示一次
    assert out.count("[正在分析…]") == 1


# --- 违规呈现（Req 3.4）---

def test_gate_fail_shows_violations():
    out = _render(
        [
            _ev(
                EventKind.GATE_RESULT,
                passed=False,
                violations=["数字被改：0.87→0.99", "引用被删：cite{a}"],
            )
        ]
    )
    assert "✗" in out
    assert "数字被改：0.87→0.99" in out
    assert "引用被删" in out


def test_gate_pass_shows_check():
    out = _render([_ev(EventKind.GATE_RESULT, passed=True)])
    assert "✓" in out


# --- 新候选分区 + 重试原因（Req 3.5）---

def test_attempt_start_separates_and_retry_reason():
    out = _render(
        [
            _ev(EventKind.GENERATION_ATTEMPT_START, attempt=2),
            _ev(EventKind.GATE_RETRY, attempt=2),
        ]
    )
    assert "─" in out  # 分隔线
    assert "第 2 次尝试" in out


# --- 候选作废/取消提示（Req 4.4）---

def test_attempt_end_rejected_shows_discarded():
    out = _render([_ev(EventKind.GENERATION_ATTEMPT_END, status="rejected")])
    assert "该候选已作废" in out


def test_attempt_end_accepted_no_discard_notice():
    out = _render([_ev(EventKind.GENERATION_ATTEMPT_END, status="accepted")])
    assert "该候选已作废" not in out


def test_run_cancelled_shows_notice():
    out = _render([_ev(EventKind.RUN_CANCELLED, phase="generation")])
    assert "已取消" in out


# --- 非 LLM 步骤状态 ---

def test_retrieval_status_line():
    out = _render([_ev(EventKind.RETRIEVAL_END, count=5)])
    assert "5" in out


# --- ANSI / 控制符转义（防伪造）---

def test_escape_strips_ansi_and_cr():
    raw = "\x1b[32m✓ 已通过\x1b[0m\r覆盖行\x07"
    cleaned = escape_terminal_controls(raw)
    assert "\x1b" not in cleaned
    assert "\r" not in cleaned
    assert "\x07" not in cleaned
    assert "已通过" in cleaned  # 文本内容保留，只是失去伪造格式能力


def test_delta_content_is_escaped():
    out = _render(
        [_ev(EventKind.LLM_DELTA, llm_call_id="c1", text="\x1b[2J清屏攻击")]
    )
    assert "\x1b" not in out
    assert "清屏攻击" in out


def test_escape_preserves_newline_tab_unicode():
    assert escape_terminal_controls("行1\n行2\t列\u4e2d文") == "行1\n行2\t列\u4e2d文"
