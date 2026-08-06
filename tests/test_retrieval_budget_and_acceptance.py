"""dogfood 实测挖出的两处修复：max_provider_calls 可配 + 接受策略可收 weakly_relevant。"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.application.literature_composition import (
    _acceptance_policy,
    _limits,
)
from paper_agent.config import Config, load_config
from paper_agent.literature.relevance import RelevanceAssessment


# ---------------------------------------------------------------- max_provider_calls 可配

def test_max_provider_calls_default_and_config():
    assert Config().literature_retrieval_max_provider_calls == 200
    lim = _limits(Config(literature_retrieval_max_provider_calls=500))
    assert lim.max_provider_calls == 500                # 之前恒为 60（漏映射），现在贯通


def test_max_provider_calls_env(monkeypatch):
    monkeypatch.setenv("PAPER_LIT_RETRIEVAL_MAX_PROVIDER_CALLS", "999")
    assert load_config().literature_retrieval_max_provider_calls == 999


# ---------------------------------------------------------------- 接受策略可配

def _asmt(verdict, basis="title_and_abstract"):
    return RelevanceAssessment(
        reference_id="r", task_fingerprint="tf", reference_content_hash="h", judge_key="k",
        status="SUCCESS", verdict=verdict, confidence="high", basis=basis)


def test_default_policy_rejects_weakly():
    pol = _acceptance_policy(Config(literature_accept_weakly_relevant=False))
    assert pol.is_accepted(_asmt("relevant")) is True
    assert pol.is_accepted(_asmt("weakly_relevant")) is False


def test_policy_accepts_weakly_when_configured():
    pol = _acceptance_policy(Config(literature_accept_weakly_relevant=True))
    assert pol.is_accepted(_asmt("relevant")) is True
    assert pol.is_accepted(_asmt("weakly_relevant")) is True
    assert pol.is_accepted(_asmt("unrelated")) is False   # unrelated 恒拒


def test_accept_weakly_env_default_off():
    assert Config().literature_accept_weakly_relevant is False


def test_accept_weakly_env(monkeypatch):
    monkeypatch.setenv("PAPER_LIT_ACCEPT_WEAKLY_RELEVANT", "true")
    assert load_config().literature_accept_weakly_relevant is True
