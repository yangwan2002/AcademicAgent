"""Convert 抽取器（Task 10）。

槽位（target_ext / presets）与规矩声明在 `CONVERT_SPEC`。`target_ext` 的归一
（Word→docx）与合法性校验由 spec 的 normalizer/validator 做：框架跑 normalizer 得到
规范值（source=normalized）、跑 validator 拦非法（invalid）。

convert 是**终止操作**（其后不应再有编辑操作），但那是 Static_Plan_Validator
（Task 14）的职责；抽取器只管抽槽。
"""

from __future__ import annotations

from paper_agent.intents.router import Intent
from paper_agent.tasking.extractors.base import PreExtractor
from paper_agent.tasking.intent_spec import CONVERT_SPEC, IntentSpec


class ConvertExtractor(PreExtractor):
    @property
    def intent(self) -> Intent:
        return Intent.CONVERT_FORMAT

    def _spec(self) -> IntentSpec:
        return CONVERT_SPEC


__all__ = ["ConvertExtractor"]
