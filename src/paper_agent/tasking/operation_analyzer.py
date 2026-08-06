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

import uuid
from collections.abc import Sequence

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

#: 允许写入 `facts.claims` 的项目级事实 key（白名单；模型自造的一律丢弃）。
#: 与 `SearchContext.project_gap/project_novelty` 及 `_paper_context` 的消费口径对齐。
_PROJECT_FACT_KEYS = {"research_topic", "novelty", "gap"}

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
                                    "尽量包含这个操作的全部修饰语，两类都要，不要只顾一类：\n"
                                    "- **限制/禁止/须保留**类：「别改数字」「保留引用」"
                                    "「不超过 300 字」\n"
                                    "- **要求做到/数量**类：「引用 10 篇文献」"
                                    "「英文 8 篇中文 2 篇」「最近五年的工作」\n"
                                    "宁长勿短，不要压缩成关键词。"
                                ),
                            },
                            "requirement_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "本操作覆盖的 requirement 的 id。\n"
                                    "**该操作的全部修饰语对应的 id 都要列进来**——"
                                    "约束、数量配额、年份/语言门槛、参数，一个都不要漏。\n"
                                    "这个字段决定了后续环节能看到哪些要求：漏填的要求"
                                    "不会被执行，会被当作「无法安排」退回给用户。\n"
                                    "多个操作时，只列属于本操作的，不要把别的操作的 id "
                                    "也列进来。"
                                ),
                            },
                        },
                        # intent/verbatim 是操作项的**结构性**必填（没有它们这一项就
                        # 无意义），与「业务槽位必填」不同，故保留。
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
                "project_facts": {
                    "type": "array",
                    "description": (
                        "**关于这篇论文本身**的陈述，而非要执行的操作。\n"
                        "这类话是在交代背景（「课题是 X」「本文研究 X」「我做的是 X 方向」"
                        "「创新点是 X」），说一次即对整篇论文长期有效，不是这次要做的事。\n"
                        "放这里的会被记入项目记忆，供后续所有操作和会话复用；"
                        "**不要**硬塞进某个操作的槽位，也不要因为它不像操作就丢掉。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "enum": ["research_topic", "novelty", "gap"],
                                "description": (
                                    "research_topic：课题/研究方向（最常见）；"
                                    "novelty：创新点；gap：现有工作的不足。"
                                ),
                            },
                            "value": {
                                "type": "string",
                                "description": "该事实的内容，用用户自己的说法，去掉「课题是」这类引导词。",
                            },
                            "requirement_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "本条陈述对应的 requirement id（务必填，否则会被当成未安排退回用户）。",
                            },
                        },
                        "required": ["key", "value"],
                    },
                },
                "unmapped_text": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "无法归入上述五种意图的原话片段。用户要求的事情若不属于五种意图"
                        "（如翻译、查重、投稿、生成图表），**必须**放这里，"
                        "不要勉强映射到最接近的意图。宁可留空也不要猜。\n"
                        "注意：交代论文背景的陈述（「课题是 X」）属于 project_facts，不是这里。"
                    ),
                },
            },
            # 不设顶层 required：请求可能整体落在五意图之外，此时 operations 应为空
            # 而全部原话进 unmapped_text；强制 operations 会逼模型硬凑一个操作。
            "required": [],
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

    # 项目级事实：key 白名单外的丢弃（防模型自造 key 污染 claims）
    facts_out: list[tuple[str, str, tuple[str, ...]]] = []
    for pf in raw.get("project_facts", []) or []:
        if not isinstance(pf, dict):
            continue
        key = str(pf.get("key", "")).strip()
        value = str(pf.get("value", "")).strip()
        if key not in _PROJECT_FACT_KEYS or not value:
            continue
        rids = tuple(str(x) for x in (pf.get("requirement_ids") or ()))
        facts_out.append((key, value, rids))

    return AnalyzeResult(
        call_status=CallStatus.SUCCESS,
        operations=tuple(operations),
        requirements=requirements,
        unmapped_text=tuple(unmapped),
        project_facts=tuple(facts_out),
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


# --- 归属：把 requirement 路由到它所属的操作 -----------------------------------
#
# `RawOperation.requirement_ids` 由 analyzer 填写（「本操作覆盖哪些要求」），但在
# 2026-08-01 之前**下游一处都没读**——所有读取都是抽槽之后的
# `SlotValue.requirement_ids`。字段被丢弃的理由见 `plan_builder` docstring：
#
#   > 覆盖判定按 Slot 消费粒度：只有落进某个具体 SlotValue.requirement_ids 才算
#   > mapped；「仅挂在 Operation 上」不算。
#
# 那条规则**本身正确**（挂在操作上不证明执行器会用它），但它只关于**记账**。
# 不能用于记账 ≠ 不能用于**路由**。两件事被混为一谈，于是字段被用于零处，
# 抽取器只能看到 analyzer 裁过的 `verbatim` 切片 —— 真机缺陷即由此而来
# （用户说「要求参考文献10，英文8，中文2」，切片是「完善我的引言部分」，
# 抽取器视野内一个数字都没有）。
#
# 本函数只做路由。**记账规则不变。**

#: 归属来源，用于观测（哪条路径决定了这批要求）。
ATTRIBUTION_CLAIMED = "claimed"            # analyzer 显式声明了归属
ATTRIBUTION_SOLE_OPERATION = "sole_operation"  # 单操作计划，全部只能属于它
ATTRIBUTION_FALLBACK_ALL = "fallback_all"  # 多操作但 analyzer 未声明 → 退回现状


def requirements_for(
    operation: RawOperation,
    all_requirements: Sequence[AtomicRequirement],
    n_operations: int,
) -> tuple[tuple[AtomicRequirement, ...], str]:
    """该操作应当看到哪些 requirement。返回 `(要求元组, 归属来源)`。

    三档，**降级方向恒为「不比现状更差」**：

    ==========================  ==========================================
    `requirement_ids` 非空       按 id 过滤 → 顺带**收紧**多操作隔离
    单操作 + 未声明              全部要求归它（无歧义：整条消息只有一个操作）
    多操作 + 未声明              退回现状（列全部）+ 由调用方计数告警
    ==========================  ==========================================

    第三档刻意保守：宁可维持现状的交叉污染风险，也不因 analyzer 少填一个字段
    就让抽取器什么都看不到。该字段此前是「装饰性」的（填了也没人读），
    其填充质量从未被度量，故不能假定它总被填对。

    幻觉 id（引用了不存在的 requirement）被过滤，与
    `plan_builder._build_dispositions` 同一口径。
    """
    by_id = {r.requirement_id: r for r in all_requirements}
    claimed = tuple(
        by_id[rid] for rid in operation.requirement_ids if rid in by_id
    )
    if claimed:
        return claimed, ATTRIBUTION_CLAIMED
    if n_operations <= 1:
        return tuple(all_requirements), ATTRIBUTION_SOLE_OPERATION
    return tuple(all_requirements), ATTRIBUTION_FALLBACK_ALL


__all__ = [
    "analyze", "new_requirement_id", "requirements_for",
    "ATTRIBUTION_CLAIMED", "ATTRIBUTION_SOLE_OPERATION", "ATTRIBUTION_FALLBACK_ALL",
]
