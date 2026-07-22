"""一次工作流共享的硬预算上下文。"""

from __future__ import annotations

import time
import queue
import threading
from contextvars import copy_context
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Callable, TypeVar


_T = TypeVar("_T")


class BudgetExceededError(RuntimeError):
    """调用在发出前被 token、调用次数或墙钟硬预算拒绝。"""

    def __init__(self, reason: str, *, limit: float = 0, observed: float = 0) -> None:
        self.reason = reason
        self.limit = limit
        self.observed = observed
        super().__init__(f"{reason} budget exceeded: observed={observed}, limit={limit}")


@dataclass
class RunBudgetContext:
    """从 ``Orchestrator.run`` 入口开始计时、供整条调用链共享。"""

    token_cap: int = 0
    duration_cap_s: float = 0.0
    call_cap: int = 0
    started_at: float = field(default_factory=time.monotonic)
    exceeded_reason: str = ""

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_s(self) -> float:
        if self.duration_cap_s <= 0:
            return float("inf")
        return max(0.0, self.duration_cap_s - self.elapsed_s)

    def check(self, *, total_tokens: int = 0, calls: int = 0, reserve_tokens: int = 0) -> None:
        if self.exceeded_reason:
            raise BudgetExceededError(self.exceeded_reason)
        if self.duration_cap_s > 0 and self.elapsed_s >= self.duration_cap_s:
            self.exceeded_reason = "deadline"
            raise BudgetExceededError(
                "deadline", limit=self.duration_cap_s, observed=self.elapsed_s
            )
        if self.call_cap > 0 and calls >= self.call_cap:
            self.exceeded_reason = "llm_calls"
            raise BudgetExceededError("llm_calls", limit=self.call_cap, observed=calls)
        projected = total_tokens + max(0, reserve_tokens)
        if self.token_cap > 0 and (
            total_tokens >= self.token_cap or projected > self.token_cap
        ):
            self.exceeded_reason = "tokens"
            raise BudgetExceededError(
                "tokens", limit=self.token_cap, observed=projected
            )

    def expire_deadline(self) -> BudgetExceededError:
        """Atomically mark the shared wall-clock budget as exhausted."""
        self.exceeded_reason = "deadline"
        return BudgetExceededError(
            "deadline", limit=self.duration_cap_s, observed=self.elapsed_s
        )


_CURRENT: ContextVar[RunBudgetContext | None] = ContextVar(
    "paper_agent_run_budget", default=None
)


def current_run_budget() -> RunBudgetContext | None:
    return _CURRENT.get()


def activate_run_budget(context: RunBudgetContext) -> Token:
    return _CURRENT.set(context)


def reset_run_budget(token: Token) -> None:
    _CURRENT.reset(token)


def remaining_deadline_s() -> float:
    """Return the global remaining wall-clock budget, or infinity."""
    context = current_run_budget()
    if context is None or context.duration_cap_s <= 0:
        return float("inf")
    context.check()
    return context.remaining_s


def clamp_timeout(opts: dict) -> float:
    """Clamp a provider ``timeout`` option to the global remaining deadline."""
    remaining = remaining_deadline_s()
    if remaining == float("inf"):
        return remaining
    configured = opts.get("timeout")
    try:
        requested = float(configured) if configured is not None else remaining
    except (TypeError, ValueError):
        requested = remaining
    opts["timeout"] = max(0.0, min(requested, remaining))
    return remaining


def call_with_deadline(call: Callable[[], _T], timeout_s: float) -> _T:
    """Run a blocking call without allowing it to hold the main flow past deadline.

    Python cannot safely kill a thread.  A non-cooperative provider is therefore
    isolated in a daemon thread, which cannot prevent process exit.  Only the
    result delivered before the deadline is observed by the caller.
    """
    if timeout_s == float("inf"):
        return call()
    context = current_run_budget()
    if timeout_s <= 0:
        if context is not None:
            raise context.expire_deadline()
        raise BudgetExceededError("deadline", limit=0, observed=0)

    result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
    caller_context = copy_context()

    def run() -> None:
        try:
            value: tuple[bool, object] = (True, caller_context.run(call))
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller
            value = (False, exc)
        try:
            result.put_nowait(value)
        except queue.Full:
            pass

    threading.Thread(target=run, name="paper-agent-provider", daemon=True).start()
    try:
        succeeded, value = result.get(timeout=timeout_s)
    except queue.Empty:
        if context is not None:
            raise context.expire_deadline()
        raise BudgetExceededError("deadline", limit=timeout_s, observed=timeout_s)
    if context is not None:
        # The provider and timeout wake-up may race at the boundary.  A result
        # that arrived after the global deadline is still a late result.
        context.check()
    if succeeded:
        return value  # type: ignore[return-value]
    raise value  # type: ignore[misc]


__all__ = [
    "BudgetExceededError",
    "RunBudgetContext",
    "activate_run_budget",
    "call_with_deadline",
    "clamp_timeout",
    "current_run_budget",
    "remaining_deadline_s",
    "reset_run_budget",
]
