"""A2d-2：EvidenceSpan 全字段契约 + quota-aware EvidenceBundle。"""

from __future__ import annotations

from paper_agent.literature.evidence import (
    BundleStatus,
    EvidenceCapability,
    build_bundle,
    capability_of,
    is_stale,
    make_span,
    normalize_source,
)
from paper_agent.literature.models import ReferenceRecord
from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
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
from paper_agent.literature.language import DEFAULT_THRESHOLDS, classify_record_language
from paper_agent.literature.verification import EventKind, make_event


# ---------------------------------------------------------------- EvidenceSpan

def test_span_verbatim_equals_slice():
    text = "We study <b>deep learning</b> for 医学诊断 tasks."
    span = make_span("r1", "abstract", text)
    normalized = normalize_source(text)
    assert span.verbatim_text == normalized[span.start_offset:span.end_offset]
    assert span.end_offset == len(normalized)


def test_span_slice_offsets():
    text = "abcdefghij"
    span = make_span("r1", "abstract", text, start=2, end=5)
    assert span.verbatim_text == "cde"
    assert span.verbatim_text == normalize_source(text)[2:5]


def test_span_capability_mapping():
    assert capability_of("title") is EvidenceCapability.DISCOVERY_ONLY
    assert capability_of("abstract") is EvidenceCapability.CLAIM_SUPPORT
    assert capability_of("abstract_section:methods") is EvidenceCapability.CLAIM_SUPPORT
    assert make_span("r", "title", "T").capability is EvidenceCapability.DISCOVERY_ONLY


def test_span_hash_recomputable_and_stale():
    text = "Original abstract text here."
    span = make_span("r1", "abstract", text)
    assert not is_stale(span, text)
    assert is_stale(span, "Changed abstract text.")


def test_span_id_stable_and_offset_sensitive():
    text = "abcdefghij"
    a = make_span("r1", "abstract", text, start=0, end=5)
    b = make_span("r1", "abstract", text, start=0, end=5)
    c = make_span("r1", "abstract", text, start=1, end=5)
    assert a.span_id == b.span_id
    assert a.span_id != c.span_id


def test_nfc_equivalent_same_hash():
    # NFD 与 NFC 等价输入 → 同一 normalized → 同 content_hash
    nfc = "é abstract"
    nfd = "e\u0301 abstract"
    assert make_span("r", "abstract", nfc).content_hash == make_span("r", "abstract", nfd).content_hash


# ---------------------------------------------------------------- Bundle

def _verified(rid, title, abstract, *, year=2022, lang_hint="en", numeric="1"):
    ev = make_event(EventKind.VERIFIED, source="crossref", identifier_type="doi",
                    identifier=f"10.1/{rid}", authority="final", checked_at="2026-01-01T00:00:00Z")
    return ReferenceRecord(
        reference_id=rid, title=title, abstract=abstract, year=year,
        language_hint=lang_hint, work_identifiers=(("doi", f"10.1/{rid}"),),
        numeric_aliases=(numeric,), bibkey=f"key{rid}", verification_events=(ev,),
    )


_CRIT = RelevanceCriteria(topic="deep learning medical")


def _store(records, *, judge_key="j"):
    s = RelevanceStore()
    for r in records:
        s.put(RelevanceAssessment(
            r.reference_id, _CRIT.fingerprint(),
            relevance_content_hash(r.title, r.abstract), judge_key,
            status="SUCCESS", verdict="relevant", confidence="high",
            basis="title_and_abstract",
        ))
    return s


def _classifier(rec):
    return classify_record_language(rec, DEFAULT_THRESHOLDS)


def _quota(constraints):
    return QuotaSpec(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED,
                     count_constraints=tuple(constraints))


def test_bundle_language_lower_bound_priority():
    # zh min 1：即使中文文献排序靠后，也必须入选
    en1 = _verified("en1", "Deep Learning A", "Study A on medical imaging.", lang_hint="en", numeric="1")
    en2 = _verified("en2", "Deep Learning B", "Study B on medical imaging.", lang_hint="en", numeric="2")
    zh1 = _verified("zh1", "深度学习医学影像研究", "本文研究深度学习在医学影像中的应用。",
                    lang_hint="zh", numeric="3")
    records = [en1, en2, zh1]
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 2),
            CountConstraint(CountDimension.LANGUAGE_ZH, CountOperator.MIN, 1)]
    res = build_bundle(records, criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store(records), effective_constraints=cons,
                       budget_chars=0, classifier=_classifier, judge_key="j")
    assert res.status is BundleStatus.OK
    assert "zh1" in res.bundle.reference_ids()


def test_bundle_excludes_unverified_and_unrelated():
    ok = _verified("r1", "Deep Learning", "Abstract text about medical.", numeric="1")
    unverified = ReferenceRecord(reference_id="r2", title="No verify", abstract="x",
                                 numeric_aliases=("2",))
    records = [ok, unverified]
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1)]
    res = build_bundle(records, criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store([ok]), effective_constraints=cons,
                       budget_chars=0, classifier=_classifier, judge_key="j")
    assert res.bundle.reference_ids() == ("r1",)
    assert res.excluded_count == 1


def test_bundle_preexisting_cited_fixed():
    a = _verified("ra", "Deep Learning A", "Abstract A about medical imaging.", numeric="1")
    b = _verified("rb", "Deep Learning B", "Abstract B about medical imaging.", numeric="2")
    records = [a, b]
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1)]
    res = build_bundle(records, criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store(records), effective_constraints=cons,
                       budget_chars=0, classifier=_classifier, judge_key="j",
                       preexisting_cited_ids=("rb",))
    assert res.bundle.reference_ids()[0] == "rb"       # 原有引用被优先固定


def test_char_cost_does_not_double_count_title():
    """标题只算一次：它已是一个 span,不得再叠加 len(title)（X-10）。

    此前 `char_cost` 是 `sum(spans) + len(title) + 40`,而 `_entry_of` 已为 title
    建了 span ⇒ 每条虚高约一个标题的长度（真机 10 条虚高约 800 字符）。
    """
    title = "T" * 100
    abstract = "A" * 500
    rec = _verified("rc", title, abstract, numeric="1")
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1)]
    res = build_bundle([rec], criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store([rec]), effective_constraints=cons,
                       budget_chars=0, classifier=_classifier, judge_key="j")
    entry = res.bundle.entries[0]
    spans_total = sum(len(s.verbatim_text) for s in entry.spans)
    assert spans_total == len(title) + len(abstract)     # title + abstract 各一个 span
    assert entry.char_cost() == spans_total + 40


def test_bundle_budget_insufficient():
    long_abs = "x" * 2000
    a = _verified("ra", "Deep Learning A", long_abs, numeric="1")
    records = [a]
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1)]
    res = build_bundle(records, criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store(records), effective_constraints=cons,
                       budget_chars=100, classifier=_classifier, judge_key="j")
    assert res.status is BundleStatus.CONTEXT_BUDGET_INSUFFICIENT
    assert res.bundle is None and res.bundle_gaps


def test_bundle_id_content_sensitive():
    a = _verified("ra", "A", "Abstract A about medical.", numeric="1")
    b = _verified("rb", "B", "Abstract B about medical.", numeric="2")
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1)]
    r1 = build_bundle([a], criteria=_CRIT, quota=_quota(cons), relevance_store=_store([a]),
                      effective_constraints=cons, budget_chars=0, classifier=_classifier, judge_key="j")
    r2 = build_bundle([a, b], criteria=_CRIT, quota=_quota(cons), relevance_store=_store([a, b]),
                      effective_constraints=cons, budget_chars=0, classifier=_classifier, judge_key="j")
    assert r1.bundle.bundle_id != r2.bundle.bundle_id


def test_bundle_entry_evidence_available_flag():
    no_abs = _verified("rn", "Title Only Paper", "", numeric="1")
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1)]
    res = build_bundle([no_abs], criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store([no_abs]), effective_constraints=cons,
                       budget_chars=0, classifier=_classifier, judge_key="j")
    assert res.bundle.entries[0].evidence_available is False
