"""A2d-4 / Task 1+2+3：Preflight 领域服务 + typed port + facts flows 持久化。"""

from __future__ import annotations

import json

from paper_agent.application.literature_ports import (
    BatchDecision,
    BundleDecision,
    FinalGateDecision,
    GapDecision,
    LiteratureInteractionPort,
    PreflightCheckpoint,
    checkpoint_from_dict,
    checkpoint_to_dict,
)
from paper_agent.literature.language import DEFAULT_THRESHOLDS, classify_record_language
from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.models import ReferenceRecord
from paper_agent.literature.preflight import (
    AcceptedQuotaDeviation,
    acquisition_gaps,
    assess_availability,
    derive_retrieval_quota,
    effective_constraints,
    quota_hash,
)
from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
    EligibilityFilter,
    FilterField,
    FilterOperator,
    GapDirection,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
)
from paper_agent.literature.relevance import (
    relevance_content_hash,
    RelevanceAssessment,
    RelevanceCriteria,
    RelevanceStore,
)
from paper_agent.literature.verification import EventKind, make_event
from paper_agent.session.facts import SessionFacts

_CRIT = RelevanceCriteria(topic="deep learning medical")


def _rec(rid, *, lang="en", verified=True, year=2022, numeric="1"):
    events = ()
    if verified:
        events = (make_event(EventKind.VERIFIED, source="crossref", identifier_type="doi",
                             identifier=f"10.1/{rid}", authority="final",
                             checked_at="2026-01-01T00:00:00Z"),)
    title = "深度学习医学影像研究" if lang == "zh" else "Deep Learning for Medical Imaging"
    abstract = "本文研究深度学习。" if lang == "zh" else "We study deep learning."
    return ReferenceRecord(
        reference_id=rid, title=title, abstract=abstract, year=year, language_hint=lang,
        work_identifiers=(("doi", f"10.1/{rid}"),), numeric_aliases=(numeric,),
        bibkey=f"key{rid}", verification_events=events,
    )


def _store(records, verdict="relevant", status="SUCCESS", judge_key=""):
    s = RelevanceStore()
    for r in records:
        s.put(RelevanceAssessment(r.reference_id, _CRIT.fingerprint(),
                                  relevance_content_hash(r.title, r.abstract),
                                  judge_key, status=status,
                                  verdict=verdict if status == "SUCCESS" else None,
                                  confidence="high", basis="title_and_abstract"))
    return s


def _classifier(rec):
    return classify_record_language(rec, DEFAULT_THRESHOLDS)


def _quota(constraints, filters=()):
    return QuotaSpec(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED,
                     eligibility_filters=tuple(filters), count_constraints=tuple(constraints))


# ---------------------------------------------------------------- availability

def test_availability_counts_only_usable():
    ok = _rec("r1")
    unverified = _rec("r2", verified=False, numeric="2")
    ledger = ReferenceLedger([ok, unverified])
    q = _quota([CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 2)])
    av = assess_availability(q, _CRIT, ledger, _store([ok, unverified]),
                             classifier=_classifier)
    assert av.available_ids == ("r1",)              # 未 STRICT verified 不计


def test_availability_relevance_error_not_counted():
    ok = _rec("r1")
    ledger = ReferenceLedger([ok])
    q = _quota([CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1)])
    av = assess_availability(q, _CRIT, ledger, _store([ok], status="ERROR"),
                             classifier=_classifier)
    assert av.available_ids == ()                   # ERROR 不伪装、不计可用


def test_availability_filter_excludes():
    old = _rec("r1", year=2010)
    ledger = ReferenceLedger([old])
    q = _quota([CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1)],
               filters=[EligibilityFilter(FilterField.YEAR, FilterOperator.GTE, 2020)])
    av = assess_availability(q, _CRIT, ledger, _store([old]), classifier=_classifier)
    assert av.available_ids == ()


# ---------------------------------------------------------------- gaps

def test_gaps_only_below():
    ledger = ReferenceLedger([_rec("r1")])
    cons = (CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 3),
            CountConstraint(CountDimension.LANGUAGE_EN, CountOperator.MAX, 0))
    q = _quota(cons)
    av = assess_availability(q, _CRIT, ledger, _store([_rec("r1")]), classifier=_classifier)
    gaps = acquisition_gaps(av, cons)
    assert all(g.direction is GapDirection.BELOW for g in gaps)   # ABOVE 不触发检索


# ---------------------------------------------------------------- derive

def test_derive_copies_filters_and_does_not_mutate():
    filters = (EligibilityFilter(FilterField.YEAR, FilterOperator.GTE, 2020),)
    cons = (CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 5),)
    citation = _quota(cons, filters)
    ledger = ReferenceLedger([])
    av = assess_availability(citation, _CRIT, ledger, RelevanceStore(), classifier=_classifier)
    gaps = acquisition_gaps(av, cons)
    derived = derive_retrieval_quota(gaps, citation, redundancy_factor=2.0)
    assert derived.kind is QuotaKind.RETRIEVAL and derived.scope is QuotaScope.NEW_ONLY
    assert derived.eligibility_filters == filters               # 完整复制
    assert citation.count_constraints == cons                   # 原配额未被改
    total = [c for c in derived.count_constraints if c.dimension is CountDimension.TOTAL][0]
    assert total.value == 10                                    # 缺 5 × 冗余 2


def test_derive_dimensions_independent_not_summed():
    cons = (CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 4),
            CountConstraint(CountDimension.LANGUAGE_ZH, CountOperator.MIN, 2))
    citation = _quota(cons)
    av = assess_availability(citation, _CRIT, ReferenceLedger([]), RelevanceStore(),
                             classifier=_classifier)
    derived = derive_retrieval_quota(acquisition_gaps(av, cons), citation, redundancy_factor=1.0)
    vals = {c.dimension: c.value for c in derived.count_constraints}
    assert vals[CountDimension.TOTAL] == 4                      # 不与 zh 相加
    assert vals[CountDimension.LANGUAGE_ZH] == 2


def test_derive_respects_max_target():
    cons = (CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 50),)
    citation = _quota(cons)
    av = assess_availability(citation, _CRIT, ReferenceLedger([]), RelevanceStore(),
                             classifier=_classifier)
    derived = derive_retrieval_quota(acquisition_gaps(av, cons), citation,
                                     redundancy_factor=3.0, max_target=60)
    assert derived.count_constraints[0].value == 60             # 上限生效


# ---------------------------------------------------------------- effective

def _dev(dim, *, orig=20, achieved=18, op_id="op1", fp=None, qhash=""):
    return AcceptedQuotaDeviation(
        stage="preflight", dimension=dim, original_lower_bound=orig,
        achieved_at_decision=achieved, effective_lower_bound=achieved,
        original_gap=orig - achieved, task_fingerprint=fp or _CRIT.fingerprint(),
        operation_id=op_id, quota_hash=qhash,
    )


def test_effective_replaces_lower_bound_only():
    q = _quota([CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 20)])
    dev = _dev(CountDimension.TOTAL, qhash=quota_hash(q))
    eff = effective_constraints(q, (dev,), operation_id="op1",
                                task_fingerprint=_CRIT.fingerprint())
    assert eff[0].operator is CountOperator.MIN and eff[0].value == 18


def test_effective_exact_keeps_upper_bound():
    q = _quota([CountConstraint(CountDimension.TOTAL, CountOperator.EXACT, 20)])
    dev = _dev(CountDimension.TOTAL, qhash=quota_hash(q))
    eff = effective_constraints(q, (dev,), operation_id="op1",
                                task_fingerprint=_CRIT.fingerprint())
    assert eff[0].operator is CountOperator.RANGE and eff[0].value == (18, 20)


def test_effective_not_reused_across_tasks():
    q = _quota([CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 20)])
    dev = _dev(CountDimension.TOTAL, qhash=quota_hash(q), fp="OTHER_FP")
    eff = effective_constraints(q, (dev,), operation_id="op1",
                                task_fingerprint=_CRIT.fingerprint())
    assert eff[0].value == 20                                    # 指纹不符 → 偏差不生效


def test_effective_quota_hash_mismatch_ignored():
    q = _quota([CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 20)])
    dev = _dev(CountDimension.TOTAL, qhash="WRONGHASH")
    eff = effective_constraints(q, (dev,), operation_id="op1",
                                task_fingerprint=_CRIT.fingerprint())
    assert eff[0].value == 20


# ---------------------------------------------------------------- typed port

class _StubPort:
    def confirm_retrieval_batch(self, summary):
        return BatchDecision("accept")

    def resolve_quota_gap(self, report):
        return GapDecision("accept")

    def confirm_evidence_bundle(self, summary):
        return BundleDecision("accept")

    def resolve_final_gate(self, report):
        return FinalGateDecision("accept")

    # A2f：port 协议新增两方法（检索主题澄清 / 检索意图确认）——按本期预期补齐。
    def request_search_topic(self, summary):
        return None

    def confirm_search_intent(self, summary):
        return None


def test_port_protocol_conformance():
    assert isinstance(_StubPort(), LiteratureInteractionPort)


def test_checkpoint_roundtrip_and_privacy():
    q = _quota([CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 5)])
    cp = PreflightCheckpoint(
        flow_id="f1", operation_id="op1", task_fingerprint=_CRIT.fingerprint(),
        quota_hash=quota_hash(q), state="needs_gap_decision",
        initial_query_hash="abc123", gaps=(("total", 3),),
        deviations=(_dev(CountDimension.TOTAL, qhash=quota_hash(q)),),
    )
    d = checkpoint_to_dict(cp)
    blob = json.dumps(d, ensure_ascii=False)
    assert "bundle" not in blob and "abstract" not in blob       # 不含 Bundle/摘要
    back = checkpoint_from_dict(d)
    assert back.flow_id == "f1" and back.gaps == (("total", 3),)
    assert back.deviations[0].effective_lower_bound == 18


# ---------------------------------------------------------------- facts flows

def test_facts_flows_roundtrip_v4(tmp_path):
    p = str(tmp_path / "facts.json")
    f = SessionFacts(path=p)
    f.put_literature_flow({"flow_id": "f1", "state": "assessing"})
    f.save()
    data = json.loads(open(p, encoding="utf-8").read())
    assert data["schema_version"] == 4
    g = SessionFacts.load(p)
    assert g.get_literature_flow("f1") is not None


def test_facts_old_v3_loads_flows_empty(tmp_path):
    p = str(tmp_path / "facts.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 3, "references": [], "known_numbers": [], "claims": {}}, fh)
    g = SessionFacts.load(p)
    assert g.pending_literature_flows == []


def test_facts_corrupt_flow_isolated(tmp_path):
    p = str(tmp_path / "facts.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 4, "references": [], "known_numbers": [], "claims": {},
                   "pending_literature_flows": [{"flow_id": "ok"}, "garbage", {"no_id": 1}]}, fh)
    g = SessionFacts.load(p)
    assert len(g.pending_literature_flows) == 1
    assert g.corrupt_flow_count == 2
    assert len(g.ledger) == 0                                   # Ledger 不受影响


def test_facts_flow_put_get_drop(tmp_path):
    f = SessionFacts(path=str(tmp_path / "facts.json"))
    f.put_literature_flow({"flow_id": "f1", "state": "a"})
    f.put_literature_flow({"flow_id": "f1", "state": "b"})       # 覆盖
    assert len(f.pending_literature_flows) == 1
    assert f.get_literature_flow("f1")["state"] == "b"
    assert f.drop_literature_flow("f1") is True
    assert f.get_literature_flow("f1") is None
