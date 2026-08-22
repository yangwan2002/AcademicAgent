"""Planning 预算配置验收。

- 各环境变量正确解析（上限 / 预算）
- 非法值安全降级为默认（不抛错）
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.config import load_config

_PLANNING_ENV = [
    "PAPER_PLANNING_MAX_OPERATIONS",
    "PAPER_PLANNING_MAX_REVISIONS",
    "PAPER_PLANNING_MAX_INPUT_CHARS",
    "PAPER_PLANNING_TIMEOUT",
    "PAPER_PLANNING_MAX_LLM_CALLS",
    "PAPER_PLANNING_PROVIDER_TIME_BUDGET",
]


def _clear(monkeypatch):
    for name in _PLANNING_ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_sane(monkeypatch):
    _clear(monkeypatch)
    c = load_config()
    assert c.planning_max_operations == 5
    assert c.planning_max_revisions == 3
    assert c.planning_max_input_chars == 20000
    assert c.planning_timeout_s == 60.0
    assert c.planning_max_llm_calls == 20
    assert c.planning_provider_time_budget_s == 120.0


def test_valid_env_parsed(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PAPER_PLANNING_MAX_OPERATIONS", "8")
    monkeypatch.setenv("PAPER_PLANNING_MAX_REVISIONS", "2")
    monkeypatch.setenv("PAPER_PLANNING_MAX_INPUT_CHARS", "5000")
    monkeypatch.setenv("PAPER_PLANNING_TIMEOUT", "30")
    monkeypatch.setenv("PAPER_PLANNING_MAX_LLM_CALLS", "12")
    monkeypatch.setenv("PAPER_PLANNING_PROVIDER_TIME_BUDGET", "90.5")
    c = load_config()
    assert c.planning_max_operations == 8
    assert c.planning_max_revisions == 2
    assert c.planning_max_input_chars == 5000
    assert c.planning_timeout_s == 30.0
    assert c.planning_max_llm_calls == 12
    assert c.planning_provider_time_budget_s == 90.5


def test_illegal_int_falls_back_default(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PAPER_PLANNING_MAX_OPERATIONS", "abc")
    monkeypatch.setenv("PAPER_PLANNING_MAX_REVISIONS", "-1")   # 越界（<1）
    c = load_config()
    assert c.planning_max_operations == 5
    assert c.planning_max_revisions == 3


def test_illegal_float_falls_back_default(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PAPER_PLANNING_TIMEOUT", "not-a-number")
    c = load_config()
    assert c.planning_timeout_s == 60.0
