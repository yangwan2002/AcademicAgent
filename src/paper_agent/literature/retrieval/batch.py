"""Retrieval_Batch：不可变快照、disposition、状态机、派生计数（A2c-2，design D5/D6）。

严格遵守 Req 9 的**单一事实源**纪律：``RetrievalBatch.items`` 是唯一事实源，
accepted/reused/merge/reject/counted/ledger_delta 全部为 items 的**派生视图**，不独立存储。

本文件 Task 1 部分：不可变值对象（快照 / disposition / BatchItem）。
状态机 + 派生计数 + summary（Task 3）在本模块后续补入。

关键纪律（Glossary + D5）：``CandidateSnapshot`` / ``ProposedRecordSnapshot`` 是**工作流
快照 / 拟提交命令**，**不是**第二套可写文献实体——真正的 ``ReferenceRecord`` 只在 commit
时由 ``canonical_record_payload`` 构造并写入 Ledger。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

_Pairs = tuple[tuple[str, str], ...]


class BatchDisposition(str, Enum):
    """单一枚举，四值互斥（Req 9.2）。"""

    ACCEPT_NEW = "accept_new"           # Ledger 无此篇，本批新接受
    REUSE_EXISTING = "reuse_existing"   # 命中 Ledger 已有、无元数据变更，仅复用计数
    MERGE_DUPLICATE = "merge_duplicate" # 命中已有且需合并新元数据
    REJECT = "reject"                   # 未过核验 / filter / relevance


def _pairs_of(mapping) -> _Pairs:
    """把 dict / pair 序列归一为排序后的不可变 pair 元组。"""
    if not mapping:
        return ()
    if isinstance(mapping, dict):
        items = mapping.items()
    else:
        items = mapping
    return tuple(sorted((str(k), str(v)) for k, v in items))


@dataclass(frozen=True)
class CandidateSnapshot:
    """候选的**深冻结**快照。外部事后修改原候选的 dict/list 不影响本快照（值拷贝 + tuple）。"""

    title: str
    authors: tuple[str, ...]
    year: int | None
    abstract: str
    work_identifiers: _Pairs
    container_identifiers: _Pairs
    source: str
    content_hash: str

    @staticmethod
    def _hash(title: str, authors: tuple[str, ...], year: int | None,
              abstract: str, work_ids: _Pairs) -> str:
        payload = {
            "title": unicodedata.normalize("NFC", title or ""),
            "authors": [unicodedata.normalize("NFC", a) for a in authors],
            "year": year,
            "abstract": unicodedata.normalize("NFC", abstract or ""),
            "work_identifiers": [list(p) for p in work_ids],
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_candidate(cls, cand) -> "CandidateSnapshot":
        title = getattr(cand, "title", "") or ""
        authors = tuple(str(a) for a in (getattr(cand, "authors", ()) or ()))
        year = getattr(cand, "year", None)
        abstract = getattr(cand, "abstract", "") or ""
        work_ids = _pairs_of(getattr(cand, "work_identifiers", {}) or {})
        container_ids = _pairs_of(getattr(cand, "container_identifiers", {}) or {})
        source = getattr(cand, "source", "") or ""
        return cls(
            title=title,
            authors=authors,
            year=year,
            abstract=abstract,
            work_identifiers=work_ids,
            container_identifiers=container_ids,
            source=source,
            content_hash=cls._hash(title, authors, year, abstract, work_ids),
        )


@dataclass(frozen=True)
class ProposedRecordSnapshot:
    """拟提交命令（**非** ReferenceRecord）。commit 时校验并构造真正的记录。

    - ``target_reference_id``：ACCEPT_NEW 预留的稳定 id / MERGE 命中的目标 id。
    - ``expected_base_hash``：ACCEPT_NEW 为 None；MERGE_DUPLICATE 携带**目标记录当前**
      规范哈希，供 commit 做乐观并发校验（确认期间目标已变 → revision_conflict，禁止覆盖）。
    - ``canonical_record_payload``：规范 JSON（复用 migration.record_to_dict 口径），
      commit 时据此构造 ReferenceRecord。
    """

    target_reference_id: str
    expected_base_hash: str | None
    canonical_record_payload: str

    @staticmethod
    def payload_of(record) -> str:
        """把 ReferenceRecord 序列化为规范 JSON（sort_keys，稳定可比较）。"""
        from paper_agent.literature.migration import record_to_dict

        return json.dumps(record_to_dict(record), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    @staticmethod
    def hash_of(record) -> str:
        """目标记录当前规范哈希（乐观并发基线）。"""
        canonical = ProposedRecordSnapshot.payload_of(record)
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class BatchItem:
    """Retrieval_Batch 的唯一事实源元素（Req 9.1/9.2/9.5）。"""

    item_id: str
    candidate_snapshot: CandidateSnapshot
    disposition: BatchDisposition
    proposed_record_snapshot: ProposedRecordSnapshot | None = None
    target_reference_id: str | None = None
    language_verdict: object | None = None       # A2a LanguageVerdict（A2c-3 填）
    relevance: object | None = None              # A2a RelevanceAssessment（A2c-3 填）
    verification_events: tuple = ()
    rejection_reasons: tuple[str, ...] = field(default_factory=tuple)


def validate_item(item: BatchItem) -> tuple[str, ...]:
    """校验 item 与 disposition 的一致性（Req 9.2）。返回违规项，空=合法。"""
    problems: list[str] = []
    d = item.disposition
    needs_proposed = d in (BatchDisposition.ACCEPT_NEW, BatchDisposition.MERGE_DUPLICATE)
    has_proposed = item.proposed_record_snapshot is not None

    if needs_proposed and not has_proposed:
        problems.append(f"{d.value}:missing_proposed_record_snapshot")
    if not needs_proposed and has_proposed:
        problems.append(f"{d.value}:unexpected_proposed_record_snapshot")

    needs_target = d in (
        BatchDisposition.ACCEPT_NEW,
        BatchDisposition.REUSE_EXISTING,
        BatchDisposition.MERGE_DUPLICATE,
    )
    if needs_target and not item.target_reference_id:
        problems.append(f"{d.value}:missing_target_reference_id")

    # MERGE 必须带乐观并发基线；ACCEPT_NEW 的 expected_base_hash 应为 None。
    if has_proposed:
        prs = item.proposed_record_snapshot
        if d is BatchDisposition.MERGE_DUPLICATE and prs.expected_base_hash is None:
            problems.append("merge_duplicate:missing_expected_base_hash")
        if d is BatchDisposition.ACCEPT_NEW and prs.expected_base_hash is not None:
            problems.append("accept_new:unexpected_expected_base_hash")

    return tuple(problems)


__all__ = [
    "BatchDisposition",
    "CandidateSnapshot",
    "ProposedRecordSnapshot",
    "BatchItem",
    "validate_item",
]


# ===========================================================================
# Task 3：RetrievalBatch 状态机 + 派生计数 + 结构化摘要
# ===========================================================================

from paper_agent.literature.quota import QuotaScope  # noqa: E402


class BatchStatus(str, Enum):
    """暂存批次状态机（design D6）。合法迁移见 RetrievalBatch。"""

    RUNNING = "running"        # 仅存在于本次 Loop 内存
    STAGED = "staged"          # Loop 收束成果，待用户确认
    CONFIRMED = "confirmed"    # 用户已确认，待提交
    COMMITTED = "committed"    # 已原子提交进 Ledger（终态）
    REJECTED = "rejected"      # 用户否决 / 中途放弃（终态）


class BatchTransitionError(RuntimeError):
    """非法状态迁移（拒绝，不静默）。"""


# 合法迁移：RUNNING→STAGED→CONFIRMED→COMMITTED；任意非终态 → REJECTED。
_ALLOWED_TRANSITIONS: dict[BatchStatus, frozenset[BatchStatus]] = {
    BatchStatus.RUNNING: frozenset({BatchStatus.STAGED, BatchStatus.REJECTED}),
    BatchStatus.STAGED: frozenset({BatchStatus.CONFIRMED, BatchStatus.REJECTED}),
    BatchStatus.CONFIRMED: frozenset({BatchStatus.COMMITTED, BatchStatus.REJECTED}),
    BatchStatus.COMMITTED: frozenset(),
    BatchStatus.REJECTED: frozenset(),
}


@dataclass
class RetrievalBatch:
    """暂存批次（运行态）。``items`` 是唯一事实源，计数全派生（Req 9.1/9.4/9.6）。"""

    batch_id: str
    task_fingerprint: str
    quota: object                              # A2b QuotaSpec
    verification_policy: object                # A2c-1 VerificationPolicy
    authority_registry_version: str = ""
    status: BatchStatus = BatchStatus.RUNNING
    items: tuple[BatchItem, ...] = ()
    created_at: str = ""
    revision: int = 0

    # -- 状态迁移 --

    def _transition(self, target: BatchStatus) -> None:
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise BatchTransitionError(f"非法迁移：{self.status.value} → {target.value}")
        self.status = target
        self.revision += 1

    def stage(self) -> None:
        self._transition(BatchStatus.STAGED)

    def confirm(self) -> None:
        self._transition(BatchStatus.CONFIRMED)

    def reject(self) -> None:
        self._transition(BatchStatus.REJECTED)

    def mark_committed(self) -> bool:
        """标记已提交。已 COMMITTED 时**幂等**返回 False（不重复动作）。"""
        if self.status is BatchStatus.COMMITTED:
            return False
        self._transition(BatchStatus.COMMITTED)
        return True

    # -- 派生计数（全部由 items 派生，不独立存储，Req 9.4）--

    def _ids_where(self, disposition: BatchDisposition) -> tuple[str, ...]:
        seen: list[str] = []
        for it in self.items:
            if it.disposition is disposition and it.target_reference_id:
                if it.target_reference_id not in seen:
                    seen.append(it.target_reference_id)
        return tuple(seen)

    def accepted_new_ids(self) -> tuple[str, ...]:
        return self._ids_where(BatchDisposition.ACCEPT_NEW)

    def reused_ids(self) -> tuple[str, ...]:
        return self._ids_where(BatchDisposition.REUSE_EXISTING)

    def merged_ids(self) -> tuple[str, ...]:
        return self._ids_where(BatchDisposition.MERGE_DUPLICATE)

    def rejected_items(self) -> tuple[BatchItem, ...]:
        return tuple(it for it in self.items if it.disposition is BatchDisposition.REJECT)

    def counted_reference_ids(self, scope) -> tuple[str, ...]:
        """满足当前 scope 的去重 target_reference_id（Req 9.6）。

        ACCEPT_NEW 恒计入；REUSE_EXISTING / MERGE_DUPLICATE 仅 scope=ledger_total 时计入。
        """
        counted: list[str] = list(self.accepted_new_ids())
        if scope is QuotaScope.LEDGER_TOTAL:
            for rid in (*self.reused_ids(), *self.merged_ids()):
                if rid not in counted:
                    counted.append(rid)
        return tuple(counted)

    def ledger_delta(self) -> int:
        """新增进 Ledger 的条数 = ACCEPT_NEW 条数（Req 9.6）。"""
        return len(self.accepted_new_ids())


@dataclass(frozen=True)
class BatchItemSummary:
    title: str
    year: int | None
    source: str
    disposition: str
    language_code: str = ""
    language_confidence: str = ""
    relevance_verdict: str = ""
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class BatchSummary:
    """结构化清单（Req 9.7 数据面）；交互 UI 呈现属 A2d。"""

    batch_id: str
    status: str
    items: tuple[BatchItemSummary, ...]
    accepted_new: int
    reused: int
    merged: int
    rejected: int
    ledger_delta: int


def render_batch_summary(batch: RetrievalBatch) -> BatchSummary:
    """把 Batch 渲染为结构化摘要 DTO（标题/年份/来源/语言及置信/相关性/disposition + 计数）。"""
    item_summaries = []
    for it in batch.items:
        lv = it.language_verdict
        rel = it.relevance
        item_summaries.append(BatchItemSummary(
            title=it.candidate_snapshot.title,
            year=it.candidate_snapshot.year,
            source=it.candidate_snapshot.source,
            disposition=it.disposition.value,
            language_code=getattr(lv, "language_code", "") if lv is not None else "",
            language_confidence=getattr(lv, "confidence", "") if lv is not None else "",
            relevance_verdict=(getattr(rel, "verdict", "") or "") if rel is not None else "",
            rejection_reasons=it.rejection_reasons,
        ))
    return BatchSummary(
        batch_id=batch.batch_id,
        status=batch.status.value,
        items=tuple(item_summaries),
        accepted_new=len(batch.accepted_new_ids()),
        reused=len(batch.reused_ids()),
        merged=len(batch.merged_ids()),
        rejected=len(batch.rejected_items()),
        ledger_delta=batch.ledger_delta(),
    )


__all__ += [
    "BatchStatus",
    "BatchTransitionError",
    "RetrievalBatch",
    "BatchItemSummary",
    "BatchSummary",
    "render_batch_summary",
]


# ===========================================================================
# A2c-5：pending batch 序列化（供 project facts 单 envelope 持久化 + 恢复）
# ===========================================================================

from paper_agent.literature.language import LanguageVerdict  # noqa: E402
from paper_agent.literature.quota import QuotaKind, QuotaScope, QuotaSpec  # noqa: E402
from paper_agent.literature.verification import (  # noqa: E402
    EventKind,
    VerificationEvent,
    VerificationPolicy,
)


def _event_to_dict(e: VerificationEvent) -> dict:
    return {
        "event_id": e.event_id, "kind": e.kind.value, "source": e.source,
        "provenance": e.provenance, "authority": e.authority,
        "identifier_type": e.identifier_type, "identifier": e.identifier,
        "checked_at": e.checked_at, "target_event_id": e.target_event_id, "details": e.details,
    }


def _event_from_dict(d: dict) -> VerificationEvent:
    return VerificationEvent(
        event_id=d.get("event_id", ""), kind=EventKind(d.get("kind", "verified")),
        source=d.get("source", ""), provenance=d.get("provenance", "aggregator"),
        authority=d.get("authority", "corroboration"),
        identifier_type=d.get("identifier_type", ""), identifier=d.get("identifier", ""),
        checked_at=d.get("checked_at", ""), target_event_id=d.get("target_event_id", ""),
        details=d.get("details", ""),
    )


def _lang_to_dict(lv: LanguageVerdict | None) -> dict | None:
    if lv is None:
        return None
    return {
        "language_code": lv.language_code, "quota_bucket": lv.quota_bucket,
        "confidence": lv.confidence, "basis": lv.basis,
        "script_ratios": [list(p) for p in lv.script_ratios],
        "reason_codes": list(lv.reason_codes),
    }


def _lang_from_dict(d: dict | None) -> LanguageVerdict | None:
    if d is None:
        return None
    return LanguageVerdict(
        language_code=d.get("language_code", "unknown"), quota_bucket=d.get("quota_bucket", "unknown"),
        confidence=d.get("confidence", "none"), basis=d.get("basis", "none"),
        script_ratios=tuple(tuple(p) for p in d.get("script_ratios", [])),
        reason_codes=tuple(d.get("reason_codes", [])),
    )


def _snapshot_to_dict(s: CandidateSnapshot) -> dict:
    return {
        "title": s.title, "authors": list(s.authors), "year": s.year, "abstract": s.abstract,
        "work_identifiers": [list(p) for p in s.work_identifiers],
        "container_identifiers": [list(p) for p in s.container_identifiers],
        "source": s.source, "content_hash": s.content_hash,
    }


def _snapshot_from_dict(d: dict) -> CandidateSnapshot:
    return CandidateSnapshot(
        title=d.get("title", ""), authors=tuple(d.get("authors", [])), year=d.get("year"),
        abstract=d.get("abstract", ""),
        work_identifiers=tuple(tuple(p) for p in d.get("work_identifiers", [])),
        container_identifiers=tuple(tuple(p) for p in d.get("container_identifiers", [])),
        source=d.get("source", ""), content_hash=d.get("content_hash", ""),
    )


def _proposed_to_dict(p: ProposedRecordSnapshot | None) -> dict | None:
    if p is None:
        return None
    return {
        "target_reference_id": p.target_reference_id,
        "expected_base_hash": p.expected_base_hash,
        "canonical_record_payload": p.canonical_record_payload,
    }


def _proposed_from_dict(d: dict | None) -> ProposedRecordSnapshot | None:
    if d is None:
        return None
    return ProposedRecordSnapshot(
        target_reference_id=d.get("target_reference_id", ""),
        expected_base_hash=d.get("expected_base_hash"),
        canonical_record_payload=d.get("canonical_record_payload", "{}"),
    )


def _item_to_dict(it: BatchItem) -> dict:
    from paper_agent.literature.relevance import _assessment_to_dict
    return {
        "item_id": it.item_id,
        "candidate_snapshot": _snapshot_to_dict(it.candidate_snapshot),
        "disposition": it.disposition.value,
        "proposed_record_snapshot": _proposed_to_dict(it.proposed_record_snapshot),
        "target_reference_id": it.target_reference_id,
        "language_verdict": _lang_to_dict(it.language_verdict),
        "relevance": _assessment_to_dict(it.relevance) if it.relevance is not None else None,
        "verification_events": [_event_to_dict(e) for e in it.verification_events],
        "rejection_reasons": list(it.rejection_reasons),
    }


def _item_from_dict(d: dict) -> BatchItem:
    from paper_agent.literature.relevance import _assessment_from_dict
    rel = d.get("relevance")
    return BatchItem(
        item_id=d.get("item_id", ""),
        candidate_snapshot=_snapshot_from_dict(d.get("candidate_snapshot", {})),
        disposition=BatchDisposition(d.get("disposition", "reject")),
        proposed_record_snapshot=_proposed_from_dict(d.get("proposed_record_snapshot")),
        target_reference_id=d.get("target_reference_id"),
        language_verdict=_lang_from_dict(d.get("language_verdict")),
        relevance=_assessment_from_dict(rel) if rel is not None else None,
        verification_events=tuple(_event_from_dict(e) for e in d.get("verification_events", [])),
        rejection_reasons=tuple(d.get("rejection_reasons", [])),
    )


def batch_to_dict(batch: RetrievalBatch) -> dict:
    """序列化 pending batch（供单 envelope 持久化）。quota 存最小可重建形态。"""
    q = batch.quota
    quota_d = {
        "kind": q.kind.value, "scope": q.scope.value, "base_year": q.base_year,
        "eligibility_filters": [
            {"field": f.field.value, "operator": f.operator.value, "value": f.value, "verbatim": f.verbatim}
            for f in q.eligibility_filters
        ],
        "count_constraints": [
            {"dimension": c.dimension.value, "operator": c.operator.value, "value": c.value, "verbatim": c.verbatim}
            for c in q.count_constraints
        ],
    }
    return {
        "batch_id": batch.batch_id, "task_fingerprint": batch.task_fingerprint,
        "status": batch.status.value, "verification_policy": batch.verification_policy.value,
        "authority_registry_version": batch.authority_registry_version,
        "created_at": batch.created_at, "revision": batch.revision,
        "quota": quota_d,
        "items": [_item_to_dict(it) for it in batch.items],
    }


def batch_from_dict(d: dict) -> RetrievalBatch:
    from paper_agent.literature.quota import (
        CountConstraint, CountDimension, CountOperator,
        EligibilityFilter, FilterField, FilterOperator,
    )
    qd = d.get("quota", {})

    def _val(v):
        return tuple(v) if isinstance(v, list) else v

    filters = tuple(
        EligibilityFilter(FilterField(f["field"]), FilterOperator(f["operator"]),
                          _val(f["value"]), f.get("verbatim", ""))
        for f in qd.get("eligibility_filters", [])
    )
    constraints = tuple(
        CountConstraint(CountDimension(c["dimension"]), CountOperator(c["operator"]),
                        _val(c["value"]), c.get("verbatim", ""))
        for c in qd.get("count_constraints", [])
    )
    quota = QuotaSpec(
        kind=QuotaKind(qd.get("kind", "citation")), scope=QuotaScope(qd.get("scope", "actually_cited")),
        eligibility_filters=filters, count_constraints=constraints, base_year=qd.get("base_year"),
    )
    return RetrievalBatch(
        batch_id=d.get("batch_id", ""), task_fingerprint=d.get("task_fingerprint", ""),
        quota=quota,
        verification_policy=VerificationPolicy(d.get("verification_policy", "strict_final_v1")),
        authority_registry_version=d.get("authority_registry_version", ""),
        status=BatchStatus(d.get("status", "running")),
        items=tuple(_item_from_dict(it) for it in d.get("items", [])),
        created_at=d.get("created_at", ""), revision=d.get("revision", 0),
    )


__all__ += ["batch_to_dict", "batch_from_dict"]
