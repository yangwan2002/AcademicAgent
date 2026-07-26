"""PreExtractor 家族：按 IntentSpec 抽槽（Task 9 框架 + Task 10-12 五意图）。

框架在 `base.py`：调用态/字段态分离（D3）、requirement 唯一真相源（D4）、
Fast_Path 产 requirement、整体失败兜底。具体意图抽取器在各自模块，只声明
`_spec()` 与提示词/解析，复用框架的失败处理与 schema 校验。

`build_extractor(intent, llm)` 是按意图取抽取器的注册入口；Task 10 先接入
polish + convert，rewrite/add_section/format_docx 在 Task 11/12 增量补齐。
"""

from __future__ import annotations

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.tasking.extractors.base import (
    PreExtractor,
    StructuredCallResult,
    build_extraction_messages,
    structured_call,
)
from paper_agent.tasking.extractors.add_section import AddSectionExtractor
from paper_agent.tasking.extractors.convert import ConvertExtractor
from paper_agent.tasking.extractors.format_docx import FormatDocxExtractor
from paper_agent.tasking.extractors.polish import PolishExtractor
from paper_agent.tasking.extractors.rewrite import RewriteExtractor

# 意图 → 抽取器类（五意图齐全；未注册的意图返回 None）
_REGISTRY: dict[Intent, type[PreExtractor]] = {
    Intent.POLISH: PolishExtractor,
    Intent.CONVERT_FORMAT: ConvertExtractor,
    Intent.REWRITE: RewriteExtractor,
    Intent.ADD_SECTION: AddSectionExtractor,
    Intent.FORMAT_DOCX: FormatDocxExtractor,
}


def build_extractor(
    intent: Intent,
    llm: LLMProvider,
    *,
    structured_mode: str = "prompt",
) -> PreExtractor | None:
    """按意图构造抽取器；未注册返回 None（调用方按 D3 降级/进 unmapped）。"""
    cls = _REGISTRY.get(intent)
    if cls is None:
        return None
    return cls(llm, structured_mode=structured_mode)


__all__ = [
    "PreExtractor",
    "StructuredCallResult",
    "structured_call",
    "build_extraction_messages",
    "build_extractor",
    "PolishExtractor",
    "ConvertExtractor",
    "RewriteExtractor",
    "AddSectionExtractor",
    "FormatDocxExtractor",
]
