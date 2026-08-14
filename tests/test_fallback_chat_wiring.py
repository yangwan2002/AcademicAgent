"""chat.py 的只读问答接线（P-4 第四步）。

三件事值得钉在这一层，路由测试（`test_fallback_routing.py`）覆盖不到：

1. **护栏上限来自 config**，不是写死在调用点。
2. **提问不触达写入面** —— spy 掉 `write_working` / `save_to_source`，断言零调用。
   这是 P-4 红线的可执行形态。
3. **ASK 判定在 intent-planning 接管之前**。planning 那条链路是为写设计的
   （规划 → 确认 → 执行），提问送进去会重新落回写路径，等于绕过兜底判定。
   两个 flag 都开时以 ask 为先。
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
from paper_agent.intents.router import Intent, Router
from paper_agent.session.section_index import SectionIndex


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
    agent_loop_mode = "on"
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


def test_coordinator_threaded_to_both_ask_call_sites():
    """两个调用点都要把 coordinator 传进去，否则那一路没有 trace。

    这钉的是 backlog 第七节「能力写对了，接线送不到」那一类：埋点写对了、
    白名单加对了，但某个调用点漏传 coordinator ⇒ 那条路径静默无观测。
    `:ask` 和自然语言兜底是两个独立入口，漏一个都算漏。
    """
    src = open(os.path.join(_ROOT, "scripts", "chat.py"), encoding="utf-8").read()
    calls = [
        line.strip() for line in src.splitlines()
        if "_run_ask_command(" in line and "def " not in line
    ]
    assert len(calls) == 2, calls
    for line in calls:
        assert line.endswith("coordinator)"), line


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


def test_fallback_precedes_planning_takeover():
    """FALLBACK 分支在源码里必须出现在 planning 接管之前。

    用源码顺序断言（而不是跑主循环）：主循环要装配 session/provider/facts 一整套，
    在单测里搭起来的成本远高于它能多覆盖的东西。顺序错了这条就红。
    """
    src = open(os.path.join(_ROOT, "scripts", "chat.py"), encoding="utf-8").read()
    ask_at = src.index("if route.intent is Intent.FALLBACK:")
    planning_at = src.index("if turn_service is not None and turn_service.mode")
    assert ask_at < planning_at, "FALLBACK 判定被 intent-planning 抢先接管了"


def test_router_fallback_flag_threaded_from_config():
    """chat 构造 Router 时要把开关传进去，否则判定永远不生效。"""
    src = open(os.path.join(_ROOT, "scripts", "chat.py"), encoding="utf-8").read()
    assert "Router(index, fallback_enabled=_fallback_enabled)" in src
    assert '_fallback_enabled = config.agent_loop_mode == "on"' in src


def test_router_api_requires_explicit_opt_in():
    """`Router` **形参**默认关：不传开关就是旧行为，接管必须显式要求。

    注意这钉的是 Router 这一层的 API 契约，**不是**应用默认档 ——
    `Config.agent_loop_mode` 已于 2026-08-12 翻成 `on`（见
    `test_app_default_is_on`）。两者刻意不同：库层不猜调用者意图，
    要不要接管由 `chat.py` 读配置后显式传入，这样"默认档"只有一个归属地。

    原来这里断言「这段的参考文献够吗」→ `FIX_BIBLIOGRAPHY`，即该档下仍是那个
    （错误的）旧行为。该意图已删（B-2），换用 `_POLISH_KW` 命中的那条 —— 它同样
    是只读提问被写意图关键词吞掉，且 polish 意图仍在，能继续验证「不显式开就不接管」。
    """
    r = Router(SectionIndex("# 1 引言\n\n内容。\n", "md"))
    assert r.route("引言的语言风格是怎样的").intent is Intent.POLISH
    assert r.route("这段的参考文献够吗").intent is not Intent.FALLBACK


def test_app_default_is_on(monkeypatch):
    """应用默认档是 `on`，且 `off` 能一键回退。

    钉住翻档这件事本身：关着时"只读提问被劫成写操作"对用户依然存在。
    同时钉住关档意图必须被听懂 —— `false` 这类写法不能被"非法值降级为 default"
    悄悄解析成 `on`（那与意图相反）。
    """
    from paper_agent.config import Config, load_config

    monkeypatch.delenv("PAPER_AGENT_LOOP_MODE", raising=False)
    assert Config().agent_loop_mode == "on"
    assert load_config().agent_loop_mode == "on"

    for raw in ("off", "OFF", " off ", "0", "false", "no"):
        monkeypatch.setenv("PAPER_AGENT_LOOP_MODE", raw)
        assert load_config().agent_loop_mode == "off", raw
