"""A2c-2 wave 1 / Task 1：不可变快照 / disposition / BatchItem 一致性。"""

from __future__ import annotations

import dataclasses

from paper_agent.literature.retrieval.batch import (
    BatchDisposition,
    BatchItem,
    CandidateSnapshot,
    ProposedRecordSnapshot,
    validate_item,
)
from paper_agent.workspace.models import RetrievalCandidate


def _cand(**kw):
    base = dict(id="x", title="Attention Is All You Need", authors=["A", "B"],
                year=2017, source_id="10.1/x", source="crossref",
                work_identifiers={"doi": "10.1/x"})
    base.update(kw)
    return RetrievalCandidate(**base)


# ---------------------------------------------------------------- 深冻结

def test_snapshot_deep_frozen_against_source_mutation():
    cand = _cand()
    snap = CandidateSnapshot.from_candidate(cand)
    # 事后修改原候选的可变 dict/list
    cand.work_identifiers["doi"] = "10.9/tampered"
    cand.authors.append("C")
    # 快照不受影响
    assert dict(snap.work_identifiers) == {"doi": "10.1/x"}
    assert snap.authors == ("A", "B")


def test_snapshot_frozen_instance():
    snap = CandidateSnapshot.from_candidate(_cand())
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        snap.title = "x"  # type: ignore[misc]


def test_content_hash_stable_and_sensitive():
    a = CandidateSnapshot.from_candidate(_cand())
    b = CandidateSnapshot.from_candidate(_cand())
    assert a.content_hash == b.content_hash          # 同输入同哈希
    c = CandidateSnapshot.from_candidate(_cand(title="Different Title"))
    assert c.content_hash != a.content_hash          # 标题变→哈希变


# ---------------------------------------------------------------- validate_item

def _snap():
    return CandidateSnapshot.from_candidate(_cand())


def _proposed(expected_base_hash=None):
    return ProposedRecordSnapshot(
        target_reference_id="r1", expected_base_hash=expected_base_hash,
        canonical_record_payload="{}",
    )


def test_accept_new_valid():
    item = BatchItem("i1", _snap(), BatchDisposition.ACCEPT_NEW,
                     proposed_record_snapshot=_proposed(), target_reference_id="r1")
    assert validate_item(item) == ()


def test_accept_new_missing_proposed():
    item = BatchItem("i1", _snap(), BatchDisposition.ACCEPT_NEW, target_reference_id="r1")
    probs = validate_item(item)
    assert any("missing_proposed_record_snapshot" in p for p in probs)


def test_reuse_must_not_have_proposed():
    item = BatchItem("i1", _snap(), BatchDisposition.REUSE_EXISTING,
                     proposed_record_snapshot=_proposed(), target_reference_id="r1")
    probs = validate_item(item)
    assert any("unexpected_proposed_record_snapshot" in p for p in probs)


def test_reject_may_have_no_target():
    item = BatchItem("i1", _snap(), BatchDisposition.REJECT,
                     rejection_reasons=("relevance_unrelated", "verify_not_found"))
    assert validate_item(item) == ()
    assert len(item.rejection_reasons) == 2          # 多个原因不压成一条


def test_merge_needs_expected_base_hash():
    bad = BatchItem("i1", _snap(), BatchDisposition.MERGE_DUPLICATE,
                    proposed_record_snapshot=_proposed(expected_base_hash=None),
                    target_reference_id="r1")
    assert any("missing_expected_base_hash" in p for p in validate_item(bad))
    good = BatchItem("i2", _snap(), BatchDisposition.MERGE_DUPLICATE,
                     proposed_record_snapshot=_proposed(expected_base_hash="abc"),
                     target_reference_id="r1")
    assert validate_item(good) == ()


def test_accept_new_rejects_expected_base_hash():
    item = BatchItem("i1", _snap(), BatchDisposition.ACCEPT_NEW,
                     proposed_record_snapshot=_proposed(expected_base_hash="abc"),
                     target_reference_id="r1")
    assert any("unexpected_expected_base_hash" in p for p in validate_item(item))


# ---------------------------------------------------------------- ProposedRecordSnapshot 哈希

def test_proposed_payload_and_hash_stable():
    from paper_agent.literature.models import ReferenceRecord

    rec = ReferenceRecord(reference_id="r1", title="T", work_identifiers=(("doi", "10.1/x"),))
    h1 = ProposedRecordSnapshot.hash_of(rec)
    h2 = ProposedRecordSnapshot.hash_of(rec)
    assert h1 == h2
    rec2 = rec.with_changes(title="Changed")
    assert ProposedRecordSnapshot.hash_of(rec2) != h1
