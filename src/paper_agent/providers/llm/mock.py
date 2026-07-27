"""确定性 Mock LLM provider。

返回可预测内容，供骨架运行与单元测试使用，零网络依赖。
- scripted：按序返回的脚本项；每项可以是 str（作为正文）或 list[ToolCall]
  （作为一次工具调用回合）。
- 默认（无脚本）：回显最后一条 user 消息，便于断言。

原生实现 ``stream()``，使默认 mock 装配下流式路径亦可走通。``complete`` 保持
非流式语义（不触发 ``on_delta``）；流式增量由 ``stream()`` 独立产出。
"""

from __future__ import annotations

from typing import Iterator

from paper_agent.providers.llm.base import (
    CancellationToken,
    LLMProvider,
    LLMResponse,
    Message,
    StreamChunk,
    ToolCall,
)


class MockLLMProvider(LLMProvider):
    def __init__(self, scripted: list | None = None) -> None:
        self._scripted = list(scripted or [])
        self.calls: list[list[Message]] = []

    def _decide(self, messages: list[Message]) -> LLMResponse:
        """确定本次响应（scripted 或回显），不触发流式回调。"""
        if self._scripted:
            item = self._scripted.pop(0)
            if isinstance(item, list):  # 一回合工具调用
                return LLMResponse(content="", tool_calls=item)
            if isinstance(item, ToolCall):
                return LLMResponse(content="", tool_calls=[item])
            return LLMResponse(content=str(item))
        # 缺省：回显最后一条 user 消息。
        last = ""
        for m in reversed(messages):
            if m.role == "user":
                last = m.content
                break
        return LLMResponse(content=f"[mock] {last}")

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        # on_delta 由 StreamingMixin 风格的调用方可能传入；Mock 的 complete 保持
        # 非流式语义，忽略回调（流式请用 stream()）。
        opts.pop("on_delta", None)
        opts.pop("event_sink", None)  # 可选观测 sink：Mock 不发事件，吸收即可
        opts.pop("forced_tool_name", None)  # 结构化调用的中立参数：Mock 吸收即可
        self.calls.append(messages)
        return self._decide(messages)

    def stream(
        self,
        messages: list[Message],
        *,
        cancel_token: CancellationToken | None = None,
        **opts,
    ) -> Iterator[StreamChunk]:
        """原生流式：按整段产出一个 content 增量；取消视为正常终态。

        工具调用回合（无正文）不产出任何增量。与 ``complete()`` 的 content 一致。
        """
        opts.pop("event_sink", None)  # 可选观测 sink：Mock 不发事件，吸收即可
        if cancel_token is not None and cancel_token.cancelled:
            return
        self.calls.append(messages)
        resp = self._decide(messages)
        if resp.content:
            if cancel_token is not None and cancel_token.cancelled:
                return
            yield StreamChunk(kind="content", text=resp.content)
