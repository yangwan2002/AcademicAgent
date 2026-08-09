"""Generator（B3）：单意图的「生成 → Gate → 回灌重试」循环。

Week 1 实现 polish；streaming-and-trace 在此接入观测脊柱与流式：
- 关联层级 `generation_id > attempt > llm_call_id`（design D1），用 scope 管理、退出自动恢复。
- span：`generation_attempt > llm_call > gate`（gate 与 llm_call 平级、同挂 attempt）。
- 每个 `LLM_CALL_START` 恰一 `LLM_CALL_END`（completed/cancelled/failed），
  每个 `GENERATION_ATTEMPT_START` 恰一 `GENERATION_ATTEMPT_END`（finally 保证）。
- 取消经 `OperationCancelled` **一路 raise 到 Coordinator**（不吞成 Outcome，D4）。
- 隐私：prompt→SafePrompt、response→SafeText 在**发布前**脱敏；thinking 只发标记、无原文、不入 buffer。

**默认零影响（Property 6）**：`bus=None`（→NullEventBus）+ `stream=False` 时，
走 `llm.complete(messages)`、不传 event_sink，与改造前逐字节一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from paper_agent.errors import OperationCancelled
from paper_agent.gates.diff_gate import GateResult, PolishGate
from paper_agent.generate.context_builder import build_polish_messages
from paper_agent.observability.bus import NullEventBus
from paper_agent.observability.events import EventKind
from paper_agent.observability.sanitize import sanitize_messages, sanitize_text
from paper_agent.providers.llm.base import LLMError, LLMProvider

# Candidate_Buffer 安全上限（防失控流无限累积）；正常文档远不触及（50万字符≈250页）。
# 超限视为「模型跑飞」的异常，抛错**大声失败**（→ LLM_CALL_END(failed)），
# 绝不静默截断把半截文本冒充完整结果。
_MAX_CANDIDATE_CHARS = 500_000
_MAX_DELTA_COUNT = 100_000


@dataclass
class PolishOutcome:
    ok: bool
    text: str | None                       # 通过 gate 的润色结果（ok=True 时有效）
    attempts: int                           # 实际 LLM 调用次数
    last_violations: list[str] = field(default_factory=list)  # 失败时的最后违规
    error_category: str | None = None       # 失败类别（如 validation_exhausted）


def _model_name(llm: LLMProvider) -> str:
    for attr in ("model_name", "_model", "model"):
        val = getattr(llm, attr, None)
        if isinstance(val, str) and val:
            return val
    return type(llm).__name__


def _provider_kwargs(bus) -> dict:
    """观测启用 → 传 event_sink；关闭 → 空 dict（provider 走原始签名，Property 6）。"""
    sink = bus.as_sink_or_none()
    return {"event_sink": sink} if sink is not None else {}


def _generate(llm, messages, stream, bus, cancel_token):
    """执行一次 LLM 调用，返回 (candidate, usage, usage_status, delta_count)。

    stream=False（默认）：走 complete()，与现状一致；delta_count=0。
    stream=True：走 stream()，content 增量累积进 Candidate_Buffer 并发 LLM_DELTA，
    thinking 只发标记（无原文、不入 buffer），每个增量边界查取消。
    """
    extra = _provider_kwargs(bus)
    if not stream:
        resp = llm.complete(messages, **extra)
        candidate = (resp.content or "").strip()
        pt, ct = resp.prompt_tokens, resp.completion_tokens
        usage = {"prompt_tokens": pt, "completion_tokens": ct}
        status = "reported" if (pt is not None or ct is not None) else "unavailable"
        return candidate, usage, status, 0

    parts: list[str] = []
    total = 0
    n = 0
    for chunk in llm.stream(messages, cancel_token=cancel_token, **extra):
        if cancel_token is not None and cancel_token.cancelled:
            raise OperationCancelled()
        if chunk.kind == "content":
            text = chunk.text or ""
            # 超限即失败：先于入 buffer/发事件判定，避免把越界内容当作有效候选。
            if total + len(text) > _MAX_CANDIDATE_CHARS or n + 1 > _MAX_DELTA_COUNT:
                raise LLMError(
                    f"流式候选超过安全上限（累计 {total + len(text)} 字符 / "
                    f"{n + 1} 块），疑似模型失控，已中止本次生成"
                )
            parts.append(text)
            total += len(text)
            bus.publish(EventKind.LLM_DELTA, text=text, chunk_index=n)
            n += 1
        elif chunk.kind == "thinking":
            # 只发标记事件：无原文、不入 Candidate_Buffer（Property 3）。
            bus.publish(EventKind.LLM_THINKING)
    candidate = "".join(parts).strip()
    usage = {"prompt_tokens": None, "completion_tokens": None}
    return candidate, usage, "unavailable", n


def run_polish(
    original: str,
    goal: str,
    llm: LLMProvider,
    gate: PolishGate,
    *,
    max_retry: int = 2,
    bus=None,
    cancel_token=None,
    stream: bool = False,
    constraints: list[str] | None = None,
    source_text: str = "",
) -> PolishOutcome:
    """生成 → 校验 → 回灌重试。最多 ``1 + max_retry`` 次 LLM 调用。

    ``constraints``：用户显式硬约束，透传给 ContextBuilder（Task 19 执行绑定）。
    为空/None 时 prompt 与改造前逐字节一致。
    """
    bus = bus or NullEventBus()
    generation_id = uuid4().hex
    violations: list[str] = []
    result: GateResult | None = None

    with bus.generation_scope(generation_id):
        for attempt in range(1, max_retry + 2):
            with bus.attempt_scope(attempt), bus.span("generation_attempt") as aspan:
                attempt_status = "error"
                try:
                    bus.publish(EventKind.GENERATION_ATTEMPT_START, attempt=attempt)
                    if attempt > 1:
                        bus.publish(EventKind.GATE_RETRY, attempt=attempt)

                    messages = build_polish_messages(
                        original, goal, violations or None, constraints or None,
                        source_text=source_text,
                    )
                    call_id = uuid4().hex
                    candidate_id = uuid4().hex

                    with bus.llm_call_scope(call_id), bus.span("llm_call"):
                        try:
                            bus.publish(
                                EventKind.LLM_CALL_START,
                                model=_model_name(llm),
                                call_mode=("stream" if stream else "complete"),
                                prompt=sanitize_messages(messages),
                            )
                            candidate, usage, ustatus, n = _generate(
                                llm, messages, stream, bus, cancel_token
                            )
                            bus.publish(
                                EventKind.LLM_USAGE,
                                usage_status=ustatus,
                                **usage,
                            )
                            bus.publish(
                                EventKind.LLM_CALL_END,
                                status="completed",
                                response=sanitize_text(candidate),
                                delta_count=n,
                            )
                        except OperationCancelled:
                            bus.publish(EventKind.LLM_CALL_END, status="cancelled")
                            raise
                        except Exception as exc:
                            bus.publish(
                                EventKind.LLM_CALL_END,
                                status="failed",
                                error=sanitize_text(str(exc)),
                            )
                            raise

                    with bus.span("gate"):
                        bus.publish(EventKind.GATE_START)
                        result = gate.check(original, candidate)
                        bus.publish(
                            EventKind.GATE_RESULT,
                            passed=result.passed,
                            violations=result.violations,
                            candidate_id=candidate_id,
                            source_llm_call_id=call_id,
                        )

                    if result.passed:
                        attempt_status = "accepted"
                        return PolishOutcome(ok=True, text=candidate, attempts=attempt)
                    attempt_status = "rejected"
                    violations = result.violations
                except OperationCancelled:
                    attempt_status = "cancelled"
                    aspan.cancel()
                    raise  # 一路 raise 到 Coordinator（D4）
                finally:
                    bus.publish(
                        EventKind.GENERATION_ATTEMPT_END, status=attempt_status
                    )

    return PolishOutcome(
        ok=False,
        text=None,
        attempts=max_retry + 1,
        last_violations=result.violations if result else ["未知错误"],
        error_category="validation_exhausted",
    )


__all__ = ["PolishOutcome", "run_polish"]
