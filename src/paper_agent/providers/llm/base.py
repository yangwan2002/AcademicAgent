"""LLM provider 接口。

所有智能体只依赖此抽象，不依赖任何具体大模型实现，
从而可在骨架阶段用 MockLLMProvider，后期无缝换成真实 provider。

支持工具调用（function calling）：complete 可传入 tools（OpenAI 风格 schema），
响应可能携带 tool_calls，由调用方执行并把结果作为 role="tool" 的消息回灌。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Protocol, runtime_checkable


class LLMError(Exception):
    """LLM 调用失败。"""


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str
    name: str
    arguments: dict


@dataclass
class ImageInput:
    """一张随消息携带的图像输入（多模态 / vision）。

    ``path`` 指向本地图片文件（优先，序列化时编码为 data: base64）；亦可直接给
    ``data_url``（已是 ``data:image/...;base64,...`` 或 http(s) URL）。仅多模态调用
    使用；纯文本消息不带图像，行为与现状一致。
    """

    path: str | None = None
    data_url: str | None = None
    media_type: str = "image/png"


@dataclass
class Message:
    role: str   # "system" | "user" | "assistant" | "tool"
    content: str
    # 仅 assistant 消息在请求工具时携带；回灌历史时需原样带回。
    tool_calls: list[ToolCall] | None = None
    # 仅 role="tool" 的消息携带，对应被回答的 ToolCall.id。
    tool_call_id: str | None = None
    # 仅多模态调用携带；None（默认）表示纯文本消息——序列化路径与现状逐字节一致。
    images: list["ImageInput"] | None = None


@dataclass
class LLMResponse:
    content: str
    reasoning: str | None = None  # 模型思考内容（如有，reasoning_content）
    prompt_tokens: int | None = None      # 真实用量（如 API 返回）
    completion_tokens: int | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)  # 模型请求的工具调用


@dataclass
class StreamChunk:
    """流式增量（升级 Req 5）。在 provider 层就近定义，避免 providers → workspace 的跨层依赖。

    kind ∈ {"content", "thinking"}：
    - "content"  ：构成最终 `LLMResponse.content` 的正文增量。
    - "thinking" ：模型思考内容增量（如有，reasoning_content）。
    """

    kind: str
    text: str


class CancellationToken:
    """协作式取消令牌（升级 Req 5）。

    调用方在希望中断流式输出时调用 `cancel()`；生产侧（provider）在每个
    增量边界检查 `cancelled`。取消被视为正常终态，不应抛出 `LLMError`。
    """

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """请求取消。可被多次调用，幂等。"""
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        """是否已被请求取消。"""
        return self._cancelled.is_set()


class CombinedCancellationToken(CancellationToken):
    """Cancellation requested locally, by a caller token, or by a deadline."""

    def __init__(
        self,
        parent: CancellationToken | None = None,
        *,
        deadline_at: float | None = None,
    ) -> None:
        super().__init__()
        self._parent = parent
        self._deadline_at = deadline_at

    @property
    def deadline_reached(self) -> bool:
        return self._deadline_at is not None and time.monotonic() >= self._deadline_at

    @property
    def cancelled(self) -> bool:
        return (
            super().cancelled
            or (self._parent is not None and self._parent.cancelled)
            or self.deadline_reached
        )


@runtime_checkable
class LLMProvider(Protocol):
    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        """同步一次性补全（向后兼容，保持现有签名与 on_delta 回调能力）。

        可选 `opts["event_sink"]`（`EventSink | None`，默认 None）：按调用传入、
        **不长存**的观测接收器。`ResilientLLMProvider` 用它发 `LLM_RETRY`；具体
        provider 仅吸收并忽略之（不透传给底层 API）。不传时行为逐字节不变。
        """
        ...

    def stream(
        self,
        messages: list[Message],
        *,
        cancel_token: CancellationToken | None = None,
        **opts,
    ) -> Iterator[StreamChunk]:
        """流式补全：逐块产出 `StreamChunk`（可选实现）。

        具体 provider MAY 实现此方法以获得真正的原生流式（mock/openai/anthropic
        均已原生实现）。

        语义约定（升级 Req 5）：
        - 在每个增量边界检查 `cancel_token.cancelled`，被取消时至多再产出
          1 个 `StreamChunk` 即停止并关闭底层流，且将取消视为正常终态而
          不抛出 `LLMError`。
        - content 增量按序拼接的结果应等于等价 `complete()` 的 `content`。
        """
        ...
