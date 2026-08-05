"""控制台版文献写作流交互 port（实现 `LiteratureInteractionPort` 四方法）。

规划层的 `ConsolePlanningInteractor` 只承载「确认执行计划」；文献写作流的四类决策
（检索批次 / 配额缺口 / 证据集 / final_gate 放宽）语义不同，按 design D9 用**独立窄
port** 承载，不复用 `confirm_plan()`。

纪律：
- **非交互/中止一律不默认接受**。Ctrl-C / Ctrl-D / `:quit` → 返回 ``needs_input``
  或安全的 ``cancel``/``reject``，绝不把「读不到输入」当成用户同意放宽配额或接受批次。
- 每个决策向用户展示**摘要**（不打印摘要/查询原文之外的敏感内容），再读一行。

通过注入 `ask`（默认 `input`）与 `out`（默认 `print`）与 I/O 解耦，便于测试。
"""

from __future__ import annotations

from collections.abc import Callable

from paper_agent.application.literature_ports import (
    BatchDecision,
    BundleDecision,
    FinalGateDecision,
    GapDecision,
    SearchIntentDecision,
    SearchTopicDecision,
)

_YES = ("y", "yes", "是", "确认", "好", "ok")
_ABORT = (":quit", ":q", ":exit", ":cancel", ":abort")


class ConsoleLiteratureInteractor:
    """终端实现的文献写作流四决策。默认拒绝/暂停语义,不误默认接受。"""

    def __init__(
        self,
        ask: Callable[[str], str] | None = None,
        out: Callable[[str], None] | None = None,
    ) -> None:
        self._ask = ask or input
        self._out = out or print

    def _prompt(self, text: str) -> str | None:
        """读一行；Ctrl-C/Ctrl-D 与 :quit → None（视作「无输入」，由调用方安全处理）。"""
        try:
            answer = self._ask(text)
        except (KeyboardInterrupt, EOFError):
            self._out("\n（未响应）")
            return None
        if answer.strip().lower() in _ABORT:
            self._out("（已中止）")
            return None
        return answer

    @staticmethod
    def _yes(answer: str | None) -> bool:
        return answer is not None and answer.strip().lower() in _YES

    # -- 检索批次确认 --
    def confirm_retrieval_batch(self, summary) -> BatchDecision:
        self._out("\n【写作前检索】拟入库以下候选：\n" + str(summary))
        answer = self._prompt("确认入库吗？(y=确认 / 其他=否决)\n> ")
        if answer is None:
            return BatchDecision("needs_input")
        return BatchDecision("accept") if self._yes(answer) else BatchDecision("reject")

    # -- 配额缺口决策 --
    def resolve_quota_gap(self, report) -> GapDecision:
        self._out("\n【配额缺口】检索后仍未达到你要求的引用数量：\n" + str(report))
        answer = self._prompt(
            "接受当前较少的数量继续吗？(y=接受偏差 / 其他=取消)\n> "
        )
        if answer is None:
            return GapDecision("needs_input")
        return GapDecision("accept") if self._yes(answer) else GapDecision("cancel")

    # -- 证据集确认 --
    def confirm_evidence_bundle(self, summary) -> BundleDecision:
        self._out("\n【证据集】将据以下文献写作：\n" + str(summary))
        answer = self._prompt("使用这批证据吗？(y=使用 / 其他=否决)\n> ")
        if answer is None:
            return BundleDecision("needs_input")
        return BundleDecision("accept") if self._yes(answer) else BundleDecision("reject")

    # -- final_gate 放宽 --
    def resolve_final_gate(self, report) -> FinalGateDecision:
        self._out(
            "\n【收尾】多轮生成后引用配额仍未完全满足（结构与合法性均已通过）：\n"
            + str(report)
        )
        answer = self._prompt(
            "接受当前结果并写回吗？(y=接受并写回 / 其他=放弃本次改动)\n> "
        )
        if answer is None:
            return FinalGateDecision("needs_input")
        return FinalGateDecision("accept") if self._yes(answer) else FinalGateDecision("cancel")

    # -- A2f：补充检索主题 --
    def request_search_topic(self, summary) -> SearchTopicDecision:
        self._out("\n【检索主题】没法从你的话里确定要找哪方面的文献：\n" + str(summary))
        answer = self._prompt(
            "请补充这一章要引用哪方面的文献（直接输入主题 / 空=取消）\n> "
        )
        if answer is None:
            return SearchTopicDecision("needs_input")
        text = answer.strip()
        return SearchTopicDecision("provide", text) if text else SearchTopicDecision("cancel")

    # -- A2f：确认合成的检索意图 --
    def confirm_search_intent(self, summary) -> SearchIntentDecision:
        self._out("\n【检索意图】我打算按以下主题与关键词去找文献：\n" + str(summary))
        answer = self._prompt(
            "可以吗？(y=用这个检索 / 输入新主题=修改 / 空=取消)\n> "
        )
        if answer is None:
            return SearchIntentDecision("needs_input")
        low = answer.strip().lower()
        if low in _YES:
            return SearchIntentDecision("accept")
        if not answer.strip():
            return SearchIntentDecision("cancel")
        return SearchIntentDecision("modify", answer.strip())


__all__ = ["ConsoleLiteratureInteractor"]
