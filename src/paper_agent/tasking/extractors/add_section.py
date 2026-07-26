"""Add_Section 抽取器（Task 11）。

add_section 槽位（design 附录）：
- `title`（必填，缺则追问）：新章节标题。
- `purpose`（必填，缺则追问）：这一章的目的/类型。
- `structure_kind`（必填，缺则追问）：内部结构类型 desc|list|mimic（validator 拦非法）。
- `structure_desc`（可选）：描述式骨架一句话。
- `subsections`（可选）：列表/模仿式的小节标题，list。
- `position`（可选）：插入位置（end/start/after:<sid>/before:<sid>）。
- `search_query`（可选）：引用密集型章节要检索的文献主题。
- `want_search`（可选）：是否检索——**由 search_query 非空推导**（同 rewrite）。
- `search_limit`（可选）：检索条数。

结构是**结构化联合**（structure_kind + structure_desc + subsections），而非模糊字符串，
故可直接映射到现有 `AddSectionPlan`（Task 19 Handler 消费）。
"""

from __future__ import annotations

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.base import Message
from paper_agent.tasking.contracts import AtomicRequirement, RawOperation, SlotValue
from paper_agent.tasking.extractors.base import (
    PreExtractor,
    build_extraction_messages,
    derive_want_search,
)
from paper_agent.tasking.intent_spec import ADD_SECTION_SPEC, IntentSpec

_SLOT_LINES = [
    "title：新章节的标题/名称。",
    "purpose：这一章的目的或类型（一句话）。",
    "structure_kind：内部结构类型，取值只能是 desc（描述式）/ list（列表式）/ mimic（模仿式）。",
    "structure_desc：desc 时的一句话骨架描述；无则 user_absent。",
    "subsections：list/mimic 时的小节标题数组；无则 user_absent。",
    "position：插入位置（如 末尾/开头/某章之后）；无则 user_absent。",
    "search_query：要检索的文献主题关键词；用户没提检索则 user_absent。",
    "want_search：是否需要检索文献（true/false）；不确定则 user_absent。",
    "search_limit：检索条数（整数）；无则 user_absent。",
]


class AddSectionExtractor(PreExtractor):
    @property
    def intent(self) -> Intent:
        return Intent.ADD_SECTION

    def _spec(self) -> IntentSpec:
        return ADD_SECTION_SPEC

    def _build_messages(
        self,
        operation: RawOperation,
        canonical_requirements: tuple[AtomicRequirement, ...] | None,
    ) -> list[Message]:
        return build_extraction_messages(
            operation,
            canonical_requirements,
            intent_label="新增章节",
            slot_lines=_SLOT_LINES,
        )

    def _post_process(self, slots: dict[str, SlotValue]) -> dict[str, SlotValue]:
        # search_query 非空 → want_search（同 rewrite，design 附录派生规则）
        return derive_want_search(slots)


__all__ = ["AddSectionExtractor"]
