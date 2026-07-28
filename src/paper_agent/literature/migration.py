"""迁移与序列化（A1）。

本文件分两部分：

- **记录级辅助**（任务 8 需要，本轮先建）：``known_ref_to_record`` 把旧
  ``KnownRef`` 1:1 迁成 ``ReferenceRecord``；``record_to_dict`` / ``record_from_dict``
  在 facts.json v2 形态与记录之间双向转换。
- **加载/写回编排**（任务 12 追加）：混合 schema 合并、原子写回。

**迁移不虚高**：旧 ``verified=True`` 只产生一条 ``source="legacy"`` 的核验事件，
其 ``authority`` 非 ``final`` → 聚合状态仍为 ``UNVERIFIED``；可引用性由独立的
``legacy_verified`` 标记 + Ledger 的兼容模式保证。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile

from paper_agent.literature import dedup as _dedup
from paper_agent.literature import identifiers as _ids
from paper_agent.literature.models import DedupSuggestion, FieldConflict, ReferenceRecord
from paper_agent.literature.verification import (
    EventKind,
    VerificationEvent,
    make_event_id,
)
from paper_agent.session.facts import KnownRef

_LEGACY_CHECKED_AT = "1970-01-01T00:00:00Z"


def candidate_to_record(cand) -> ReferenceRecord:
    """检索候选（``RetrievalCandidate`` / ``ReferenceEntry``）→ 候选记录（reference_id 待分配）。

    标识符归一化后入库；``work_identifiers`` 为空时从 ``source_id`` 反推类型，
    归类失败的串进 ``unclassified_identifiers``（绝不塞进别名或去重字段）。
    """
    raw_work = dict(getattr(cand, "work_identifiers", {}) or {})
    raw_container = dict(getattr(cand, "container_identifiers", {}) or {})
    unclassified: tuple[str, ...] = ()

    if not raw_work and getattr(cand, "source_id", ""):
        t = _ids.classify(cand.source_id)
        if t:
            raw_work[t] = cand.source_id
        else:
            unclassified = (cand.source_id,)

    work_pairs = tuple(
        sorted(
            (k.lower(), _ids.normalize(k, v))
            for k, v in raw_work.items()
            if v and _ids.normalize(k, v)
        )
    )
    container_pairs = tuple(
        sorted(
            (k.lower(), _ids.normalize(k, v))
            for k, v in raw_container.items()
            if v and _ids.normalize(k, v)
        )
    )
    source = getattr(cand, "source", "") or ""
    field_prov = (("title", source),) if getattr(cand, "title", "") and source else ()

    prospective = ReferenceRecord(
        reference_id="",
        work_identifiers=work_pairs,
        container_identifiers=container_pairs,
        unclassified_identifiers=unclassified,
        title=getattr(cand, "title", "") or "",
        authors=tuple(getattr(cand, "authors", ()) or ()),
        year=getattr(cand, "year", None),
        abstract=getattr(cand, "abstract", "") or "",
        discovery_sources=(source,) if source else (),
        field_provenance=field_prov,
    )
    return prospective.with_changes(
        reference_id=_dedup.make_reference_id(prospective)
    )


def known_ref_to_record(row: KnownRef) -> ReferenceRecord:
    """旧 ``KnownRef`` → ``ReferenceRecord``（1:1，不合并不丢弃）。"""
    numeric = (row.id,) if row.id else ()
    field_prov: list[tuple[str, str]] = []
    if row.title:
        field_prov.append(("title", "legacy"))

    events: tuple[VerificationEvent, ...] = ()
    legacy_verified = bool(row.verified)
    if legacy_verified:
        eid = make_event_id(EventKind.VERIFIED, "legacy", row.bibkey or row.id, _LEGACY_CHECKED_AT)
        events = (
            VerificationEvent(
                event_id=eid,
                kind=EventKind.VERIFIED,
                source="legacy",
                provenance="web",
                authority="corroboration",   # 非 final → 不推 VERIFIED（迁移不虚高）
                checked_at=_LEGACY_CHECKED_AT,
                details="migrated from facts v1",
            ),
        )

    prospective = ReferenceRecord(
        reference_id="",
        title=row.title or "",
        bibkey=row.bibkey or "",
        numeric_aliases=numeric,
        verification_events=events,
        legacy_verified=legacy_verified,
        verification_method="legacy" if legacy_verified else "",
        discovery_sources=("legacy",),
        field_provenance=tuple(field_prov),
    )
    return prospective.with_changes(
        reference_id=_dedup.make_reference_id(prospective)
    )


# ---------------------------------------------------------------- 序列化

def _event_to_dict(e: VerificationEvent) -> dict:
    return {
        "event_id": e.event_id,
        "kind": e.kind.value,
        "source": e.source,
        "provenance": e.provenance,
        "authority": e.authority,
        "identifier_type": e.identifier_type,
        "identifier": e.identifier,
        "checked_at": e.checked_at,
        "target_event_id": e.target_event_id,
        "details": e.details,
    }


def _event_from_dict(d: dict) -> VerificationEvent:
    return VerificationEvent(
        event_id=d.get("event_id", ""),
        kind=EventKind(d.get("kind", "provider_error")),
        source=d.get("source", ""),
        provenance=d.get("provenance", "aggregator"),
        authority=d.get("authority", "corroboration"),
        identifier_type=d.get("identifier_type", ""),
        identifier=d.get("identifier", ""),
        checked_at=d.get("checked_at", ""),
        target_event_id=d.get("target_event_id", ""),
        details=d.get("details", ""),
    )


def _conflict_to_dict(c: FieldConflict) -> dict:
    return {
        "field_name": c.field_name,
        "kept_value": c.kept_value,
        "kept_source": c.kept_source,
        "dropped_value": c.dropped_value,
        "dropped_source": c.dropped_source,
    }


def _conflict_from_dict(d: dict) -> FieldConflict:
    return FieldConflict(
        field_name=d.get("field_name", ""),
        kept_value=d.get("kept_value", ""),
        kept_source=d.get("kept_source", ""),
        dropped_value=d.get("dropped_value", ""),
        dropped_source=d.get("dropped_source", ""),
    )


#: 直接透传的标量字段（str / int|None / bool）。
_SCALAR_KEYS = (
    "title", "year", "publication_date", "abstract", "language_hint",
    "reference_type", "container_title", "volume", "issue", "pages",
    "article_number", "publisher", "publisher_place", "edition", "url",
    "accessed_at", "conference_name", "conference_place",
    "bibkey", "legacy_verified", "verification_method", "quarantined",
    "created_at",
)


def record_to_dict(record: ReferenceRecord) -> dict:
    """转 facts.json v2 形态。pair 存为二元列表；``verification_status`` 不落（派生）。"""
    out: dict = {"reference_id": record.reference_id}
    out["work_identifiers"] = [list(p) for p in record.work_identifiers]
    out["container_identifiers"] = [list(p) for p in record.container_identifiers]
    out["unclassified_identifiers"] = list(record.unclassified_identifiers)
    for key in _SCALAR_KEYS:
        out[key] = getattr(record, key)
    out["authors"] = list(record.authors)
    out["bibkey_aliases"] = list(record.bibkey_aliases)
    out["numeric_aliases"] = list(record.numeric_aliases)
    out["merged_from"] = list(record.merged_from)
    out["discovery_sources"] = list(record.discovery_sources)
    out["field_provenance"] = [list(p) for p in record.field_provenance]
    out["verification_events"] = [_event_to_dict(e) for e in record.verification_events]
    out["field_conflicts"] = [_conflict_to_dict(c) for c in record.field_conflicts]
    return out


def _pairs(raw) -> tuple[tuple[str, str], ...]:
    return tuple((str(a), str(b)) for a, b in (raw or []))


def record_from_dict(d: dict) -> ReferenceRecord:
    kwargs: dict = {"reference_id": d.get("reference_id", "")}
    kwargs["work_identifiers"] = _pairs(d.get("work_identifiers"))
    kwargs["container_identifiers"] = _pairs(d.get("container_identifiers"))
    kwargs["unclassified_identifiers"] = tuple(d.get("unclassified_identifiers", []))
    for key in _SCALAR_KEYS:
        if key in d:
            kwargs[key] = d[key]
    kwargs["authors"] = tuple(d.get("authors", []))
    kwargs["bibkey_aliases"] = tuple(d.get("bibkey_aliases", []))
    kwargs["numeric_aliases"] = tuple(str(n) for n in d.get("numeric_aliases", []))
    kwargs["merged_from"] = tuple(d.get("merged_from", []))
    kwargs["discovery_sources"] = tuple(d.get("discovery_sources", []))
    kwargs["field_provenance"] = _pairs(d.get("field_provenance"))
    kwargs["verification_events"] = tuple(
        _event_from_dict(e) for e in d.get("verification_events", [])
    )
    kwargs["field_conflicts"] = tuple(
        _conflict_from_dict(c) for c in d.get("field_conflicts", [])
    )
    return ReferenceRecord(**kwargs)


def suggestion_to_dict(s: DedupSuggestion) -> dict:
    return {
        "left_id": s.left_id,
        "right_id": s.right_id,
        "basis": s.basis,
        "confidence": s.confidence,
        "evidence": s.evidence,
    }


def suggestion_from_dict(d: dict) -> DedupSuggestion:
    return DedupSuggestion(
        left_id=d.get("left_id", ""),
        right_id=d.get("right_id", ""),
        basis=d.get("basis", ""),
        confidence=d.get("confidence", "low"),
        evidence=d.get("evidence", ""),
    )


# ---------------------------------------------------------------- 原子写回

def _backup_path(path: str) -> str:
    if path.endswith(".json"):
        return path[: -len(".json")] + ".v1.bak.json"
    return path + ".bak"


def atomic_write_json(path: str, data) -> None:
    """原子写 JSON：首次备份原字节 → 写临时文件 → fsync → os.replace。

    中途失败清理临时文件，原文件与备份保持完整（Req 19.4）。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    backup = _backup_path(path)
    if os.path.isfile(path) and not os.path.isfile(backup):
        shutil.copy2(path, backup)

    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


__all__ = [
    "candidate_to_record",
    "known_ref_to_record",
    "record_to_dict",
    "record_from_dict",
    "suggestion_to_dict",
    "suggestion_from_dict",
    "atomic_write_json",
]
