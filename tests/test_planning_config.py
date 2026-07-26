"""intent-planning Task 8 验收：Config 接入。

- 默认 off（行为不变）
- 各环境变量正确解析（mode / 上限 / 预算）
- 非法值安全降级为默认（不抛错、不接管）
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.config import load_config

_PLANNING_ENV = [
    "PAPER_INTENT_PLANNING_MODE",
    "PAPER_PLANNING_MAX_OPERATIONS",
    "PAPER_PLANNING_MAX_REVISIONS",
    "PAPER_PLANNING_MAX_INPUT_CHARS",
    "PAPER_PLANNING_TIMEOUT",
    "PAPER_PLANNING_MAX_LLM_CALLS",
    "PAPER_PLANNING_PROVIDER_TIME_BUDGET",
    "PAPER_SHADOW_TIMEOUT",
]


def _clear(monkeypatch):
    for name in _PLANNING_ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_off_and_sane(monkeypatch):
    _clear(monkeypatch)
    c = load_config()
    assert c.intent_planning_mode == "off"      # 默认不接管
    assert c.planning_max_operations == 5
    assert c.planning_max_revisions == 3
    assert c.planning_max_input_chars == 20000
    assert c.planning_timeout_s == 60.0
    assert c.planning_max_llm_calls == 20
    assert c.planning_provider_time_budget_s == 120.0
    assert c.shadow_timeout_s == 15.0


def test_valid_env_parsed(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PAPER_INTENT_PLANNING_MODE", "shadow")
    monkeypatch.setenv("PAPER_PLANNING_MAX_OPERATIONS", "8")
    monkeypatch.setenv("PAPER_PLANNING_MAX_REVISIONS", "2")
    monkeypatch.setenv("PAPER_PLANNING_MAX_INPUT_CHARS", "5000")
    monkeypatch.setenv("PAPER_PLANNING_TIMEOUT", "30")
    monkeypatch.setenv("PAPER_PLANNING_MAX_LLM_CALLS", "12")
    monkeypatch.setenv("PAPER_PLANNING_PROVIDER_TIME_BUDGET", "90.5")
    monkeypatch.setenv("PAPER_SHADOW_TIMEOUT", "8")
    c = load_config()
    assert c.intent_planning_mode == "shadow"
    assert c.planning_max_operations == 8
    assert c.planning_max_revisions == 2
    assert c.planning_max_input_chars == 5000
    assert c.planning_timeout_s == 30.0
    assert c.planning_max_llm_calls == 12
    assert c.planning_provider_time_budget_s == 90.5
    assert c.shadow_timeout_s == 8.0


def test_mode_active_and_case_insensitive(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PAPER_INTENT_PLANNING_MODE", "ACTIVE")
    assert load_config().intent_planning_mode == "active"


def test_illegal_mode_falls_back_off(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PAPER_INTENT_PLANNING_MODE", "turbo")
    assert load_config().intent_planning_mode == "off"


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
    monkeypatch.setenv("PAPER_SHADOW_TIMEOUT", "-5")           # 越界（<0）
    c = load_config()
    assert c.planning_timeout_s == 60.0
    assert c.shadow_timeout_s == 15.0
