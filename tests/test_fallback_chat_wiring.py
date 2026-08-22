"""chat.py 的只读 AskLoop 接线。

三件事值得钉在这一层，路由测试（`test_fallback_routing.py`）覆盖不到：

1. **护栏上限来自 config**，不是写死在调用点。
2. **提问不触达写入面** —— spy 掉 `write_working` / `save_to_source`，断言零调用。
   这是 P-4 红线的可执行形态。
3. 自动只读交接由 Planning 的零操作出口触发，不再依赖关键词 Router。
"""

from __future__ import annotations

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.application.ask_loop import AskResult, AskStopReason


def _chat_module():
    path = os.path.join(_ROOT, "scripts", "chat.py")
    spec = importlib.util.spec_from_file_location("chat_ask_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _SpySession:
    """只读快照能捕获、写入必炸。

    `capture()` 需要的读方法照常提供；任何写方法被调用就 `AssertionError` ——
    比"事后断言零调用"更早失败，栈也更清楚。
    """

    def __init__(self, text="# 1 引言\n\n内容。\n"):
        self._text = text
        self.working_path = "working.md"
        self.source_path = "source.md"

    def read_working(self):
        return self._text

    def history(self, limit=20):
        return []

    # ---- 写入面：一律炸 ----
    def write_working(self, *a, **k):
        raise AssertionError("只读问答调用了 write_working")

    def save_to_source(self, *a, **k):
        raise AssertionError("只读问答调用了 save_to_source")


class _StubLLM:
    """不请求工具，直接给答案 —— 循环一轮结束。"""

    def __init__(self):
        self.calls = 0

    def complete(self, messages, **kw):
        self.calls += 1
        from paper_agent.providers.llm.base import LLMResponse
        return LLMResponse(content="共 1 章。", tool_calls=[])


class _Cfg:
    agent_loop_max_iterations = 3
    agent_loop_max_llm_calls = 4


def test_chat_exposes_ask_wiring():
    mod = _chat_module()
    assert hasattr(mod, "_run_ask_command")


def test_ask_never_touches_write_surface(capsys):
    """红线：跑完整条问答，写入面零调用。"""
    mod = _chat_module()
    mod._run_ask_command("这篇论文有几章", _SpySession(), _StubLLM(), _Cfg())
    out = capsys.readouterr().out
    assert "共 1 章" in out


def test_guardrails_come_from_config(monkeypatch):
    """config 的上限要真的传进 run_ask，不是用函数默认值。"""
    mod = _chat_module()
    seen = {}

    def _fake_run_ask(question, ctx, llm, **kw):
        seen.update(kw)
        return AskResult(
            answer="ok", stop_reason=AskStopReason.ANSWERED,
            tool_calls=[], llm_calls=1,
        )

    monkeypatch.setattr(
        "paper_agent.application.ask_loop.run_ask", _fake_run_ask, raising=True
    )
    mod._run_ask_command("几章", _SpySession(), _StubLLM(), _Cfg())
    assert seen == {"max_iterations": 3, "max_llm_calls": 4}


def test_no_config_falls_back_to_defaults(monkeypatch):
    """`:ask` 早期调用点不传 config 时不能炸，用 run_ask 自己的默认值。"""
    mod = _chat_module()
    seen = {"kw": None}

    def _fake_run_ask(question, ctx, llm, **kw):
        seen["kw"] = kw
        return AskResult(
            answer="ok", stop_reason=AskStopReason.ANSWERED,
            tool_calls=[], llm_calls=1,
        )

    monkeypatch.setattr(
        "paper_agent.application.ask_loop.run_ask", _fake_run_ask, raising=True
    )
    mod._run_ask_command("几章", _SpySession(), _StubLLM(), None)
    assert seen["kw"] == {}


# ---- 观测接线（P-4 补齐）----


def test_only_explicit_ask_starts_an_independent_run():
    """``:ask`` 保留独立 run；自然语言兜底只展示 TurnService 的现成结果。"""
    src = open(os.path.join(_ROOT, "scripts", "chat.py"), encoding="utf-8").read()
    calls = [
        line.strip() for line in src.splitlines()
        if "_run_ask_command(" in line and "def " not in line
    ]
    assert calls == [
        "_run_ask_command(question, session, llm, config, facts, coordinator)"
    ]
    assert "_print_ask_result(out.readonly_result)" in src


def test_ask_run_publishes_events_through_coordinator(capsys):
    """接线的行为面：给了 coordinator，事件就真的经 bus 发出去。

    源码断言证明"传了"，这条证明"传了有用" —— 两者缺一不可。
    """
    mod = _chat_module()
    seen: list = []

    class _CapturingCoordinator:
        def run(self, fn, **kw):
            class _Bus:
                def publish(self, kind, **data):
                    seen.append(getattr(kind, "value", str(kind)))

                def span(self, *a, **k):
                    import contextlib
                    return contextlib.nullcontext()

            class _Report:
                error = None
            rep = _Report()
            rep.value = fn(_Bus(), None)
            return rep

    mod._run_ask_command(
        "这篇论文有几章", _SpySession(), _StubLLM(), _Cfg(),
        coordinator=_CapturingCoordinator(),
    )
    assert "ask_turn_start" in seen
    assert "ask_turn_end" in seen
    assert "共 1 章" in capsys.readouterr().out


def test_ask_without_coordinator_still_answers(capsys):
    """没有 coordinator 时照常回答 —— 只读路径不能因观测装配缺失而失效。"""
    mod = _chat_module()
    mod._run_ask_command("这篇论文有几章", _SpySession(), _StubLLM(), _Cfg())
    assert "共 1 章" in capsys.readouterr().out


def test_failed_run_does_not_print_none(capsys):
    """run 自身失败（report.value 为 None）时给出说明，不能把 None 当结果用。"""
    mod = _chat_module()

    class _FailingCoordinator:
        def run(self, fn, **kw):
            class _Report:
                value = None
                error = {"category": "RuntimeError", "preview": "x"}
            return _Report()

    mod._run_ask_command(
        "几章", _SpySession(), _StubLLM(), _Cfg(), coordinator=_FailingCoordinator()
    )
    out = capsys.readouterr().out
    assert "RuntimeError" in out
    assert "None" not in out


def test_natural_language_fallback_is_owned_by_planning():
    src = open(os.path.join(_ROOT, "scripts", "chat.py"), encoding="utf-8").read()
    assert "route_to_readonly" in src
    assert "Router(" not in src
    assert "未识别到受支持的写作操作" in src
