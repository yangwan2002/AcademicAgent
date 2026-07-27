"""Rewrite 抽取器（Task 11）。

槽位与其**抽取规矩**都声明在 `REWRITE_SPEC`（`SlotSpec.description`），由框架自动
生成 function schema，故本模块只需声明 intent/spec 与跨槽位推导。

检索三元组：用户说「引用现有工作，搜 X」时给出 search_query，`want_search` 由
`derive_want_search` 从 query 推导（source=normalized），不覆盖用户明确取值。
"""

from __future__ import annotations

from paper_agent.intents.router import Intent
from paper_agent.tasking.contracts import SlotValue
from paper_agent.tasking.extractors.base import PreExtractor, derive_want_search
from paper_agent.tasking.intent_spec import REWRITE_SPEC, IntentSpec


class RewriteExtractor(PreExtractor):
    @property
    def intent(self) -> Intent:
        return Intent.REWRITE

    def _spec(self) -> IntentSpec:
        return REWRITE_SPEC

    def _post_process(self, slots: dict[str, SlotValue]) -> dict[str, SlotValue]:
        # search_query 非空 → want_search（design 附录派生规则）
        return derive_want_search(slots)


__all__ = ["RewriteExtractor"]
