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

_SYSTEM = (
    "你是学术论文写作助手的意图分解器。把用户消息按其表达顺序切分为若干操作，"
    "并识别其中的原子要求。只输出 JSON，不要解释。格式：\n"
    '{"operations":[{"intent":"polish|rewrite|add_section|convert_format|format_docx",'
    '"verbatim":"该操作对应的用户原话","requirement_ids":["r1"]}],'
    '"requirements":[{"requirement_id":"r1","verbatim":"用户原话中的一条要求",'
    '"category":"target|constraint|goal|evidence|other"}],'
    '"unmapped_text":["无法归类的原话片段"]}\n'
    "\n"
    "【五种意图的判别】\n"
    "- polish：只改语言表达（更通顺/更简洁/更学术），不动事实与结构。\n"
    "- rewrite：改内容或组织（补充论证、调整结构、更换叙述角度）。\n"
    "- add_section：新增一个当前不存在的章节。\n"
    "- convert_format：**换文件格式**——把文档转成/导出为 Word、docx、PDF、"
    "LaTeX、Markdown 等。关键词：转成、转换为、导出为、另存为某格式。\n"
    "- format_docx：**只调排版属性**，不换文件格式——字体、字号、行距、对齐、"
    "缩进、分栏、页边距、页面方向、三线表。\n"
    "  区分要点：「转成 Word / 转换为 docx / 导出成 markdown」是 convert_format，"
    "**不是** format_docx；「正文宋体小四、标题居中、双栏」才是 format_docx。\n"
    "\n"
    "【切分粒度】\n"
    "- 一个动作 = 一个操作。**按动作切，不要按诉求条数切。**\n"
    "- 同一个动作下的多个细节（如「正文宋体小四、标题居中、全文双栏」都是排版）"
    "必须合并成**一个** format_docx 操作，不要拆成多个。\n"
    "- 同理，「改写某章并补充论证、引用文献」是**一个** rewrite 操作，"
    "不要因为提到文献就额外产生 add_section。\n"
    "- 只有出现明确的先后连接（先…再…、然后、接着、并且再）且动作确实不同时，"
    "才切分为多个操作。\n"
    "\n"
    "【不要硬凑意图】\n"
    "- 只能使用上面五种意图。用户要求的事情若不属于这五种"
    "（如翻译、查重、投稿、生成图表、订机票），**必须**把该片段放进 "
    'unmapped_text，**不要**勉强映射到最接近的意图。宁可留空也不要猜。\n'
    "\n"
    "不要计算字符位置；requirement 只给 verbatim 原文。"
)


def _extract_json(text: str) -> dict:
    """从模型输出里取第一段 JSON 对象；失败抛 ValueError。"""
    s = text.strip()
    # 容忍 ```json ... ``` 包裹
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no json object")
    return json.loads(s[start:end + 1])


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
    """调用 LLM 拆分操作；四态失败分类，绝不塌成空。"""
    messages = [
        Message(role="system", content=_SYSTEM),
        Message(role="user", content=text),
    ]

    # 1) provider 调用
    try:
        resp = llm.complete(messages)
    except Exception as exc:  # noqa: BLE001
        return AnalyzeResult(
            call_status=CallStatus.PROVIDER_FAILED,
            error_category=type(exc).__name__,
        )

    content = (resp.content or "").strip()

    # 2) JSON 解析
    try:
        raw = _extract_json(content)
    except (ValueError, json.JSONDecodeError) as exc:
        return AnalyzeResult(
            call_status=CallStatus.PARSE_FAILED,
            error_category=type(exc).__name__,
        )

    # 3) 结构组装
    try:
        return _coerce_result(raw, text)
    except (KeyError, TypeError, ValueError) as exc:
        return AnalyzeResult(
            call_status=CallStatus.SCHEMA_FAILED,
            error_category=type(exc).__name__,
        )


def new_requirement_id() -> str:
    return f"req_{uuid.uuid4().hex[:8]}"


__all__ = ["analyze", "new_requirement_id"]
