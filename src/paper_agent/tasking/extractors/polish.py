"""Polish 抽取器（Task 10）。

polish 是最简单的意图之一，用来先验证 PreExtractor 框架协议。槽位（design 附录）：
- `section_id`（必填，缺则追问）：要润色哪一章。
- `goal`（可选）：润色目标/程度（默认值由 PlanBuilder 填，抽取器不填）。
- `constraints`（可选）：约束（如「别改数字/保留引用」），list。

抽取器只负责「用户到底说了什么」（present/user_absent/ambiguous/invalid），
默认值与执行绑定不在此处理。
"""

from __future__ import annotations

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.base import Message
from paper_agent.tasking.contracts import AtomicRequirement, RawOperation
from paper_agent.tasking.extractors.base import (
    PreExtractor,
    build_extraction_messages,
)
from paper_agent.tasking.intent_spec import POLISH_SPEC, IntentSpec

_SLOT_LINES = [
    "section_id：要润色的章节标识或名称（用户指明哪一章）。",
    "goal：润色的**程度/风格**（如「保守」「只删冗余」「大改」「更学术」）。",
    "constraints：用户提出的**限制/禁止/须保留**类要求，字符串数组。"
    "凡是「别改…」「不要动…」「保留…」「保持…」「不超过…字」「控制在…以内」"
    "「维持篇幅」这类要求，都必须放进 constraints（**不要**混进 goal，"
    "也不要因为它同时像目标就省略）。示例："
    '「别改里面的实验数字」→ constraints=["别改里面的实验数字"]；'
    '「保留数字和引用」→ constraints=["保留数字和引用"]；'
    '「控制在 250 字以内」→ constraints=["控制在 250 字以内"]。'
    "用户没提任何限制才 user_absent。",
]


class PolishExtractor(PreExtractor):
    @property
    def intent(self) -> Intent:
        return Intent.POLISH

    def _spec(self) -> IntentSpec:
        return POLISH_SPEC

    def _build_messages(
        self,
        operation: RawOperation,
        canonical_requirements: tuple[AtomicRequirement, ...] | None,
    ) -> list[Message]:
        return build_extraction_messages(
            operation,
            canonical_requirements,
            intent_label="润色",
            slot_lines=_SLOT_LINES,
        )


__all__ = ["PolishExtractor"]
