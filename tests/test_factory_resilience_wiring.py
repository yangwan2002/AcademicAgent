"""工厂必须把重试层真正接上线。

背景（真机）：写正文那一步报
`failed — APITimeoutError: Request timed out or interrupted`，
整个计划直接失败。而 `ResilientLLMProvider` 早就写好了，`APITimeoutError` 也在
`is_retryable` 的可重试名单里 —— 问题是 `build_llm_provider` 从来没构造过它，
返回的是裸 provider。**能力写好但没接线**，症状与"根本没实现重试"完全相同。

这类缺陷单测很难发现：重试层自己的测试全绿（它确实能重试），provider 的测试也全绿
（它确实透传异常）。断的是**装配**，所以这里断言的是接线本身。

与 test_docx_inplace.py 里那条同源：那次是排版报成功而文件没变，这次是重试写好了
但没生效。两者都属于"分层各自正确、接缝处失效"。
"""
from __future__ import annotations


import pytest

from paper_agent.config import Config, load_config
from paper_agent.providers.factory import (
    build_llm_provider,
    build_reviewer_llm_provider,
    build_vlm_provider,
)
from paper_agent.providers.llm.base import LLMError, LLMResponse
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.llm.resilient import ResilientLLMProvider, is_retryable
from paper_agent.workspace.models import RetryPolicy

_KEY_ENV = "PAPER_TEST_FAKE_KEY"


class APITimeoutError(Exception):
    """复刻 SDK 的超时异常：`is_retryable` 按**类名**判定，故同名即等价。"""


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setenv(_KEY_ENV, "sk-test")


def _cfg(**kw) -> Config:
    base = dict(llm_provider="openai", llm_model="m",
                llm_base_url="http://example.invalid", llm_api_key_env=_KEY_ENV)
    return Config(**{**base, **kw})


# ---------------- 接线 ----------------

def test_openai_path_is_wrapped():
    assert isinstance(build_llm_provider(_cfg()), ResilientLLMProvider)


def test_anthropic_path_is_wrapped():
    """另一条 provider 分支同样要包 —— 漏一条就等于那条链路没有重试。"""
    provider = build_llm_provider(
        _cfg(llm_provider="anthropic", llm_model="claude-x", llm_base_url=None))
    assert isinstance(provider, ResilientLLMProvider)


def test_preset_path_is_wrapped():
    from paper_agent.providers.llm.presets import VENDOR_PRESETS

    name = sorted(VENDOR_PRESETS)[0]
    provider = build_llm_provider(_cfg(llm_provider=name, llm_base_url=None))
    assert isinstance(provider, ResilientLLMProvider)


def test_vlm_path_is_wrapped():
    provider = build_vlm_provider(
        Config(vlm_provider="openai", vlm_model="v", vlm_api_key_env=_KEY_ENV))
    assert isinstance(provider, ResilientLLMProvider)


def test_mock_is_not_wrapped():
    """mock 不包：它不产生瞬时错误，包了只会给测试里的异常多套一层 LLMError。"""
    assert isinstance(build_llm_provider(Config(llm_provider="mock")),
                      MockLLMProvider)


def test_retries_disabled_returns_bare_provider():
    """置 0 = 关掉重试，回到裸 provider 的原始异常语义（便于调试看真实错误类型）。"""
    assert not isinstance(build_llm_provider(_cfg(llm_max_retries=0)),
                          ResilientLLMProvider)


# ---------------- 配置真的传下去了 ----------------

def test_policy_comes_from_config():
    """配置项不得只是"定义了但没人消费"——planning_timeout_s 就是那样的前例。"""
    provider = build_llm_provider(_cfg(
        llm_max_retries=7, llm_retry_base_backoff_s=2.5,
        llm_retry_max_backoff_s=11.0))
    assert isinstance(provider, ResilientLLMProvider)
    policy = provider._policy
    assert (policy.max_retries, policy.base_backoff, policy.max_backoff) == (
        7, 2.5, 11.0)


def test_request_timeout_reaches_inner_provider():
    """断言落在 SDK client 上 —— 那才是链路终点。

    断 provider 自己存的副本会漏掉"存了但没传给 client"这种情况。
    """
    provider = build_llm_provider(_cfg(llm_request_timeout_s=45.0))
    assert provider._inner._client.timeout == 45.0


def test_reviewer_inherits_resilience_settings():
    """reviewer 走的是新建 Config 的路径，最容易把新字段丢回默认值。"""
    provider = build_reviewer_llm_provider(_cfg(
        reviewer_llm_model="other-model", llm_max_retries=5,
        llm_request_timeout_s=33.0))
    assert isinstance(provider, ResilientLLMProvider)
    assert provider._policy.max_retries == 5
    assert provider._inner._client.timeout == 33.0


@pytest.mark.parametrize("env,field,expect", [
    ("PAPER_LLM_MAX_RETRIES", "llm_max_retries", 6),
    ("PAPER_LLM_TIMEOUT", "llm_request_timeout_s", 90.0),
])
def test_env_overrides(monkeypatch, env, field, expect):
    monkeypatch.setenv(env, str(expect))
    assert getattr(load_config(), field) == expect


# ---------------- 行为：超时确实被吸收 ----------------

def _flaky(fail_times: int, calls: list):
    class _P:
        def complete(self, messages, **opts):
            calls.append(1)
            if len(calls) <= fail_times:
                raise APITimeoutError("Request timed out or interrupted")
            return LLMResponse(content="写好的正文[6]。")
    return _P()


def test_api_timeout_is_classified_retryable():
    assert is_retryable(APITimeoutError("Request timed out")) is True


def test_transient_timeout_is_absorbed():
    """真机场景的正解：首次超时 → 自动重试 → 成功，用户看不到 failed。"""
    calls: list = []
    provider = ResilientLLMProvider(
        _flaky(1, calls),
        policy=RetryPolicy(max_retries=3, base_backoff=0.01, max_backoff=0.02))
    assert provider.complete([]).content == "写好的正文[6]。"
    assert len(calls) == 2


def test_exhausted_retries_report_call_count():
    """持续超时仍应失败 —— 重试不是"永不失败"，且底层调用次数有上界。"""
    calls: list = []
    provider = ResilientLLMProvider(
        _flaky(99, calls),
        policy=RetryPolicy(max_retries=2, base_backoff=0.01, max_backoff=0.02))
    with pytest.raises(LLMError, match="重试 2 次"):
        provider.complete([])
    assert len(calls) == 3          # 1 + max_retries，不得无界重试


def test_non_retryable_fails_immediately():
    """配置错误（如 key 不对）必须一次就抛，不该把 4 次额度浪费在必败调用上。"""
    calls: list = []

    class _Auth:
        def complete(self, messages, **opts):
            calls.append(1)
            raise type("AuthenticationError", (Exception,), {})("bad key")

    provider = ResilientLLMProvider(
        _Auth(), policy=RetryPolicy(max_retries=3, base_backoff=0.01))
    with pytest.raises(LLMError):
        provider.complete([])
    assert len(calls) == 1


def test_structured_capability_owner_survives_wrapping():
    """包一层后，结构化调用能力探测仍须落到内层适配器上。

    否则"包上重试"会顺带把工具调用/强制 tool 的能力协商打断 —— 修一个 bug 引入
    另一个。`structured.py` 靠 `structured_call_capability_owner` 逐层解包。
    """
    from paper_agent.providers.llm.structured import _capability_owner

    provider = build_llm_provider(_cfg())
    assert _capability_owner(provider) is provider._inner
