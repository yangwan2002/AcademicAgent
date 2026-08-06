"""A2c-5 / Task 4：单 envelope 持久化 + 原子提交 + RelevanceStore 合并（故障注入）。"""

from __future__ import annotations

import json

import pytest

from paper_agent.literature.migration import record_to_dict
from paper_agent.literature.models import ReferenceRecord
from paper_agent.literature.quota import QuotaKind, QuotaScope, QuotaSpec
from paper_agent.literature.relevance import RelevanceAssessment, RelevanceStore
from paper_agent.literature.retrieval.batch import (
    BatchDisposition,
    BatchItem,
    BatchStatus,
    CandidateSnapshot,
    ProposedRecordSnapshot,
    RetrievalBatch,
    batch_from_dict,
    batch_to_dict,
)
from paper_agent.literature.retrieval.transaction import commit_batch
from paper_agent.literature.verification import VerificationPolicy
from paper_agent.session.facts import SessionFacts
from paper_agent.workspace.models import RetrievalCandidate


# ---------------------------------------------------------------- RelevanceStore 合并

def _asmt(status="SUCCESS", verdict="relevant", ts="2026-01-01T00:00:00Z"):
    return RelevanceAssessment("r1", "tf", "h", "j", status=status, verdict=verdict,
                               basis="title_and_abstract", assessed_at=ts)


def test_relevance_merge_success_over_error():
    store = RelevanceStore()
    store.put(_asmt(status="ERROR", verdict=None))
    store.merge_put(_asmt(status="SUCCESS", verdict="relevant"))
    got = store.get("r1", "tf", "h", "j")
    assert got.status == "SUCCESS"


def test_relevance_merge_newer_wins():
    store = RelevanceStore()
    store.put(_asmt(ts="2026-01-01T00:00:00Z", verdict="unrelated"))
    store.merge_put(_asmt(ts="2026-06-01T00:00:00Z", verdict="relevant"))
    assert store.get("r1", "tf", "h", "j").verdict == "relevant"


def test_relevance_merge_keeps_different_key():
    store = RelevanceStore()
    store.put(_asmt())
    other = RelevanceAssessment("r2", "tf", "h", "j", status="SUCCESS", verdict="relevant")
    store.merge_put(other)
    assert store.get("r1", "tf", "h", "j") is not None
    assert store.get("r2", "tf", "h", "j") is not None


# ---------------------------------------------------------------- facts envelope v3 往返 + 旧 v2 无损

def test_envelope_roundtrip_v3(tmp_path):
    p = str(tmp_path / "facts.json")
    f = SessionFacts(path=p)
    f.ledger.add_record(ReferenceRecord(reference_id="r1", title="T",
                                        work_identifiers=(("doi", "10.1/x"),), numeric_aliases=("1",)))
    f.relevance_store.put(_asmt())
    f.pending_batches = [{"batch_id": "b1"}]
    f.save()
    data = json.loads(open(p, encoding="utf-8").read())
    # A2d-4 起 envelope 升到 v4（新增 pending_literature_flows；旧版本无损加载）
    assert data["schema_version"] == 4
    g = SessionFacts.load(p)
    assert len(g.ledger) == 1
    assert g.relevance_store.get("r1", "tf", "h", "j") is not None
    assert g.pending_batches == [{"batch_id": "b1"}]


def test_old_v2_loads_with_empty_segments(tmp_path):
    p = str(tmp_path / "facts.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 2, "references": [], "known_numbers": [], "claims": {}}, fh)
    g = SessionFacts.load(p)
    assert g.relevance_store.to_json() == []
    assert g.pending_batches == []


# ---------------------------------------------------------------- batch 序列化往返

def test_batch_roundtrip():
    snap = CandidateSnapshot.from_candidate(
        RetrievalCandidate(id="x", title="T", authors=["A"], year=2020, source_id="s", source="crossref"))
    item = BatchItem("i1", snap, BatchDisposition.ACCEPT_NEW,
                     proposed_record_snapshot=ProposedRecordSnapshot("r1", None, "{}"),
                     target_reference_id="r1", rejection_reasons=())
    b = RetrievalBatch(batch_id="b1", task_fingerprint="tf",
                       quota=QuotaSpec(QuotaKind.RETRIEVAL, QuotaScope.NEW_ONLY),
                       verification_policy=VerificationPolicy.STRICT_FINAL_V1, items=(item,))
    b.stage()
    d = batch_to_dict(b)
    b2 = batch_from_dict(d)
    assert b2.batch_id == "b1" and b2.status is BatchStatus.STAGED
    assert b2.items[0].disposition is BatchDisposition.ACCEPT_NEW
    assert b2.items[0].proposed_record_snapshot.target_reference_id == "r1"


# ---------------------------------------------------------------- 单 envelope 原子提交

def _confirmed_batch_with_new(rid="r_new"):
    rec = ReferenceRecord(reference_id=rid, title="New",
                          work_identifiers=(("doi", "10.1/new"),), numeric_aliases=("1",))
    payload = json.dumps(record_to_dict(rec), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snap = CandidateSnapshot.from_candidate(
        RetrievalCandidate(id="x", title="New", authors=[], year=2020, source_id="10.1/new", source="crossref"))
    item = BatchItem("i1", snap, BatchDisposition.ACCEPT_NEW,
                     proposed_record_snapshot=ProposedRecordSnapshot(rid, None, payload),
                     target_reference_id=rid)
    b = RetrievalBatch(batch_id="b1", task_fingerprint="tf",
                       quota=QuotaSpec(QuotaKind.RETRIEVAL, QuotaScope.NEW_ONLY),
                       verification_policy=VerificationPolicy.STRICT_FINAL_V1, items=(item,))
    b.stage(); b.confirm()
    return b


def test_commit_batch_atomic_success(tmp_path):
    p = str(tmp_path / "facts.json")
    f = SessionFacts(path=p)
    out = commit_batch(f, _confirmed_batch_with_new())
    assert out.ok and out.ledger_delta == 1
    # 内存与磁盘一致
    assert f.ledger.get("r_new") is not None
    g = SessionFacts.load(p)
    assert g.ledger.get("r_new") is not None
    assert "b1" in g.committed_batch_ids


def test_commit_batch_idempotent(tmp_path):
    p = str(tmp_path / "facts.json")
    f = SessionFacts(path=p)
    b = _confirmed_batch_with_new()
    commit_batch(f, b)
    b2 = _confirmed_batch_with_new()   # 同 batch_id
    out2 = commit_batch(f, b2)
    assert out2.already_committed and out2.ledger_delta == 0
    assert len(f.ledger) == 1          # 不重复增加


def test_commit_batch_fault_injection_leaves_state_unchanged(tmp_path, monkeypatch):
    p = str(tmp_path / "facts.json")
    f = SessionFacts(path=p)
    f.save()                            # 先落一份空 envelope
    before = open(p, encoding="utf-8").read()

    # 注入：atomic_write_json 抛错（模拟落盘故障）。commit_batch 每次从 migration 取该名，
    # 故 patch 源头模块即可生效。
    import paper_agent.literature.migration as mig
    monkeypatch.setattr(mig, "atomic_write_json", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        commit_batch(f, _confirmed_batch_with_new())
    # 内存 Ledger 未变、磁盘未变
    assert len(f.ledger) == 0
    assert open(p, encoding="utf-8").read() == before


def test_commit_non_confirmed_rejected(tmp_path):
    p = str(tmp_path / "facts.json")
    f = SessionFacts(path=p)
    _b = _confirmed_batch_with_new()
    b_running = RetrievalBatch(batch_id="b2", task_fingerprint="tf",
                               quota=QuotaSpec(QuotaKind.RETRIEVAL, QuotaScope.NEW_ONLY),
                               verification_policy=VerificationPolicy.STRICT_FINAL_V1)
    out = commit_batch(f, b_running)    # RUNNING → 拒绝
    assert not out.ok
    assert any("not_confirmed" in c for c in out.conflicts)
