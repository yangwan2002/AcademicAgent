"""结构化 LLM 调用的中立收敛点（A2a 从 tasking 迁入）。

历史:``structured_call`` 三件套原住 ``tasking/extractors/base.py``、``CallStatus`` 原住
``tasking/contracts.py``。它们其实是**provider 层**的通用概念（「调一次 LLM 拿回结构化
payload、四态失败分类」),不该只属于规划层。迁到这里后:

- ``tasking`` 与 ``literature`` 都从中立处导入,``literature`` 不再需要依赖 ``tasking``
  （断掉架构倒挂）。
- ``tasking.contracts`` / ``tasking.extractors.base`` 保留 re-export,既有导入点零改动。

**本模块不得 import tasking**,只依赖 ``providers.llm.base`` 与标准库。
迁移为**零行为变化**:实现逐字保留。
"""

from __future__ import annotations

import json
import threading
import time
from enum import Enum

from paper_agent.observability.current import ambient_publish, ambient_span
from paper_agent.providers.llm.base import LLMProvider, Message


class CallStatus(str, Enum):
    """一次抽取/分析调用的整体状态（不与字段态折叠）。"""

    SUCCESS = "success"
    PROVIDER_FAILED = "provider_failed"
    PARSE_FAILED = "parse_failed"
    SCHEMA_FAILED = "schema_failed"


class StructuredCallResult:
    """一次结构化调用的结果：调用态 + payload（成功时）+ 错误类别。"""

    __slots__ = (
        "call_status",
        "payload",
        "error_category",
        "provider_attempts",
        "compatibility_retries",
    )

    def __init__(
        self,
        call_status: CallStatus,
        payload: dict | None = None,
        error_category: str | None = None,
        *,
        provider_attempts: int = 0,
        compatibility_retries: int = 0,
    ) -> None:
        self.call_status = call_status
        self.payload = payload
        self.error_category = error_category
        self.provider_attempts = provider_attempts
        self.compatibility_retries = compatibility_retries


def _extract_json_object(text: str) -> dict:
    """从模型文本输出里取第一段 JSON 对象；失败抛 ValueError/JSONDecodeError。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no json object")
    obj = json.loads(s[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("not a json object")
    return obj


def _forced_tool_name(tool_schema: dict) -> str:
    """要强制调用的工具名。**协议形态由各 provider 自行适配**（#8）——
    统一层只传 `forced_tool_name`，不拼 Anthropic/OpenAI 各异的 `tool_choice` 结构。
    """
    fn = tool_schema.get("function", tool_schema) if isinstance(tool_schema, dict) else {}
    return str(fn.get("name", "") or "")


# 只有「参数不被支持」才值得去掉 forced_tool_name 重试一次；
# 超时/限流/网络错误重试会造成重复请求与延迟翻倍（由 Resilient 层负责退避）。
_UNSUPPORTED_HINTS = (
    "forced_tool_name", "tool_choice", "unexpected keyword", "unknown parameter",
    "unsupported parameter", "invalid_request_error", "extra fields not permitted",
)


def _looks_unsupported(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    if isinstance(exc, TypeError):
        return True          # provider 签名不认识该参数
    return any(h in text for h in _UNSUPPORTED_HINTS)


class _ForcedToolCapabilityState:
    """Operation-independent capability memory owned by one provider instance.

    The first caller probes forced tool choice.  Concurrent callers wait for that
    probe instead of repeating it.  A deterministic unsupported response freezes
    the provider in compatibility mode; transient failures leave it unknown.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._supported: bool | None = None
        self._probing = False

    def acquire_mode(self) -> str:
        with self._condition:
            while self._probing:
                self._condition.wait()
            if self._supported is True:
                return "forced"
            if self._supported is False:
                return "compatible"
            self._probing = True
            return "probe"

    def finish_probe(self, supported: bool | None) -> None:
        with self._condition:
            if supported is not None:
                self._supported = supported
            self._probing = False
            self._condition.notify_all()

    def downgrade(self) -> None:
        with self._condition:
            self._supported = False
            self._probing = False
            self._condition.notify_all()


_CAPABILITY_STATE_ATTR = "_paper_agent_forced_tool_capability_state"
_CAPABILITY_INIT_LOCK = threading.Lock()


def _capability_owner(llm):
    """Unwrap explicit provider decorators without relying on global identity maps."""

    current = llm
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        owner = getattr(current, "structured_call_capability_owner", None)
        if owner is None:
            return current
        current = owner() if callable(owner) else owner
    return current


def _capability_state(llm) -> _ForcedToolCapabilityState:
    owner = _capability_owner(llm)
    state = getattr(owner, _CAPABILITY_STATE_ATTR, None)
    if isinstance(state, _ForcedToolCapabilityState):
        return state
    with _CAPABILITY_INIT_LOCK:
        state = getattr(owner, _CAPABILITY_STATE_ATTR, None)
        if isinstance(state, _ForcedToolCapabilityState):
            return state
        state = _ForcedToolCapabilityState()
        try:
            setattr(owner, _CAPABILITY_STATE_ATTR, state)
        except (AttributeError, TypeError):
            # Slot-only third-party providers still work; they simply cannot
            # retain the adaptive capability between calls.
            pass
        return state


def structured_call(
    llm: LLMProvider,
    messages: list[Message],
    *,
    tool_schema: dict | None = None,
    call_options: dict | None = None,
) -> StructuredCallResult:
    """调 LLM 拿结构化 payload；四态失败分类，绝不塌成空。

    统一走 function calling（`tool_schema` 必给）：`tool_choice` 强制调用，读
    `tool_calls[0].arguments`；模型若未走工具而回了正文，则按 JSON 兜底解析。
    未提供 `tool_schema` 时退化为「解析正文 JSON」（仅供无 schema 的调用方）。

    这是全项目结构化输出的唯一收敛点；换实现只改此函数。

    **观测**：本函数是全项目结构化 LLM 调用的**唯一**收敛点，因此也是给
    relevance / relevance_batch / query_planner / extractors 一次性埋 span 的
    唯一位置。span 名取 `llm_call:<工具名>`（工具名来自 schema，非用户输入，
    可安全入 trace）；耗时与四态结果记在 span 属性上。

    此前 `literature/` 全目录零 bus 接线 ⇒ 一次 1057 秒的 run 里那个 945 秒的
    `operation:*` span 是 `attributes={} children=[]`，说不出时间花在哪。
    埋点走 ambient bus（`observability.current`），不改本函数签名，也不动上游
    任何一层 —— 见该模块 docstring 对「为何不显式穿 bus」的论证。
    """
    label = _forced_tool_name(tool_schema) if tool_schema is not None else "plain_json"
    started = time.monotonic()
    with ambient_span(f"llm_call:{label or 'unnamed'}"):
        result = _structured_call(
            llm, messages, tool_schema=tool_schema, call_options=call_options
        )
        _annotate(result, started, label or "unnamed")
    return result


def _annotate(result: StructuredCallResult, started: float, label: str = "") -> None:
    """把调用结果挂到当前 span 上，发 `LLM_CALL_END`。

    走事件而非改 span 属性：`SimpleTraceRecorder` 只在 `SPAN_START` 时读一次
    `attrs`，之后改 `SpanScope._attrs` 不会进 trace；而 `LLM_CALL_END` 有既成的
    `_attach` → `_merge_attrs` 路径，会并进所属 span 的 attributes。
    """
    data = {
        "status": result.call_status.value,
        "provider_attempts": result.provider_attempts,
        "compatibility_retries": result.compatibility_retries,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
    }
    # label 此前只存在于 span 名里。做时间归因时要按用途分桶
    # （relevance / query_planner / extractor 各占多少秒），事件里必须自带。
    if label:
        data["label"] = label
    if result.error_category is not None:   # 成功时不写 `error: null`（少一行噪声）
        data["error"] = result.error_category
    ambient_publish("LLM_CALL_END", **data)


def _structured_call(
    llm: LLMProvider,
    messages: list[Message],
    *,
    tool_schema: dict | None = None,
    call_options: dict | None = None,
) -> StructuredCallResult:
    """`structured_call` 的实现体（逐字保留原逻辑；外层只加 span，不改行为）。"""
    # 1) provider 调用
    use_tools = tool_schema is not None
    options = dict(call_options or {})
    if "tools" in options or "forced_tool_name" in options:
        raise ValueError("call_options cannot override structured-call protocol options")
    provider_attempts = 0
    compatibility_retries = 0
    capability = _capability_state(llm) if use_tools else None
    mode = capability.acquire_mode() if capability is not None else "plain"
    try:
        if use_tools:
            provider_attempts += 1
            if mode == "compatible":
                resp = llm.complete(messages, tools=[tool_schema], **options)
            else:
                # 只传中立的 forced_tool_name；具体 tool_choice 形态由 provider 适配。
                resp = llm.complete(
                    messages, tools=[tool_schema],
                    forced_tool_name=_forced_tool_name(tool_schema),
                    **options,
                )
                if mode == "probe":
                    capability.finish_probe(True)
        else:
            provider_attempts += 1
            resp = llm.complete(messages, **options)
    except Exception as exc:  # noqa: BLE001
        # **只**在「该参数不被支持」时降级重试一次；超时/限流等一律直接失败，
        # 交由 Resilient 层退避，避免重复请求与延迟翻倍（#8）。
        if use_tools and mode != "compatible" and _looks_unsupported(exc):
            capability.downgrade()
            compatibility_retries += 1
            try:
                provider_attempts += 1
                resp = llm.complete(messages, tools=[tool_schema], **options)
            except Exception as exc2:  # noqa: BLE001
                return StructuredCallResult(
                    CallStatus.PROVIDER_FAILED,
                    None,
                    type(exc2).__name__,
                    provider_attempts=provider_attempts,
                    compatibility_retries=compatibility_retries,
                )
        else:
            if use_tools and mode == "probe":
                capability.finish_probe(None)
            return StructuredCallResult(
                CallStatus.PROVIDER_FAILED,
                None,
                type(exc).__name__,
                provider_attempts=provider_attempts,
                compatibility_retries=compatibility_retries,
            )

    # 2) 取 payload
    if use_tools:
        calls = getattr(resp, "tool_calls", None) or []
        if calls:
            args = calls[0].arguments
            if isinstance(args, dict):
                return StructuredCallResult(
                    CallStatus.SUCCESS,
                    args,
                    None,
                    provider_attempts=provider_attempts,
                    compatibility_retries=compatibility_retries,
                )
            return StructuredCallResult(
                CallStatus.SCHEMA_FAILED,
                None,
                "tool_args_not_dict",
                provider_attempts=provider_attempts,
                compatibility_retries=compatibility_retries,
            )
        # 模型没走工具而是回了正文 → 兜底按 JSON 解析（不塌成失败）；
        # 正文也不是 JSON → parse_failed（确实"解析"失败，非结构不符）。
        try:
            payload = _extract_json_object((resp.content or "").strip())
        except (ValueError, json.JSONDecodeError) as exc:
            return StructuredCallResult(
                CallStatus.PARSE_FAILED,
                None,
                type(exc).__name__,
                provider_attempts=provider_attempts,
                compatibility_retries=compatibility_retries,
            )
        return StructuredCallResult(
            CallStatus.SUCCESS,
            payload,
            None,
            provider_attempts=provider_attempts,
            compatibility_retries=compatibility_retries,
        )

    content = (resp.content or "").strip()
    try:
        payload = _extract_json_object(content)
    except (ValueError, json.JSONDecodeError) as exc:
        return StructuredCallResult(
            CallStatus.PARSE_FAILED,
            None,
            type(exc).__name__,
            provider_attempts=provider_attempts,
            compatibility_retries=compatibility_retries,
        )
    return StructuredCallResult(
        CallStatus.SUCCESS,
        payload,
        None,
        provider_attempts=provider_attempts,
        compatibility_retries=compatibility_retries,
    )


__all__ = [
    "CallStatus",
    "StructuredCallResult",
    "structured_call",
]
