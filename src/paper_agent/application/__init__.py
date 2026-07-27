"""应用编排层：把「理解层（tasking）」以三态（off/shadow/active）挂到 Turn 处理。

- off：完全走旧路径，不跑新层（默认）。
- shadow：旧路径照常执行；理解层在**独立** run 里基于**执行前只读快照**分析、只记录，
  失败/超时不影响旧路径、不改 Working_Copy。
- active：由 Task 24 接管（本模块预留分派点）。
"""

from paper_agent.application.console_interactor import ConsolePlanningInteractor
from paper_agent.application.turn_service import (
    PlanningShadowResult,
    TurnService,
    capture_snapshot,
)

__all__ = [
    "TurnService", "PlanningShadowResult", "capture_snapshot",
    "ConsolePlanningInteractor",
]
