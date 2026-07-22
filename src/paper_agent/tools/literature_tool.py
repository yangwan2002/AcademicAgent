"""写作期"按需检索文献"工具（Req 4.4）。

供写作智能体在 ReAct 工具循环中调用：模型发现需要引用时调 search_literature，
工具检索候选并**经核验**后返回（保持引用真实性硬约束 Req 4）。
仅核验通过的文献会被累积，供写作引用与回写工作区。
"""

from __future__ import annotations

from paper_agent.providers.retrieval.base import RetrievalError, RetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import ReferenceEntry

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "检索关键词（建议用英文、聚焦具体主题）",
        },
        "limit": {
            "type": "integer",
            "description": "返回条数上限，默认 5",
        },
    },
    "required": ["query"],
}


class LiteratureSearchTool:
    """检索 + 核验，累积已验证文献。"""

    def __init__(
        self, provider: RetrievalProvider, verifier: CitationVerifier
    ) -> None:
        self._provider = provider
        self._verifier = verifier
        self.found: dict[str, ReferenceEntry] = {}

    def search(self, query: str, limit: int = 5) -> str:
        try:
            candidates = self._provider.search(query, limit=int(limit))
        except RetrievalError as exc:
            return f"检索失败：{exc}"
        verified_new: list[ReferenceEntry] = []
        for cand in candidates:
            marked = self._verifier.verify_and_mark(cand)
            if marked.verified and marked.id not in self.found:
                self.found[marked.id] = marked
                verified_new.append(marked)
        if not verified_new:
            return f"未找到可核验的相关文献（query={query}）。"
        lines = [f"找到 {len(verified_new)} 篇已验证文献，可用其 id 引用："]
        for r in verified_new:
            authors = ", ".join(r.authors) or "佚名"
            lines.append(f"- [{r.id}] {authors}. {r.title} ({r.year})")
        return "\n".join(lines)


def build_writing_tools(
    provider: RetrievalProvider,
    verifier: CitationVerifier,
    hooks=None,
) -> tuple[ToolRegistry, LiteratureSearchTool]:
    """构造写作工具集与文献累积器。``hooks`` 注入工具调用扩展点（#15）。"""
    tool = LiteratureSearchTool(provider, verifier)
    registry = ToolRegistry(hooks=hooks)
    registry.register(
        name="search_literature",
        description="按主题检索并核验真实学术文献，返回可引用的已验证文献清单。"
        "写作中需要引用支撑时调用。",
        handler=tool.search,
        parameters=_SEARCH_SCHEMA,
    )
    return registry, tool
