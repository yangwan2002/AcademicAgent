"""运行时执行层：把已确认的 Task_Plan 落到现有 `execute_*`（不重写 SOP）。

- `handlers`：五个 IntentHandler，薄适配 `execute_*`，把 Slot 真正喂进执行器。
- `plan_executor`（Task 20+）：有序串行执行、状态聚合、取消/回滚。
"""

from paper_agent.runtime.handlers import (
    HandlerContext,
    IntentHandler,
    handler_for,
)
from paper_agent.runtime.plan_executor import (
    KEEP,
    ROLLBACK,
    aggregate_plan_status,
    dispose_baseline,
    execute_plan,
    pre_operation_check,
)

__all__ = [
    "HandlerContext", "IntentHandler", "handler_for",
    "execute_plan", "aggregate_plan_status", "pre_operation_check",
    "dispose_baseline", "KEEP", "ROLLBACK",
]
