"""Generator（B3）：单意图的「生成 → Gate → 回灌重试」循环。

Week 1 只实现 polish。保持纯粹：输入原文 + 目标 + provider + gate，输出结果，
不碰文件系统（落盘由上层 SOP 负责），便于测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.gates.diff_gate import GateResult, PolishGate
from paper_agent.generate.context_builder import build_polish_messages
from paper_agent.providers.llm.base import LLMProvider


@dataclass
class PolishOutcome:
    ok: bool
    text: str | None                       # 通过 gate 的润色结果（ok=True 时有效）
    attempts: int                           # 实际 LLM 调用次数
    last_violations: list[str] = field(default_factory=list)  # 失败时的最后违规


def run_polish(
    original: str,
    goal: str,
    llm: LLMProvider,
    gate: PolishGate,
    *,
    max_retry: int = 2,
) -> PolishOutcome:
    """生成 → 校验 → 回灌重试。最多 ``1 + max_retry`` 次 LLM 调用。"""
    violations: list[str] = []
    attempts = 0
    result: GateResult | None = None
    for _ in range(max_retry + 1):
        messages = build_polish_messages(original, goal, violations or None)
        resp = llm.complete(messages)
        attempts += 1
        candidate = (resp.content or "").strip()
        result = gate.check(original, candidate)
        if result.passed:
            return PolishOutcome(ok=True, text=candidate, attempts=attempts)
        violations = result.violations

    return PolishOutcome(
        ok=False,
        text=None,
        attempts=attempts,
        last_violations=result.violations if result else ["未知错误"],
    )


__all__ = ["PolishOutcome", "run_polish"]
