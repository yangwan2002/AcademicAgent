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

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class QueryPlanner(Protocol):
    def propose(self, request: PlannerRequest) -> SearchPlan:
        ...


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
                extra.append(f"{base} 中文")
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
)


class LLMQueryPlanner:
    """走 structured_call 的 Planner。注入 writer 模型客户端，不复用写作 prompt/历史。"""

    def __init__(self, llm: LLMProvider, *, prompt_version: str = PLANNER_PROMPT_VERSION) -> None:
        self._llm = llm
        self.prompt_version = prompt_version

    def propose(self, request: PlannerRequest) -> SearchPlan:
        gaps = "、".join(request.gap_dimensions) or "（无明确维度）"
        tried = "；".join(request.tried_queries[-5:])
        user = "\n".join([
            f"主题：{clean_external_text(request.topic or request.initial_query, max_chars=500)}",
            f"未满足维度：{gaps}",
            f"已试查询：{tried}",
            f"可用源：{'、'.join(request.available_source_names)}",
        ])
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
