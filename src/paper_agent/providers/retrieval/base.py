"""文献检索 provider 接口。

可插拔的检索抽象，背后可挂 Mock / 真实 API / MCP 三种实现，
切换数据源不影响主流程（依赖倒置）。

返回的 `ReferenceEntry` 必须带可核验的 source_id（DOI / arXiv id 等），
以支撑引用真实性核验（Req 3.3 / 4）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from paper_agent.workspace.models import ReferenceEntry


class RetrievalError(Exception):
    """检索源不可用或调用失败。"""


@runtime_checkable
class RetrievalProvider(Protocol):
    def search(self, query: str, limit: int = 10) -> list[ReferenceEntry]:
        """按查询词检索候选文献。"""
        ...

    def fetch_metadata(self, identifier: str) -> ReferenceEntry | None:
        """按标识符（DOI/arXiv id）取回单条文献元数据；不存在返回 None。"""
        ...
