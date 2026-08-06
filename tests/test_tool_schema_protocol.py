"""工具 schema 协议合法性回归（真机 E2E 前发现的 P0）。

**背景**：`OpenAICompatibleProvider` 曾把 `tools` 原样透传给 OpenAI API，而 OpenAI 协议
要求每项 tool 带顶层 `"type": "function"`。`AnthropicProvider._to_anthropic_tools` 只读
`function` 子字典、忽略顶层 `type`，所以缺失该字段在 Anthropic 网关上**完全正常**，
换 OpenAI 兼容端点则全线失败：

    invalid_request_error
      → structured_call._looks_unsupported() 命中 "invalid_request_error"
      → 误判为「不支持 forced tool」→ 降级重试（第 2 次物理调用）
      → 仍失败 → RelevanceAssessment(status=ERROR, error_kind="provider_failed")

症状是「每次判定 2 次调用后仍 provider_failed」，错误被吞掉，极难定位。
1073 个既有测试没抓到，因为 `MockLLMProvider` 无条件吸收 `tools`、从不校验形状。

本测试是护栏：**遍历项目里所有工具 schema**，任何新增 schema 漏了 type 都会红。
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.providers.llm.anthropic_provider import _to_anthropic_tools
from paper_agent.providers.llm.openai_compatible import _normalize_tools


def _all_project_tool_schemas() -> dict[str, dict]:
    """项目里所有静态工具 schema（新增 schema 请在此登记）。"""
    from paper_agent.literature.relevance import _TOOL as RELEVANCE
    from paper_agent.literature.relevance_batch import _BATCH_TOOL as RELEVANCE_BATCH
    from paper_agent.literature.retrieval.query_planner import _TOOL as QUERY_PLANNER
    from paper_agent.tasking.operation_analyzer import _ANALYZE_TOOL as ANALYZE
    from paper_agent.tasking.query_synthesizer import _TOOL as SYNTHESIZER

    return {
        "relevance._TOOL": RELEVANCE,
        "relevance_batch._BATCH_TOOL": RELEVANCE_BATCH,
        "query_planner._TOOL": QUERY_PLANNER,
        "query_synthesizer._TOOL": SYNTHESIZER,
        "operation_analyzer._ANALYZE_TOOL": ANALYZE,
    }


def _dynamic_extractor_schemas() -> dict[str, dict]:
    """PreExtractor 动态构造的 schema（每个意图一个）。"""
    from paper_agent.tasking import intent_spec as spec_mod
    from paper_agent.tasking.extractors.base import build_tool_schema

    out = {}
    for name in dir(spec_mod):
        if not name.endswith("_SPEC"):
            continue
        spec = getattr(spec_mod, name)
        if hasattr(spec, "intent"):
            out[f"extractors.build_tool_schema({name})"] = build_tool_schema(spec)
    return out


def _all_schemas() -> dict[str, dict]:
    merged = dict(_all_project_tool_schemas())
    try:
        merged.update(_dynamic_extractor_schemas())
    except Exception:
        pass
    return merged


@pytest.mark.parametrize("name", sorted(_all_schemas()))
def test_schema_is_openai_protocol_valid(name):
    """每个 schema 必须自带顶层 type=function + function.name（OpenAI 协议必填）。"""
    schema = _all_schemas()[name]
    assert schema.get("type") == "function", (
        f"{name} 缺少顶层 'type': 'function' —— 打 OpenAI 兼容端点会 "
        f"invalid_request_error，且错误会被吞成 provider_failed"
    )
    fn = schema.get("function")
    assert isinstance(fn, dict), f"{name} 缺少 function 子字典"
    assert fn.get("name"), f"{name} 缺少 function.name"


@pytest.mark.parametrize("name", sorted(_all_schemas()))
def test_schema_survives_both_protocol_adapters(name):
    """同一 schema 必须能同时被两个协议适配器正确消化（双向对称）。"""
    schema = _all_schemas()[name]
    expected_name = schema["function"]["name"]

    # OpenAI 侧
    normalized = _normalize_tools([schema])
    assert len(normalized) == 1
    assert normalized[0]["type"] == "function"
    assert normalized[0]["function"]["name"] == expected_name

    # Anthropic 侧
    anth = _to_anthropic_tools([schema])
    assert len(anth) == 1
    assert anth[0]["name"] == expected_name
    assert isinstance(anth[0]["input_schema"], dict)


# ============ _normalize_tools 本体 ============

def test_normalize_adds_missing_type():
    out = _normalize_tools([{"function": {"name": "f", "parameters": {}}}])
    assert out == [{"type": "function", "function": {"name": "f", "parameters": {}}}]


def test_normalize_keeps_already_valid_untouched():
    valid = {"type": "function", "function": {"name": "f", "parameters": {}}}
    assert _normalize_tools([valid]) == [valid]


def test_normalize_wraps_bare_function_definition():
    out = _normalize_tools([{"name": "f", "description": "d", "parameters": {}}])
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "f"


def test_normalize_skips_non_dict_entries():
    assert _normalize_tools(["nope", None, 42]) == []


def test_normalize_handles_none_and_empty():
    assert _normalize_tools(None) == []
    assert _normalize_tools([]) == []


def test_normalize_does_not_mutate_input():
    original = {"function": {"name": "f"}}
    snapshot = {"function": {"name": "f"}}
    _normalize_tools([original])
    assert original == snapshot


# ============ provider 真的用了归一化 ============

def _capture_kwargs(monkeypatch):
    """拦住 SDK 调用，捕获实际发出的 kwargs（不联网）。"""
    from paper_agent.providers.llm import openai_compatible as mod

    captured = {}

    class _Msg:
        content = "ok"
        tool_calls = None
        reasoning_content = None

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]
        usage = None

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    provider = mod.OpenAICompatibleProvider(
        model="m", api_key="k", base_url="http://example.invalid/v1")
    monkeypatch.setattr(provider, "_client", _Client(), raising=False)
    return provider, captured


def test_provider_sends_normalized_tools(monkeypatch):
    from paper_agent.providers.llm.base import Message

    provider, captured = _capture_kwargs(monkeypatch)
    provider.complete(
        [Message(role="user", content="hi")],
        tools=[{"function": {"name": "f", "parameters": {}}}],   # 故意缺 type
        forced_tool_name="f",
    )
    assert captured["tools"][0]["type"] == "function"
    assert captured["tool_choice"] == {
        "type": "function", "function": {"name": "f"}}


def test_provider_normalizes_real_relevance_schema(monkeypatch):
    """用真实的相关性 schema 走一遍 —— 这条链路就是线上失败的那条。"""
    from paper_agent.literature.relevance import _TOOL
    from paper_agent.providers.llm.base import Message

    provider, captured = _capture_kwargs(monkeypatch)
    provider.complete([Message(role="user", content="hi")], tools=[_TOOL],
                      forced_tool_name="report_relevance")
    sent = captured["tools"][0]
    assert sent["type"] == "function"
    assert sent["function"]["name"] == "report_relevance"
