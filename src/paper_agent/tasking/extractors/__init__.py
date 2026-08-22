"""PreExtractor 家族：按 IntentSpec 抽槽（Task 9 框架 + Task 10-12 五意图）。

框架在 `base.py`：调用态/字段态分离（D3）、requirement 唯一真相源（D4）、
Fast_Path 产 requirement、整体失败兜底。

**结构化输出统一走 function calling**：每个槽位的抽取规矩写在 `SlotSpec.description`
里，由 `build_tool_schema` 自动生成 JSON schema（规矩挂在字段本身）。真机实测显著
优于「prompt 约束 JSON」（约束召回 73%→100%、澄清追问 7→1），故 prompt 模式已移除。
具体抽取器因此非常薄：只声明 intent 与 spec。
"""

from __future__ import annotations

from paper_agent.intents.intent import Intent
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.tasking.extractors.add_section import AddSectionExtractor
from paper_agent.tasking.extractors.base import (
    PreExtractor,
    StructuredCallResult,
    build_tool_schema,
    structured_call,
)
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


def build_extractor(intent: Intent, llm: LLMProvider) -> PreExtractor | None:
    """按意图构造抽取器；未注册返回 None（调用方按 D3 降级/进 unmapped）。"""
    cls = _REGISTRY.get(intent)
    if cls is None:
        return None
    return cls(llm)


__all__ = [
    "PreExtractor",
    "StructuredCallResult",
    "structured_call",
    "build_tool_schema",
    "build_extractor",
    "PolishExtractor",
    "ConvertExtractor",
    "RewriteExtractor",
    "AddSectionExtractor",
    "FormatDocxExtractor",
]
