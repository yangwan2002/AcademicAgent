"""只读问答循环的测试（P-4 第一步）。

重点不是覆盖率，是钉住三条：
1. **只读保证**：跑完整循环，session 的写入方法零调用（红线的可执行形态）
2. **护栏**：超限是失败终态，绝不报 ANSWERED
3. **截断可见**：超长正文必须带截断标注，不能静默截
"""

from __future__ import annotations

import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_agent.application.ask_context import (
    ASK_TOOLS,
    READ_SECTION_MAX_CHARS,
    AskContext,
    ToolDispatchError,
    capture,
    dispatch,
)
from paper_agent.application.ask_loop import (
    AskStopReason,
    run_ask,
)
from paper_agent.providers.llm.base import LLMResponse, ToolCall

_DOC = """# 1 引言
空地协同的视觉关联需求日益突出 [1]。

# 2 方法
本文提出的方法基于跨视角匹配。

# 3 实验
"""


class _FakeSession:
    """最小 session：只提供 read_working / ext，并记录是否被写过。"""

    def __init__(self, text: str = _DOC, ext: str = "md") -> None:
        self._text = text
        self.ext = ext
        self.writes: list = []

    def read_working(self) -> str:
        return self._text

    # 下面三个都是"不该被调用"的写入面。
    def write_working(self, *a, **k):
        self.writes.append("write_working")

    def save_to_source(self, *a, **k):
        self.writes.append("save_to_source")

    def commit(self, *a, **k):
        self.writes.append("commit")


class _ScriptedLLM:
    """按脚本依次返回响应；记录每次收到的 messages 与 tools。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.seen_tools = []

    def complete(self, messages, **opts):
        self.calls += 1
        self.seen_tools.append(opts.get("tools"))
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="（脚本用尽）")


def _tool_resp(name, args=None, cid="c1"):
    return LLMResponse(
        content="", tool_calls=[ToolCall(id=cid, name=name, arguments=args or {})]
    )


# ---- 1. 只读保证 ----


def test_loop_never_writes_to_session():
    """跑完带工具调用的完整循环，session 写入面零调用。"""
    session = _FakeSession()
    ctx = capture(session)
    llm = _ScriptedLLM([
        _tool_resp("list_sections"),
        _tool_resp("read_section", {"section_id": "1_引言"}, cid="c2"),
        LLMResponse(content="引言提到了空地协同的需求。"),
    ])

    res = run_ask("引言写了什么", ctx, llm)

    assert res.ok
    assert session.writes == []


def test_context_does_not_hold_session():
    """AskContext 不持有 session —— 想写也没有对象可写。"""
    ctx = capture(_FakeSession())
    for value in vars(ctx).values():
        assert not hasattr(value, "write_working")
    assert not hasattr(ctx, "session")


def test_context_is_frozen():
    ctx = capture(_FakeSession())
    # 断具体的 FrozenInstanceError 而非宽 Exception：后者连 AttributeError（字段名写错）
    # 也会算通过，等于这个测试对"真的冻住了吗"不再有判别力。
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.total_chars = 999  # type: ignore[misc]


# ---- 2. 护栏 ----


def test_iteration_limit_is_not_answered():
    """一直要工具 → 撞上限 → ITERATION_LIMIT，且 ok 为假。"""
    ctx = capture(_FakeSession())
    llm = _ScriptedLLM([_tool_resp("list_sections") for _ in range(20)])

    res = run_ask("随便问", ctx, llm, max_iterations=4, max_llm_calls=99)

    assert res.stop_reason is AskStopReason.ITERATION_LIMIT
    assert not res.ok
    assert res.iterations == 4
    assert llm.calls == 4


def test_iteration_limit_display_says_not_complete():
    """超限的展示文本必须说明这不是完整结论。"""
    ctx = capture(_FakeSession())
    llm = _ScriptedLLM([_tool_resp("list_sections") for _ in range(20)])
    res = run_ask("q", ctx, llm, max_iterations=2)
    assert "不是完整结论" in res.display()


def test_budget_exhausted_stops_before_spending():
    """预算为 0 时一次都不该调 LLM。"""
    ctx = capture(_FakeSession())
    llm = _ScriptedLLM([LLMResponse(content="x")])

    res = run_ask("q", ctx, llm, max_llm_calls=0)

    assert res.stop_reason is AskStopReason.BUDGET_EXHAUSTED
    assert not res.ok
    assert llm.calls == 0


def test_provider_failure_is_classified_not_swallowed():
    class _Boom:
        def complete(self, messages, **opts):
            raise RuntimeError("网关炸了")

    res = run_ask("q", capture(_FakeSession()), _Boom())

    assert res.stop_reason is AskStopReason.PROVIDER_FAILED
    assert not res.ok
    assert "网关炸了" in (res.error or "")


def test_cancel_token_stops_loop():
    class _Cancelled:
        cancelled = True

    llm = _ScriptedLLM([LLMResponse(content="x")])
    res = run_ask("q", capture(_FakeSession()), llm, cancel_token=_Cancelled())

    assert res.stop_reason is AskStopReason.CANCELLED
    assert llm.calls == 0


def test_answered_on_first_pass_without_tools():
    llm = _ScriptedLLM([LLMResponse(content="直接答")])
    res = run_ask("q", capture(_FakeSession()), llm)
    assert res.stop_reason is AskStopReason.ANSWERED
    assert res.answer == "直接答"
    assert res.iterations == 1


# ---- 3. 工具行为 ----


def test_read_section_truncation_is_visible():
    """超长正文必须带截断标注，且标注里有全长。"""
    long_body = "字" * (READ_SECTION_MAX_CHARS + 500)
    ctx = AskContext(ext="md", bodies={"s1": long_body})

    out = dispatch(ctx, "read_section", {"section_id": "s1"})

    assert "已截断" in out
    assert str(len(long_body)) in out
    assert len(out) < len(long_body) + 200


def test_read_section_short_body_not_truncated():
    ctx = AskContext(ext="md", bodies={"s1": "很短"})
    assert dispatch(ctx, "read_section", {"section_id": "s1"}) == "很短"


def test_read_section_unknown_id_lists_available():
    """拒绝里要带上正确做法：报错含可用 id。"""
    ctx = capture(_FakeSession())
    with pytest.raises(ToolDispatchError) as exc:
        dispatch(ctx, "read_section", {"section_id": "不存在"})
    assert "1_引言" in str(exc.value)


def test_read_section_rejects_bad_args():
    ctx = capture(_FakeSession())
    with pytest.raises(ToolDispatchError):
        dispatch(ctx, "read_section", {})
    with pytest.raises(ToolDispatchError):
        dispatch(ctx, "read_section", {"section_id": 123})


def test_unknown_tool_raises():
    with pytest.raises(ToolDispatchError):
        dispatch(capture(_FakeSession()), "delete_everything", {})


def test_list_sections_has_no_body_text():
    """章节清单不能泄漏正文 —— 这是"最小上下文"的关键一条。"""
    ctx = capture(_FakeSession())
    out = dispatch(ctx, "list_sections", {})
    assert "1_引言" in out
    assert "空地协同" not in out


def test_empty_section_reported_as_empty():
    ctx = capture(_FakeSession())
    assert "空的" in dispatch(ctx, "read_section", {"section_id": "3_实验"})


def test_dispatch_error_is_fed_back_not_fatal():
    """工具调错不是终态：错误回灌后模型能继续。"""
    ctx = capture(_FakeSession())
    llm = _ScriptedLLM([
        _tool_resp("read_section", {"section_id": "没有这章"}),
        LLMResponse(content="改用清单回答"),
    ])
    res = run_ask("q", ctx, llm)
    assert res.ok
    assert llm.calls == 2


# ---- 4. 快照与摘要 ----


def test_capture_counts_chars_per_section():
    ctx = capture(_FakeSession())
    by_id = {s.id: s for s in ctx.sections}
    assert by_id["3_实验"].chars == 0
    assert by_id["1_引言"].chars > 0


def test_facts_digest_is_compact_and_has_no_body():
    """事实摘要要小：结构与计数，不含正文。"""
    ctx = capture(_FakeSession())
    digest = ctx.facts_digest()
    # 口径是"正文章节"——参考文献/致谢/附录不算（见 test_ask_counts_history.py）。
    assert "3 个正文章节" in digest
    assert "空地协同" not in digest
    assert len(digest) < 400


def test_capture_handles_missing_sections():
    ctx = capture(_FakeSession(text="", ext="docx"))
    assert ctx.sections == ()
    assert "没有可识别的章节" in dispatch(ctx, "list_sections", {})


def test_capture_survives_read_failure():
    class _Broken(_FakeSession):
        def read_working(self):
            raise OSError("读不了")

    ctx = capture(_Broken())
    assert ctx.sections == ()


# ---- 5. 工具 schema 协议合法性（本地快检，全量遍历在 test_tool_schema_protocol）----


def test_ask_tools_are_protocol_valid():
    for t in ASK_TOOLS:
        assert t["type"] == "function"
        assert t["function"]["name"]
        assert t["function"]["parameters"]["type"] == "object"


def test_loop_passes_tools_to_llm():
    llm = _ScriptedLLM([LLMResponse(content="答")])
    run_ask("q", capture(_FakeSession()), llm)
    assert llm.seen_tools[0] is not None
    assert len(llm.seen_tools[0]) == len(ASK_TOOLS)
