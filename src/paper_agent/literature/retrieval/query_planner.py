"""QueryPlanner：Loop 的检索策略调整（A2c-4 / 任务 2，design D10）。

查询改写是**生成/规划**任务，不是证据审查——默认注入 writer 模型客户端（组合层），
但**不复用** writer 的写作 prompt / 对话历史 / 可变上下文；也不占用相关性 Reviewer。

**结构性约束**：Planner 只能产出查询 / 源名 / 检索参数，其输出 schema **不含**、调用方也
**不传**可写入的 QuotaSpec / TaskFingerprint / RelevanceCriteria / 预算——从类型上杜绝
「Planner 顺手放宽配额或改指纹」。Planner 失败 → Loop 退到确定性 fallback，**不**解释为
「已无文献」。

不 import `tasking`、不读全局 config。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from paper_agent.literature.language import script_counts
from paper_agent.literature.untrusted import clean_external_text
from paper_agent.providers.llm.base import LLMProvider, Message
from paper_agent.providers.llm.structured import CallStatus, structured_call

PLANNER_PROMPT_VERSION = "query-planner-a2c-v1"


@dataclass(frozen=True)
class SearchPlan:
    queries: tuple[str, ...] = ()
    source_names: tuple[str, ...] = ()
    provider_filters: tuple[tuple[str, object], ...] = ()
    strategy_reason: str = ""
    llm_called: bool = False          # A2f：本次 propose 是否真的发了模型调用（计费依据）

    @property
    def is_empty(self) -> bool:
        return not self.queries


@dataclass(frozen=True)
class PlannerRequest:
    """只读输入：Planner 能看到的信息（**不含**可写 quota/criteria/fingerprint）。"""

    initial_query: str
    topic: str = ""
    gap_dimensions: tuple[str, ...] = ()        # 仍未满足的维度名（如 language.zh）
    tried_queries: tuple[str, ...] = ()
    available_source_names: tuple[str, ...] = ()
    note: str = ""
    # 用户原始表述（criteria.section_purpose）。合成器会把 semantic_topic 归一成英文
    # （实测："跨视角图像地理定位" → "cross-view geo-localization"），于是补 language.zh
    # 缺口时 Planner 手里只有英文，拼不出可用的中文查询。这里单独带上原文，
    # **只用于构造查询**，不进 criteria / 不进 TaskFingerprint。
    purpose: str = ""


@runtime_checkable
class QueryPlanner(Protocol):
    def propose(self, request: PlannerRequest) -> SearchPlan:
        ...


def _has_han(text: str) -> bool:
    return script_counts(text)["han"] > 0


def _zh_query(base: str, purpose: str) -> str:
    """补 `language.zh` 缺口用的查询词。

    旧实现是 `f"{base} 中文"`。真机实测（2026-08，OpenAlex，per-page=20）该查询
    召回**中文文献 0~1 条** —— 「中文」被当普通检索词，匹到的是标题里含 Chinese
    的英文论文。换成中文检索词后同一主题召回中文 10~15 条：

        主题                查询            中文条数  其中切题
        跨视角图像地理定位   英文+「中文」        1        1
        跨视角图像地理定位   中文原文            10        3
        大语言模型代码生成   英文+「中文」        0        0
        大语言模型代码生成   中文原文            13        7
        知识图谱问答        英文+「中文」        1        1
        知识图谱问答        中文原文            15       12

    也试过 OpenAlex 原生 `filter=language:zh`：语言纯度最高（19/20）但**切题率归零**
    （返回锂铌钽矿床、天体生物学之类），因为过滤只约束语言、不约束相关性；
    「中英并置」在 3 个主题上不稳定（切题 4/9/1，且会把命中总数压到 6 条）。故取中文原文。

    `base` 已含汉字时直接用；否则退回 `purpose`（用户原始表述）。两者都没有汉字
    说明这个任务确实没有中文表述可用，保持旧行为 —— 不硬拼一个必然无效的查询。
    """
    if _has_han(base):
        return base
    if purpose and _has_han(purpose):
        return purpose
    return f"{base} 中文"


class DeterministicQueryPlanner:
    """无 LLM 的确定性兜底：首轮用初始查询，后续对未满足维度做固定改写/换源。"""

    def propose(self, request: PlannerRequest) -> SearchPlan:
        if not request.tried_queries:
            return SearchPlan(
                queries=(request.initial_query,),
                source_names=request.available_source_names,
                strategy_reason="initial",
            )
        # 已试过初始查询：对语言维度做固定扩展（确定性，不猜内容）
        base = request.topic or request.initial_query
        extra: list[str] = []
        for dim in request.gap_dimensions:
            if dim.endswith("zh"):
                extra.append(_zh_query(base, request.purpose))
            elif dim.endswith("en"):
                extra.append(f"{base} review")
        queries = tuple(q for q in extra if q and q not in request.tried_queries)
        return SearchPlan(
            queries=queries,
            source_names=request.available_source_names,
            strategy_reason="deterministic_expand",
        )


_TOOL = {
    "type": "function",          # OpenAI 协议必填；见 relevance.py::_TOOL 的说明
    "function": {
        "name": "propose_search_plan",
        "description": (
            "据当前未满足的检索配额维度，提出下一轮检索策略：改写查询词、选择检索源。"
            "只输出查询与源名，不得修改任何配额或任务标准。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {"type": "array", "items": {"type": "string"}},
                "source_names": {"type": "array", "items": {"type": "string"}},
                "strategy_reason": {"type": "string"},
            },
        },
    }
}

_SYSTEM = (
    "你是文献检索策略规划器。据未满足的配额维度提出下一轮查询词与检索源，"
    "目标是补齐缺口。只通过 propose_search_plan 返回；不得修改配额、任务标准或预算。"
    # 实测：给英文查询拼「中文」二字召回中文文献 0~1 条（检索引擎把它当普通词），
    # 必须用中文学术术语本身。见 `_zh_query` docstring 的三主题对照数据。
    "\n补 language.zh 缺口时，查询词必须是**中文学术术语**（如「跨视角图像地理定位」），"
    "不要用英文查询词，也不要在英文查询后附加「中文」二字——两种写法都检索不到中文文献。"
    "补 language.en 缺口时同理用英文术语。"
)


class LLMQueryPlanner:
    """走 structured_call 的 Planner。注入 writer 模型客户端，不复用写作 prompt/历史。"""

    def __init__(self, llm: LLMProvider, *, prompt_version: str = PLANNER_PROMPT_VERSION) -> None:
        self._llm = llm
        self.prompt_version = prompt_version

    def propose(self, request: PlannerRequest) -> SearchPlan:
        gaps = "、".join(request.gap_dimensions) or "（无明确维度）"
        tried = "；".join(request.tried_queries[-5:])
        lines = [
            f"主题：{clean_external_text(request.topic or request.initial_query, max_chars=500)}",
        ]
        # 主题常已被合成器归一成英文；带上用户原始表述，模型补 language.zh 时才有
        # 中文词可用（否则它只能照英文主题瞎译）。见 `_zh_query` 的实测数据。
        purpose = clean_external_text(request.purpose or "", max_chars=500)
        if purpose and purpose != lines[0].split("：", 1)[1]:
            lines.append(f"用户原始表述：{purpose}")
        lines += [
            f"未满足维度：{gaps}",
            f"已试查询：{tried}",
            f"可用源：{'、'.join(request.available_source_names)}",
        ]
        user = "\n".join(lines)
        call = structured_call(
            self._llm,
            [Message(role="system", content=_SYSTEM), Message(role="user", content=user)],
            tool_schema=_TOOL,
        )
        # 无论成功与否,已发一次真实模型调用 → llm_called=True（计费依据，A2f D9）。
        if call.call_status is not CallStatus.SUCCESS:
            return SearchPlan(strategy_reason="planner_failed", llm_called=True)
        payload = call.payload or {}
        queries = tuple(str(q) for q in (payload.get("queries") or []) if str(q).strip())
        sources = tuple(str(s) for s in (payload.get("source_names") or []))
        return SearchPlan(
            queries=queries,
            source_names=sources,
            strategy_reason=str(payload.get("strategy_reason", ""))[:200],
            llm_called=True,
        )


__all__ = [
    "SearchPlan",
    "PlannerRequest",
    "QueryPlanner",
    "DeterministicQueryPlanner",
    "LLMQueryPlanner",
    "PLANNER_PROMPT_VERSION",
]
