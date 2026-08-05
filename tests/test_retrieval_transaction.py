"""A2c-2 wave 3 / Task 4：CommitPlan 内存试算 + 幂等 + revision 冲突。"""

from __future__ import annotations

import json

from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import record_to_dict
from paper_agent.literature.models import ReferenceRecord
from paper_agent.literature.quota import QuotaKind, QuotaScope, QuotaSpec
from paper_agent.literature.retrieval.batch import (
    BatchDisposition,
    BatchItem,
    BatchStatus,
    CandidateSnapshot,
    ProposedRecordSnapshot,
    RetrievalBatch,
)
from paper_agent.literature.retrieval.transaction import plan_commit
from paper_agent.literature.verification import VerificationPolicy
from paper_agent.workspace.models import RetrievalCandidate


def _snap():
    return CandidateSnapshot.from_candidate(
        RetrievalCandidate(id="x", title="T", authors=[], year=2020, source_id="s", source="crossref")
    )


def _payload(record) -> str:
    return json.dumps(record_to_dict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _accept_item(item_id, record):
    return BatchItem(
        item_id, _snap(), BatchDisposition.ACCEPT_NEW,
        proposed_record_snapshot=ProposedRecordSnapshot(
            target_reference_id=record.reference_id, expected_base_hash=None,
            canonical_record_payload=_payload(record),
        ),
        target_reference_id=record.reference_id,
    )


def _batch(items, status=BatchStatus.CONFIRMED):
    b = RetrievalBatch(
        batch_id="b1", task_fingerprint="tf",
        quota=QuotaSpec(QuotaKind.RETRIEVAL, QuotaScope.NEW_ONLY),
        verification_policy=VerificationPolicy.STRICT_FINAL_V1,
        items=tuple(items),
    )
    b.status = status
    return b


# ---------------------------------------------------------------- 前置校验

def test_only_confirmed_can_plan():
    b = _batch([], status=BatchStatus.STAGED)
    plan = plan_commit(b, ReferenceLedger())
    assert plan.ok is False
    assert any("not_confirmed" in c for c in plan.conflicts)


def test_already_committed_idempotent():
    b = _batch([], status=BatchStatus.COMMITTED)
    plan = plan_commit(b, ReferenceLedger())
    assert plan.ok is True and plan.already_committed is True
    assert plan.ledger_delta == 0


# ---------------------------------------------------------------- ACCEPT_NEW 试算不改主 Ledger

def test_accept_new_projected_not_mutating_main():
    ledger = ReferenceLedger()
    rec = ReferenceRecord(reference_id="r_new", title="New Paper",
                          work_identifiers=(("doi", "10.1/new"),), numeric_aliases=("1",))
    b = _batch([_accept_item("i1", rec)])
    plan = plan_commit(b, ledger)
    assert plan.ok is True and plan.ledger_delta == 1
    # 主 Ledger 逐字节不变
    assert len(ledger) == 0
    # 拟提交账本含新记录
    assert any(r["reference_id"] == "r_new" for r in plan.projected_ledger_json)


# ---------------------------------------------------------------- MERGE 乐观并发

def test_merge_revision_conflict_when_target_changed():
    existing = ReferenceRecord(reference_id="r1", title="Orig",
                               work_identifiers=(("doi", "10.1/x"),), numeric_aliases=("1",))
    ledger = ReferenceLedger([existing])
    # 用一个「过期」的 expected_base_hash（与当前 target 不符）
    merged = existing.with_changes(abstract="new abstract")
    item = BatchItem(
        "i1", _snap(), BatchDisposition.MERGE_DUPLICATE,
        proposed_record_snapshot=ProposedRecordSnapshot(
            target_reference_id="r1", expected_base_hash="STALEHASH",
            canonical_record_payload=_payload(merged),
        ),
        target_reference_id="r1",
    )
    plan = plan_commit(_batch([item]), ledger)
    assert plan.ok is False
    assert any(c.startswith("revision_conflict:r1") for c in plan.conflicts)
    assert len(ledger) == 1 and ledger.get("r1").abstract == ""   # 主 Ledger 未被覆盖


def test_merge_applies_when_base_hash_matches():
    existing = ReferenceRecord(reference_id="r1", title="Orig",
                               work_identifiers=(("doi", "10.1/x"),), numeric_aliases=("1",))
    ledger = ReferenceLedger([existing])
    good_hash = ProposedRecordSnapshot.hash_of(existing)
    merged = existing.with_changes(abstract="new abstract")
    item = BatchItem(
        "i1", _snap(), BatchDisposition.MERGE_DUPLICATE,
        proposed_record_snapshot=ProposedRecordSnapshot(
            target_reference_id="r1", expected_base_hash=good_hash,
            canonical_record_payload=_payload(merged),
        ),
        target_reference_id="r1",
    )
    plan = plan_commit(_batch([item]), ledger)
    assert plan.ok is True
    # 主 Ledger 仍不变（试算不落盘）；projected 含合并结果
    assert ledger.get("r1").abstract == ""
    proj = {r["reference_id"]: r for r in plan.projected_ledger_json}
    assert proj["r1"]["abstract"] == "new abstract"
