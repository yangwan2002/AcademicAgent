"""Rewrite 抽取器（Task 11）。

rewrite 槽位（design 附录）：
- `section_id`（必填，缺则追问）：改哪一章。
- `direction`（必填，缺则追问）：改动方向（补论证/调结构/换角度/其他）。
- `details`（可选）：方向细节。
- `search_query`（可选）：要检索的文献主题。
- `want_search`（可选）：是否检索——**由 search_query 非空推导**（见 `_post_process`）。
- `search_limit`（可选）：检索条数。

复杂点在检索三元组：用户「补充论证并引用现有工作，搜 X」时，search_query=X，
want_search 由框架从 query 推导（source=normalized），无需模型显式给。
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
from paper_agent.tasking.intent_spec import REWRITE_SPEC, IntentSpec

_SLOT_LINES = [
    "section_id：要改写的章节标识或名称。",
    "direction：改动方向（如 补充论证 / 调整结构 / 更换叙述角度 / 其他）。",
    "details：方向的具体细节；无则 user_absent。",
    "search_query：要检索的文献主题关键词；用户没提检索则 user_absent。",
    "want_search：是否需要检索文献（true/false）；不确定则 user_absent。",
    "search_limit：检索条数（整数）；无则 user_absent。",
]


class RewriteExtractor(PreExtractor):
    @property
    def intent(self) -> Intent:
        return Intent.REWRITE

    def _spec(self) -> IntentSpec:
        return REWRITE_SPEC

    def _build_messages(
        self,
        operation: RawOperation,
        canonical_requirements: tuple[AtomicRequirement, ...] | None,
    ) -> list[Message]:
        return build_extraction_messages(
            operation,
            canonical_requirements,
            intent_label="改写",
            slot_lines=_SLOT_LINES,
        )

    def _post_process(self, slots: dict[str, SlotValue]) -> dict[str, SlotValue]:
        # search_query 非空 → want_search（design 附录派生规则）
        return derive_want_search(slots)


__all__ = ["RewriteExtractor"]
