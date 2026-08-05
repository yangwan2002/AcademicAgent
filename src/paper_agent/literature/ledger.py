"""ReferenceLedger：唯一修改入口 + 五套索引（A1 / 任务 8）。

Ledger 是 ``ReferenceRecord`` 的唯一容器与唯一修改入口。任何写操作
（``upsert`` / ``replace`` / ``add_record`` / ``ingest_known_refs``）之后都**整体重建
五套索引**，杜绝「记录已改、索引未更」（A1 数据量小，正确优先于增量维护）。

**五套索引**（缺 ``merged_from`` 会让「旧 reference_id 仍可解析」落空）：
work key / bibkey / bibkey_alias / numeric_alias / merged_from → reference_id。

``resolve_token`` 命中多条时返回 ``AMBIGUOUS`` + 候选集合，**绝不任选一条，也不谎报
找不到**——迁移遗留的重复 bibkey/别名完全可能撞车，任选或谎报都会让引用悄悄指错文献。

**两个引用集合**（Req 4.2）：
- ``resolvable_*``：全部可解析标记，**含用户原稿的未核验引用**。A1 用它保持
  ``facts.bibkeys()`` 的旧语义，使 ``RewriteGate`` 行为逐字节不变。
- ``citable_*``：仅 VERIFIED（兼容模式含 legacy_verified）。供 A3 判断「新增引用允许用哪些」。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from paper_agent.literature import dedup as _dedup
from paper_agent.literature import identifiers as _ids
from paper_agent.literature.bibkey import make_bibkey
from paper_agent.literature.models import (
    DedupSuggestion,
    ReferenceRecord,
)
from paper_agent.literature.verification import (
    VerificationEvent,
    VerificationStatus,
    fold_events,
)
from paper_agent.session.facts import KnownRef


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ResolutionResult:
    status: ResolutionStatus
    reference_id: str = ""                 # 仅 RESOLVED
    candidates: tuple[str, ...] = ()       # 仅 AMBIGUOUS
    matched_via: str = ""                  # bibkey | bibkey_alias | numeric | merged_from


class UpsertAction(str, Enum):
    CREATED = "created"
    CREATED_WITH_SUGGESTION = "created_with_suggestion"
    MERGED = "merged"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class UpsertResult:
    action: UpsertAction
    reference_id: str
    suggestion: DedupSuggestion | None = None


@dataclass
class LedgerIntegrityReport:
    ambiguous_tokens: tuple[tuple[str, tuple[str, ...]], ...] = field(default_factory=tuple)
    invalid_transitions: tuple[tuple[str, tuple[str, ...]], ...] = field(default_factory=tuple)


class ReferenceLedger:
    def __init__(self, records: Iterable[ReferenceRecord] = ()) -> None:
        self._records: dict[str, ReferenceRecord] = {}
        self._suggestions: list[DedupSuggestion] = []
        for rec in records:
            self._records[rec.reference_id] = rec
        self._rebuild_indexes()

    # ------------------------------------------------------------ 索引
    def _rebuild_indexes(self) -> None:
        self._by_work: dict[tuple[str, str], list[str]] = {}
        self._by_bibkey: dict[str, list[str]] = {}
        self._by_alias: dict[str, list[str]] = {}
        self._by_numeric: dict[str, list[str]] = {}
        self._by_merged: dict[str, list[str]] = {}
        for rid, rec in self._records.items():
            for key in _ids.work_keys(rec.work_identifiers):
                self._by_work.setdefault(key, []).append(rid)
            if rec.bibkey:
                self._by_bibkey.setdefault(rec.bibkey, []).append(rid)
            for a in rec.bibkey_aliases:
                self._by_alias.setdefault(a, []).append(rid)
            for n in rec.numeric_aliases:
                self._by_numeric.setdefault(n, []).append(rid)
            for m in rec.merged_from:
                self._by_merged.setdefault(m, []).append(rid)

    # ------------------------------------------------------------ 读
    def get(self, reference_id: str) -> ReferenceRecord | None:
        return self._records.get(reference_id)

    def all_records(self) -> tuple[ReferenceRecord, ...]:
        return tuple(self._records.values())

    def suggestions(self) -> tuple[DedupSuggestion, ...]:
        return tuple(self._suggestions)

    def __len__(self) -> int:
        return len(self._records)

    def resolve_token(self, token: str, *, ext: str) -> ResolutionResult:
        """把一个引用标记解析到 reference_id。tex 查 bibkey/别名，md/docx 查数字别名。

        解析顺序由 `literature.dialect.resolution_order` 单一来源给出 —— 与 prompt 公布
        标记的口径同源，二者不可能再各自漂移（详见 dialect 模块 docstring）。
        """
        from paper_agent.literature.dialect import resolution_order

        token = (token or "").strip()
        if not token:
            return ResolutionResult(ResolutionStatus.NOT_FOUND)
        indexes = {
            "bibkey": self._by_bibkey,
            "bibkey_alias": self._by_alias,
            "numeric": self._by_numeric,
        }
        order: list[tuple[str, dict[str, list[str]]]] = [
            (name, indexes[name]) for name in resolution_order(ext) if name in indexes
        ]
        order.append(("merged_from", self._by_merged))   # 所有格式共有的兜底

        for via, index in order:
            hit = index.get(token)
            if not hit:
                continue
            unique = list(dict.fromkeys(hit))
            if len(unique) == 1:
                return ResolutionResult(
                    ResolutionStatus.RESOLVED, reference_id=unique[0], matched_via=via
                )
            return ResolutionResult(
                ResolutionStatus.AMBIGUOUS, candidates=tuple(unique), matched_via=via
            )
        return ResolutionResult(ResolutionStatus.NOT_FOUND)

    # ------------------------------------------------------------ 引用集合
    def _taken_bibkeys(self) -> set[str]:
        taken: set[str] = set()
        for rec in self._records.values():
            if rec.bibkey:
                taken.add(rec.bibkey)
            taken.update(rec.bibkey_aliases)
        return taken

    def next_numeric_alias(self) -> str:
        nums = [
            int(n)
            for rec in self._records.values()
            for n in rec.numeric_aliases
            if n.isdigit()
        ]
        return str(max(nums) + 1 if nums else 1)

    def _citable(self, rec: ReferenceRecord, *, allow_legacy: bool) -> bool:
        if rec.verification_status == VerificationStatus.VERIFIED:
            return True
        return allow_legacy and rec.legacy_verified

    def resolvable_bibkeys(self) -> set[str]:
        out: set[str] = set()
        for rec in self._records.values():
            if rec.bibkey:
                out.add(rec.bibkey)
            out.update(rec.bibkey_aliases)
        return out

    def resolvable_numeric_aliases(self) -> set[str]:
        return {n for rec in self._records.values() for n in rec.numeric_aliases}

    def citable_ids(self, *, allow_legacy: bool = True) -> set[str]:
        return {
            rid
            for rid, rec in self._records.items()
            if self._citable(rec, allow_legacy=allow_legacy)
        }

    def citable_bibkeys(self, *, allow_legacy: bool = True) -> set[str]:
        out: set[str] = set()
        for rec in self._records.values():
            if self._citable(rec, allow_legacy=allow_legacy) and rec.bibkey:
                out.add(rec.bibkey)
        return out

    def citable_numeric_aliases(self, *, allow_legacy: bool = True) -> set[str]:
        out: set[str] = set()
        for rec in self._records.values():
            if self._citable(rec, allow_legacy=allow_legacy):
                out.update(rec.numeric_aliases)
        return out

    # ------------------------------------------------------------ 写
    def replace(self, record: ReferenceRecord) -> None:
        """整条替换（唯一的原地覆盖入口）；随后重建索引。"""
        self._records[record.reference_id] = record
        self._rebuild_indexes()

    def add_record(self, record: ReferenceRecord) -> None:
        """低级追加（迁移用，1:1 不去重不合并）；随后重建索引。"""
        self._records[record.reference_id] = record
        self._rebuild_indexes()

    def upsert(
        self,
        record: ReferenceRecord,
        events: Iterable[VerificationEvent] = (),
    ) -> UpsertResult:
        """检索路径入库：分配 id → 去重 → 合并或新建 → 分配 bibkey/数字别名。

        ``record`` 是一个「候选记录」（reference_id 可为空）。high 置信重复 → 合并；
        low 置信 → 仍新建并留一条建议。
        """
        events = tuple(events)
        if events:
            record = record.with_changes(
                verification_events=(*record.verification_events, *events)
            )
        if not record.reference_id:
            record = record.with_changes(reference_id=_dedup.make_reference_id(record))

        match = _dedup.find_duplicate(record, self._records.values(), allow_low=True)

        if match is not None and match.confidence == "high":
            existing = self._records[match.reference_id]
            merged = _dedup.merge_records(existing, record).record
            if merged == existing:
                return UpsertResult(UpsertAction.UNCHANGED, existing.reference_id)
            self._records[existing.reference_id] = merged
            self._rebuild_indexes()
            return UpsertResult(UpsertAction.MERGED, existing.reference_id)

        # 新建：补 bibkey 与数字别名（仅在候选未自带时）
        assigned = {}
        if not record.bibkey:
            assigned["bibkey"] = make_bibkey(record, self._taken_bibkeys())
        if not record.numeric_aliases:
            assigned["numeric_aliases"] = (self.next_numeric_alias(),)
        # reference_id 种子碰撞（如两条无 work-id、标题+年份相同的低置信记录）
        # 时追加后缀，保证不同记录不互相覆盖；同 work-id 的重复已在上方 merge 分支处理。
        if record.reference_id in self._records:
            base = record.reference_id
            n = 2
            while f"{base}-{n}" in self._records:
                n += 1
            assigned["reference_id"] = f"{base}-{n}"
        if assigned:
            record = record.with_changes(**assigned)

        self._records[record.reference_id] = record
        self._rebuild_indexes()

        if match is not None and match.confidence == "low":
            suggestion = DedupSuggestion(
                left_id=record.reference_id,
                right_id=match.reference_id,
                basis=match.basis,
                confidence="low",
            )
            self._suggestions.append(suggestion)
            return UpsertResult(
                UpsertAction.CREATED_WITH_SUGGESTION, record.reference_id, suggestion
            )
        return UpsertResult(UpsertAction.CREATED, record.reference_id)

    def ingest_known_refs(self, rows: Iterable[KnownRef]) -> int:
        """迁移入口：旧 KnownRef → ReferenceRecord，**1:1 不合并**。返回新增条数。

        按 bibkey / 数字别名去重（沿用旧 ``add_ref`` 的去重语义），已存在则跳过。
        """
        from paper_agent.literature.migration import known_ref_to_record

        added = 0
        for row in rows:
            existing_bibkeys = self.resolvable_bibkeys()
            existing_numeric = self.resolvable_numeric_aliases()
            if row.bibkey and row.bibkey in existing_bibkeys:
                continue
            if row.id and row.id in existing_numeric:
                continue
            rec = known_ref_to_record(row)
            self._records[rec.reference_id] = rec
            self._rebuild_indexes()
            added += 1
        return added

    # ------------------------------------------------------------ 审计
    def integrity_report(self) -> LedgerIntegrityReport:
        ambiguous: list[tuple[str, tuple[str, ...]]] = []
        for index in (self._by_bibkey, self._by_alias, self._by_numeric, self._by_merged):
            for token, rids in index.items():
                unique = list(dict.fromkeys(rids))
                if len(unique) > 1:
                    ambiguous.append((token, tuple(unique)))
        invalid: list[tuple[str, tuple[str, ...]]] = []
        for rid, rec in self._records.items():
            fr = fold_events(rec.verification_events)
            if fr.invalid_transitions:
                invalid.append((rid, fr.invalid_transitions))
        return LedgerIntegrityReport(tuple(ambiguous), tuple(invalid))

    # ------------------------------------------------------------ 序列化
    def to_json(self) -> list[dict]:
        from paper_agent.literature.migration import record_to_dict

        return [record_to_dict(r) for r in self._records.values()]

    @classmethod
    def from_json(
        cls,
        rows: Iterable[dict],
        suggestions: Iterable[dict] = (),
    ) -> "ReferenceLedger":
        from paper_agent.literature.migration import (
            record_from_dict,
            suggestion_from_dict,
        )

        ledger = cls()
        for row in rows:
            rec = record_from_dict(row)
            ledger._records[rec.reference_id] = rec
        ledger._suggestions = [suggestion_from_dict(s) for s in suggestions]
        ledger._rebuild_indexes()
        return ledger


__all__ = [
    "ResolutionStatus",
    "ResolutionResult",
    "UpsertAction",
    "UpsertResult",
    "LedgerIntegrityReport",
    "ReferenceLedger",
]
