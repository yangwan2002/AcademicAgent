"""A2d-1：QuotaMaterializer + Plan 字段 + 条件式澄清 + feature flag（机制，未激活）。"""

from __future__ import annotations

from paper_agent.config import Config, load_config
from paper_agent.intents.plans import AddSectionPlan, RewritePlan
from paper_agent.literature.quota import (
    QuotaKind,
    QuotaScope,
    RawCountClause,
    RawFilterClause,
    RawQuotaSpec,
)
from paper_agent.tasking.contracts import FieldStatus, SlotValue
from paper_agent.tasking.clarifier import _is_gap
from paper_agent.tasking.intent_spec import (
    CITATION_QUOTA_SLOT_SPEC,
    BindingKind,
    ExecutionBinding,
    SlotSpec,
    spec_for,
)
from paper_agent.intents.intent import Intent
from paper_agent.tasking.quota_materializer import materialize_citation_quota


def _raw(filters=(), constraints=()):
    return RawQuotaSpec(filters=tuple(filters), constraints=tuple(constraints))


# ---------------------------------------------------------------- materialize

def test_materialize_full_citation():
    raw = _raw(
        filters=[RawFilterClause("year", "recent_years", 5, "最近五年"),
                 RawFilterClause("reference_type", "in", ["journal_article"], "期刊")],
        constraints=[RawCountClause("total", "bare", 20, "引用20篇"),
                     RawCountClause("language.zh", "min", 5, "中文至少5")],
    )
    res = materialize_citation_quota(raw, base_year=2026)
    assert res.executable and res.spec is not None
    assert res.spec.kind is QuotaKind.CITATION           # 代码注入
    assert res.spec.scope is QuotaScope.ACTUALLY_CITED
    assert res.spec.base_year == 2026
    # 裸数量按 min
    total = [c for c in res.spec.count_constraints if c.dimension.value == "total"][0]
    assert total.operator.value == "min" and total.value == 20


def test_materialize_none_raw_no_clarify():
    res = materialize_citation_quota(None, base_year=2026)
    assert res.spec is None and res.needs_clarification is False
    res2 = materialize_citation_quota(_raw(), base_year=2026)
    assert res2.spec is None and res2.needs_clarification is False


def test_materialize_approx_needs_clarification():
    raw = _raw(constraints=[RawCountClause("total", "approx", 20, "约20篇")])
    res = materialize_citation_quota(raw, base_year=2026)
    assert res.needs_clarification is True
    assert res.spec is None                              # 不可执行不外传


def test_materialize_conflict_not_executable():
    raw = _raw(constraints=[
        RawCountClause("total", "exact", 5, "恰好5"),
        RawCountClause("total", "min", 6, "至少6"),
    ])
    res = materialize_citation_quota(raw, base_year=2026)
    assert not res.executable and res.spec is None


# ---------------------------------------------------------------- Plan 字段

def test_plan_citation_quota_default_none():
    assert RewritePlan(section_id="s", direction="d").citation_quota is None
    assert AddSectionPlan(title="t", purpose="p").citation_quota is None


# ---------------------------------------------------------------- 条件式澄清

def _slot(status):
    return SlotValue(value="约20篇" if status is FieldStatus.INVALID else None, field_status=status)


def test_conditional_clarify_optional_invalid_is_gap():
    # citation_quota 槽（clarify_if_user_invalid=True）：用户给了但 INVALID → 追问
    assert CITATION_QUOTA_SLOT_SPEC.clarify_if_user_invalid is True
    assert _is_gap(CITATION_QUOTA_SLOT_SPEC, _slot(FieldStatus.INVALID)) is True
    assert _is_gap(CITATION_QUOTA_SLOT_SPEC, _slot(FieldStatus.AMBIGUOUS)) is True
    # USER_ABSENT 的可选槽仍不追问
    assert _is_gap(CITATION_QUOTA_SLOT_SPEC, _slot(FieldStatus.USER_ABSENT)) is False
    assert _is_gap(CITATION_QUOTA_SLOT_SPEC, None) is False


def test_plain_optional_slot_invalid_not_gap():
    # 未标记的普通可选槽：INVALID 也不追问（既有行为不变）
    plain = SlotSpec("x", required=False,
                     execution_binding=ExecutionBinding(BindingKind.PLAN_FIELD, "X.x"))
    assert _is_gap(plain, _slot(FieldStatus.INVALID)) is False


def test_required_slot_gap_unchanged():
    req = SlotSpec("x", required=True, must_ask=True,
                   execution_binding=ExecutionBinding(BindingKind.PLAN_FIELD, "X.x"))
    assert _is_gap(req, None) is True
    assert _is_gap(req, _slot(FieldStatus.USER_ABSENT)) is True
    assert _is_gap(req, SlotValue(value="v", field_status=FieldStatus.PRESENT)) is False


# ---------------------------------------------------------------- flag + 未激活

def test_writing_integration_flag_default_off():
    assert Config().literature_writing_integration is False


def test_writing_integration_flag_env(monkeypatch):
    monkeypatch.setenv("PAPER_LIT_WRITING_INTEGRATION", "true")
    assert load_config().literature_writing_integration is True


def test_citation_quota_slot_active_in_writing_specs():
    # 按 A2d-5 预期变更更新：A2d-1 时槽未激活，A2d-5 与 Handler 消费同批挂入。
    for intent in (Intent.REWRITE, Intent.ADD_SECTION):
        assert "citation_quota" in spec_for(intent).slots
