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

from paper_agent.errors import OperationCancelled
from paper_agent.runtime.plan_executor import KEEP, ROLLBACK
from paper_agent.tasking.clarifier import ConfirmDecision, UnmappedDisposition

_YES = ("y", "yes", "是", "确认", "好", "ok")
_NO = ("n", "no", "否", "不")
# 嵌套提问中用户想中止整轮，而不是把这些当成答案
_ABORT = (":quit", ":q", ":exit", ":cancel", ":abort")


class ConsolePlanningInteractor:
    """终端交互实现。`ask` 返回用户输入的一行；`out` 输出提示。

    **中止语义（#15）**：规划期的每次提问都可能被用户中止——
    - `Ctrl-C` / `Ctrl-D`：`KeyboardInterrupt`/`EOFError` **不是** `Exception` 的子类，
      Coordinator 捕获不到，会直接杀掉 REPL。故在此统一转成 `OperationCancelled`，
      走既有取消路径（保留已完成步骤 + 回滚选项）。
    - `:quit` 等命令：在嵌套提问里不能被当成普通答案（否则会变成章节名/约束文本）。
    """

    def __init__(
        self,
        ask: Callable[[str], str] | None = None,
        out: Callable[[str], None] | None = None,
    ) -> None:
        self._ask = ask or input
        self._out = out or print

    def _prompt(self, text: str) -> str:
        """读一行；Ctrl-C/Ctrl-D 与 :quit 一律转为 OperationCancelled。"""
        try:
            answer = self._ask(text)
        except (KeyboardInterrupt, EOFError):
            self._out("\n（已中止本轮）")
            raise OperationCancelled() from None
        if answer.strip().lower() in _ABORT:
            self._out("（已中止本轮）")
            raise OperationCancelled()
        return answer

    # --- Clarifier ---

    def ask_field(self, prompt: str) -> str:
        return self._prompt(f"\n{prompt}\n> ").strip()

    # --- 未映射要求处置 ---

    def resolve_unmapped(self, item: str) -> UnmappedDisposition:
        # 措辞刻意保守：「没能安排进任何操作」是**账本口径**（没有槽位认领它），
        # 不等于「这条要求做不到」。真机常见成因是一个槽位覆盖了多条要求、
        # 但只自报了其中几条（如配额槽吃下「参考文献10/英文8/中文2」却只报后两条）。
        # 旧文案只说「没能安排」，用户无从判断选「忽略」会不会丢东西 —— 于是把
        # 一个本可知情的决定变成了猜。上下文（已理解的操作与槽位）要靠扩展
        # PlanningInteractor 协议才能展示，见 backlog X-8。
        self._out(f"\n有一条要求我没能安排进任何操作：「{item}」")
        self._out(
            "（可能它已被其他要求涵盖，也可能我确实没理解。"
            "紧接着的计划确认会列出我实际要做的事，可据此核对。）"
        )
        answer = self._prompt(
            "怎么处理？(1=补充说明 / 2=忽略这条并继续 / 3=取消本次)\n> "
        ).strip()
        if answer.startswith("1"):
            text = self._prompt("请补充说明：\n> ").strip()
            return UnmappedDisposition("supplement", text or item)
        if answer.startswith("3"):
            return UnmappedDisposition("cancel")
        return UnmappedDisposition("ignore")

    # --- 执行前确认 ---

    def confirm_plan(self, summary: str) -> ConfirmDecision:
        self._out("\n" + summary)
        answer = self._prompt(
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
        # 处置询问发生在「已经出错/已取消」之后：此时再抛 OperationCancelled 无意义，
        # 中止即按默认「保留已完成」处理，绝不因为读不到输入就丢掉用户的成果。
        try:
            answer = self._ask("(回车=保留已完成 / r=全部回滚)\n> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            self._out("\n（未选择，默认保留已完成的改动）")
            return KEEP
        return ROLLBACK if answer.startswith("r") else KEEP


__all__ = ["ConsolePlanningInteractor"]
