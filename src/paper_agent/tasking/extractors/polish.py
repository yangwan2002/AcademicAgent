"""Polish 抽取器（Task 10）。

槽位（section_id / goal / constraints）与其**抽取规矩**都声明在 `POLISH_SPEC` 的
`SlotSpec.description` 里，由框架自动生成 function schema——规矩挂在字段本身，
真机实测比堆在 system prompt 里显著更准（约束召回 73%→100%）。

抽取器只负责「用户到底说了什么」；默认值由 PlanBuilder 按 IntentSpec 填（D10）。
"""

from __future__ import annotations

from paper_agent.intents.router import Intent
from paper_agent.tasking.extractors.base import PreExtractor
from paper_agent.tasking.intent_spec import POLISH_SPEC, IntentSpec


class PolishExtractor(PreExtractor):
    @property
    def intent(self) -> Intent:
        return Intent.POLISH

    def _spec(self) -> IntentSpec:
        return POLISH_SPEC


__all__ = ["PolishExtractor"]
