"""A2b wave 1：配额领域模型与枚举（Task 1）。

只覆盖数据模型/枚举:构造、`QuotaFinalizeResult.executable` 在各 severity 下的取值、
frozen 不可变、kind/scope 助手。`finalize_quota` / 评估器 / summary 属 Task 2/3。
"""

from __future__ import annotations

import dataclasses

import pytest

from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
    EligibilityFilter,
    FilterField,
    FilterOperator,
    GapDirection,
    QuotaFinalizeResult,
    QuotaGap,
    QuotaIssue,
    QuotaIssueSeverity,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
    RawCountClause,
    RawFilterClause,
    RawQuotaSpec,
    default_scope_for,
    is_allowed_kind_scope,
)


# ---------------------------------------------------------------- 枚举口径

def test_filter_field_has_no_topic():
    """topic 归 A2a relevance，不作 filter field（D1）。"""
    values = {f.value for f in FilterField}
    assert values == {"year", "reference_type", "language"}
    assert not any(f.value == "topic" for f in FilterField)


def test_count_dimension_only_zh_en():
    values = {d.value for d in CountDimension}
    assert values == {"total", "language.zh", "language.en"}


# ---------------------------------------------------------------- kind/scope

def test_allowed_kind_scope_combos():
    assert is_allowed_kind_scope(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED)
    assert is_allowed_kind_scope(QuotaKind.RETRIEVAL, QuotaScope.NEW_ONLY)
    assert is_allowed_kind_scope(QuotaKind.RETRIEVAL, QuotaScope.LEDGER_TOTAL)


def test_disallowed_kind_scope_combos():
    # citation 只允许 actually_cited
    assert not is_allowed_kind_scope(QuotaKind.CITATION, QuotaScope.NEW_ONLY)
    assert not is_allowed_kind_scope(QuotaKind.CITATION, QuotaScope.LEDGER_TOTAL)
    assert not is_allowed_kind_scope(QuotaKind.RETRIEVAL, QuotaScope.ACTUALLY_CITED)


def test_default_scope_for_kind():
    assert default_scope_for(QuotaKind.CITATION) is QuotaScope.ACTUALLY_CITED
    assert default_scope_for(QuotaKind.RETRIEVAL) is QuotaScope.NEW_ONLY


# ---------------------------------------------------------------- 模型构造

def test_eligibility_filter_year_range():
    f = EligibilityFilter(FilterField.YEAR, FilterOperator.RANGE, (2022, 2026), "最近五年")
    assert f.value == (2022, 2026)
    assert f.verbatim == "最近五年"


def test_count_constraint_min():
    c = CountConstraint(CountDimension.LANGUAGE_ZH, CountOperator.MIN, 5, "中文至少5篇")
    assert c.dimension is CountDimension.LANGUAGE_ZH
    assert c.operator is CountOperator.MIN
    assert c.value == 5


def test_quota_spec_defaults():
    spec = QuotaSpec(kind=QuotaKind.CITATION, scope=QuotaScope.ACTUALLY_CITED)
    assert spec.eligibility_filters == ()
    assert spec.count_constraints == ()
    assert spec.base_year is None


def test_quota_gap_fields():
    gap = QuotaGap(
        dimension=CountDimension.TOTAL, operator=CountOperator.MIN,
        required=20, achieved=18, direction=GapDirection.BELOW, amount=2,
    )
    assert gap.direction is GapDirection.BELOW
    assert gap.amount == 2


# ---------------------------------------------------------------- executable

def _issue(sev: QuotaIssueSeverity) -> QuotaIssue:
    return QuotaIssue(code="x", severity=sev, detail="d")


def test_executable_true_when_spec_and_no_blocking():
    spec = QuotaSpec(kind=QuotaKind.CITATION, scope=QuotaScope.ACTUALLY_CITED)
    res = QuotaFinalizeResult(spec=spec, issues=(_issue(QuotaIssueSeverity.WARNING),))
    assert res.executable is True


def test_executable_false_without_spec():
    res = QuotaFinalizeResult(spec=None, issues=())
    assert res.executable is False


@pytest.mark.parametrize(
    "sev", [QuotaIssueSeverity.NEEDS_CLARIFICATION, QuotaIssueSeverity.ERROR]
)
def test_executable_false_when_blocking_issue(sev):
    spec = QuotaSpec(kind=QuotaKind.CITATION, scope=QuotaScope.ACTUALLY_CITED)
    res = QuotaFinalizeResult(spec=spec, issues=(_issue(sev),))
    assert res.executable is False


# ---------------------------------------------------------------- frozen

def test_models_are_frozen():
    f = EligibilityFilter(FilterField.YEAR, FilterOperator.GTE, 2020)
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.value = 2021  # type: ignore[misc]

    spec = QuotaSpec(kind=QuotaKind.CITATION, scope=QuotaScope.ACTUALLY_CITED)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.base_year = 2026  # type: ignore[misc]


# ---------------------------------------------------------------- Raw 模型

def test_raw_models_carry_clauses():
    raw = RawQuotaSpec(
        filters=(RawFilterClause(field="year", operator="range", value="最近五年", verbatim="最近五年"),),
        constraints=(
            RawCountClause(dimension="total", operator="min", value=20, verbatim="引用20篇"),
            RawCountClause(dimension="language.zh", operator="min", value=5, verbatim="中文至少5篇"),
        ),
        notes=("其余说明",),
    )
    assert len(raw.constraints) == 2
    assert raw.filters[0].field == "year"
    assert raw.explicit_scope == ""


# ==========================================================================
# Task 2：finalize_quota（分流 / 年份固化 / 区间求交 / 冲突检测）
# ==========================================================================

from paper_agent.literature.quota import finalize_quota


def _raw(filters=(), constraints=(), explicit_scope="") -> RawQuotaSpec:
    return RawQuotaSpec(filters=tuple(filters), constraints=tuple(constraints),
                        explicit_scope=explicit_scope)


def _codes(res) -> set[str]:
    return {i.code for i in res.issues}


def _con(res, dim) -> CountConstraint | None:
    for c in res.spec.count_constraints:
        if c.dimension is dim:
            return c
    return None


def _filt(res, field) -> EligibilityFilter | None:
    for f in res.spec.eligibility_filters:
        if f.field is field:
            return f
    return None


# ---- 综合：引言引用20篇，中文至少5，最近五年期刊 ----

def test_finalize_full_request():
    res = finalize_quota(
        _raw(
            filters=[
                RawFilterClause("year", "recent_years", 5, "最近五年"),
                RawFilterClause("reference_type", "in", ["journal_article"], "期刊论文"),
            ],
            constraints=[
                RawCountClause("total", "bare", 20, "引用20篇"),
                RawCountClause("language.zh", "min", 5, "中文至少5篇"),
            ],
        ),
        kind=QuotaKind.CITATION, base_year=2026,
    )
    assert res.executable
    yf = _filt(res, FilterField.YEAR)
    assert yf.operator is FilterOperator.RANGE and yf.value == (2022, 2026)
    assert res.spec.base_year == 2026
    rt = _filt(res, FilterField.REFERENCE_TYPE)
    assert rt.value == ("journal_article",)
    # 裸数量默认 min（多引不拦）
    total = _con(res, CountDimension.TOTAL)
    assert total.operator is CountOperator.MIN and total.value == 20
    zh = _con(res, CountDimension.LANGUAGE_ZH)
    assert zh.operator is CountOperator.MIN and zh.value == 5
    assert res.spec.kind is QuotaKind.CITATION
    assert res.spec.scope is QuotaScope.ACTUALLY_CITED


# ---- 裸数量 vs 显式 exact ----

def test_bare_count_defaults_to_min():
    res = finalize_quota(_raw(constraints=[RawCountClause("total", "bare", 20, "引用20篇")]),
                         kind=QuotaKind.CITATION, base_year=2026)
    total = _con(res, CountDimension.TOTAL)
    assert total.operator is CountOperator.MIN and total.value == 20


def test_explicit_exact_stays_exact():
    res = finalize_quota(_raw(constraints=[RawCountClause("total", "exact", 20, "恰好20篇")]),
                         kind=QuotaKind.CITATION, base_year=2026)
    total = _con(res, CountDimension.TOTAL)
    assert total.operator is CountOperator.EXACT and total.value == 20


def test_range_and_max_operators():
    res = finalize_quota(_raw(constraints=[
        RawCountClause("total", "range", [20, 30], "20到30篇"),
    ]), kind=QuotaKind.CITATION, base_year=2026)
    total = _con(res, CountDimension.TOTAL)
    assert total.operator is CountOperator.RANGE and total.value == (20, 30)

    res2 = finalize_quota(_raw(constraints=[RawCountClause("total", "max", 10, "最多10篇")]),
                          kind=QuotaKind.CITATION, base_year=2026)
    assert _con(res2, CountDimension.TOTAL).operator is CountOperator.MAX


# ---- 同维度区间求交 ----

def test_same_dimension_intersection_ok():
    res = finalize_quota(_raw(constraints=[
        RawCountClause("total", "exact", 5, "恰好5"),
        RawCountClause("total", "min", 4, "至少4"),
    ]), kind=QuotaKind.CITATION, base_year=2026)
    assert res.executable
    total = _con(res, CountDimension.TOTAL)
    assert total.operator is CountOperator.EXACT and total.value == 5


def test_same_dimension_intersection_conflict():
    res = finalize_quota(_raw(constraints=[
        RawCountClause("total", "exact", 5, "恰好5"),
        RawCountClause("total", "min", 6, "至少6"),
    ]), kind=QuotaKind.CITATION, base_year=2026)
    assert not res.executable
    assert "constraint_conflict" in _codes(res)


def test_range_low_gt_high_conflict():
    res = finalize_quota(_raw(constraints=[RawCountClause("total", "range", [30, 20], "30到20")]),
                         kind=QuotaKind.CITATION, base_year=2026)
    assert "constraint_conflict" in _codes(res)


# ---- 跨维度矛盾（Req 6.6 两条子规则）----

def test_cross_dimension_conflict_total_max():
    res = finalize_quota(_raw(constraints=[
        RawCountClause("total", "max", 10, "最多10"),
        RawCountClause("language.zh", "min", 6, "中文至少6"),
        RawCountClause("language.en", "min", 6, "英文至少6"),
    ]), kind=QuotaKind.CITATION, base_year=2026)
    assert not res.executable
    assert "constraint_conflict" in _codes(res)


def test_cross_dimension_total_min_no_conflict():
    # total 只有下界 → 分项之和超过它不算冲突（Req 6.6 末句）
    res = finalize_quota(_raw(constraints=[
        RawCountClause("total", "min", 10, "至少10"),
        RawCountClause("language.zh", "min", 8, "中文至少8"),
        RawCountClause("language.en", "min", 8, "英文至少8"),
    ]), kind=QuotaKind.CITATION, base_year=2026)
    assert res.executable
    assert "constraint_conflict" not in _codes(res)


# ---- 相对年份固化确定性 ----

def test_relative_year_deterministic():
    r = _raw(filters=[RawFilterClause("year", "recent_years", 5, "最近五年")])
    a = finalize_quota(r, kind=QuotaKind.CITATION, base_year=2026)
    b = finalize_quota(r, kind=QuotaKind.CITATION, base_year=2026)
    assert _filt(a, FilterField.YEAR).value == _filt(b, FilterField.YEAR).value == (2022, 2026)


def test_relative_year_tracks_base_year():
    r = _raw(filters=[RawFilterClause("year", "recent_years", 3, "最近三年")])
    res = finalize_quota(r, kind=QuotaKind.CITATION, base_year=2020)
    assert _filt(res, FilterField.YEAR).value == (2018, 2020)
    assert res.spec.base_year == 2020


# ---- 近似 / 不支持维度 / 未知字段 ----

def test_approx_needs_clarification():
    res = finalize_quota(_raw(constraints=[RawCountClause("total", "approx", 20, "约20篇")]),
                         kind=QuotaKind.CITATION, base_year=2026)
    assert not res.executable
    assert "ambiguous_approximation" in _codes(res)
    assert _con(res, CountDimension.TOTAL) is None  # 不生成任意范围


def test_unsupported_language_dimension():
    res = finalize_quota(_raw(constraints=[RawCountClause("language.ja", "min", 5, "日文5篇")]),
                         kind=QuotaKind.CITATION, base_year=2026)
    assert not res.executable
    assert "unsupported_dimension" in _codes(res)
    assert res.spec.count_constraints == ()


def test_unknown_field_not_silently_dropped():
    res = finalize_quota(_raw(filters=[RawFilterClause("venue", "eq", "Nature", "发在Nature")]),
                         kind=QuotaKind.CITATION, base_year=2026)
    assert "unknown_field" in _codes(res)


def test_invalid_number():
    res = finalize_quota(_raw(constraints=[RawCountClause("total", "min", "很多", "很多篇")]),
                         kind=QuotaKind.CITATION, base_year=2026)
    assert "invalid_number" in _codes(res)


# ---- 语言 filter ----

def test_language_filter_eq_and_in():
    res = finalize_quota(_raw(filters=[RawFilterClause("language", "in", ["zh", "en"], "只要中英文")]),
                         kind=QuotaKind.CITATION, base_year=2026)
    lf = _filt(res, FilterField.LANGUAGE)
    assert lf.operator is FilterOperator.IN and set(lf.value) == {"zh", "en"}

    res2 = finalize_quota(_raw(filters=[RawFilterClause("language", "eq", "zh", "只要中文")]),
                          kind=QuotaKind.CITATION, base_year=2026)
    lf2 = _filt(res2, FilterField.LANGUAGE)
    assert lf2.operator is FilterOperator.EQ and lf2.value == "zh"


def test_language_filter_rejects_non_zh_en():
    res = finalize_quota(_raw(filters=[RawFilterClause("language", "eq", "ja", "只要日文")]),
                         kind=QuotaKind.CITATION, base_year=2026)
    assert "unsupported_dimension" in _codes(res)


# ---- filter/constraint 联合冲突（D7）----

def test_only_zh_but_en_min_conflict():
    res = finalize_quota(_raw(
        filters=[RawFilterClause("language", "eq", "zh", "只要中文")],
        constraints=[RawCountClause("language.en", "min", 1, "英文至少1")],
    ), kind=QuotaKind.CITATION, base_year=2026)
    assert not res.executable
    assert "filter_constraint_conflict" in _codes(res)


def test_only_zh_total_min_gt_zh_max_conflict():
    res = finalize_quota(_raw(
        filters=[RawFilterClause("language", "eq", "zh", "只要中文")],
        constraints=[
            RawCountClause("total", "min", 20, "至少20"),
            RawCountClause("language.zh", "max", 10, "中文最多10"),
        ],
    ), kind=QuotaKind.CITATION, base_year=2026)
    assert not res.executable
    assert "filter_constraint_conflict" in _codes(res)


# ---- kind/scope ----

def test_invalid_kind_scope():
    res = finalize_quota(_raw(explicit_scope="new_only"),
                         kind=QuotaKind.CITATION, base_year=2026)
    assert "invalid_kind_scope" in _codes(res)


def test_retrieval_default_scope():
    res = finalize_quota(_raw(constraints=[RawCountClause("total", "min", 10, "至少10")]),
                         kind=QuotaKind.RETRIEVAL, base_year=2026)
    assert res.spec.scope is QuotaScope.NEW_ONLY
    assert res.executable


# ---- LLM 误放纠正：把 count 放进 filters 列表仍按语义归位 ----

def test_misplaced_clause_rerouted_by_token():
    res = finalize_quota(RawQuotaSpec(
        filters=(RawFilterClause("total", "min", 10, "至少10"),),  # 误放
        constraints=(RawCountClause("year", "recent_years", 5, "最近五年"),),  # 误放
    ), kind=QuotaKind.CITATION, base_year=2026)
    assert _con(res, CountDimension.TOTAL) is not None
    assert _filt(res, FilterField.YEAR) is not None


# ==========================================================================
# Task 3：评估器（filter/count/gap）+ summary DTO
# ==========================================================================

from paper_agent.literature.quota import (
    DimensionCounts,
    build_quota_summary,
    count_by_dimension,
    evaluate_constraints,
    filter_matches,
)


@dataclasses.dataclass
class _Rec:
    """最小 ReferenceRecord 替身：语言判定读 title/abstract/language_hint。"""
    reference_id: str = "r1"
    title: str = ""
    abstract: str = ""
    language_hint: str = ""
    year: int | None = None
    reference_type: str | None = None


def _zh(rid, **kw):
    return _Rec(reference_id=rid, title="深度学习在医学诊断中的应用研究", language_hint="zh", **kw)


def _en(rid, **kw):
    return _Rec(reference_id=rid, title="Deep Learning for Medical Diagnosis", language_hint="en", **kw)


# ---- filter_matches ----

def test_filter_year_range_and_missing():
    f = EligibilityFilter(FilterField.YEAR, FilterOperator.RANGE, (2022, 2026))
    assert filter_matches(_en("r1", year=2024), f)
    assert not filter_matches(_en("r2", year=2019), f)
    assert not filter_matches(_en("r3", year=None), f)  # 缺字段保守不通过


def test_filter_reference_type():
    f = EligibilityFilter(FilterField.REFERENCE_TYPE, FilterOperator.IN, ("journal_article",))
    assert filter_matches(_en("r1", reference_type="journal_article"), f)
    assert not filter_matches(_en("r2", reference_type="preprint"), f)
    assert not filter_matches(_en("r3", reference_type=None), f)


def test_filter_language_applies_policy():
    f = EligibilityFilter(FilterField.LANGUAGE, FilterOperator.EQ, "zh")
    # 中文标题 + 源 zh → zh/high → 通过
    assert filter_matches(_zh("r1"), f, policy="high_only")
    # 英文文献不满足 zh 过滤
    assert not filter_matches(_en("r2"), f, policy="high_only")


def test_filter_language_high_only_admits_chinese_without_source():
    """中文标题即便源字段缺失，high_only 下也必须通过语言 filter。

    改前判 zh/medium 被 high_only 挡掉，导致带 `zh>=N` 的检索恒定失败
    （真机 OpenAlex 的 language 字段对中文文献只有约 19% 标对）。
    """
    rec = _Rec(reference_id="r1", title="深度学习在医学诊断中的应用研究", language_hint="")
    f = EligibilityFilter(FilterField.LANGUAGE, FilterOperator.EQ, "zh")
    assert filter_matches(rec, f, policy="high_only")
    assert filter_matches(rec, f, policy="include_low_with_warning")


def test_filter_language_high_only_still_excludes_source_only_zh():
    """放宽只针对**汉字硬证据**，不得顺带放宽「只有源字段说 zh」的低置信判定。

    标题无可判书写系统（元数据残缺）+ 源说 zh → title_ambiguous/low，
    high_only 下仍应挡住；否则这次改动就从「信字符」滑成了「信一切」，
    而源字段恰恰是被实测证明不可靠的那一路信号。
    """
    rec = _Rec(reference_id="r2", title="2024 -- 12345", language_hint="zh")
    f = EligibilityFilter(FilterField.LANGUAGE, FilterOperator.EQ, "zh")
    assert not filter_matches(rec, f, policy="high_only")
    assert filter_matches(rec, f, policy="include_low_with_warning")


# ---- count_by_dimension ----

def test_count_dedup_then_filter():
    spec = QuotaSpec(
        kind=QuotaKind.CITATION, scope=QuotaScope.ACTUALLY_CITED,
        eligibility_filters=(EligibilityFilter(FilterField.YEAR, FilterOperator.GTE, 2020),),
    )
    recs = [
        _en("r1", year=2024), _en("r1", year=2024),  # 重复 id → 去重
        _zh("r2", year=2023),
        _en("r3", year=2010),  # 被 year filter 排除
    ]
    counts = count_by_dimension(recs, spec, policy="high_only")
    assert counts.total == 2  # r1, r2
    assert counts.language_zh == 1
    assert counts.language_en == 1


def test_count_other_unknown_into_total_not_language():
    spec = QuotaSpec(kind=QuotaKind.CITATION, scope=QuotaScope.ACTUALLY_CITED)
    # 拉丁标题 + 源缺失 → unknown：计入 total，不计 zh/en
    unk = _Rec(reference_id="r1", title="Deep Learning for X", language_hint="")
    counts = count_by_dimension([unk], spec, policy="high_only")
    assert counts.total == 1
    assert counts.language_zh == 0 and counts.language_en == 0


# ---- evaluate_constraints ----

def test_evaluate_min_below_and_satisfied():
    cons = (CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 20),)
    gaps = evaluate_constraints({CountDimension.TOTAL: 18}, cons)
    assert len(gaps) == 1 and gaps[0].direction is GapDirection.BELOW and gaps[0].amount == 2
    # 多引不产 gap
    assert evaluate_constraints({CountDimension.TOTAL: 22}, cons) == ()


def test_evaluate_exact_above_and_below():
    cons = (CountConstraint(CountDimension.TOTAL, CountOperator.EXACT, 20),)
    below = evaluate_constraints({CountDimension.TOTAL: 18}, cons)
    above = evaluate_constraints({CountDimension.TOTAL: 22}, cons)
    assert below[0].direction is GapDirection.BELOW and below[0].amount == 2
    assert above[0].direction is GapDirection.ABOVE and above[0].amount == 2


def test_evaluate_accepts_dimension_counts():
    cons = (CountConstraint(CountDimension.LANGUAGE_ZH, CountOperator.MIN, 5),)
    counts = DimensionCounts(total=10, language_zh=3, language_en=7)
    gaps = evaluate_constraints(counts, cons)
    assert gaps[0].dimension is CountDimension.LANGUAGE_ZH and gaps[0].amount == 2


# ---- build_quota_summary ----

def test_summary_partitions_filters_constraints():
    res = finalize_quota(
        _raw(
            filters=[RawFilterClause("year", "recent_years", 5, "最近五年")],
            constraints=[
                RawCountClause("total", "bare", 20, "引用20篇"),
                RawCountClause("language.zh", "min", 5, "中文至少5篇"),
            ],
        ),
        kind=QuotaKind.CITATION, base_year=2026,
    )
    summary = build_quota_summary(res)
    assert summary.kind is QuotaKind.CITATION
    assert summary.scope is QuotaScope.ACTUALLY_CITED
    assert summary.base_year == 2026
    assert any("年份" in s for s in summary.filters)
    assert any("总数" in s for s in summary.constraints)
    assert any("中文" in s for s in summary.constraints)
    # 相对年份区间与「至少」口径出现在 assumptions
    assert any("2022" in a and "2026" in a for a in summary.assumptions)
    assert any("至少" in a for a in summary.assumptions)


def test_summary_when_no_spec():
    res = finalize_quota(_raw(constraints=[RawCountClause("total", "approx", 20, "约20篇")]),
                         kind=QuotaKind.CITATION, base_year=2026)
    # spec 仍构造（近似只是缺该 constraint），但 issues 有澄清项
    summary = build_quota_summary(res)
    assert any("ambiguous_approximation" in s for s in summary.issues)
