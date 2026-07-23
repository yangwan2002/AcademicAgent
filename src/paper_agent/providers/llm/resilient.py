"""健壮性装饰器层：`ResilientLLMProvider`（升级 Req 4）。

把重试 / 超时 / 429 限流退避从具体 provider 内部上提为一个可复用的装饰器，
层叠在任意 `LLMProvider` 之外。这样所有 provider（含未来新增的原生适配器）自动
获得统一健壮性，且可选的 `event_sink` 能让观测层看到「重试中」事件。

本模块实现 `complete()` 与 `stream()` 的健壮重试。重试相关的纯函数
（`is_retryable` / `backoff_delay` / `retry_after_seconds`）与事件发射逻辑独立，
被 `complete()` 与 `stream()` 共同复用。

`stream()` 的可重试 / 不重试语义（升级 Req 5.7–5.9 / 4.9）：
- 尚未产出任何增量且错误可重试 → 按 `RetryPolicy` 重试；
- 已产出 ≥1 个增量后底层失败 → 抛出 `LLMError` 且**不**重试（避免重复输出），
  且保留已产出增量不回滚；
- 取消（`cancel_token.cancelled`）被内层视为正常终态，本层据此干净停止、不抛错。

可观测与安全（Req 10.6）：重试事件与日志**绝不**打印 API 密钥或完整请求体，
任何预览片段都截断到不超过 500 字符。
"""

from __future__ import annotations

import email.utils
import errno
import random
import socket
import time
from typing import Iterator

from paper_agent.observability.budget import (
    BudgetExceededError,
    clamp_timeout,
    current_run_budget,
)
from paper_agent.observability.events import Event, EventKind, EventSink
from paper_agent.providers.llm.base import (
    CancellationToken,
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    StreamChunk,
)
from paper_agent.workspace.models import RetryPolicy

# 预览片段长度上限（Req 10.6）：事件 / 日志中携带的任何文本预览不得超过此长度。
_PREVIEW_MAX_CHARS = 500

# 明确不可重试的异常类型名（鉴权 / 请求格式错误等永久性错误）。
# 借鉴 OpenAI SDK 的异常类命名；以类名匹配避免对 openai 包产生硬依赖。
_NON_RETRYABLE_NAMES = frozenset(
    {
        "AuthenticationError",
        "PermissionDeniedError",
        "BadRequestError",
        "NotFoundError",
        "UnprocessableEntityError",
        "ConflictError",
    }
)

# 明确可重试的异常类型名（限流 / 超时 / 连接 / 服务端错误）。
_RETRYABLE_NAMES = frozenset(
    {
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "APIConnectionTimeoutError",
        "InternalServerError",
        "Timeout",
        "ConnectTimeout",
        "ReadTimeout",
    }
)


def _status_code_of(exc: Exception) -> int | None:
    """防御式地从异常中提取 HTTP 状态码。

    兼容多种客户端约定：直接的 `status_code` / `status` 属性，或挂在
    `response` 对象上的 `status_code` / `status`。任何异常都吞掉，返回 None。
    """
    for attr in ("status_code", "status", "code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            val = getattr(response, attr, None)
            if isinstance(val, int):
                return val
    return None


def _is_connection_reset(exc: Exception) -> bool:
    """判断是否为连接重置 / 网络瞬时错误（如 WinError 10054、ECONNRESET）。"""
    if isinstance(exc, (ConnectionError, socket.timeout, TimeoutError)):
        return True
    err_no = getattr(exc, "errno", None)
    if err_no in {
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.ETIMEDOUT,
        errno.EPIPE,
    }:
        return True
    return False


def is_retryable(exc: Exception) -> bool:
    """区分可重试与不可重试异常（Req 4.3 / 4.4）。

    可重试：超时、连接重置、429（限流）、5xx 服务端错误。
    不可重试：鉴权失败（401/403）、400 请求格式错误等永久性错误。

    判定顺序：先看明确的状态码（最权威），再看异常类型名，最后看 stdlib
    网络错误。无法判定时保守地视为不可重试，避免对永久性错误做无谓重试。
    """
    name = type(exc).__name__

    # 1) 类名黑名单：明确不可重试。
    if name in _NON_RETRYABLE_NAMES:
        return False

    # 2) HTTP 状态码：最权威的判定依据。
    status = _status_code_of(exc)
    if status is not None:
        if status == 429:
            return True
        if 500 <= status <= 599:
            return True
        if status in (408, 409, 425):  # 请求超时 / 冲突 / too early
            return True
        if 400 <= status <= 499:
            # 其余 4xx（鉴权 / 格式错误等）不可重试。
            return False

    # 3) 类名白名单：明确可重试。
    if name in _RETRYABLE_NAMES:
        return True

    # 4) stdlib 网络 / 超时错误。
    if _is_connection_reset(exc):
        return True

    # 5) 兜底：无法判定，保守地视为不可重试。
    return False


def retry_after_seconds(exc: Exception) -> float | None:
    """从异常的响应头中解析 `Retry-After`（Req 4.5）。

    仅对 429 响应有效；支持「秒数」与「HTTP-date」两种格式。无法获取或解析
    失败时返回 None（调用方据此回退到退避公式）。返回值非负。
    """
    status = _status_code_of(exc)
    if status != 429:
        return None

    headers = _headers_of(exc)
    if not headers:
        return None

    raw = None
    for key in ("retry-after", "Retry-After", "Retry-after"):
        if key in headers:
            raw = headers[key]
            break
    if raw is None:
        # headers 可能是大小写不敏感映射；逐项扫描兜底。
        for k, v in headers.items():
            if str(k).lower() == "retry-after":
                raw = v
                break
    if raw is None:
        return None

    raw = str(raw).strip()
    # 形式一：纯秒数。
    try:
        secs = float(raw)
        return max(0.0, secs)
    except (ValueError, TypeError):
        pass

    # 形式二：HTTP-date（相对当前时间求差）。
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    delta = when.timestamp() - time.time()
    return max(0.0, delta)


def _headers_of(exc: Exception) -> dict | None:
    """防御式地取出异常携带的响应头映射。"""
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers is not None:
            return headers
    headers = getattr(exc, "headers", None)
    return headers


def backoff_delay(policy: RetryPolicy, attempt: int, exc: Exception) -> float:
    """计算第 `attempt`（从 0 开始）次失败后的休眠时长（Req 4.3 / 4.5 / 4.6）。

    - 当 `respect_retry_after` 为真且为 429 且响应含 `Retry-After` 时：优先采用
      `Retry-After`，并封顶到 `max_backoff`。
    - 否则按退避公式：`min(base_backoff * 2^attempt, max_backoff)` 叠加
      `[0, jitter]` 比例抖动；单次休眠封顶为 `max_backoff * (1 + jitter)`。
    """
    if policy.respect_retry_after:
        ra = retry_after_seconds(exc)
        if ra is not None:
            return min(ra, policy.max_backoff)

    base = min(policy.base_backoff * (2 ** attempt), policy.max_backoff)
    delay = base * (1 + random.uniform(0, policy.jitter))
    # 单次休眠封顶（Req 4.3）。
    cap = policy.max_backoff * (1 + policy.jitter)
    return min(delay, cap)


def _error_category(exc: Exception) -> str:
    """归类异常用于事件载荷（不含敏感内容）。"""
    status = _status_code_of(exc)
    if status == 429:
        return "rate_limit"
    if status is not None and 500 <= status <= 599:
        return "server_error"
    if _is_connection_reset(exc):
        return "connection"
    return type(exc).__name__


def _safe_preview(text: object) -> str:
    """生成安全的文本预览：截断到不超过 500 字符（Req 10.6）。"""
    s = str(text)
    if len(s) > _PREVIEW_MAX_CHARS:
        return s[:_PREVIEW_MAX_CHARS]
    return s


class ResilientLLMProvider(LLMProvider):
    """健壮性装饰器：为内层 `LLMProvider` 提供统一的重试 / 退避 / 429 处理。

    层叠位置：`ResilientLLMProvider`（本层）→ 具体 `LLMProvider`（内）。

    Args:
        inner: 被包装的具体 provider。
        policy: 重试策略；为空则采用默认 `RetryPolicy`。
        sink: 可选事件接收器；重试时发出 `LLM_RETRY` 事件。
    """

    def __init__(
        self,
        inner: LLMProvider,
        policy: RetryPolicy | None = None,
        sink: EventSink | None = None,
    ) -> None:
        self._inner = inner
        self._policy = policy or RetryPolicy()
        self._sink = sink

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        """带退避与 429 处理的健壮补全（算法 4 / Req 4）。

        前置：`inner.complete` 可能抛出瞬时（可重试）或永久（不可重试）错误。
        后置：返回成功响应，或在耗尽重试 / 遇不可重试错误后抛出 `LLMError`，
        并保留底层原因。底层总调用次数 ≤ `max_retries + 1`；不可重试错误
        恰好调用底层一次并立即抛出。

        #1：当调用方传入 push 风格的 ``on_delta`` 回调（如 ctrip 预设的
        ``stream=True`` 经 ``complete`` 流式）且已向下游产出过增量时，任何
        底层失败都**不重试**——重试会让 ``on_delta`` 从头再推一遍，造成重复
        输出（与 ``stream()`` 的 ``produced_any`` 语义对齐）。
        """
        # 按调用传入的观测 sink（不长存）；缺省回退构造期 sink（向后兼容）。
        # 消费后**不**透传给内层，避免内层再吸收一次。
        sink = opts.pop("event_sink", None) or self._sink
        # 若调用方提供了 on_delta，包一层守卫以追踪「是否已产出增量」。
        raw_on_delta = opts.get("on_delta")
        produced_any = False
        if raw_on_delta is not None:
            def _guarded_delta(kind: str, text: str) -> None:
                nonlocal produced_any
                produced_any = True
                raw_on_delta(kind, text)
            opts = {**opts, "on_delta": _guarded_delta}

        last_exc: Exception | None = None
        for attempt in range(self._policy.max_retries + 1):
            # Recompute before every attempt: a timeout captured for the first
            # request must never let a retry cross the shared run deadline.
            clamp_timeout(opts)
            try:
                return self._inner.complete(messages, **opts)
            except BudgetExceededError:
                raise
            except LLMError:
                # 内层已是终态错误（如具体 provider 自身耗尽兜底重试后抛出），
                # 直接上抛，不在本层重复重试。
                raise
            except Exception as exc:  # noqa: BLE001 - 需区分可重试与否
                last_exc = exc
                # 已产出增量：不重试，避免重复输出（#1）。
                if produced_any:
                    raise LLMError(
                        "流式 complete 在已产出增量后失败，不重试以避免重复输出："
                        f"{exc}"
                    ) from exc
                if attempt >= self._policy.max_retries or not is_retryable(exc):
                    break
                delay = backoff_delay(self._policy, attempt, exc)
                self._emit_retry(sink, attempt + 1, delay, exc)
                self._deadline_aware_sleep(delay)

        raise LLMError(
            f"LLM 调用失败（重试 {self._policy.max_retries} 次）：{last_exc}"
        ) from last_exc

    def stream(
        self,
        messages: list[Message],
        *,
        cancel_token: CancellationToken | None = None,
        **opts,
    ) -> Iterator[StreamChunk]:
        """带可重试 / 不重试语义的流式补全（算法见 design 时序 3；Req 5.7–5.9 / 4.9）。

        前置：`inner` 原生实现 `stream`，在每个增量边界检查 `cancel_token` 并把
        取消视为正常终态。

        后置：
        - 尚未向下游产出任何增量、底层失败且错误可重试且仍有重试余量 →
          发出 `LLM_RETRY`、按退避休眠后用全新的内层流重试（此时下游未收到任何
          增量，重试不会造成重复输出）。
        - 已向下游产出 ≥1 个增量后底层失败 → 抛出 `LLMError` 且**不**重试，
          已产出增量保留、不回滚（一旦 `yield` 给下游便无法撤回）。
        - 底层正常结束或因取消而停止 → 视为正常终态，干净返回、不抛错。
        - 不可重试错误或耗尽重试仍失败（且尚未产出增量）→ 抛出 `LLMError`
          并保留底层原因。底层流的总建立次数 ≤ `max_retries + 1`。
        """
        # 按调用传入的观测 sink（不长存）；缺省回退构造期 sink。消费后不透传内层。
        sink = opts.pop("event_sink", None) or self._sink
        # 跨重试持久：一旦向下游 yield 过任何增量即置真，此后任何底层失败都
        # 不得重试（避免重复输出）。
        produced_any = False
        last_exc: Exception | None = None

        for attempt in range(self._policy.max_retries + 1):
            clamp_timeout(opts)
            try:
                for chunk in self._inner.stream(
                    messages, cancel_token=cancel_token, **opts
                ):
                    produced_any = True
                    yield chunk
                # 内层迭代器自然耗尽：正常完成或因取消而干净停止，均为正常终态。
                return
            except BudgetExceededError:
                raise
            except LLMError:
                # 内层已是终态错误（如具体 provider 自身耗尽兜底重试后抛出），
                # 直接上抛、不在本层重试，与 complete() 行为保持一致。
                raise
            except Exception as exc:  # noqa: BLE001 - 需区分可重试与否
                last_exc = exc
                # 已产出至少一个增量：不可重试，抛 LLMError 并保留底层原因；
                # 已产出增量保留、不回滚（Req 5.7 / 5.8 / 4.9）。
                if produced_any:
                    raise LLMError(
                        f"流式调用在已产出增量后失败，不重试以避免重复输出：{exc}"
                    ) from exc
                # 尚未产出任何增量：可重试且仍有余量则按策略重试（Req 5.9）。
                if attempt >= self._policy.max_retries or not is_retryable(exc):
                    break
                delay = backoff_delay(self._policy, attempt, exc)
                self._emit_retry(sink, attempt + 1, delay, exc)
                self._deadline_aware_sleep(delay)

        # 不可重试，或耗尽全部重试仍失败（且尚未产出增量）：抛出并保留底层原因。
        raise LLMError(
            f"流式调用失败（重试 {self._policy.max_retries} 次）：{last_exc}"
        ) from last_exc

    # --- 内部：事件发射 ---

    @staticmethod
    def _deadline_aware_sleep(delay: float) -> None:
        """Sleep for backoff without starting another attempt past deadline."""
        context = current_run_budget()
        if context is None or context.duration_cap_s <= 0:
            time.sleep(delay)
            return
        remaining = context.remaining_s
        if remaining <= 0:
            raise context.expire_deadline()
        time.sleep(min(delay, remaining))
        context.check()
        if delay >= remaining:
            raise context.expire_deadline()

    def _emit_retry(
        self, sink: EventSink | None, attempt: int, delay: float, exc: Exception
    ) -> None:
        """发出 `LLM_RETRY` 事件（Req 4.8 / 10.6）。

        载荷含重试序号、计划休眠时长、异常类别与安全的原因预览；
        绝不打印 API 密钥或完整请求体。`sink` 为按调用解析出的观测接收器。
        """
        if sink is None:
            return
        category = _error_category(exc)
        sink.emit(
            Event(
                kind=EventKind.LLM_RETRY,
                message=f"LLM 调用失败，准备第 {attempt} 次重试（{category}）",
                data={
                    "attempt": attempt,
                    "delay": round(delay, 3),
                    "error_type": category,
                    "error_preview": _safe_preview(exc),
                },
            )
        )
