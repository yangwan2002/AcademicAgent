"""Operation_Analyzer（Task 6）：把一条自然语言消息拆成有序操作 + 原子要求。

只做「切分 + 每段意图 + 原话 + 识别 AtomicRequirement」，**不抽细节、不画依赖图、
按用户表达顺序**（细节由 PreExtractor，顺序由用户表达、执行序由后续能力模拟）。

LLM 只返回 JSON（含每条 requirement 的 verbatim）；**offset 由程序用 Task 2 的
locate 定位**（不让 LLM 算字符位）。失败分四态（provider/parse/schema/success），
绝不塌成空：
- provider 调用异常 → provider_failed
- 返回非合法 JSON → parse_failed
- JSON 合法但结构不符 → schema_failed
非法 intent、无法归类的文本 → 进 unmapped_text（不静默丢弃）。
"""

from __future__ import annotations

import json
import os
import uuid

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.base import LLMProvider, Message
from paper_agent.tasking.contracts import (
    AnalyzeResult,
    AtomicRequirement,
    CallStatus,
    RawOperation,
)
from paper_agent.tasking.spans import locate_requirements

_SUPPORTED = {
    Intent.POLISH, Intent.REWRITE, Intent.ADD_SECTION,
    Intent.CONVERT_FORMAT, Intent.FORMAT_DOCX,
}

# --- function calling schema（字段级规矩，与 _SYSTEM 同源）-------------------

_INTENT_RULE = (
    "该操作的意图，只能取五者之一：\n"
    "- polish：只改语言表达（更通顺/简洁/学术），不动事实与结构。\n"
    "- rewrite：改内容或组织（补充论证、调整结构、更换叙述角度）。\n"
    "- add_section：新增一个当前不存在的章节。\n"
    "- convert_format：**换文件格式**（转成/转换为/导出为 Word、docx、PDF、"
    "LaTeX、Markdown 等）。\n"
    "- format_docx：**只调排版属性**，不换文件格式（字体、字号、行距、对齐、"
    "缩进、分栏、页边距、页面方向、三线表）。\n"
    "区分要点：「转成 Word / 导出成 markdown」是 convert_format，不是 format_docx。"
)

_ANALYZE_TOOL = {
    "type": "function",
    "function": {
        "name": "decompose_request",
        "description": (
            "把用户消息按其表达顺序拆解为若干操作，并识别其中的原子要求。"
            "只拆解用户确实表达过的内容，不要臆造。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": (
                        "按用户表达顺序排列的操作列表。\n"
                        "切分标准是「(意图, 作用目标) 组合」：组合相同则合并成一个操作，"
                        "不同则切分。\n"
                        "- 同一目标上的多个细节要合并：如「正文宋体小四、标题居中、双栏」"
                        "是对同一文档的排版 → **一个** format_docx。\n"
                        "- 作用目标不同必须切分（即使意图相同）：如「先润色引言，"
                        "再润色结论」→ **两个** polish。\n"
                        "- 意图不同必须切分：如「先润色引言，再转成 docx」→ "
                        "polish + convert_format。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "intent": {"type": "string", "description": _INTENT_RULE},
                            "verbatim": {
                                "type": "string",
                                "description": (
                                    "该操作对应的用户原话片段，**逐字连续摘取**。\n"
                                    "必须包含这个操作的**全部**修饰语——目标、程度、"
                                    "以及所有限制/禁止/须保留类要求（如「别改数字」"
                                    "「保留引用」「不超过 300 字」）。\n"
                                    "宁长勿短：后续环节只能看到这段文字，被裁掉的内容"
                                    "就等于丢失。不要压缩成关键词。"
                                ),
                            },
                            "requirement_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "本操作覆盖的 requirement 的 id。",
                            },
                        },
                        "required": ["intent", "verbatim"],
                    },
                },
                "requirements": {
                    "type": "array",
                    "description": (
                        "消息中各条**原子要求**。一条要求 = 一个独立、可判定是否被满足的"
                        "诉求；把复合句拆成多条，但每条要**语义完整**"
                        "（如「别改里面的实验数字」，不要碎成「数字」）。"
                        "不要计算字符位置。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "requirement_id": {
                                "type": "string",
                                "description": "本次调用内自定的临时唯一 id，如 r1/r2。",
                            },
                            "verbatim": {
                                "type": "string",
                                "description": "该要求在用户原话中的**逐字**片段。",
                            },
                            "category": {
                                "type": "string",
                                "description": (
                                    "要求类别：target（作用对象）/ constraint（边界约束，"
                                    "限定不能做什么或须维持什么）/ goal（要达成什么）/ "
                                    "evidence（证据来源）/ other。"
                                ),
                            },
                        },
                        "required": ["requirement_id", "verbatim"],
                    },
                },
                "unmapped_text": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "无法归入上述五种意图的原话片段。用户要求的事情若不属于五种意图"
                        "（如翻译、查重、投稿、生成图表），**必须**放这里，"
                        "不要勉强映射到最接近的意图。宁可留空也不要猜。"
                    ),
                },
            },
            "required": ["operations"],
        },
    },
}


_TOOL_SYSTEM = (
    "调用工具 decompose_request 拆解用户消息，不要输出正文。"
    "只拆解用户确实表达过的内容；不属于五种意图的部分放进 unmapped_text，不要硬凑。"
)


def _coerce_result(raw: dict, text: str) -> AnalyzeResult:
    """把解析出的 dict 组装为 AnalyzeResult；结构不符抛 KeyError/TypeError。"""
    unmapped = list(raw.get("unmapped_text", []) or [])

    # requirements：offset 由程序定位
    reqs_in = raw.get("requirements", []) or []
    requirements = tuple(
        AtomicRequirement(
            requirement_id=str(r["requirement_id"]),
            verbatim=str(r["verbatim"]),
            category=str(r.get("category", "")),
        )
        for r in reqs_in
    )
    requirements = locate_requirements(text, requirements)

    ops_in = raw.get("operations", [])
    if not isinstance(ops_in, list):
        raise TypeError("operations must be a list")
    operations: list[RawOperation] = []
    for op in ops_in:
        intent_str = str(op["intent"])
        try:
            intent = Intent(intent_str)
        except ValueError:
            # 非法/不支持的 intent → 进 unmapped，不静默
            unmapped.append(op.get("verbatim", intent_str))
            continue
        if intent not in _SUPPORTED:
            unmapped.append(op.get("verbatim", intent_str))
            continue
        operations.append(
            RawOperation(
                intent=intent,
                verbatim=str(op.get("verbatim", "")),
                requirement_ids=tuple(str(x) for x in op.get("requirement_ids", [])),
            )
        )

    return AnalyzeResult(
        call_status=CallStatus.SUCCESS,
        operations=tuple(operations),
        requirements=requirements,
        unmapped_text=tuple(unmapped),
    )


def analyze(text: str, llm: LLMProvider) -> AnalyzeResult:
    """调用 LLM 拆分操作；四态失败分类，绝不塌成空。

    结构化输出经 `extractors.base.structured_call` 统一收口（与 PreExtractor 同一路径），
    统一走 function calling：字段规矩写在 `_ANALYZE_TOOL` 的 description 里。
    """
    # 延迟导入：避免 operation_analyzer ↔ extractors 的模块级循环依赖。
    from paper_agent.tasking.extractors.base import structured_call

    messages = [
        Message(role="system", content=_TOOL_SYSTEM),
        Message(role="user", content=text),
    ]
    call = structured_call(llm, messages, tool_schema=_ANALYZE_TOOL)
    if call.call_status is not CallStatus.SUCCESS:
        return AnalyzeResult(
            call_status=call.call_status, error_category=call.error_category,
        )

    try:
        return _coerce_result(call.payload or {}, text)
    except (KeyError, TypeError, ValueError) as exc:
        return AnalyzeResult(
            call_status=CallStatus.SCHEMA_FAILED,
            error_category=type(exc).__name__,
        )


def new_requirement_id() -> str:
    return f"req_{uuid.uuid4().hex[:8]}"


__all__ = ["analyze", "new_requirement_id"]
