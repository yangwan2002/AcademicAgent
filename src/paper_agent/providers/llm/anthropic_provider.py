"""Anthropic Messages 协议 LLM provider（与 OpenAICompatibleProvider 并列的适配器）。

与 OpenAI 协议的关键差异，全部在本适配器内消化，对上层透明——上层只依赖
``LLMProvider`` 抽象与中立的 ``Message``/``ToolCall``/``LLMResponse``：

- system 提示单独作为顶层 ``system`` 参数，不进 messages；
- 消息内容用 content block（``text``/``thinking``/``tool_use``/``tool_result``）；
- 工具结果（内部 ``role="tool"``）需并入一条 ``user`` 消息的 ``tool_result`` 块，
  且同一 assistant 回合的多个工具结果要合并进同一条 user 消息；
- 工具 schema 形态不同：OpenAI 的 ``{"function": {...}}`` → Anthropic 的
  ``{"name", "description", "input_schema"}``；
- 必须显式传 ``max_tokens``。

依赖 ``anthropic`` SDK（惰性导入）。为可测试，``client`` 可注入。
"""

from __future__ import annotations

import os
import platform
import sys
from typing import Iterator

from paper_agent.providers.llm.base import (
    CancellationToken,
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    StreamChunk,
    ToolCall,
)

_DEFAULT_MAX_TOKENS = 4096


def _prewarm_platform_cache() -> None:
    """填 `platform` 缓存，规避 Windows 上 anthropic SDK 的 WMI 挂死。

    SDK 每次构造请求头都会调 `platform.platform()` 取 UA（`_base_client.get_platform`）。
    在部分 Windows 环境下这条路径会走 WMI（`platform._wmi_query`）并**长时间挂住**：
    实测本机每次 `complete()` 卡 45 秒以上直到超时，而同一个请求用 urllib 直发网关
    只要 2 秒 —— 网络和网关都正常，时间全花在取 UA 字符串上。

    WMI 有三处入口，逐一堵掉（少一处就仍然卡，实测确认）：

    1. `uname()` 的 `_uname_cache` —— 填好即不再查；
    2. `platform.platform()` 另走 `win32_ver()`；
    3. `uname_result.processor` 是 `functools.cached_property`，`platform()` 迭代
       uname 时**才**触发 WMI —— 所以只填 1 不够，得把 processor 一起定死。

    数据取自 `sys.getwindowsversion()` 与环境变量，都是免 WMI 的来源；UA 字符串
    仅用于遥测，精度不影响功能。非 Windows 直接返回，任何异常都忽略。

    放模块导入期而非调用期：SDK 构造请求头时就读，届时再补已经晚了。
    """
    if os.name != "nt":
        return
    try:
        wv = sys.getwindowsversion()
        version = f"{wv.major}.{wv.minor}.{wv.build}"
        release = "11" if wv.build >= 22000 else "10"
        machine = os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64")
        node = os.environ.get("COMPUTERNAME", "localhost")

        cached = platform.uname_result("Windows", node, release, version, machine)
        # processor 是 cached_property：直接塞进实例字典，堵掉第 3 处 WMI。
        try:
            object.__setattr__(
                cached, "processor", os.environ.get("PROCESSOR_IDENTIFIER", machine)
            )
        except Exception:  # noqa: BLE001 - 塞不进就算了，最多慢，不会错
            pass
        platform._uname_cache = cached  # type: ignore[attr-defined]

        sp_major = getattr(wv, "service_pack_major", 0) or 0
        platform.win32_ver = lambda *a, **k: (  # type: ignore[assignment]
            release, version, f"SP{sp_major}" if sp_major else "", "Multiprocessor Free"
        )
    except Exception:  # noqa: BLE001 - 性能兜底，绝不因此让 provider 不可用
        pass


_prewarm_platform_cache()


def _to_anthropic_tools(openai_tools: list[dict]) -> list[dict]:
    """OpenAI 风格 tools schema → Anthropic ``tools`` 形态。"""
    out: list[dict] = []
    for t in openai_tools or []:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        out.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return out


def _to_anthropic_messages(messages: list[Message]) -> tuple[str | None, list[dict]]:
    """把内部 Message 序列转成 (system, anthropic_messages)。

    - system 消息汇聚为顶层 system 串；
    - role="tool" 汇聚为 ``tool_result`` 块，连续的合并进同一条 user 消息
      （紧跟在触发它们的 assistant ``tool_use`` 回合之后）。
    """
    system_parts: list[str] = []
    converted: list[dict] = []
    pending_results: list[dict] = []

    def _flush_results() -> None:
        if pending_results:
            converted.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        if m.role == "system":
            if m.content:
                system_parts.append(m.content)
            continue

        if m.role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content or "",
                }
            )
            continue

        # 非工具结果消息：先把累积的工具结果作为一条 user 消息落定。
        _flush_results()

        if m.role == "assistant":
            converted.append({"role": "assistant", "content": _assistant_blocks(m)})
        else:  # user
            converted.append({"role": "user", "content": m.content or ""})

    _flush_results()
    system = "\n\n".join(system_parts) if system_parts else None
    return system, converted


def _assistant_blocks(m: Message) -> list[dict]:
    """assistant 消息 → content block 列表（文本 + tool_use）。"""
    blocks: list[dict] = []
    if m.content:
        blocks.append({"type": "text", "text": m.content})
    for tc in m.tool_calls or []:
        blocks.append(
            {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
        )
    if not blocks:
        # Anthropic 不接受空内容；用占位文本兜底（正常不会触发）。
        blocks.append({"type": "text", "text": ""})
    return blocks


def _parse_response(resp) -> LLMResponse:
    """Anthropic Message → 中立 LLMResponse（文本/思考/工具调用/用量）。"""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in getattr(resp, "content", None) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif btype == "thinking":
            reasoning_parts.append(getattr(block, "thinking", "") or "")
        elif btype == "tool_use":
            name = getattr(block, "name", "") or ""
            tool_calls.append(
                ToolCall(
                    id=getattr(block, "id", "") or name,
                    name=name,
                    arguments=dict(getattr(block, "input", None) or {}),
                )
            )

    usage = getattr(resp, "usage", None)
    return LLMResponse(
        content="".join(text_parts),
        reasoning="".join(reasoning_parts) or None,
        prompt_tokens=getattr(usage, "input_tokens", None) if usage else None,
        completion_tokens=getattr(usage, "output_tokens", None) if usage else None,
        tool_calls=tool_calls,
    )


def _anthropic():
    try:
        import anthropic  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise LLMError(
            "Anthropic provider 需要 anthropic 包：pip install anthropic"
        ) from exc
    return anthropic


class AnthropicProvider(LLMProvider):
    """Anthropic Messages 协议适配器（含 Anthropic 兼容网关）。

    Args:
        model: 模型名（网关可为 qwen3.7-max 等）。
        api_key / api_key_env: 密钥或其环境变量名。
        base_url: 网关端点；为空用 Anthropic 官方默认。
        max_tokens: 单次生成上限（Anthropic 必填，默认 4096）。
        default_options: 默认采样参数（temperature 等），可被每次 opts 覆盖。
        client: 可注入的底层客户端（测试用）；为空则惰性构造真实 SDK 客户端。
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_key_env: str = "ANTHROPIC_API_KEY",
        base_url: str | None = None,
        timeout: float = 120.0,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        default_options: dict | None = None,
        client=None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._base_url = base_url
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._default_options = default_options or {}
        self._client = client  # 惰性：为空时首次调用再构造

    def _get_client(self):
        if self._client is not None:
            return self._client
        anthropic = _anthropic()
        key = self._api_key or os.environ.get(self._api_key_env)
        if not key:
            raise LLMError(
                f"缺少 API Key：请设置环境变量 {self._api_key_env} 或显式传入 api_key。"
            )
        kwargs: dict = {"api_key": key, "timeout": self._timeout}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        """调用 Anthropic Messages 接口；瞬时错误原样上抛交由 Resilient 层重试。

        当调用方传入 push 风格 ``on_delta`` 回调时走**流式**：边接收边把内容增量
        经 ``on_delta("content", text)`` 推给下游（可观测层据此发 LLM_DELTA 事件，
        实现"边想边输出"）。流式端点不可用/失败时**回退**到一次性 create，并把完整
        正文补推一次，保证功能不因网关不支持流式而中断。
        """
        on_delta = opts.pop("on_delta", None)
        opts.pop("event_sink", None)  # 可选观测 sink：本层不透传给底层 API
        tools = opts.pop("tools", None)
        # 中立参数 → Anthropic 的 tool_choice 形态（协议适配在 provider 内，调用方
        # 不需要知道各厂商语法差异）。
        forced_tool_name = opts.pop("forced_tool_name", None)
        max_tokens = int(opts.pop("max_tokens", self._max_tokens))

        system, conv = _to_anthropic_messages(messages)
        call_opts = {**self._default_options, **opts}
        call_opts.pop("stream", None)
        call_opts.pop("stream_options", None)
        # StructuredParser uses the OpenAI ``response_format`` hint.  Anthropic
        # Messages has no equivalent; the JSON-only prompt remains authoritative.
        call_opts.pop("response_format", None)
        call_opts.pop("max_completion_tokens", None)

        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": conv,
            **call_opts,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)
            if forced_tool_name:
                kwargs["tool_choice"] = {"type": "tool", "name": forced_tool_name}

        if on_delta is not None:
            return self._complete_streaming(kwargs, on_delta)

        resp = self._get_client().messages.create(**kwargs)
        return _parse_response(resp)

    def _complete_streaming(self, kwargs: dict, on_delta) -> LLMResponse:
        """流式补全：逐块经 on_delta 推送内容增量，返回完整（含工具调用）响应。

        流式失败（网关不支持 messages.stream 等）→ 回退一次性 create，并把完整正文
        经 on_delta 补推一次，保证下游至少收到一次完整输出。
        """
        client = self._get_client()
        try:
            with client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    if text:
                        on_delta("content", text)
                final = stream.get_final_message()
            return _parse_response(final)
        except Exception:  # noqa: BLE001 - 流式不可用则回退非流式（不中断功能）
            resp = client.messages.create(**kwargs)
            parsed = _parse_response(resp)
            if parsed.content:
                on_delta("content", parsed.content)
            return parsed

    def stream(
        self,
        messages: list[Message],
        *,
        cancel_token: CancellationToken | None = None,
        **opts,
    ) -> Iterator[StreamChunk]:
        """最小流式实现：一次性 complete 后把正文作为单块产出。

        此方法为满足 ``LLMProvider`` 协议与走 ``stream`` 的封装层而提供。取消令牌被尊重。
        """
        opts.pop("event_sink", None)  # 可选观测 sink：本层不透传给底层 API
        if cancel_token is not None and cancel_token.cancelled:
            return
        resp = self.complete(messages, **opts)
        if resp.reasoning:
            yield StreamChunk(kind="thinking", text=resp.reasoning)
        if resp.content:
            yield StreamChunk(kind="content", text=resp.content)


__all__ = ["AnthropicProvider"]
