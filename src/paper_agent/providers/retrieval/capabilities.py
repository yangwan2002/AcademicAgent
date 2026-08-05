"""按能力拆分的检索源协议（A2c-1 / 任务 1，design-a2c D1）。

现状 :class:`~paper_agent.providers.retrieval.base.RetrievalProvider` 把 ``search`` 与
``fetch_metadata`` 塞进同一个 Protocol。但 Crossref 主要是**核验/书目反查**源，不该为了
满足过宽的接口而伪造主题检索能力；反过来纯 SEARCH 源也不该被迫实现按标识符回查。

因此按**能力**拆三个 Protocol：

- ``SearchProvider``：按查询词检索候选（OpenAlex / arXiv / S2 / mock）。
- ``VerifyProvider``：按标识符精确回查、按书目文本模糊反查（Crossref / arXiv / …）。
- ``DiscoveryProvider``：只发现自由文本线索（A2c-5 才用；此期只声明协议与占位类型）。

一个源可同时符合多个协议（结构化子类型，运行期用 ``isinstance`` 判定角色）。

**``FetchResult`` 三态**是本模块的核心不变量（Req 2.7 / design D3）：
``FOUND`` / ``NOT_FOUND_AT_SOURCE`` / ``PROVIDER_ERROR`` 严格区分——
「该源未命中此标识符」（NOT_FOUND_AT_SOURCE）绝不等于「论文不存在」（Crossref 之外还有
DataCite 等注册库），网络错误（PROVIDER_ERROR）更绝不能伪装成空结果。

既有 ``base.py`` 的 ``RetrievalProvider`` / ``RetrievalError`` 及其子类**保持不变**，
旧 provider 与 ``search_and_verify`` 继续使用；本模块是并行增量，不替换。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from paper_agent.workspace.models import RetrievalCandidate


class FetchStatus(str, Enum):
    """按标识符精确回查的结果类别（Req 2.6 / 2.7）。"""

    FOUND = "found"                          # 命中，candidate 非空
    NOT_FOUND_AT_SOURCE = "not_found_at_source"  # 该源未命中此标识符（≠ 论文不存在）
    PROVIDER_ERROR = "provider_error"        # 连接/超时/限流/重试耗尽（≠ 查无此文）


@dataclass(frozen=True)
class FetchResult:
    """精确回查结果。``candidate`` 仅在 ``FOUND`` 时非空。

    ``detail`` 供审计/trace（如 HTTP 状态、错误类型），不承载业务分支语义——
    分支只看 ``status``，杜绝把网络错误读成空结果。
    """

    status: FetchStatus
    candidate: RetrievalCandidate | None = None
    detail: str = ""

    @property
    def found(self) -> bool:
        return self.status is FetchStatus.FOUND


@dataclass(frozen=True)
class BibliographicQuery:
    """书目文本模糊反查的输入（Identifier_Resolution 用，A2c-5 消费）。"""

    title: str
    authors: tuple[str, ...] = ()
    year: int | None = None


@dataclass(frozen=True)
class DiscoveryLead:
    """Discovery 源返回的自由文本线索（A2c-5 消费；此期仅定义占位）。

    尚无身份、无 work identifier、可能是模型幻觉；必须经 Identifier_Resolution 反查权威源
    才可能可信化。``url`` 一律作**不可信数据**存储/展示，不得作为指令或自动访问目标。
    """

    title: str
    authors: tuple[str, ...] = ()
    year: int | None = None
    url: str = ""
    source: str = ""


@runtime_checkable
class SearchProvider(Protocol):
    """按查询词检索候选文献。"""

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        filters: tuple[tuple[str, object], ...] = (),
    ) -> list[RetrievalCandidate]:
        ...


@runtime_checkable
class VerifyProvider(Protocol):
    """按标识符精确回查、按书目文本模糊反查的核验源。"""

    def fetch_metadata(self, identifier_type: str, identifier: str) -> FetchResult:
        """按标识符类型 + 值精确回查。

        契约（Req 2.7）：命中 → ``FetchResult(FOUND, candidate)``；该源明确未命中 →
        ``NOT_FOUND_AT_SOURCE``；调用失败（超时/限流/连接）→ ``PROVIDER_ERROR``。
        **绝不**用空结果表示失败。
        """
        ...

    def search_bibliographic(
        self,
        query: BibliographicQuery,
        *,
        limit: int = 5,
    ) -> list[RetrievalCandidate]:
        """按书目文本模糊反查，返回候选供 Identifier_Resolution 比对。

        **无匹配返回空列表**（语义为 ``UNRESOLVED_NO_MATCH``，由调用方据此判定）——
        模糊查询无结果**不证明**作品不存在，绝不产生 not_found / 疑似不存在。
        """
        ...


@runtime_checkable
class DiscoveryProvider(Protocol):
    """只发现自由文本线索、不能产生核验事件（A2c-5 消费）。"""

    def discover(self, query: str, *, limit: int = 10) -> list[DiscoveryLead]:
        ...


__all__ = [
    "FetchStatus",
    "FetchResult",
    "BibliographicQuery",
    "DiscoveryLead",
    "SearchProvider",
    "VerifyProvider",
    "DiscoveryProvider",
]
