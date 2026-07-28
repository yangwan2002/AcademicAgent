"""检索源角色、来源层级、标识符类型级核验权限（A1 / 任务 2）。

**为什么核验权限要细化到标识符类型**：若只用一个 ``authoritative: bool``，OpenAlex
（一个聚合库）只要回查到某个 DOI 就能把该文献判成 VERIFIED——但聚合库对 DOI 的记录
是二手转录，不该作为 DOI 的最终裁决依据（Req 11.17）。正确做法是：每个源分别声明
它对**哪些标识符类型**具备最终核验权、对哪些只能提供佐证。

- Crossref 是 DOI 注册机构 → 对 ``doi`` 有最终核验权。
- arXiv 是预印本平台 → 对 ``arxiv`` 有最终核验权，对 DOI 只作佐证。
- OpenAlex / Semantic Scholar 是聚合库 → 只对各自的库内 id 有最终核验权，
  对 DOI / arXiv / PMID 一律只作佐证。

:func:`verification_authority` 把这条规则变成结构性保证：
:func:`~paper_agent.literature.verification.aggregate_status` 的 VERIFIED 分支只认
``authority="final"`` 的事件，于是「aggregator 单独把 DOI 判成 VERIFIED」这条路径
在代码里不存在。

来源层级 :data:`PROVENANCE_RANK` 用于字段冲突裁决：registry > publisher >
aggregator > web，同层级才落到人工配置的源优先级。``web`` rank 为 0 且
:func:`resolve_field_conflict` 恒不选它，使「web 来源不得作裁决依据」（Req 11.21）
结构性成立。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProviderRole(str, Enum):
    """检索源的能力角色，一个源可兼具多个。"""

    SEARCH = "search"       # 按主题/关键词检索，返回候选与摘要
    VERIFY = "verify"       # 按标识符或书目文本反查，可产生核验事件
    DISCOVER = "discover"   # 只能发现候选，不能产生 verified 事件


#: 来源层级排名（越大越权威）。字段冲突时高层级胜，同级才看配置序。
PROVENANCE_RANK: dict[str, int] = {
    "registry": 3,      # DOI/标识符注册机构（Crossref、DataCite、PubMed）
    "publisher": 2,     # 出版方官网/平台（arXiv）
    "aggregator": 1,    # 聚合库（OpenAlex、Semantic Scholar）
    "web": 0,           # 网页、二手站点、迁移遗留
}


@dataclass(frozen=True)
class SourceProfile:
    """一个检索源的能力档案。

    - ``roles``：能力角色集合。
    - ``provenance``：元数据来源层级（用于字段冲突裁决）。
    - ``final_identifier_types``：该源可**最终核验**的标识符类型（native + registry）。
    - ``corroboration_identifier_types``：该源对这些类型只能提供**佐证**，不能最终核验。
    """

    name: str
    roles: frozenset[ProviderRole]
    provenance: str
    final_identifier_types: frozenset[str] = frozenset()
    corroboration_identifier_types: frozenset[str] = frozenset()


def _p(
    name: str,
    roles: set[ProviderRole],
    provenance: str,
    final: set[str] | None = None,
    corroboration: set[str] | None = None,
) -> SourceProfile:
    return SourceProfile(
        name=name,
        roles=frozenset(roles),
        provenance=provenance,
        final_identifier_types=frozenset(final or set()),
        corroboration_identifier_types=frozenset(corroboration or set()),
    )


#: 所有已知源的档案。``crossref`` / ``web_search`` 在 A1 只有声明、无 provider 实现
#: （A2 接入），但先登记好，使 A2 无需改动 A1 的裁决逻辑。
#:
#: ``mock`` 对全部类型保留最终核验权：既有测试与真机 A1 阶段（未接 Crossref）依赖
#: 「用 DOI 核验能得到 VERIFIED」这一现状，见 ``literature_require_final_verification``。
SOURCE_PROFILES: dict[str, SourceProfile] = {
    "crossref": _p("crossref", {ProviderRole.VERIFY}, "registry", final={"doi"}),
    "pubmed": _p(
        "pubmed",
        {ProviderRole.SEARCH, ProviderRole.VERIFY},
        "registry",
        final={"pmid", "doi"},
    ),
    "arxiv": _p(
        "arxiv",
        {ProviderRole.SEARCH, ProviderRole.VERIFY},
        "publisher",
        final={"arxiv"},
        corroboration={"doi"},
    ),
    "openalex": _p(
        "openalex",
        {ProviderRole.SEARCH, ProviderRole.VERIFY},
        "aggregator",
        final={"openalex_work"},
        corroboration={"doi", "arxiv", "pmid"},
    ),
    "semantic_scholar": _p(
        "semantic_scholar",
        {ProviderRole.SEARCH, ProviderRole.VERIFY},
        "aggregator",
        final={"semantic_scholar_paper"},
        corroboration={"doi", "arxiv", "pmid"},
    ),
    "web_search": _p("web_search", {ProviderRole.DISCOVER}, "web"),
    "legacy": _p("legacy", set(), "web"),
    "mock": _p(
        "mock",
        {ProviderRole.SEARCH, ProviderRole.VERIFY},
        "aggregator",
        final={"doi", "arxiv", "openalex_work", "semantic_scholar_paper", "pmid"},
    ),
}

#: 未知源的兜底档案：无任何角色、web 层级、无核验权。绝不默认授予信任。
_UNKNOWN = _p("", set(), "web")


def profile_for(source: str) -> SourceProfile:
    """取源档案；未知源返回最保守的兜底档案（无核验权、web 层级）。"""
    return SOURCE_PROFILES.get((source or "").strip().lower(), _UNKNOWN)


def provenance_of(source: str) -> str:
    return profile_for(source).provenance


def is_authoritative(source: str) -> bool:
    """该源是否具备 VERIFY 角色（能产生任何核验事件）。"""
    return ProviderRole.VERIFY in profile_for(source).roles


def verification_authority(source: str, id_type: str) -> str:
    """该源对某标识符类型的核验权限：``final`` | ``corroboration`` | ``none``。

    这是「不含 VERIFY 的源永不产生 VERIFIED」「aggregator 对 DOI 只作佐证」两条
    不变量的实现点。``aggregate_status`` 的 VERIFIED 分支只接受 ``final`` 事件。
    """
    profile = profile_for(source)
    if ProviderRole.VERIFY not in profile.roles:
        return "none"
    kind = (id_type or "").strip().lower()
    if kind in profile.final_identifier_types:
        return "final"
    if kind in profile.corroboration_identifier_types:
        return "corroboration"
    return "none"


def resolve_field_conflict(
    a_source: str,
    b_source: str,
    priority: list[str] | tuple[str, ...] | None = None,
) -> str:
    """字段冲突裁决：返回胜出方的源名。

    先按 :data:`PROVENANCE_RANK` 层级；同层级才按配置的源优先级 ``priority``
    （靠前者胜）；仍无法区分则取 ``a_source``（稳定、可预测）。
    """
    a = (a_source or "").strip().lower()
    b = (b_source or "").strip().lower()
    rank_a = PROVENANCE_RANK.get(provenance_of(a), 0)
    rank_b = PROVENANCE_RANK.get(provenance_of(b), 0)
    if rank_a != rank_b:
        return a if rank_a > rank_b else b

    order = [s.strip().lower() for s in (priority or [])]
    ia = order.index(a) if a in order else len(order)
    ib = order.index(b) if b in order else len(order)
    if ia != ib:
        return a if ia < ib else b
    return a


__all__ = [
    "ProviderRole",
    "PROVENANCE_RANK",
    "SourceProfile",
    "SOURCE_PROFILES",
    "profile_for",
    "provenance_of",
    "is_authoritative",
    "verification_authority",
    "resolve_field_conflict",
]
