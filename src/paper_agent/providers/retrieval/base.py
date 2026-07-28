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
    """检索源不可用或调用失败（基类）。

    分两种子类，使「查无此文」与「调用失败」自 HTTP 层起可区分（Req 2.7）：
    - :class:`RetrievalNotFound`：目标标识符不存在（HTTP 404 / 空结果）。
    - :class:`RetrievalUnavailable`：超时 / 限流 / 连接失败 / 重试耗尽 / 权限。
    二者均继承本类，故既有 ``except RetrievalError`` 调用点行为不变。
    """


class RetrievalNotFound(RetrievalError):
    """目标标识符查无此文（HTTP 404 或空结果）。``fetch_metadata`` 据此返回 None。"""


class RetrievalUnavailable(RetrievalError):
    """检索源不可用：超时 / 限流 / 连接失败 / 重试耗尽 / 其余 4xx。"""


@runtime_checkable
class RetrievalProvider(Protocol):
    def search(self, query: str, limit: int = 10) -> list[ReferenceEntry]:
        """按查询词检索候选文献。"""
        ...

    def fetch_metadata(self, identifier: str) -> ReferenceEntry | None:
        """按标识符取回单条文献元数据。

        返回 ``None`` **仅**表示「该标识符查无此文」；调用失败（超时/限流/连接）
        SHALL 抛 :class:`RetrievalUnavailable`，SHALL NOT 用 None 表示失败
        （否则 not_found 与 provider_error 无法区分，见 Req 2.7）。
        """
        ...
