"""Discovery 契约 + IdentifierResolution + 隔离（A2c-5，design D13）。

Discovery 源（如网页搜索）返回**自由文本线索**：有标题/大致出处，但无 work identifier、
可能是模型幻觉。它**只能**经 Identifier_Resolution（向权威源书目反查 → 取得 work id →
严格核验）才可信化。

**本项目不接真实 Web 后端**：只提供协议（在 `capabilities.py`）、测试 Stub、
IdentifierResolution 逻辑与隔离规则。**不提供**永远返回空却伪装成功的生产 provider。

关键纪律：
- **模糊反查无匹配 → `UNRESOLVED_NO_MATCH`**（不证明作品不存在，不产 not_found）；
  仅取得精确 identifier 后的 exact fetch 未命中才 `SUSPECTED_NONEXISTENT`（限该源）。
- `UnresolvedLead` 不入可引用集合、不计任何配额（Req 11.12）；URL 作不可信数据。
- Discovery 文本经 A2a 清洗后才进任何反查/提示（Req 11.15）。

不 import `tasking`、不读全局 config（阈值/预算注入）。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from paper_agent.literature import dedup as _dedup
from paper_agent.literature.untrusted import clean_external_text
from paper_agent.providers.retrieval.capabilities import (
    BibliographicQuery,
    DiscoveryLead,
)

_TITLE_MAX = 1000


@dataclass(frozen=True)
class UnresolvedLead:
    """未能解析出 work identifier 的 Discovery 线索。清洗后存储；URL 为不可信数据。"""

    title: str
    authors: tuple[str, ...] = ()
    year: int | None = None
    url: str = ""
    source: str = ""


@dataclass(frozen=True)
class ResolutionOutcome:
    """一次 IdentifierResolution 的结果。"""

    kind: str                          # resolved | unresolved_no_match | suspected_nonexistent
    candidate: object | None = None    # resolved 时的可核验候选
    verification_assessment: object | None = None   # resolved 时的严格核验结果
    lead: UnresolvedLead | None = None


@dataclass(frozen=True)
class DiscoveryStats:
    """四类互斥计数（Req 11.16）。"""

    discovered: int = 0
    resolved: int = 0
    unresolved_no_match: int = 0
    suspected_nonexistent: int = 0


def discovery_enabled_precondition(
    *,
    authoritative_tried: bool,
    has_gap: bool,
    enabled: bool,
    has_provider: bool,
) -> bool:
    """Discovery 启用前置（Req 11.7/11.9）：四条件同时成立才启用。

    `enabled=True` 但 `has_provider=False` → 明确配置警告 + 返回 False（不假装完成）。
    """
    if enabled and not has_provider:
        warnings.warn(
            "PAPER_LIT_DISCOVERY_ENABLED 已开启，但未配置任何 Discovery provider；"
            "Discovery 兜底不会执行（本项目不接真实 Web 后端）。",
            RuntimeWarning,
            stacklevel=2,
        )
        return False
    return bool(authoritative_tried and has_gap and enabled and has_provider)


def _lead_to_query(lead: DiscoveryLead) -> BibliographicQuery:
    title = clean_external_text(lead.title or "", max_chars=_TITLE_MAX)
    authors = tuple(clean_external_text(a, max_chars=200) for a in (lead.authors or ()))
    return BibliographicQuery(title=title, authors=authors, year=lead.year)


def _title_similar(a: str, b: str, threshold: float) -> bool:
    na, nb = _dedup.normalize_title(a), _dedup.normalize_title(b)
    if not na or not nb:
        return False
    return SequenceMatcher(None, na, nb).ratio() >= threshold


def resolve_identifier(
    lead: DiscoveryLead,
    *,
    registry,
    cross_verifier,
    budget,
    cache,
    title_similarity_threshold: float = 0.85,
) -> ResolutionOutcome:
    """把自由文本线索升级为可核验候选（Req 11.10–11.13）。

    向 registry 中支持书目反查的 VERIFY 源反查 → 相似度达标取候选 → 严格核验。
    模糊无匹配 → UNRESOLVED_NO_MATCH（非 not_found）；取得候选但严格核验未过（含 exact
    未命中）→ suspected_nonexistent（限该源）。
    """
    query = _lead_to_query(lead)
    unresolved = UnresolvedLead(
        title=query.title, authors=query.authors, year=lead.year,
        url=lead.url, source=lead.source,
    )
    if not query.title:
        return ResolutionOutcome("unresolved_no_match", lead=unresolved)

    matched = None
    for name, provider in registry.verify_pairs():
        search_bib = getattr(provider, "search_bibliographic", None)
        if search_bib is None:
            continue
        for cand in search_bib(query, limit=5):
            if _title_similar(query.title, getattr(cand, "title", ""), title_similarity_threshold):
                matched = cand
                break
        if matched is not None:
            break

    if matched is None:
        # 模糊反查无相似度达标候选 → 未解析，绝不产 not_found（Req 11.13）
        return ResolutionOutcome("unresolved_no_match", lead=unresolved)

    # 取得候选 → 严格核验
    assessment = cross_verifier.assess(matched, budget=budget, cache=cache)
    if assessment.meets_strict_threshold:
        return ResolutionOutcome("resolved", candidate=matched, verification_assessment=assessment)
    # 取得精确 identifier 但严格核验未过（含 exact 未命中）→ 疑似不存在（限该源）
    return ResolutionOutcome("suspected_nonexistent", candidate=matched,
                             verification_assessment=assessment, lead=unresolved)


def tally(outcomes, *, discovered: int) -> DiscoveryStats:
    """把一批 ResolutionOutcome 汇成四类互斥计数。"""
    resolved = sum(1 for o in outcomes if o.kind == "resolved")
    no_match = sum(1 for o in outcomes if o.kind == "unresolved_no_match")
    suspected = sum(1 for o in outcomes if o.kind == "suspected_nonexistent")
    return DiscoveryStats(
        discovered=discovered, resolved=resolved,
        unresolved_no_match=no_match, suspected_nonexistent=suspected,
    )


__all__ = [
    "UnresolvedLead",
    "ResolutionOutcome",
    "DiscoveryStats",
    "discovery_enabled_precondition",
    "resolve_identifier",
    "tally",
]
