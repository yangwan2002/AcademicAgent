"""通用 OpenAI 兼容 LLM provider。

大多数厂商（OpenAI、DeepSeek、Kimi/Moonshot、智谱 GLM、通义千问等）都提供
OpenAI 兼容的 chat/completions 接口，彼此只差三样：base_url、API Key、模型名。
本类把这三样参数化，使得「接入一个新的 OpenAI 兼容厂商」无需新增代码，
只需提供配置（base_url + api_key_env + model）。

对于**非** OpenAI 兼容的厂商（如 Anthropic / Gemini 原生协议），
只需另写一个实现 LLMProvider 协议的适配器即可——这正是该抽象的意义所在。
"""

from __future__ import annotations

import json
import os
from typing import Iterator

import base64

from paper_agent.providers.llm.base import (
    CancellationToken,
    ImageInput,
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    StreamChunk,
    ToolCall,
)


def _image_data_url(img: ImageInput) -> str | None:
    """把 ImageInput 解析为可放进 OpenAI content part 的图片 URL；失败返回 None。

    ``data_url`` 已就绪则直用；否则读本地 ``path`` 编码为 ``data:<media>;base64,<..>``。
    """
    if img.data_url:
        return img.data_url
    if img.path:
        try:
            with open(img.path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            return f"data:{img.media_type};base64,{b64}"
        except OSError:
            return None
    return None


def _openai():
    try:
        from openai import OpenAI  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise LLMError(
            "OpenAI 兼容 provider 需要 openai 包：pip install '.[llm]'"
        ) from exc
    return OpenAI


def _usage_of(resp) -> tuple[int | None, int | None]:
    """从响应中提取 (prompt_tokens, completion_tokens)，无则返回 (None, None)。"""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None, None
    return (
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
    )


def _to_api_message(m: Message) -> dict:
    """把内部 Message 转成 OpenAI API 消息格式（含工具调用字段）。

    ``m.images`` 为空/None 时 content 仍为**纯字符串**，序列化与现状逐字节一致；
    非空时 content 转为 OpenAI 风格多模态 parts（text + image_url）。
    """
    if m.images:
        parts: list[dict] = []
        if m.content:
            parts.append({"type": "text", "text": m.content})
        for img in m.images:
            url = _image_data_url(img)
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        msg: dict = {"role": m.role, "content": parts}
    else:
        msg = {"role": m.role, "content": m.content}
    if m.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in m.tool_calls
        ]
    if m.tool_call_id is not None:
        msg["tool_call_id"] = m.tool_call_id
    return msg


def _normalize_tools(tools) -> list[dict]:
    """把中立的工具 schema 归一化为 **OpenAI 协议要求的形态**（每项带顶层 ``type``）。

    协议差异在 provider 内消化（与 ``AnthropicProvider._to_anthropic_tools`` 对称）：
    Anthropic 适配器只读 ``function`` 子字典、忽略顶层 ``type``，故项目里有若干 schema
    历史上只写了 ``{"function": {...}}``。这些 schema 打 Anthropic 网关正常，直接透传给
    OpenAI 端点则会被拒（invalid_request_error），且 ``structured_call`` 会把该错误当成
    「不支持 forced tool」而降级重试 —— 表现为 **每次判定 2 次物理调用后仍失败**，
    错误被吞成 ``provider_failed``，极难定位。故在此**统一补齐**，而不是要求每个调用点自觉。
    """
    out: list[dict] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and "function" in t:
            out.append(t)
            continue
        if "function" in t:
            out.append({"type": "function", **t})
            continue
        # 裸 function 定义（{"name","description","parameters"}）→ 包一层
        out.append({"type": "function", "function": t})
    return out


def _parse_tool_calls(message) -> list[ToolCall]:
    raw = getattr(message, "tool_calls", None)
    if not raw:
        return []
    calls: list[ToolCall] = []
    for tc in raw:
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", "") if fn else ""
        args_str = getattr(fn, "arguments", "") if fn else ""
        try:
            args = json.loads(args_str) if args_str else {}
        except (ValueError, TypeError):
            args = {}
        calls.append(
            ToolCall(id=getattr(tc, "id", "") or name, name=name, arguments=args)
        )
    return calls


class OpenAICompatibleProvider(LLMProvider):
    """通过 OpenAI SDK 访问任意 OpenAI 兼容服务。

    Args:
        model: 模型名。
        api_key: 显式 API Key；为空则从 api_key_env 指定的环境变量读取。
        api_key_env: 读取 API Key 的环境变量名（默认 OPENAI_API_KEY）。
        base_url: 服务端点；为空则使用 OpenAI 官方默认。
        timeout: 请求超时（秒）。
        max_retries: 瞬时网络错误（连接重置/超时/限流/5xx）的最大重试次数。
        retry_backoff: 重试退避基数（秒），按指数增长。
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        timeout: float = 120.0,
        extra_body: dict | None = None,
        default_options: dict | None = None,
        max_retries: int | None = None,
        retry_backoff: float | None = None,
    ) -> None:
        OpenAI = _openai()
        self._model = model
        key = api_key or os.environ.get(api_key_env)
        if not key:
            raise LLMError(
                f"缺少 API Key：请设置环境变量 {api_key_env} 或显式传入 api_key。"
            )
        client_kwargs: dict = {"api_key": key, "timeout": timeout}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = OpenAI(**client_kwargs)
        # 厂商专有的非标准请求参数（如千问的 chat_template_kwargs）走 extra_body 透传。
        self._extra_body = extra_body or {}
        # 默认采样参数（temperature/top_p/stream 等），可被每次调用的 opts 覆盖。
        self._default_options = default_options or {}
        # 重试/退避已上提至 ResilientLLMProvider 统一治理（避免双重重试）；
        # 此处仅接受旧式参数以保持构造签名向后兼容，不再在 provider 内部重试。
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        """调用 OpenAI 兼容接口；瞬时错误以**原始异常**上抛，交由外层
        ``ResilientLLMProvider`` 统一做重试/退避/429 处理（避免 provider 内部
        与 Resilient 双重重试，且使 Resilient 的状态码/Retry-After 判定生效）。

        仅「缺 API Key」「缺 openai 包」这类配置错误在本层以 ``LLMError`` 抛出
        （构造期已处理）；运行期 API 错误一律透传原始异常。
        """
        # on_delta：流式逐块回调 callable(kind, text)，kind ∈ {"content","thinking"}。
        on_delta = opts.pop("on_delta", None)
        opts.pop("event_sink", None)  # 可选观测 sink：本层不透传给底层 API
        tools = opts.pop("tools", None)
        # 中立参数 → OpenAI 的 tool_choice 形态（各厂商语法差异在 provider 内消化）
        forced_tool_name = opts.pop("forced_tool_name", None)
        call_opts = {**self._default_options, **opts}
        if self._extra_body:
            # 合并调用方传入的 extra_body（若有）。
            call_opts["extra_body"] = {
                **self._extra_body,
                **call_opts.get("extra_body", {}),
            }
        payload = [_to_api_message(m) for m in messages]
        # 工具调用回合不走流式（tool_calls 在流式下需额外拼装，从简）。
        if tools:
            call_opts["tools"] = _normalize_tools(tools)
            if forced_tool_name:
                call_opts["tool_choice"] = {
                    "type": "function",
                    "function": {"name": forced_tool_name},
                }
            call_opts.pop("stream", None)
            call_opts.pop("stream_options", None)
        streaming = bool(call_opts.get("stream")) and not tools

        if streaming:
            return self._complete_stream(payload, call_opts, on_delta)
        return self._complete_once(payload, call_opts)

    def stream(
        self,
        messages: list[Message],
        *,
        cancel_token: CancellationToken | None = None,
        **opts,
    ) -> Iterator[StreamChunk]:
        """原生流式：逐块产出 ``StreamChunk``（content / thinking），增量边界查取消。

        原生实现 ``stream()`` 协议，使 ``ResilientLLMProvider.stream`` 经本类端到端
        可用。工具调用回合不支持流式（与 ``complete`` 一致），如传入 tools 则不开启流式。
        """
        # stream() 是 pull 风格，直接 yield；忽略可能传入的 push 风格 on_delta。
        opts.pop("on_delta", None)
        opts.pop("event_sink", None)  # 可选观测 sink：本层不透传给底层 API
        tools = opts.pop("tools", None)
        call_opts = {**self._default_options, **opts}
        if self._extra_body:
            call_opts["extra_body"] = {
                **self._extra_body,
                **call_opts.get("extra_body", {}),
            }
        if tools:
            call_opts["tools"] = _normalize_tools(tools)
        call_opts["stream"] = True
        call_opts.setdefault("stream_options", {"include_usage": True})
        payload = [_to_api_message(m) for m in messages]

        try:
            stream = self._client.chat.completions.create(
                model=self._model, messages=payload, **call_opts
            )
        except Exception:
            # 个别网关不认 stream_options，去掉重试一次。
            call_opts.pop("stream_options", None)
            stream = self._client.chat.completions.create(
                model=self._model, messages=payload, **call_opts
            )

        for chunk in stream:
            # 增量边界检查取消：取消视为正常终态，至多再产出 1 块即停止。
            if cancel_token is not None and cancel_token.cancelled:
                return
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield StreamChunk(kind="content", text=piece)
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield StreamChunk(kind="thinking", text=reasoning)

    def _complete_once(self, payload: list[dict], call_opts: dict) -> LLMResponse:
        resp = self._client.chat.completions.create(
            model=self._model, messages=payload, **call_opts
        )
        message = resp.choices[0].message
        # 部分支持思考的模型（如 Qwen）会返回 reasoning_content。
        reasoning = getattr(message, "reasoning_content", None)
        pt, ct = _usage_of(resp)
        return LLMResponse(
            content=message.content or "",
            reasoning=reasoning,
            prompt_tokens=pt,
            completion_tokens=ct,
            tool_calls=_parse_tool_calls(message),
        )

    def _complete_stream(
        self, payload: list[dict], call_opts: dict, on_delta=None
    ) -> LLMResponse:
        """流式调用：边接收边回调 on_delta，避免长响应看起来卡住。"""
        opts = dict(call_opts)
        # 请求在流末附带用量统计（OpenAI 兼容；部分网关支持）。
        opts.setdefault("stream_options", {"include_usage": True})
        try:
            stream = self._client.chat.completions.create(
                model=self._model, messages=payload, **opts
            )
        except Exception:
            # 个别网关不认 stream_options，去掉重试一次。
            opts.pop("stream_options", None)
            stream = self._client.chat.completions.create(
                model=self._model, messages=payload, **opts
            )

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        prompt_tokens = completion_tokens = None
        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", None)
                completion_tokens = getattr(usage, "completion_tokens", None)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                content_parts.append(piece)
                if on_delta:
                    on_delta("content", piece)
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_parts.append(reasoning)
                if on_delta:
                    on_delta("thinking", reasoning)
        return LLMResponse(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts) or None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
