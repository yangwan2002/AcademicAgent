"""去重、合并与 reference_id 分配（A1 / 任务 6 + 任务 7）。

**去重只用作品级标识符**。``CONTAINER_TYPES``（ISSN/ISBN）在本模块**任何位置都不出现**——
:func:`~paper_agent.literature.identifiers.work_keys` 只遍历 ``WORK_TYPES``，所以「用
ISSN 把同一期刊的不同论文合并成一条」在结构上不可能发生（Property 3）。

两级匹配，**只有高置信会合并**：

- high：任一 work identifier 归一化同值 → 合并。
- low：归一化标题 + 年份 + 首作者姓氏三者全同 → **仍创建独立记录** + 产一条
  ``DedupSuggestion``。低置信绝不自动合并（中文姓名、团体作者、常见标题都易误判，
  且系统无 unmerge 能力，见 Req 3.5）。

``reference_id`` 用内容派生哈希，**首次入库分配后永不重算**（D1）：同一 DOI 在任何
会话、任何机器上得同一 id → 跨会话合并天然幂等。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from paper_agent.literature import identifiers as _ids
from paper_agent.literature.models import (
    DedupSuggestion,
    FieldConflict,
    ReferenceRecord,
)
from paper_agent.literature.sources import resolve_field_conflict

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+", re.UNICODE)
_NON_ALPHA = re.compile(r"[^\w]", re.UNICODE)


def normalize_title(title: str) -> str:
    """标题归一化：NFC → casefold → 去标点 → 压空白。用于低置信比对与 id 种子。"""
    if not title:
        return ""
    text = unicodedata.normalize("NFC", title).casefold()
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def first_author_key(authors: Iterable[str]) -> str:
    """取首作者姓氏并归一化。空作者返回空串。"""
    authors = list(authors or [])
    if not authors:
        return ""
    first = unicodedata.normalize("NFC", authors[0]).strip()
    if not first:
        return ""
    # 英文取末段（姓氏），中文名整体保留；再去掉非字词字符并 casefold。
    parts = first.split()
    surname = parts[-1] if parts else first
    return _NON_ALPHA.sub("", surname).casefold()


# ---------------------------------------------------------------- reference_id

def make_reference_id(record: ReferenceRecord) -> str:
    """按内容派生稳定主键，**一次分配后不应重算**。

    种子优先级：首个可得 work identifier → 标题+年份 → legacy（标题+首个数字别名）。
    """
    seed = _reference_id_seed(record)
    return "r" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _reference_id_seed(record: ReferenceRecord) -> str:
    picked = _ids.pick_by_priority(record.work_identifiers)
    if picked is not None:
        return f"{picked[0]}:{picked[1]}"
    title_key = normalize_title(record.title)
    if title_key:
        return f"title:{title_key}|{record.year or ''}"
    numeric = record.numeric_aliases[0] if record.numeric_aliases else ""
    return f"legacy:{title_key}|{numeric}"


# ---------------------------------------------------------------- 去重匹配

@dataclass(frozen=True)
class DedupMatch:
    reference_id: str
    basis: str
    confidence: str          # high | low


def _work_key_set(record: ReferenceRecord) -> set[tuple[str, str]]:
    return _ids.work_keys(record.work_identifiers)


def find_duplicate(
    incoming: ReferenceRecord,
    existing: Iterable[ReferenceRecord],
    *,
    allow_low: bool = True,
) -> DedupMatch | None:
    """在既有记录中找与 ``incoming`` 重复者。

    high 置信（work identifier 同值）**总是**返回；low 置信仅在 ``allow_low`` 为真时
    返回，且调用方（Ledger）据此只产建议、不合并。
    """
    incoming_keys = _work_key_set(incoming)
    low_title = normalize_title(incoming.title)
    low_author = first_author_key(incoming.authors)

    low_hit: DedupMatch | None = None
    for rec in existing:
        if rec.reference_id == incoming.reference_id:
            continue
        # high：任一 work identifier 归一化同值
        shared = incoming_keys & _work_key_set(rec)
        if shared:
            id_type = sorted(shared)[0][0]
            return DedupMatch(rec.reference_id, f"identifier:{id_type}", "high")
        # low：标题 + 年份 + 首作者姓氏三者全同（且都非空）
        if (
            low_hit is None
            and low_title
            and low_author
            and incoming.year is not None
            and normalize_title(rec.title) == low_title
            and rec.year == incoming.year
            and first_author_key(rec.authors) == low_author
        ):
            low_hit = DedupMatch(rec.reference_id, "title+year+author", "low")

    if low_hit is not None and allow_low:
        return low_hit
    return None


# ---------------------------------------------------------------- 合并

#: 参与合并的标量字符串字段。
_SCALAR_FIELDS = (
    "title",
    "publication_date",
    "abstract",
    "language_hint",
    "reference_type",
    "container_title",
    "volume",
    "issue",
    "pages",
    "article_number",
    "publisher",
    "publisher_place",
    "edition",
    "url",
    "accessed_at",
    "conference_name",
    "conference_place",
)


@dataclass
class MergeResult:
    record: ReferenceRecord
    conflicts: tuple[FieldConflict, ...] = ()


def _field_source(record: ReferenceRecord, field_name: str) -> str:
    prov = record.field_provenance_map().get(field_name)
    if prov:
        return prov
    return record.discovery_sources[0] if record.discovery_sources else ""


def _merge_pairs(
    a: tuple[tuple[str, str], ...],
    b: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """按 key 并集合并 pair 列表；同 key 冲突时保留 a 的值（调用点已按需排序）。"""
    out: dict[str, str] = {}
    for k, v in b:
        out.setdefault(k, v)
    for k, v in a:            # a 优先
        out[k] = v
    return tuple(sorted(out.items()))


def merge_records(
    existing: ReferenceRecord,
    incoming: ReferenceRecord,
    *,
    priority: list[str] | tuple[str, ...] | None = None,
) -> MergeResult:
    """把 ``incoming`` 合并进 ``existing``，返回新的不可变记录。

    保留 ``existing`` 的 ``reference_id``（``incoming`` 的进 ``merged_from``）；
    标识符与核验事件取并集；标量字段按 provenance 层级裁决；``abstract`` 绝不清空。
    """
    changes: dict = {}
    conflicts: list[FieldConflict] = list(existing.field_conflicts)
    field_prov: dict[str, str] = existing.field_provenance_map()

    # 标识符并集（existing 优先保留同 key 值）
    changes["work_identifiers"] = _merge_pairs(
        existing.work_identifiers, incoming.work_identifiers
    )
    changes["container_identifiers"] = _merge_pairs(
        existing.container_identifiers, incoming.container_identifiers
    )
    changes["unclassified_identifiers"] = tuple(
        dict.fromkeys(
            (*existing.unclassified_identifiers, *incoming.unclassified_identifiers)
        )
    )

    # 核验事件并集：按 event_id 去重，按 checked_at 排序
    ev: dict[str, object] = {e.event_id: e for e in existing.verification_events}
    for e in incoming.verification_events:
        ev.setdefault(e.event_id, e)
    changes["verification_events"] = tuple(
        sorted(ev.values(), key=lambda e: (e.checked_at, e.event_id))
    )

    # 标量字段
    for name in _SCALAR_FIELDS:
        ex_val = getattr(existing, name)
        in_val = getattr(incoming, name)
        if not in_val:
            continue
        if not ex_val:
            changes[name] = in_val
            field_prov[name] = _field_source(incoming, name)
            continue
        if ex_val != in_val:
            winner_src = resolve_field_conflict(
                _field_source(existing, name),
                _field_source(incoming, name),
                priority,
            )
            in_src = _field_source(incoming, name)
            if winner_src == in_src and in_src != _field_source(existing, name):
                changes[name] = in_val
                field_prov[name] = in_src
                conflicts.append(
                    FieldConflict(name, in_val, in_src, ex_val, _field_source(existing, name))
                )
            else:
                conflicts.append(
                    FieldConflict(name, ex_val, _field_source(existing, name), in_val, in_src)
                )

    # year / authors 单独处理
    if incoming.year is not None:
        if existing.year is None:
            changes["year"] = incoming.year
            field_prov["year"] = _field_source(incoming, "year")
        elif existing.year != incoming.year:
            winner_src = resolve_field_conflict(
                _field_source(existing, "year"), _field_source(incoming, "year"), priority
            )
            in_src = _field_source(incoming, "year")
            if winner_src == in_src and in_src != _field_source(existing, "year"):
                changes["year"] = incoming.year
                field_prov["year"] = in_src
                conflicts.append(
                    FieldConflict("year", str(incoming.year), in_src,
                                  str(existing.year), _field_source(existing, "year"))
                )
            else:
                conflicts.append(
                    FieldConflict("year", str(existing.year), _field_source(existing, "year"),
                                  str(incoming.year), in_src)
                )
    if incoming.authors and not existing.authors:
        changes["authors"] = incoming.authors
        field_prov["authors"] = _field_source(incoming, "authors")

    # bibkey：保留 existing，incoming 的进 aliases
    alias = list(existing.bibkey_aliases)
    if incoming.bibkey and incoming.bibkey != existing.bibkey:
        if incoming.bibkey not in alias:
            alias.append(incoming.bibkey)
    for a in incoming.bibkey_aliases:
        if a and a != existing.bibkey and a not in alias:
            alias.append(a)
    changes["bibkey_aliases"] = tuple(alias)

    # 数字别名并集，existing 首个仍为主别名
    numeric = list(existing.numeric_aliases)
    for a in incoming.numeric_aliases:
        if a and a not in numeric:
            numeric.append(a)
    changes["numeric_aliases"] = tuple(numeric)

    # reference_id：existing 保留，incoming 的进 merged_from
    merged_from = list(existing.merged_from)
    if incoming.reference_id and incoming.reference_id != existing.reference_id:
        if incoming.reference_id not in merged_from:
            merged_from.append(incoming.reference_id)
    for m in incoming.merged_from:
        if m and m not in merged_from:
            merged_from.append(m)
    changes["merged_from"] = tuple(merged_from)

    # legacy_verified 逻辑或；discovery_sources 并集
    changes["legacy_verified"] = existing.legacy_verified or incoming.legacy_verified
    if existing.legacy_verified or incoming.legacy_verified:
        changes["verification_method"] = existing.verification_method or incoming.verification_method
    changes["discovery_sources"] = tuple(
        dict.fromkeys((*existing.discovery_sources, *incoming.discovery_sources))
    )

    changes["field_provenance"] = tuple(sorted(field_prov.items()))
    changes["field_conflicts"] = tuple(conflicts)

    return MergeResult(existing.with_changes(**changes), tuple(conflicts))


__all__ = [
    "normalize_title",
    "first_author_key",
    "make_reference_id",
    "DedupMatch",
    "MergeResult",
    "find_duplicate",
    "merge_records",
]
