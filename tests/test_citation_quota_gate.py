"""A2d-3：公开 token parser + CitationQuotaGate（双评估 + 新增引用边界）。"""

from __future__ import annotations

from paper_agent.gates.citation_quota_gate import CitationQuotaGate
from paper_agent.gates.diff_gate import _cite_tokens, extract_citation_tokens
from paper_agent.generate.context_builder import build_evidence_block
from paper_agent.literature.evidence import build_bundle
from paper_agent.literature.language import DEFAULT_THRESHOLDS, classify_record_language
from paper_agent.literature.ledger import ReferenceLedger
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
from paper_agent.literature.verification import EventKind, make_event

_CRIT = RelevanceCriteria(topic="deep learning medical")


# ---------------------------------------------------------------- 公开 parser

def test_extract_tokens_tex_and_md():
    assert extract_citation_tokens(r"see \cite{a,b} and \cite{c}", "tex") == ("a", "b", "c")
    assert extract_citation_tokens("see [1] and [2] and [1]", "md") == ("1", "2")


def test_cite_tokens_thin_wrapper_unchanged():
    text = r"\cite{a,b}"
    assert _cite_tokens(text, "tex") == {"a", "b"}
    assert set(extract_citation_tokens(text, "tex")) == _cite_tokens(text, "tex")


# ---------------------------------------------------------------- fixtures

def _rec(rid, title, abstract, *, lang="en", numeric="1", bibkey=None):
    ev = make_event(EventKind.VERIFIED, source="crossref", identifier_type="doi",
                    identifier=f"10.1/{rid}", authority="final", checked_at="2026-01-01T00:00:00Z")
    return ReferenceRecord(
        reference_id=rid, title=title, abstract=abstract, year=2022, language_hint=lang,
        work_identifiers=(("doi", f"10.1/{rid}"),), numeric_aliases=(numeric,),
        bibkey=bibkey or f"key{rid}", verification_events=(ev,),
    )


def _classifier(rec):
    return classify_record_language(rec, DEFAULT_THRESHOLDS)


def _store(records, judge_key="j"):
    s = RelevanceStore()
    for r in records:
        s.put(RelevanceAssessment(r.reference_id, _CRIT.fingerprint(),
                                  relevance_content_hash(r.title, r.abstract), judge_key,
                                  status="SUCCESS", verdict="relevant", confidence="high",
                                  basis="title_and_abstract"))
    return s


def _setup(n=2, total_min=2):
    records = [
        _rec(f"r{i}", f"Deep Learning Paper {i}", f"Abstract {i} on medical imaging.",
             numeric=str(i + 1), bibkey=f"key{i}")
        for i in range(n)
    ]
    ledger = ReferenceLedger(records)
    cons = (CountConstraint(CountDimension.TOTAL, CountOperator.MIN, total_min),)
    quota = QuotaSpec(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED, count_constraints=cons)
    res = build_bundle(records, criteria=_CRIT, quota=quota, relevance_store=_store(records),
                       effective_constraints=cons, budget_chars=0,
                       classifier=_classifier, judge_key="j")
    return ledger, quota, res.bundle, cons


# ---------------------------------------------------------------- Gate

def test_gate_passes_when_quota_met():
    ledger, quota, bundle, cons = _setup(2, 2)
    candidate = r"text \cite{key0} more \cite{key1}"
    r = CitationQuotaGate().check(candidate, ext="tex", bundle=bundle, ledger=ledger,
                                 quota=quota, effective_constraints=cons,
                                 classifier=_classifier)
    assert r.passed and r.effective_gaps == ()
    assert set(r.used_reference_ids) == {"r0", "r1"} and r.unused_reference_ids == ()


def test_gate_fails_when_under_quota():
    ledger, quota, bundle, cons = _setup(2, 2)
    r = CitationQuotaGate().check(r"only \cite{key0}", ext="tex", bundle=bundle,
                                 ledger=ledger, quota=quota, effective_constraints=cons,
                                 classifier=_classifier)
    assert not r.passed and r.effective_gaps
    assert r.unused_reference_ids == ("r1",)


def test_gate_dedups_by_reference_id():
    ledger, quota, bundle, cons = _setup(2, 2)
    # 同一文献引两次 → 只算 1 篇 → 未达 2
    r = CitationQuotaGate().check(r"\cite{key0} and \cite{key0}", ext="tex", bundle=bundle,
                                 ledger=ledger, quota=quota, effective_constraints=cons,
                                 classifier=_classifier)
    assert len(r.used_reference_ids) == 1 and not r.passed


def test_gate_new_unresolved_hard_fail():
    ledger, quota, bundle, cons = _setup(2, 2)
    r = CitationQuotaGate().check(r"\cite{key0} \cite{key1} \cite{ghost}", ext="tex",
                                 bundle=bundle, ledger=ledger, quota=quota,
                                 effective_constraints=cons, classifier=_classifier)
    assert not r.passed                                # 配额虽满足，非法引用硬失败
    assert r.new_unresolved_tokens == ("ghost",)


def test_gate_out_of_bundle_hard_fail():
    ledger, quota, bundle, cons = _setup(2, 2)
    # Ledger 里再加一条不在 Bundle 的记录
    extra = _rec("rx", "Other Paper", "Other abstract.", numeric="99", bibkey="keyx")
    ledger.add_record(extra)
    r = CitationQuotaGate().check(r"\cite{key0} \cite{key1} \cite{keyx}", ext="tex",
                                 bundle=bundle, ledger=ledger, quota=quota,
                                 effective_constraints=cons, classifier=_classifier)
    assert not r.passed and r.new_out_of_bundle_ids == ("rx",)


def test_rewrite_preserves_original_citations():
    ledger, quota, bundle, cons = _setup(2, 2)
    extra = _rec("rx", "Legacy Paper", "Legacy abstract.", numeric="99", bibkey="keyx")
    ledger.add_record(extra)
    original = r"legacy text \cite{keyx}"
    candidate = r"rewritten \cite{keyx} plus \cite{key0} \cite{key1}"
    r = CitationQuotaGate().check(candidate, ext="tex", bundle=bundle, ledger=ledger,
                                 quota=quota, effective_constraints=cons,
                                 original_text=original, classifier=_classifier)
    # keyx 是原文保留 → 不算新增违规、不推进配额
    assert r.new_out_of_bundle_ids == ()
    assert "keyx" in r.preserved_legacy_tokens
    assert set(r.used_reference_ids) == {"r0", "r1"}
    assert r.passed


def test_add_section_all_citations_must_be_in_bundle():
    ledger, quota, bundle, cons = _setup(2, 2)
    extra = _rec("rx", "Other", "Other abstract.", numeric="99", bibkey="keyx")
    ledger.add_record(extra)
    # original_text="" → 全部引用都是新增
    r = CitationQuotaGate().check(r"\cite{key0} \cite{key1} \cite{keyx}", ext="tex",
                                 bundle=bundle, ledger=ledger, quota=quota,
                                 effective_constraints=cons, original_text="",
                                 classifier=_classifier)
    assert not r.passed and r.new_out_of_bundle_ids == ("rx",)


def test_effective_vs_original_dual_evaluation():
    """用户要 20 但只可用 2 并接受 → effective 下界 2：引 2 篇 passed 且 original_gaps 非空；
    引 1 篇仍 failed（不是维度级无条件跳过）。"""
    ledger, _q, bundle, _c = _setup(2, 2)
    quota_20 = QuotaSpec(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED,
                         count_constraints=(CountConstraint(CountDimension.TOTAL,
                                                            CountOperator.MIN, 20),))
    effective_2 = (CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 2),)

    ok = CitationQuotaGate().check(r"\cite{key0} \cite{key1}", ext="tex", bundle=bundle,
                                   ledger=ledger, quota=quota_20,
                                   effective_constraints=effective_2, classifier=_classifier)
    assert ok.passed                                  # 达 effective 下界
    assert ok.original_gaps                           # 原始 20 的缺口仍如实报告

    bad = CitationQuotaGate().check(r"\cite{key0}", ext="tex", bundle=bundle,
                                    ledger=ledger, quota=quota_20,
                                    effective_constraints=effective_2, classifier=_classifier)
    assert not bad.passed                             # 低于 effective 下界仍失败


def test_used_unused_partition():
    ledger, quota, bundle, cons = _setup(3, 2)
    r = CitationQuotaGate().check(r"\cite{key0} \cite{key1}", ext="tex", bundle=bundle,
                                 ledger=ledger, quota=quota, effective_constraints=cons,
                                 classifier=_classifier)
    used, unused = set(r.used_reference_ids), set(r.unused_reference_ids)
    assert used.isdisjoint(unused)
    assert used | unused == set(bundle.reference_ids())


# ---------------------------------------------------------------- ContextBuilder

def test_evidence_block_wraps_as_data():
    _l, _q, bundle, _c = _setup(1, 1)
    block = build_evidence_block(bundle)
    assert "不得" in block or "指令" in block          # 声明指令不可执行
    assert "摘要依据" in block


def test_evidence_block_marks_no_abstract():
    records = [_rec("rn", "Title Only", "", numeric="1", bibkey="keyn")]
    cons = (CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1),)
    quota = QuotaSpec(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED, count_constraints=cons)
    res = build_bundle(records, criteria=_CRIT, quota=quota, relevance_store=_store(records),
                       effective_constraints=cons, budget_chars=0,
                       classifier=_classifier, judge_key="j")
    block = build_evidence_block(res.bundle)
    assert "无摘要可用" in block


def test_evidence_block_none_is_empty():
    assert build_evidence_block(None) == ""
