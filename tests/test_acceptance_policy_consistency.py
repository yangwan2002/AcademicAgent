"""相关性「接受语义」的单一真相源（F-3 / 真机 E2E 后发现的 P0）。

**背景**：「哪些相关性结论算接受」这条规则曾实现在**三个地方**，其中两个硬编码：

| 位置 | 接受集合 | 可配置 |
|---|---|---|
| `retrieval/attempt.py::RelevanceAcceptancePolicy` | 由 `literature_accept_weakly_relevant` 决定 | 是 |
| `preflight.py::_ACCEPTED_VERDICTS` | `("relevant",)` | **否（硬编码）** |
| `evidence.py::build_bundle` | `("relevant",)` | **否（硬编码）** |

后果：打开 `PAPER_LIT_ACCEPT_WEAKLY_RELEVANT=true` 会得到「**入库了但永远用不上**」——
检索接受 weakly_relevant 并提交进 Ledger，但可用性统计不数它、证据集不选它
→ 配额永远算不满、写作永远看不到这些文献。flag 只改了摄入、没改消费。

这与项目其他地方刻意维持的「单一真相源」纪律矛盾
（对比 `relevance.relevance_content_hash` 的注释：读写两侧必须走同一函数）。

真机数据（2026-08-01 dogfood）：18 个候选里 10 个被判 weakly_relevant，
其中含写引言必引的综述。strict-only 让 20 个候选只产出 1 篇。
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.literature.evidence import BundleStatus, build_bundle
from paper_agent.literature.language import DEFAULT_THRESHOLDS, classify_record_language
from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.models import ReferenceRecord
from paper_agent.literature.preflight import assess_availability, effective_constraints
from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
)
from paper_agent.literature.relevance import (
    STRICT_ACCEPTANCE,
    WEAKLY_ACCEPTANCE,
    RelevanceAcceptancePolicy,
    RelevanceAssessment,
    RelevanceCriteria,
    RelevanceStore,
    acceptance_for_purpose,
    relevance_content_hash,
)
from paper_agent.literature.verification import EventKind, make_event

_CRIT = RelevanceCriteria(topic="cross-view image matching")


def _rec(rid, *, verdict_title="Cross-View Image Matching", numeric="1"):
    events = (make_event(EventKind.VERIFIED, source="crossref", identifier_type="doi",
                         identifier=f"10.1/{rid}", authority="final",
                         checked_at="2026-01-01T00:00:00Z"),)
    return ReferenceRecord(
        reference_id=rid, title=f"{verdict_title} {rid}",
        abstract="We match aerial and ground views.", year=2023, language_hint="en",
        work_identifiers=(("doi", f"10.1/{rid}"),), numeric_aliases=(numeric,),
        bibkey=f"key{rid}", verification_events=events,
    )


def _store(pairs):
    """pairs: [(record, verdict)] → RelevanceStore。"""
    s = RelevanceStore()
    for rec, verdict in pairs:
        s.put(RelevanceAssessment(
            rec.reference_id, _CRIT.fingerprint(),
            relevance_content_hash(rec.title, rec.abstract), "",
            status="SUCCESS", verdict=verdict, confidence="high",
            basis="title_and_abstract"))
    return s


def _classifier(rec):
    return classify_record_language(rec, DEFAULT_THRESHOLDS)


def _quota(total):
    return QuotaSpec(
        QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED,
        count_constraints=(CountConstraint(
            CountDimension.TOTAL, CountOperator.MIN, total),))


# ============ 三处必须一致（原 P0） ============

def test_weakly_accepted_flows_through_all_three_stages():
    """接受 weakly 时：attempt 收 → availability 数 → bundle 选。三处必须一致。

    这是原 bug 的回归：以前 attempt 收了，后两处硬编码只认 relevant，
    导致文献入库却永远算不进配额、也进不了证据集。
    """
    strong, weak = _rec("r1"), _rec("r2", numeric="2")
    store = _store([(strong, "relevant"), (weak, "weakly_relevant")])
    ledger = ReferenceLedger([strong, weak])
    quota = _quota(2)

    # 1) attempt 侧
    asmt_weak = store.get(weak.reference_id, _CRIT.fingerprint(),
                          relevance_content_hash(weak.title, weak.abstract), "")
    assert WEAKLY_ACCEPTANCE.is_accepted(asmt_weak) is True

    # 2) availability 侧
    av = assess_availability(quota, _CRIT, ledger, store, classifier=_classifier,
                             acceptance=WEAKLY_ACCEPTANCE)
    assert set(av.available_ids) == {"r1", "r2"}
    assert av.counts.total == 2

    # 3) bundle 侧
    eff = effective_constraints(quota, (), operation_id="op", task_fingerprint="fp")
    built = build_bundle([strong, weak], criteria=_CRIT, quota=quota,
                         relevance_store=store, effective_constraints=eff,
                         budget_chars=8000, classifier=_classifier,
                         acceptance=WEAKLY_ACCEPTANCE)
    assert built.status is BundleStatus.OK
    assert set(built.bundle.reference_ids()) == {"r1", "r2"}


def test_strict_excludes_weakly_in_all_three_stages():
    """严格策略下三处同样一致地排除 weakly（对称性）。"""
    strong, weak = _rec("r1"), _rec("r2", numeric="2")
    store = _store([(strong, "relevant"), (weak, "weakly_relevant")])
    ledger = ReferenceLedger([strong, weak])
    quota = _quota(2)

    asmt_weak = store.get(weak.reference_id, _CRIT.fingerprint(),
                          relevance_content_hash(weak.title, weak.abstract), "")
    assert STRICT_ACCEPTANCE.is_accepted(asmt_weak) is False

    av = assess_availability(quota, _CRIT, ledger, store, classifier=_classifier,
                             acceptance=STRICT_ACCEPTANCE)
    assert set(av.available_ids) == {"r1"}

    eff = effective_constraints(quota, (), operation_id="op", task_fingerprint="fp")
    built = build_bundle([strong, weak], criteria=_CRIT, quota=quota,
                         relevance_store=store, effective_constraints=eff,
                         budget_chars=8000, classifier=_classifier,
                         acceptance=STRICT_ACCEPTANCE)
    assert set(built.bundle.reference_ids()) == {"r1"}


def test_default_is_strict_byte_for_byte_unchanged():
    """不传 acceptance 时必须与改造前完全一致（严格只收 relevant）。"""
    strong, weak = _rec("r1"), _rec("r2", numeric="2")
    store = _store([(strong, "relevant"), (weak, "weakly_relevant")])
    ledger = ReferenceLedger([strong, weak])
    quota = _quota(2)

    av = assess_availability(quota, _CRIT, ledger, store, classifier=_classifier)
    assert set(av.available_ids) == {"r1"}

    eff = effective_constraints(quota, (), operation_id="op", task_fingerprint="fp")
    built = build_bundle([strong, weak], criteria=_CRIT, quota=quota,
                         relevance_store=store, effective_constraints=eff,
                         budget_chars=8000, classifier=_classifier)
    assert set(built.bundle.reference_ids()) == {"r1"}


def test_error_and_unrelated_never_accepted_under_any_policy():
    """失败不伪装：ERROR / unrelated 在任何策略下都不接受。"""
    rec = _rec("r1")
    fp, ch = _CRIT.fingerprint(), relevance_content_hash(rec.title, rec.abstract)

    err = RelevanceAssessment(rec.reference_id, fp, ch, "", status="ERROR",
                              verdict=None, error_kind="provider_failed")
    unrel = RelevanceAssessment(rec.reference_id, fp, ch, "", status="SUCCESS",
                                verdict="unrelated", confidence="high",
                                basis="title_and_abstract")
    for pol in (STRICT_ACCEPTANCE, WEAKLY_ACCEPTANCE):
        assert pol.is_accepted(err) is False
        assert pol.is_accepted(unrel) is False
        assert pol.is_accepted(None) is False


def test_availability_and_bundle_agree_for_every_policy_and_verdict():
    """属性式：任意 (策略 × 判定) 组合下，availability 与 bundle 的取舍必须一致。

    这两处一旦分叉，就会出现「统计说够了但证据集是空的」或反之。
    """
    eff_quota = _quota(1)
    for pol in (STRICT_ACCEPTANCE, WEAKLY_ACCEPTANCE):
        for verdict in ("relevant", "weakly_relevant", "unrelated"):
            rec = _rec("r1")
            store = _store([(rec, verdict)])
            av = assess_availability(eff_quota, _CRIT, ReferenceLedger([rec]), store,
                                     classifier=_classifier, acceptance=pol)
            eff = effective_constraints(eff_quota, (), operation_id="op",
                                        task_fingerprint="fp")
            built = build_bundle([rec], criteria=_CRIT, quota=eff_quota,
                                 relevance_store=store, effective_constraints=eff,
                                 budget_chars=8000, classifier=_classifier,
                                 acceptance=pol)
            in_av = "r1" in av.available_ids
            in_bundle = built.bundle is not None and "r1" in built.bundle.reference_ids()
            assert in_av == in_bundle, (
                f"分叉：policy={pol.accepted_verdicts} verdict={verdict} "
                f"availability={in_av} bundle={in_bundle}")


# ============ F-3：按章节信号解析 ============
#
# **第一版 F-3 就死在这里**：当时用「精确集合匹配 section_purpose」，
# 而测试喂的是 "introduction_background" —— 一个**实践中从不出现**的合成字符串。
# 真机实测 section_purpose 实际是整个课题名，于是该模式静默失效。
# 下面的测试一律用**真实路径会产生的值**。

#: 真机 2026-08-01 探针实测：确定性与 LLM 两条合成路径都把它填进 section_purpose。
REAL_SECTION_PURPOSE = "面向空地协同的大视角大尺度差图像匹配研究"

#: Handler 真实传入的 section_kind（add_section 的 plan.title / rewrite 的 section_id）。
REAL_SECTION_KINDS_WEAKLY = ("引言", "Introduction", "相关工作", "Related Work", "绪论")
REAL_SECTION_KINDS_STRICT = ("Method", "方法", "Experiments", "实验", "Conclusion", "结论")


def test_real_section_purpose_alone_stays_strict():
    """真实的 section_purpose（=课题名）不含章节信号 → 严格。这是正确行为，不是 bug。"""
    assert acceptance_for_purpose(REAL_SECTION_PURPOSE) == STRICT_ACCEPTANCE


def test_real_section_kind_makes_it_reachable():
    """**回归第一版的失效**：加上可靠的 section_kind 信号后，by_purpose 必须真的生效。"""
    for kind in REAL_SECTION_KINDS_WEAKLY:
        pol = acceptance_for_purpose(kind, REAL_SECTION_PURPOSE)
        assert pol == WEAKLY_ACCEPTANCE, f"{kind} 应收 weakly，实际 {pol.accepted_verdicts}"


def test_non_intro_section_kinds_stay_strict():
    for kind in REAL_SECTION_KINDS_STRICT:
        pol = acceptance_for_purpose(kind, REAL_SECTION_PURPOSE)
        assert pol == STRICT_ACCEPTANCE, f"{kind} 应严格，实际 {pol.accepted_verdicts}"


def test_free_text_purpose_from_user_interview_is_matched():
    """真实写作路径下 purpose 是用户访谈原话（自由文本），关键词要接得住。"""
    for purpose in (
        "介绍空地协同图像匹配的研究背景、已有工作与本文要解决的 gap",
        "综述跨视角匹配的国内外研究现状",
        "discuss prior work and state of the art",
    ):
        assert acceptance_for_purpose("", purpose) == WEAKLY_ACCEPTANCE, purpose


def test_canonical_bucket_names_still_work():
    """显式传规范化桶名时继续可用（供上层想用受控值的场景）。

    注意**不含**裸的 "background" —— 见
    `test_bare_background_words_must_not_loosen_the_gate`。
    """
    for purpose in ("introduction_background", "introduction",
                    "related_work", "discussion"):
        pol = acceptance_for_purpose(purpose)
        assert "weakly_relevant" in pol.accepted_verdicts, purpose


def test_single_source_of_truth_for_acceptance_signals():
    """接受信号只有一处定义 —— 不得再出现第二个「哪些用途收 weakly」的集合。

    历史：曾并存 `PURPOSE_ACCEPTS_WEAKLY`（含裸 background）与 `_WEAKLY_SIGNALS`
    （刻意排除它），二者对同一输入给出不同答案。
    """
    from paper_agent.literature import relevance as mod

    assert not hasattr(mod, "PURPOSE_ACCEPTS_WEAKLY")
    assert isinstance(mod._WEAKLY_SIGNALS, tuple) and mod._WEAKLY_SIGNALS


def test_bare_background_words_must_not_loosen_the_gate():
    """裸的「背景」/「background」在视觉领域是**研究对象**，不得当章节信号。

    误判方向必须是「更严格」，绝不因为课题名里出现这些词就放宽引用门槛。
    """
    for topic in (
        "复杂背景下的红外小目标检测",
        "background subtraction for video surveillance",
        "动态背景建模方法研究",
    ):
        assert acceptance_for_purpose(topic) == STRICT_ACCEPTANCE, topic


def test_method_like_purposes_stay_strict():
    for purpose in ("method_evidence", "results", "experiment", ""):
        assert acceptance_for_purpose(purpose) == STRICT_ACCEPTANCE, purpose


def test_resolution_is_case_and_space_insensitive():
    assert acceptance_for_purpose("  Introduction_Background  ") == WEAKLY_ACCEPTANCE
    assert acceptance_for_purpose("  RELATED WORK ") == WEAKLY_ACCEPTANCE


def test_no_signal_falls_back_to_strict():
    """无信号降级为**更严格**（不静默放宽），与 config 的降级方向一致。"""
    assert acceptance_for_purpose() == STRICT_ACCEPTANCE
    assert acceptance_for_purpose(None) == STRICT_ACCEPTANCE
    assert acceptance_for_purpose("", None, "  ") == STRICT_ACCEPTANCE
    assert acceptance_for_purpose("something_new") == STRICT_ACCEPTANCE


def test_any_signal_hitting_is_enough():
    """多信号中任一命中即收 weakly（可靠信号优先传，但不要求它一定非空）。"""
    assert acceptance_for_purpose(None, "", "Related Work") == WEAKLY_ACCEPTANCE


def test_explicit_override_beats_signals():
    pol = acceptance_for_purpose("Method", REAL_SECTION_PURPOSE, force_weakly=True)
    assert "weakly_relevant" in pol.accepted_verdicts


def test_title_only_still_rejected_by_default_under_weakly():
    """收 weakly 不等于放宽 title_only —— 无摘要的候选仍不接受（保守规则不变）。"""
    rec = _rec("r1")
    a = RelevanceAssessment(
        rec.reference_id, _CRIT.fingerprint(),
        relevance_content_hash(rec.title, rec.abstract), "",
        status="SUCCESS", verdict="weakly_relevant", confidence="medium",
        basis="title_only")
    assert WEAKLY_ACCEPTANCE.is_accepted(a) is False


# ============ 向后兼容：attempt 仍可导出这两个名字 ============

def test_attempt_still_reexports_policy_names():
    """既有代码/测试从 retrieval.attempt 导入这两个名字，必须继续可用。"""
    from paper_agent.literature.retrieval import attempt as att

    assert att.RelevanceAcceptancePolicy is RelevanceAcceptancePolicy
    assert att.STRICT_ACCEPTANCE == STRICT_ACCEPTANCE


# ============ 组合层：模式解析 ============

def _cfg(**kw):
    from paper_agent.config import Config

    return Config(**kw)


def test_resolver_strict_is_default():
    from paper_agent.application.literature_composition import build_acceptance_resolver

    r = build_acceptance_resolver(_cfg())
    assert r("introduction_background") == STRICT_ACCEPTANCE
    assert r("method_evidence") == STRICT_ACCEPTANCE


def test_resolver_by_purpose_splits_by_section():
    from paper_agent.application.literature_composition import build_acceptance_resolver

    r = build_acceptance_resolver(_cfg(literature_acceptance_mode="by_purpose"))
    assert r("introduction_background") == WEAKLY_ACCEPTANCE
    assert r("related_work") == WEAKLY_ACCEPTANCE
    assert r("method_evidence") == STRICT_ACCEPTANCE


def test_resolver_permissive_accepts_weakly_everywhere():
    from paper_agent.application.literature_composition import build_acceptance_resolver

    r = build_acceptance_resolver(_cfg(literature_acceptance_mode="permissive"))
    assert r("method_evidence") == WEAKLY_ACCEPTANCE


def test_legacy_bool_still_means_permissive():
    """旧 flag 语义保持：accept_weakly_relevant=True 等价 permissive（向后兼容）。"""
    from paper_agent.application.literature_composition import build_acceptance_resolver

    r = build_acceptance_resolver(_cfg(literature_accept_weakly_relevant=True))
    assert r("method_evidence") == WEAKLY_ACCEPTANCE
    # 且能覆盖 mode（旧 flag 更强，避免升级时行为倒退）
    r2 = build_acceptance_resolver(_cfg(
        literature_accept_weakly_relevant=True,
        literature_acceptance_mode="strict"))
    assert r2("method_evidence") == WEAKLY_ACCEPTANCE


def test_invalid_mode_downgrades_to_strict_with_warning(monkeypatch):
    """非法模式降级为**更严格**并告警，绝不静默放宽（与 lang_policy 同纪律）。"""
    import warnings

    from paper_agent.config import load_config

    monkeypatch.setenv("PAPER_LIT_ACCEPTANCE_MODE", "totally-bogus")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = load_config()
    assert cfg.literature_acceptance_mode == "strict"
    assert any("strict" in str(w.message) for w in caught)


def test_valid_modes_parse_from_env(monkeypatch):
    from paper_agent.config import load_config

    for mode in ("strict", "by_purpose", "permissive"):
        monkeypatch.setenv("PAPER_LIT_ACCEPTANCE_MODE", mode)
        assert load_config().literature_acceptance_mode == mode


# ============ 编排器：一次解析、三处共用 ============

class _StubDeps:
    """只承载 acceptance_policy 的最小 deps 替身（dataclasses.replace 不适用）。"""

    def __init__(self):
        self.acceptance_policy = STRICT_ACCEPTANCE
        self.active_relevance_runtime = None


def _orch(resolver=None):
    from paper_agent.application.literature_flow import WritingLiteratureOrchestrator

    return WritingLiteratureOrchestrator(
        deps=_StubDeps(), limits=object(), planner=object(), classifier=_classifier,
        acceptance_resolver=resolver)


def _request(**kw):
    from paper_agent.application.literature_flow import WritingEvidenceRequest

    kw.setdefault("criteria", RelevanceCriteria(topic="t"))
    kw.setdefault("citation_quota", _quota(1))
    return WritingEvidenceRequest(**kw)


def test_orchestrator_resolves_from_real_request_shape():
    """**核心回归**：用 Handler 真实构造的请求形状，by_purpose 必须真的生效。

    criteria.section_purpose = 课题名（不可靠），section_kind = 章节标题（可靠）。
    第一版只看前者 → 静默失效；现在必须命中。
    """
    from paper_agent.application.literature_composition import build_acceptance_resolver

    orch = _orch(build_acceptance_resolver(_cfg(
        literature_acceptance_mode="by_purpose")))

    intro = _request(
        criteria=RelevanceCriteria(topic="t", section_purpose=REAL_SECTION_PURPOSE),
        section_kind="Introduction")
    method = _request(
        criteria=RelevanceCriteria(topic="t", section_purpose=REAL_SECTION_PURPOSE),
        section_kind="Method")

    assert orch._resolve_acceptance(intro) == WEAKLY_ACCEPTANCE
    assert orch._resolve_acceptance(method) == STRICT_ACCEPTANCE


def test_orchestrator_without_section_kind_falls_back_to_purpose_text():
    """没有 section_kind 时退化为读 purpose 文本（旧调用方/兜底）。"""
    orch = _orch(lambda *s: acceptance_for_purpose(*s))
    req = _request(criteria=RelevanceCriteria(
        topic="t", section_purpose="综述已有的跨视角匹配研究现状"))
    assert orch._resolve_acceptance(req) == WEAKLY_ACCEPTANCE


def test_orchestrator_reads_search_intent_purpose_as_middle_signal():
    """SearchIntent.section_purpose 作为中间可靠度的信号也要被看到。"""
    class _Intent:
        section_purpose = "Related Work"

    orch = _orch(lambda *s: acceptance_for_purpose(*s))
    req = _request(search_intent=_Intent())
    assert orch._resolve_acceptance(req) == WEAKLY_ACCEPTANCE


def test_orchestrator_strict_mode_ignores_all_signals():
    """strict 模式（默认）下任何章节信号都不放宽 —— flag 纪律。"""
    from paper_agent.application.literature_composition import build_acceptance_resolver

    orch = _orch(build_acceptance_resolver(_cfg()))
    req = _request(section_kind="Introduction")
    assert orch._resolve_acceptance(req) == STRICT_ACCEPTANCE


def test_orchestrator_without_resolver_keeps_assembly_policy():
    """不传 resolver（旧调用方）→ 沿用装配期策略，行为不变。"""
    orch = _orch(None)
    req = _request(section_kind="Introduction")
    assert orch._resolve_acceptance(req) is STRICT_ACCEPTANCE


def test_section_kind_does_not_affect_task_fingerprint():
    """`section_kind` 是策略提示、不是语义判准 → 不得进 fingerprint。

    否则加这个字段会让**已缓存的相关性判定全部失效**（白烧一遍 reviewer 调用）。
    """
    crit = RelevanceCriteria(topic="t", section_purpose=REAL_SECTION_PURPOSE)
    a = _request(criteria=crit, section_kind="Introduction")
    b = _request(criteria=crit, section_kind="Method")
    c = _request(criteria=crit)
    assert a.task_fingerprint == b.task_fingerprint == c.task_fingerprint


def test_orchestrator_injects_resolved_policy_into_deps():
    """解析出的策略必须写回 deps，使 Attempt 与 availability/bundle 同口径。"""
    import dataclasses

    from paper_agent.application.literature_flow import WritingLiteratureOrchestrator
    from paper_agent.literature.retrieval.attempt import AttemptDeps

    deps = AttemptDeps(
        registry=object(), ledger=object(), cross_verifier=object(), judge=object(),
        relevance_store=object(), budget=object(), verify_cache=object())
    assert deps.acceptance_policy == STRICT_ACCEPTANCE

    orch = WritingLiteratureOrchestrator(
        deps=deps, limits=object(), planner=object(), classifier=_classifier,
        acceptance_resolver=lambda p: acceptance_for_purpose(p))

    new_deps = orch._deps_with_acceptance(WEAKLY_ACCEPTANCE)
    assert new_deps is not deps                       # 副本，不 mutate 原 deps
    assert new_deps.acceptance_policy == WEAKLY_ACCEPTANCE
    assert deps.acceptance_policy == STRICT_ACCEPTANCE  # 原 deps 未被改
    assert dataclasses.is_dataclass(new_deps)


def test_deps_with_acceptance_tolerates_non_dataclass_deps():
    """测试替身/非 dataclass deps 不应让编排器崩（观测/兼容优先）。"""
    from paper_agent.application.literature_flow import WritingLiteratureOrchestrator

    orch = WritingLiteratureOrchestrator(
        deps=_StubDeps(), limits=object(), planner=object(), classifier=_classifier)
    assert orch._deps_with_acceptance(WEAKLY_ACCEPTANCE) is not None
