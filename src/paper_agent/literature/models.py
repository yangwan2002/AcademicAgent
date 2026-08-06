"""持久化文献实体（A1 / 任务 4）。

``ReferenceRecord`` 是系统中**唯一可写、唯一持久化**的文献实体（Req 1.1）。
它是 ``frozen`` 且全字段不可变的：``list`` → ``tuple``，``dict`` → 有序的
``tuple[tuple[str, str], ...]``。这样 :class:`~paper_agent.literature.ledger.ReferenceLedger`
返回的对象无法被外部改动，索引不会静默失效。修改一律经 ``with_changes`` 产出新实例
再由 Ledger 重建索引。

几个字段刻意做成**派生 property 而非存储字段**，避免双真相源：

- ``verification_status``：由 ``verification_events`` 折叠（见 :mod:`.verification`）。
- ``abstract_content_hash``：由 ``abstract`` 实时算——存字段会在改摘要忘更新时让
  A3 的证据失效检测漏判。
- ``canonical_identifier``：只从 ``work_identifiers`` 按优先级取，结构上不可能选到
  container 标识符。

``bibliographic_complete`` 不是无参 property：字段是否齐全取决于目标引文格式与
``reference_type``。改为 :meth:`missing_fields`（带 style 参数）。A1 只留接口与骨架，
完整字段表属最后一期。
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, replace

from paper_agent.literature import identifiers as _ids
from paper_agent.literature.verification import (
    VerificationEvent,
    VerificationStatus,
    aggregate_status,
)
from paper_agent.session.facts import KnownRef

_Pairs = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class FieldConflict:
    """一次非空字段冲突的记录（合并时产生）。"""

    field_name: str
    kept_value: str
    kept_source: str
    dropped_value: str
    dropped_source: str


@dataclass(frozen=True)
class DedupSuggestion:
    """一条低置信去重建议（不自动合并，供用户/后续流程处置）。"""

    left_id: str
    right_id: str
    basis: str = ""
    confidence: str = "low"
    evidence: str = ""


#: 各 reference_type 在「基础样式渲染」下建议具备的著录字段（A1 骨架，非最终格式表）。
_TYPE_BASE_FIELDS: dict[str, tuple[str, ...]] = {
    "journal_article": ("title", "authors", "year", "container_title"),
    "conference_paper": ("title", "authors", "year", "conference_name"),
    "book": ("title", "authors", "year", "publisher"),
    "book_chapter": ("title", "authors", "year", "container_title", "publisher"),
    "thesis": ("title", "authors", "year", "publisher"),
    "preprint": ("title", "authors", "year"),
    "report": ("title", "authors", "year", "publisher"),
    "standard": ("title", "year"),
    "web_page": ("title", "url"),
}
_DEFAULT_BASE_FIELDS = ("title", "authors", "year")


@dataclass(frozen=True)
class ReferenceRecord:
    """一条持久化文献记录。全字段不可变。"""

    reference_id: str
    # ---- 标识符（作品级 / 载体级严格分离，见 identifiers 模块）----
    work_identifiers: _Pairs = ()
    container_identifiers: _Pairs = ()
    unclassified_identifiers: tuple[str, ...] = ()   # 无法归类的串，绝不参与去重
    # ---- 核心元数据 ----
    title: str = ""
    authors: tuple[str, ...] = ()
    year: int | None = None
    publication_date: str = ""
    abstract: str = ""
    language_hint: str = ""            # provider 原始 language 字段，判定属 A2
    # ---- 著录字段（Req 1.9；A1 只落库，渲染属最后一期）----
    reference_type: str = ""
    container_title: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    article_number: str = ""
    publisher: str = ""
    publisher_place: str = ""
    edition: str = ""
    url: str = ""
    accessed_at: str = ""
    conference_name: str = ""
    conference_place: str = ""
    # ---- 身份与别名（D2）----
    bibkey: str = ""
    bibkey_aliases: tuple[str, ...] = ()
    numeric_aliases: tuple[str, ...] = ()       # md/docx 的 [n]；首个为当前主别名
    merged_from: tuple[str, ...] = ()           # 被合并掉的旧 reference_id
    # ---- 核验（D3）----
    verification_events: tuple[VerificationEvent, ...] = ()
    legacy_verified: bool = False
    verification_method: str = ""               # "" | "legacy"
    quarantined: bool = False                   # 保留位；A1 不主动置位（变迁走事件）
    # ---- 溯源（D6）----
    discovery_sources: tuple[str, ...] = ()     # 可多个：多源合并不丢来源
    created_at: str = ""
    field_provenance: _Pairs = ()               # (字段名, 源名)
    field_conflicts: tuple[FieldConflict, ...] = ()

    # ---- 派生 property（均不作存储字段、不进 JSON）----
    @property
    def verification_status(self) -> VerificationStatus:
        return aggregate_status(self.verification_events)

    @property
    def canonical_identifier(self) -> tuple[str, str] | None:
        return _ids.pick_by_priority(self.work_identifiers)

    @property
    def abstract_content_hash(self) -> str:
        if not self.abstract:
            return ""
        text = unicodedata.normalize("NFC", self.abstract)
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

    @property
    def evidence_available(self) -> bool:
        return bool(self.abstract)

    # ---- 视图 / 变更 ----
    def work_id_map(self) -> dict[str, str]:
        return dict(self.work_identifiers)

    def container_id_map(self) -> dict[str, str]:
        return dict(self.container_identifiers)

    def field_provenance_map(self) -> dict[str, str]:
        return dict(self.field_provenance)

    def all_citation_tokens(self) -> set[str]:
        """本记录可被解析的全部引用标记（当前 + 历史 bibkey + 数字别名）。"""
        tokens: set[str] = set()
        if self.bibkey:
            tokens.add(self.bibkey)
        tokens.update(self.bibkey_aliases)
        tokens.update(self.numeric_aliases)
        return tokens

    def missing_fields(self, style: str = "") -> tuple[str, ...]:
        """目标格式下缺失的著录字段（A1 用按 reference_type 的基础骨架）。

        ``style`` 目前不细分（各格式的完整必需字段表属最后一期），只按
        ``reference_type`` 判断基础字段是否齐全，使「信息不全」可先被标注出来。
        """
        needed = _TYPE_BASE_FIELDS.get(self.reference_type, _DEFAULT_BASE_FIELDS)
        missing: list[str] = []
        for name in needed:
            value = getattr(self, name, "")
            if name == "authors":
                if not self.authors:
                    missing.append(name)
            elif name == "year":
                if self.year is None:
                    missing.append(name)
            elif not value:
                missing.append(name)
        return tuple(missing)

    def bibliographic_complete(self, style: str = "") -> bool:
        return not self.missing_fields(style)

    def with_changes(self, **kw) -> "ReferenceRecord":
        return replace(self, **kw)

    def to_known_ref(self) -> KnownRef:
        """降级为只读兼容投影。``id`` 取首个数字别名（供 .md 的 [n]）。

        ``verified`` 取兼容语义：VERIFIED 或 legacy_verified 均视为可引用，
        与 ``ReferenceLedger.citable_*`` 的默认 allow_legacy=True 一致。
        """
        numeric = self.numeric_aliases[0] if self.numeric_aliases else ""
        citable = (
            self.verification_status == VerificationStatus.VERIFIED
            or self.legacy_verified
        )
        return KnownRef(
            id=numeric,
            bibkey=self.bibkey,
            title=self.title,
            verified=citable,
        )


__all__ = [
    "ReferenceRecord",
    "FieldConflict",
    "DedupSuggestion",
]
