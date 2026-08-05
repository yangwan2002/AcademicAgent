"""A2c-2 wave 2 / Task 3：RetrievalBatch 状态机 + 派生计数 + summary。"""

from __future__ import annotations

import pytest

from paper_agent.literature.quota import QuotaKind, QuotaScope, QuotaSpec
from paper_agent.literature.retrieval.batch import (
    BatchDisposition,
    BatchItem,
    BatchStatus,
    BatchTransitionError,
    CandidateSnapshot,
    ProposedRecordSnapshot,
    RetrievalBatch,
    render_batch_summary,
)
from paper_agent.literature.verification import VerificationPolicy
from paper_agent.workspace.models import RetrievalCandidate


def _snap(title="T", **kw):
    return CandidateSnapshot.from_candidate(
        RetrievalCandidate(id="x", title=title, authors=[], year=2020,
                           source_id="s", source="crossref", **kw)
    )


def _item(item_id, disposition, rid=None, *, rejection=()):
    proposed = None
    if disposition in (BatchDisposition.ACCEPT_NEW, BatchDisposition.MERGE_DUPLICATE):
        proposed = ProposedRecordSnapshot(
            target_reference_id=rid or "r", canonical_record_payload="{}",
            expected_base_hash=("h" if disposition is BatchDisposition.MERGE_DUPLICATE else None),
        )
    return BatchItem(item_id, _snap(), disposition, proposed_record_snapshot=proposed,
                     target_reference_id=rid, rejection_reasons=rejection)


def _batch(items=()):
    return RetrievalBatch(
        batch_id="b1", task_fingerprint="tf", quota=QuotaSpec(QuotaKind.RETRIEVAL, QuotaScope.NEW_ONLY),
        verification_policy=VerificationPolicy.STRICT_FINAL_V1, items=tuple(items),
    )


# ---------------------------------------------------------------- 状态机

def test_happy_path_transitions():
    b = _batch()
    assert b.status is BatchStatus.RUNNING
    b.stage(); assert b.status is BatchStatus.STAGED
    b.confirm(); assert b.status is BatchStatus.CONFIRMED
    assert b.mark_committed() is True
    assert b.status is BatchStatus.COMMITTED


def test_illegal_transition_rejected():
    b = _batch()
    with pytest.raises(BatchTransitionError):
        b.confirm()                    # RUNNING → CONFIRMED 非法（须先 stage）


def test_reject_from_running_and_staged():
    b = _batch(); b.reject(); assert b.status is BatchStatus.REJECTED
    b2 = _batch(); b2.stage(); b2.reject(); assert b2.status is BatchStatus.REJECTED


def test_no_transition_from_terminal():
    b = _batch(); b.reject()
    with pytest.raises(BatchTransitionError):
        b.stage()


def test_commit_idempotent():
    b = _batch(); b.stage(); b.confirm(); b.mark_committed()
    assert b.mark_committed() is False           # 幂等，不重复动作
    assert b.status is BatchStatus.COMMITTED


# ---------------------------------------------------------------- 派生计数

def test_derived_counts_and_scope():
    items = [
        _item("i1", BatchDisposition.ACCEPT_NEW, "r1"),
        _item("i2", BatchDisposition.ACCEPT_NEW, "r2"),
        _item("i3", BatchDisposition.REUSE_EXISTING, "r3"),
        _item("i4", BatchDisposition.MERGE_DUPLICATE, "r4"),
        _item("i5", BatchDisposition.REJECT, rejection=("unrelated",)),
    ]
    b = _batch(items)
    assert set(b.accepted_new_ids()) == {"r1", "r2"}
    assert b.reused_ids() == ("r3",)
    assert b.merged_ids() == ("r4",)
    assert len(b.rejected_items()) == 1
    assert b.ledger_delta() == 2                 # 只 ACCEPT_NEW
    # new_only：只计 ACCEPT_NEW
    assert set(b.counted_reference_ids(QuotaScope.NEW_ONLY)) == {"r1", "r2"}
    # ledger_total：ACCEPT_NEW + REUSE + MERGE
    assert set(b.counted_reference_ids(QuotaScope.LEDGER_TOTAL)) == {"r1", "r2", "r3", "r4"}


def test_counted_dedup_by_reference_id():
    items = [
        _item("i1", BatchDisposition.ACCEPT_NEW, "r1"),
        _item("i2", BatchDisposition.ACCEPT_NEW, "r1"),   # 同 rid → 去重
    ]
    b = _batch(items)
    assert b.accepted_new_ids() == ("r1",)
    assert b.ledger_delta() == 1


# ---------------------------------------------------------------- summary

def test_render_summary():
    items = [
        _item("i1", BatchDisposition.ACCEPT_NEW, "r1"),
        _item("i2", BatchDisposition.REJECT, rejection=("verify_not_found", "unrelated")),
    ]
    b = _batch(items)
    s = render_batch_summary(b)
    assert s.batch_id == "b1"
    assert s.accepted_new == 1 and s.rejected == 1 and s.ledger_delta == 1
    assert len(s.items) == 2
    rej = [it for it in s.items if it.disposition == "reject"][0]
    assert rej.rejection_reasons == ("verify_not_found", "unrelated")
