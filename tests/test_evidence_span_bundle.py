"""A2d-2：EvidenceSpan 全字段契约 + quota-aware EvidenceBundle。"""

from __future__ import annotations

from paper_agent.literature.evidence import (
    BundleStatus,
    EvidenceCapability,
    _sha as _sha_full,
    _truncate_abstract_end,
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
    # budget=100 连最短档（400）都装不下 → 降级救不了，仍判 INSUFFICIENT
    long_abs = "x" * 2000
    a = _verified("ra", "Deep Learning A", long_abs, numeric="1")
    records = [a]
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1)]
    res = build_bundle(records, criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store(records), effective_constraints=cons,
                       budget_chars=100, classifier=_classifier, judge_key="j")
    assert res.status is BundleStatus.CONTEXT_BUDGET_INSUFFICIENT
    assert res.bundle is None and res.bundle_gaps
    # 缺口必须**量化**：只说"装不下"，调用方无法判断该提预算还是该减篇数
    assert res.required_chars > 0
    assert res.budget_chars == 100
    assert res.shortfall_chars() == res.required_chars - 100
    assert res.min_abstract_limit == 400


# ---------------- 分级降级（改法 C）----------------

def _long_records(n, *, abs_chars=1500):
    """n 条长摘要英文记录。句子长 ~30 字符，保证有句边界可回退。"""
    out = []
    sent = "This study examines medical imaging deeply. "
    for i in range(n):
        body = (sent * (abs_chars // len(sent) + 2))[:abs_chars]
        out.append(_verified(f"r{i}", f"Deep Learning {i}", body, numeric=str(i + 1)))
    return out


def test_bundle_truncates_abstract_instead_of_dropping_entries():
    """全摘要装不下配额下限时，先截摘要保篇数，而不是直接判 INSUFFICIENT。

    真机动机（total=20）：25 条需 28698 字符 > 预算 24000，旧实现直接失败，
    检索白跑。少给篇数会违背用户明确要求的引用数量，代价高于截短摘要。
    """
    records = _long_records(10)                     # 每条 ~1540 字符
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 10)]
    # 10 条全摘要需 ~15400，装不下；截到 1000 档需 ~10400，装得下
    res = build_bundle(records, criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store(records), effective_constraints=cons,
                       budget_chars=12000, classifier=_classifier, judge_key="j")
    assert res.status is BundleStatus.OK
    assert len(res.bundle.entries) == 10           # 配额篇数保住了
    assert res.bundle.degradation == "truncated_abstract"
    assert res.bundle.abstract_limit == 1000
    assert res.bundle.truncated_entry_count() == 10
    assert sum(e.char_cost() for e in res.bundle.entries) <= 12000


def test_default_budget_avoids_truncation_at_real_corpus_scale():
    """**默认预算**必须撑得住真机规模的全摘要，不触发截断。

    P-3a 实测：截断让正文的可核验性能数字从 12 个掉到 0 个（引用率三档都 ~100%，
    区分不出好坏，别用它判质量）。24000 时恒定截断，故默认值改为 40000。
    这条钉的是「默认配置下不降级」这个结论本身 —— 有人调小默认值、或摘要变长
    导致悄悄退回截断，都会让它变红。
    """
    from paper_agent.config import Config

    budget = Config().literature_bundle_budget_chars
    # 真机语料规模：25 条、平均 ~1500 字符/条（2026-08 dogfood 账本实测）
    records = _long_records(25)
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 20)]
    res = build_bundle(records, criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store(records), effective_constraints=cons,
                       budget_chars=budget, classifier=_classifier, judge_key="j")
    assert res.status is BundleStatus.OK
    assert res.bundle.degradation == "full", (
        f"默认预算 {budget} 在 25 条真机规模下退回了截断"
        f"（{res.bundle.degradation}/{res.bundle.abstract_limit}）")
    assert res.bundle.truncated_entry_count() == 0
    assert len(res.bundle.entries) >= 20          # 配额篇数同时保住


def test_bundle_no_degradation_when_budget_fits():
    """预算够时不得降级——降级只在必要时发生，否则白损信息。"""
    records = _long_records(3)
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 3)]
    res = build_bundle(records, criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store(records), effective_constraints=cons,
                       budget_chars=0, classifier=_classifier, judge_key="j")
    assert res.bundle.degradation == "full"
    assert res.bundle.abstract_limit == 0
    assert res.bundle.truncated_entry_count() == 0
    assert all(e.abstract_truncated_from == 0 for e in res.bundle.entries)


def test_bundle_picks_shallowest_ladder_tier_that_fits():
    """阶梯取**第一个装得下**的档，不过度截断。"""
    records = _long_records(10)
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 10)]
    # ~7500 只够 700 档（10*740=7400），1000 档需 ~10400
    res = build_bundle(records, criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store(records), effective_constraints=cons,
                       budget_chars=7800, classifier=_classifier, judge_key="j")
    assert res.status is BundleStatus.OK
    assert res.bundle.abstract_limit == 700


def test_truncated_span_preserves_verbatim_and_source_hash():
    """截断必须保住溯源：verbatim_text 仍是 normalized[start:end)，
    content_hash 仍是**完整**字段哈希（Req 12.5/12.6）。否则引用无法回溯原文。"""
    records = _long_records(10)
    cons = [CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 10)]
    res = build_bundle(records, criteria=_CRIT, quota=_quota(cons),
                       relevance_store=_store(records), effective_constraints=cons,
                       budget_chars=12000, classifier=_classifier, judge_key="j")
    entry = res.bundle.entries[0]
    rec = next(r for r in records if r.reference_id == entry.reference_id)
    span = next(s for s in entry.spans if s.source_field == "abstract")
    full = normalize_source(rec.abstract)
    assert span.verbatim_text == full[span.start_offset:span.end_offset]
    assert span.end_offset < len(full)              # 确实截了
    assert span.content_hash == _sha_full(full)     # 哈希仍指向完整原文
    assert not is_stale(span, rec.abstract)         # 故不算 stale


def test_truncation_lands_on_sentence_boundary():
    """截断落在句边界，不截半句/半词——半句证据会误导写作。"""
    text = "First sentence here. " * 100
    end = _truncate_abstract_end(text, 400)
    assert normalize_source(text)[:end].endswith(".")
    assert end <= 400


def test_truncation_falls_back_to_word_boundary():
    """超长句（窗口内无句末）退到词边界，仍不截半词。真机 1/25 会走到这里。"""
    text = "word " * 400                            # 无任何句末标点
    end = _truncate_abstract_end(text, 400)
    sliced = normalize_source(text)[:end]
    assert not sliced.endswith("wor")               # 没截在半词
    assert sliced.endswith("word") or sliced.endswith(" ")


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
