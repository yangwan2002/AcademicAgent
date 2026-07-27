"""控制台版 PlanningInteractor（规划层的真实终端交互）。

实现 `tasking.clarifier.PlanningInteractor` 协议的三类交互，外加 PlanExecutor 需要的
baseline 处置询问：

- `ask_field`：澄清缺口（问句由 Clarifier 的问法目录给出，此处只负责读一行）。
- `resolve_unmapped`：未映射要求的强制处置（补充 / 忽略 / 取消）。
- `confirm_plan`：执行前确认（确认 / 否决 / 修改）。
- `ask_disposition`：部分失败/取消后「保留已完成 or 回滚整个计划」。

通过注入 `ask`（默认 `input`）与 `out`（默认 `print`）与 I/O 解耦，便于测试。
"""

from __future__ import annotations

from collections.abc import Callable

from paper_agent.runtime.plan_executor import KEEP, ROLLBACK
from paper_agent.tasking.clarifier import ConfirmDecision, UnmappedDisposition

_YES = ("y", "yes", "是", "确认", "好", "ok")
_NO = ("n", "no", "否", "不")


class ConsolePlanningInteractor:
    """终端交互实现。`ask` 返回用户输入的一行；`out` 输出提示。"""

    def __init__(
        self,
        ask: Callable[[str], str] | None = None,
        out: Callable[[str], None] | None = None,
    ) -> None:
        self._ask = ask or input
        self._out = out or print

    # --- Clarifier ---

    def ask_field(self, prompt: str) -> str:
        return self._ask(f"\n{prompt}\n> ").strip()

    # --- 未映射要求处置 ---

    def resolve_unmapped(self, item: str) -> UnmappedDisposition:
        self._out(f"\n有一条要求我没能安排进任何操作：「{item}」")
        answer = self._ask(
            "怎么处理？(1=补充说明 / 2=忽略这条 / 3=取消本次)\n> "
        ).strip()
        if answer.startswith("1"):
            text = self._ask("请补充说明：\n> ").strip()
            return UnmappedDisposition("supplement", text or item)
        if answer.startswith("3"):
            return UnmappedDisposition("cancel")
        return UnmappedDisposition("ignore")

    # --- 执行前确认 ---

    def confirm_plan(self, summary: str) -> ConfirmDecision:
        self._out("\n" + summary)
        answer = self._ask(
            "\n执行这个计划吗？(回车/y=执行 / n=放弃 / 直接输入修改意见=改计划)\n> "
        ).strip()
        low = answer.lower()
        if not answer or low in _YES:
            return ConfirmDecision("accept")
        if low in _NO:
            return ConfirmDecision("reject")
        # 其余输入视为修改意见 → 走修订重建（不原地改已确认计划）
        return ConfirmDecision("modify", answer)

    # --- Baseline 处置（部分失败/取消）---

    def ask_disposition(self, completed_ids: list[str]) -> str:
        self._out(
            f"\n计划未全部完成，已成功 {len(completed_ids)} 步。"
            "可以保留这些已完成的改动，或把工作副本整体回滚到本次执行前。"
        )
        answer = self._ask("(回车=保留已完成 / r=全部回滚)\n> ").strip().lower()
        return ROLLBACK if answer.startswith("r") else KEEP


__all__ = ["ConsolePlanningInteractor"]
