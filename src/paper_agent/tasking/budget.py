"""理解层预算（#9）：让 config 里的上限真正生效，而不是只存在配置里。

约束的分层（design D9）：
- **输入规模**：`planning_max_input_chars` —— 超长消息直接拒，不浪费一次 provider 调用。
- **计划规模**：`planning_max_operations` —— 单个 Turn 最多几个操作。
- **调用总量**：`planning_max_llm_calls` —— 整个 Turn（含最多 3 次修订）的 LLM 调用
  上限。因为修订会重跑理解链路，只限"单次"是不够的。
- **provider 累计耗时**：`planning_provider_time_budget_s` —— **只累加真实调用耗时**，
  不把人机交互的等待算进去（用户思考 5 分钟不该判超预算）。

`BudgetExceeded` 是**受控**信号：由规划循环转成 `PlanningExit`，绝不半途写文件。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    """理解层预算突破（受控终止，非系统故障）。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class PlanningBudget:
    """一个 Turn 的理解层预算账本（可变运行态）。"""

    max_input_chars: int = 20000
    max_operations: int = 5
    max_llm_calls: int = 20
    provider_time_budget_s: float = 120.0

    llm_calls: int = 0
    provider_elapsed_s: float = 0.0
    _events: list[str] = field(default_factory=list)

    # --- 入口检查 ---

    def check_input(self, text: str) -> None:
        n = len(text or "")
        if self.max_input_chars > 0 and n > self.max_input_chars:
            raise BudgetExceeded(
                "input_too_long",
                f"消息过长（{n} 字符 > 上限 {self.max_input_chars}），"
                "请拆成多条更小的请求。",
            )

    def check_operations(self, n_operations: int) -> None:
        if self.max_operations > 0 and n_operations > self.max_operations:
            raise BudgetExceeded(
                "too_many_operations",
                f"这条消息包含 {n_operations} 个操作，超过单轮上限 "
                f"{self.max_operations}；请分几轮来做。",
            )

    # --- 调用计量 ---

    def before_llm_call(self) -> None:
        if self.max_llm_calls > 0 and self.llm_calls >= self.max_llm_calls:
            raise BudgetExceeded(
                "too_many_llm_calls",
                f"本轮理解调用已达上限（{self.max_llm_calls} 次），已停止以控制成本。",
            )
        if (
            self.provider_time_budget_s > 0
            and self.provider_elapsed_s >= self.provider_time_budget_s
        ):
            raise BudgetExceeded(
                "provider_time_exhausted",
                f"本轮模型调用累计耗时已超预算（{self.provider_elapsed_s:.0f}s "
                f"> {self.provider_time_budget_s:.0f}s），已停止。",
            )

    def record_llm_call(self, elapsed_s: float) -> None:
        self.llm_calls += 1
        self.provider_elapsed_s += max(0.0, elapsed_s)

    def measured(self, label: str = "llm"):
        """上下文管理器：进入前查预算，退出时累加**真实耗时**（不含人机等待）。"""
        return _Measured(self, label)

    def as_dict(self) -> dict:
        return {
            "llm_calls": self.llm_calls,
            "max_llm_calls": self.max_llm_calls,
            "provider_elapsed_s": round(self.provider_elapsed_s, 3),
            "provider_time_budget_s": self.provider_time_budget_s,
        }


class _Measured:
    def __init__(self, budget: PlanningBudget, label: str) -> None:
        self._budget = budget
        self._label = label
        self._started = 0.0

    def __enter__(self):
        self._budget.before_llm_call()
        self._started = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        # 失败的调用同样消耗时间与配额，必须计入（否则失败重试可无限进行）
        self._budget.record_llm_call(time.monotonic() - self._started)
        return False


def budget_from_config(config) -> PlanningBudget:
    """从 Config 构造预算；字段缺失时退回文档化默认。"""
    return PlanningBudget(
        max_input_chars=getattr(config, "planning_max_input_chars", 20000),
        max_operations=getattr(config, "planning_max_operations", 5),
        max_llm_calls=getattr(config, "planning_max_llm_calls", 20),
        provider_time_budget_s=getattr(
            config, "planning_provider_time_budget_s", 120.0
        ),
    )


__all__ = ["PlanningBudget", "BudgetExceeded", "budget_from_config"]
