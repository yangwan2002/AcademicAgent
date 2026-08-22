"""Add_Section 抽取器（Task 11）。

槽位与规矩声明在 `ADD_SECTION_SPEC`（含 structure_kind 的三值枚举说明、position 的
`after:/before:` 形态说明），由框架自动生成 function schema。

结构是**结构化联合**（structure_kind + structure_desc + subsections），而非模糊字符串，
故可直接映射到现有 `AddSectionPlan`（Task 19 Handler 消费）。
"""

from __future__ import annotations

from paper_agent.intents.intent import Intent
from paper_agent.tasking.contracts import SlotValue
from paper_agent.tasking.extractors.base import PreExtractor, derive_want_search
from paper_agent.tasking.intent_spec import ADD_SECTION_SPEC, IntentSpec


class AddSectionExtractor(PreExtractor):
    @property
    def intent(self) -> Intent:
        return Intent.ADD_SECTION

    def _spec(self) -> IntentSpec:
        return ADD_SECTION_SPEC

    def _post_process(self, slots: dict[str, SlotValue]) -> dict[str, SlotValue]:
        # search_query 非空 → want_search（同 rewrite）
        return derive_want_search(slots)


__all__ = ["AddSectionExtractor"]
