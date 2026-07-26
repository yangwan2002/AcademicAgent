"""Convert 抽取器（Task 10）。

convert_format 槽位（design 附录）：
- `target_ext`（必填，缺则追问）：目标格式。规范化（Word→docx 等）与合法性校验
  由 CONVERT_SPEC 的 normalizer/validator 做——抽取器只把用户原话交给框架，
  框架跑 normalizer 得到规范值（source=normalized）、跑 validator 拦非法（invalid）。
- `presets`（可选）：转换预设，list。

convert 是**终止操作**（其后不应再有编辑操作），但那是 Static_Plan_Validator
（Task 14）的职责；抽取器只管抽槽。
"""

from __future__ import annotations

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.base import Message
from paper_agent.tasking.contracts import AtomicRequirement, RawOperation
from paper_agent.tasking.extractors.base import (
    PreExtractor,
    build_extraction_messages,
)
from paper_agent.tasking.intent_spec import CONVERT_SPEC, IntentSpec

_SLOT_LINES = [
    "target_ext：目标格式，用户口径原样给出（如 Word / docx / latex / markdown）。",
    "presets：转换预设/样式列表；无则 user_absent。",
]


class ConvertExtractor(PreExtractor):
    @property
    def intent(self) -> Intent:
        return Intent.CONVERT_FORMAT

    def _spec(self) -> IntentSpec:
        return CONVERT_SPEC

    def _build_messages(
        self,
        operation: RawOperation,
        canonical_requirements: tuple[AtomicRequirement, ...] | None,
    ) -> list[Message]:
        return build_extraction_messages(
            operation,
            canonical_requirements,
            intent_label="格式转换",
            slot_lines=_SLOT_LINES,
        )


__all__ = ["ConvertExtractor"]
