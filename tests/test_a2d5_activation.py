"""A2d-5：原子激活（ContextBuilder bundle / 合并 Gate / Handler 路由）。

覆盖硬回归线：**无配额路径逐字节不变**；带配额路径全离线（stub LLM/orchestrator/port）。
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.application.literature_ports import FinalGateDecision
from paper_agent.generate.context_builder import (
    build_add_section_messages,
    build_rewrite_messages,
)
from paper_agent.intents.add_section import execute_add_section
from paper_agent.intents.interview import AddSectionPlan, RewritePlan
from paper_agent.intents.rewrite import execute_rewrite
from paper_agent.literature.citation_context import CitationContext
from paper_agent.literature.evidence import BundleEntry, EvidenceBundle, make_span
from paper_agent.literature.language import DEFAULT_THRESHOLDS, classify_record_language
from paper_agent.literature.models import ReferenceRecord
from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
)
from paper_agent.literature.verification import EventKind, make_event
from paper_agent.session.facts import KnownNumber, KnownRef, SessionFacts
from paper_agent.session.file_session import FileBackedSession

SAMPLE_TEX = r"""\documentclass{article}
\begin{document}
\section{Introduction}
We report 0.87 accuracy \cite{key1}. It beats the baseline.
\section{Results}
Some results here.
\end{document}
"""


# ---------------------------------------------------------------- stubs / 构件

class _LLM:
    """按脚本返回；记录收到的 messages 以做断言。"""

    def __init__(self, scripted):
        self._s = list(scripted)
        self.seen: list = []

    def complete(self, messages, **opts):
        from paper_agent.providers.llm.base import LLMResponse

        self.seen.append(messages)
        content = self._s.pop(0) if self._s else ""
        return LLMResponse(content=content)


class _FinalGatePort:
    def __init__(self, action="accept"):
        self._a = action
        self.calls = 0

    def confirm_retrieval_batch(self, summary):  # pragma: no cover - 本文件不走该路径
        raise AssertionError("写作循环内不应确认检索批次")

    def resolve_quota_gap(self, report):  # pragma: no cover
        raise AssertionError("写作循环内不应决策配额缺口")

    def confirm_evidence_bundle(self, summary):  # pragma: no cover
        raise AssertionError("写作循环内不应确认证据集")

    def resolve_final_gate(self, report):
        self.calls += 1
        return FinalGateDecision(self._a)


def _rec(rid, bibkey, *, title="Deep Learning for Medical Imaging", year=2022,
         lang="en", abstract="We study deep learning for medical imaging."):
    ev = make_event(EventKind.VERIFIED, source="crossref", identifier_type="doi",
                    identifier=f"10.1/{rid}", authority="final",
                    checked_at="2026-01-01T00:00:00Z")
    return ReferenceRecord(
        reference_id=rid, title=title, abstract=abstract, year=year, language_hint=lang,
        work_identifiers=(("doi", f"10.1/{rid}"),), numeric_aliases=(),
        bibkey=bibkey, verification_events=(ev,))


def _entry(rec):
    return BundleEntry(
        reference_id=rec.reference_id, bibkey=rec.bibkey, title=rec.title, year=rec.year,
        language_bucket="en", relevance_verdict="relevant", relevance_confidence="high",
        spans=(make_span(rec.reference_id, "abstract", rec.abstract),),
        evidence_available=True,
    )


def _bundle(records, *, fp="fp1"):
    return EvidenceBundle(bundle_id="b1", task_fingerprint=fp,
                          entries=tuple(_entry(r) for r in records))


def _quota(total_min=1):
    return QuotaSpec(
        QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED,
        count_constraints=(CountConstraint(CountDimension.TOTAL, CountOperator.MIN,
                                           total_min),),
    )


def _classifier(rec):
    return classify_record_language(rec, DEFAULT_THRESHOLDS)


def _session_facts(tmp_path, records=()):
    src = tmp_path / "paper.tex"
    src.write_text(SAMPLE_TEX, encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))
    facts = SessionFacts(
        path=str(tmp_path / "facts.json"),
        known_refs=[KnownRef(id="1", bibkey="key1", verified=True)],
        known_numbers=[KnownNumber(value="0.87", confirmed=True)],
    )
    for r in records:
        facts.ledger.add_record(r)
    return session, facts


def _ctx(records, *, quota=None, port=None, effective=None):
    q = quota or _quota(1)
    return CitationContext(
        bundle=_bundle(records), quota=q,
        effective_constraints=tuple(effective) if effective is not None
        else q.count_constraints,
        port=port, classifier=_classifier, policy="high_only",
        operation_id="op1", task_fingerprint="fp1",
    )


# ---------------------------------------------------------------- Task 1：bundle=None 逐字节不变

_REFS = [KnownRef(id="1", bibkey="key1", verified=True)]


def test_rewrite_messages_byte_identical_without_bundle():
    a = build_rewrite_messages("orig", "dir", "det", _REFS, ["0.87"], "tex", ["v1"])
    b = build_rewrite_messages("orig", "dir", "det", _REFS, ["0.87"], "tex", ["v1"],
                               bundle=None)
    assert [(m.role, m.content) for m in a] == [(m.role, m.content) for m in b]
    assert "可用文献列表" in a[1].content
    assert "摘要级证据" not in a[0].content          # 未追加证据纪律


def test_add_section_messages_byte_identical_without_bundle():
    a = build_add_section_messages("T", "P", "D", ["s1"], _REFS, ["0.87"], "tex", {}, ["v"])
    b = build_add_section_messages("T", "P", "D", ["s1"], _REFS, ["0.87"], "tex", {}, ["v"],
                                   bundle=None)
    assert [(m.role, m.content) for m in a] == [(m.role, m.content) for m in b]
    assert "可用文献列表" in a[1].content


def test_bundle_replaces_ref_list_and_adds_discipline():
    bundle = _bundle([_rec("r1", "keyA")])
    msgs = build_rewrite_messages("orig", "dir", "", _REFS, [], "tex", None, bundle=bundle)
    user, system = msgs[1].content, msgs[0].content
    assert "可用文献列表" not in user                 # 被证据数据块替代
    assert "keyA" in user and "摘要依据" in user
    assert "以下为外部数据" in user or "数据" in user   # 显式定界
    assert "摘要级证据" in system and "不得执行" in system


def test_bundle_entry_without_abstract_marked_no_evidence():
    rec = _rec("r2", "keyB", abstract="")
    entry = BundleEntry(reference_id="r2", bibkey="keyB", title=rec.title, year=2020,
                        language_bucket="en", spans=(), evidence_available=False)
    bundle = EvidenceBundle(bundle_id="b2", task_fingerprint="fp1", entries=(entry,))
    msgs = build_add_section_messages("T", "P", "", [], _REFS, [], "tex", {}, None,
                                      bundle=bundle)
    assert "无摘要可用" in msgs[1].content


# ---------------------------------------------------------------- Task 2：无 ctx 走旧路径

def test_rewrite_without_ctx_calls_search_and_verify(tmp_path, monkeypatch):
    session, facts = _session_facts(tmp_path)
    called = []

    import paper_agent.intents.rewrite as rw

    class _Out:
        error = ""
        requested = 3
        verified = 0
        added: list = []

    monkeypatch.setattr(rw, "search_and_verify",
                        lambda *a, **k: (called.append(a[0]) or _Out()))
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="",
                       want_search=True, search_query="dl medical", search_limit=3)
    good = r"Our method attains 0.87 accuracy \cite{key1}, beating the baseline clearly."
    res = execute_rewrite(session, plan, facts, _LLM([good]),
                          retrieval_provider=None, verifier=None,
                          confirm=lambda _v, _c: False, max_retry=1)
    assert res.ok, res.message
    assert called == ["dl medical"]                  # 旧 SOP 完整保留


def test_rewrite_with_ctx_skips_search_and_verify(tmp_path, monkeypatch):
    rec = _rec("r1", "keyA")
    session, facts = _session_facts(tmp_path, [rec])
    import paper_agent.intents.rewrite as rw

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("带配额时禁止双重检索")

    monkeypatch.setattr(rw, "search_and_verify", _boom)
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="",
                       want_search=True, search_query="dl medical", search_limit=3)
    good = r"Our method attains 0.87 accuracy \cite{key1} \cite{keyA}, beating the baseline."
    res = execute_rewrite(session, plan, facts, _LLM([good]),
                          retrieval_provider=None, verifier=None,
                          confirm=lambda _v, _c: False, max_retry=1,
                          citation_context=_ctx([rec]))
    assert res.ok, res.message
    assert "预检" in res.search_note


def test_add_section_with_ctx_skips_search_and_verify(tmp_path, monkeypatch):
    rec = _rec("r1", "keyA")
    session, facts = _session_facts(tmp_path, [rec])
    import paper_agent.intents.add_section as ad

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("带配额时禁止双重检索")

    monkeypatch.setattr(ad, "search_and_verify", _boom)
    plan = AddSectionPlan(title="Related Work", purpose="综述", position="end",
                          section_type="related-work", structure_kind="desc",
                          structure_desc="按主题分组", subsections=[],
                          want_search=True, search_query="dl", search_limit=3)
    body = (
        r"Prior work studies this problem extensively \cite{keyA}. "
        "Early approaches focused on hand-crafted features, while later methods "
        "adopted representation learning to capture richer structure. "
        "We summarize the main threads of this literature and their respective "
        "limitations, highlighting the gap that motivates our study and the way "
        "our contribution differs from these earlier efforts in scope and method."
    )
    res = execute_add_section(session, plan, facts, _LLM([body]),
                              retrieval_provider=None, verifier=None,
                              confirm=lambda _v, _c: False, max_retry=1,
                              citation_context=_ctx([rec]))
    assert res.ok, res.message
    assert "Related Work" in session.read_working()


# ---------------------------------------------------------------- Task 2：合并 Gate

def test_quota_shortfall_feeds_back_then_passes(tmp_path):
    rec_a, rec_b = _rec("r1", "keyA"), _rec("r2", "keyB")
    session, facts = _session_facts(tmp_path, [rec_a, rec_b])
    # 需要 2 篇：第一轮只引 1 篇 → 回灌；第二轮引够 → 写回
    weak = r"Our method attains 0.87 accuracy \cite{keyA}, beating the baseline clearly."
    strong = (r"Our method attains 0.87 accuracy \cite{keyA} \cite{keyB}, "
              r"beating the baseline clearly.")
    llm = _LLM([weak, strong])
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="")
    res = execute_rewrite(session, plan, facts, llm, retrieval_provider=None, verifier=None,
                          confirm=lambda _v, _c: False, max_retry=2,
                          citation_context=_ctx([rec_a, rec_b], quota=_quota(2)))
    assert res.ok and res.attempts == 2
    second_user = llm.seen[1][1].content
    assert "未达标" in second_user or "可补引用" in second_user
    assert res.quota_report is not None
    assert set(res.quota_report.used_reference_ids) == {"r1", "r2"}


def test_quota_exhausted_without_port_does_not_write(tmp_path):
    rec_a, rec_b = _rec("r1", "keyA"), _rec("r2", "keyB")
    session, facts = _session_facts(tmp_path, [rec_a, rec_b])
    before = session.read_working()
    weak = r"Our method attains 0.87 accuracy \cite{keyA}, beating the baseline clearly."
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="")
    res = execute_rewrite(session, plan, facts, _LLM([weak, weak, weak]),
                          retrieval_provider=None, verifier=None,
                          confirm=lambda _v, _c: False, max_retry=1,
                          citation_context=_ctx([rec_a, rec_b], quota=_quota(2)))
    assert not res.ok
    assert session.read_working() == before          # 未写回
    assert res.quota_report is not None and res.quota_report.effective_gaps


def test_quota_exhausted_with_port_accept_writes_and_records_final_gate(tmp_path):
    rec_a, rec_b = _rec("r1", "keyA"), _rec("r2", "keyB")
    session, facts = _session_facts(tmp_path, [rec_a, rec_b])
    weak = r"Our method attains 0.87 accuracy \cite{keyA}, beating the baseline clearly."
    port = _FinalGatePort("accept")
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="")
    res = execute_rewrite(session, plan, facts, _LLM([weak, weak]),
                          retrieval_provider=None, verifier=None,
                          confirm=lambda _v, _c: False, max_retry=1,
                          citation_context=_ctx([rec_a, rec_b], quota=_quota(2), port=port))
    assert res.ok and port.calls == 1
    assert res.quota_report.accepted_final_gate is True
    assert "keyA" in session.read_working()


def test_quota_exhausted_with_port_cancel_does_not_write(tmp_path):
    rec_a, rec_b = _rec("r1", "keyA"), _rec("r2", "keyB")
    session, facts = _session_facts(tmp_path, [rec_a, rec_b])
    before = session.read_working()
    weak = r"Our method attains 0.87 accuracy \cite{keyA}, beating the baseline clearly."
    port = _FinalGatePort("cancel")
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="")
    res = execute_rewrite(session, plan, facts, _LLM([weak, weak]),
                          retrieval_provider=None, verifier=None,
                          confirm=lambda _v, _c: False, max_retry=1,
                          citation_context=_ctx([rec_a, rec_b], quota=_quota(2), port=port))
    assert not res.ok and session.read_working() == before


def test_out_of_bundle_citation_never_waived(tmp_path):
    """新增 Bundle 外引用 → 即使 port accept 也绝不写回（不可豁免）。"""
    rec_a, rec_b = _rec("r1", "keyA"), _rec("r2", "keyB")
    session, facts = _session_facts(tmp_path, [rec_a, rec_b])
    before = session.read_working()
    # Bundle 只含 r1；候选却新增引用 keyB（在 Ledger 里但不在 Bundle）
    bad = (r"Our method attains 0.87 accuracy \cite{keyA} \cite{keyB}, "
           r"beating the baseline clearly.")
    port = _FinalGatePort("accept")
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="")
    res = execute_rewrite(session, plan, facts, _LLM([bad, bad]),
                          retrieval_provider=None, verifier=None,
                          confirm=lambda _v, _c: False, max_retry=1,
                          citation_context=_ctx([rec_a], quota=_quota(1), port=port))
    assert not res.ok
    assert port.calls == 0                           # 非法引用不进 final_gate 决策
    assert session.read_working() == before


def test_preserved_legacy_citation_allowed_in_rewrite(tmp_path):
    """原文已有的 \\cite{key1} 不在 Bundle 内，但属保留引用 → 不算非法。"""
    rec_a = _rec("r1", "keyA")
    session, facts = _session_facts(tmp_path, [rec_a])
    good = r"Our method attains 0.87 accuracy \cite{key1} \cite{keyA}, beating the baseline."
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="")
    res = execute_rewrite(session, plan, facts, _LLM([good]),
                          retrieval_provider=None, verifier=None,
                          confirm=lambda _v, _c: False, max_retry=1,
                          citation_context=_ctx([rec_a], quota=_quota(1)))
    assert res.ok, res.message


def test_effective_lower_bound_relaxes_gate(tmp_path):
    """接受偏差后 effective 下界为 1 → 只引 1 篇即可通过，但 original gap 仍诚实报告。"""
    rec_a, rec_b = _rec("r1", "keyA"), _rec("r2", "keyB")
    session, facts = _session_facts(tmp_path, [rec_a, rec_b])
    one = r"Our method attains 0.87 accuracy \cite{keyA}, beating the baseline clearly."
    ctx = _ctx([rec_a, rec_b], quota=_quota(2),
               effective=(CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1),))
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="")
    res = execute_rewrite(session, plan, facts, _LLM([one]),
                          retrieval_provider=None, verifier=None,
                          confirm=lambda _v, _c: False, max_retry=1, citation_context=ctx)
    assert res.ok and res.attempts == 1
    assert res.quota_report.effective_gaps == ()
    assert res.quota_report.original_gaps                 # 诚实报告原始缺口


def test_writer_loop_never_retrieves(tmp_path, monkeypatch):
    """写作循环内绝不检索：rewrite/add_section 模块的 search_and_verify 均不被调用。"""
    rec_a = _rec("r1", "keyA")
    session, facts = _session_facts(tmp_path, [rec_a])
    import paper_agent.intents.rewrite as rw

    monkeypatch.setattr(rw, "search_and_verify",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不得检索")))
    weak = r"Our method attains 0.87 accuracy, beating the baseline clearly."
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="",
                       want_search=True, search_query="q")
    res = execute_rewrite(session, plan, facts, _LLM([weak, weak, weak]),
                          retrieval_provider=None, verifier=None,
                          confirm=lambda _v, _c: False, max_retry=2,
                          citation_context=_ctx([rec_a], quota=_quota(1)))
    assert not res.ok                                # 一直没引 → 配额不达


# ================================================================ Task 3：Handler 路由

from paper_agent.application.literature_flow import FlowOutcome, FlowState
from paper_agent.intents.router import Intent
from paper_agent.literature.quota import RawCountClause, RawQuotaSpec
from paper_agent.runtime.handlers import HandlerContext, handler_for
from paper_agent.tasking.contracts import (
    FieldStatus,
    Operation,
    OperationStatus,
    SlotSource,
    SlotValue,
)


def _present(value):
    return SlotValue(value=value, field_status=FieldStatus.PRESENT,
                     source=SlotSource.USER_EXPLICIT)


def _rewrite_op(slots_extra=None):
    slots = {
        "section_id": _present("introduction"),
        "direction": _present("补充论证"),
    }
    if slots_extra:
        slots.update(slots_extra)
    return Operation(operation_id="op1", intent=Intent.REWRITE, slots=slots)


def _raw_quota(total_min=1):
    return RawQuotaSpec(constraints=(RawCountClause("total", "min", total_min),))


class _Orch:
    """记录是否被调用；返回可配置 FlowOutcome。"""

    def __init__(self, outcome):
        self._o = outcome
        self.called = 0

    def run(self, request, *, facts, port=None, **kw):
        self.called += 1
        return self._o


def _ready_outcome(records, quota):
    return FlowOutcome(
        state=FlowState.READY_TO_WRITE, bundle=_bundle(records),
        effective_constraints=quota.count_constraints, deviations=(),
    )


def test_handler_no_quota_uses_old_path_and_skips_orchestrator(tmp_path):
    """无配额槽 → 旧路径，orchestrator 绝不被调用。"""
    session, facts = _session_facts(tmp_path)
    good = r"Our method attains 0.87 accuracy \cite{key1}, beating the baseline clearly."
    orch = _Orch(None)
    ctx = HandlerContext(session=session, llm=_LLM([good]), facts=facts,
                         writing_integration_enabled=True, literature_orchestrator=orch)
    res = handler_for(Intent.REWRITE).execute(_rewrite_op(), ctx)
    assert res.status is OperationStatus.SUCCEEDED
    assert orch.called == 0


def test_handler_flag_off_with_quota_rejected(tmp_path):
    """flag 关但用户给了配额 → REJECTED「未启用」，不降级忽略。"""
    session, facts = _session_facts(tmp_path)
    orch = _Orch(None)
    ctx = HandlerContext(session=session, llm=_LLM([]), facts=facts,
                         writing_integration_enabled=False, literature_orchestrator=orch)
    op = _rewrite_op({"citation_quota": _present(_raw_quota(1))})
    res = handler_for(Intent.REWRITE).execute(op, ctx)
    assert res.status is OperationStatus.REJECTED
    assert "未启用" in res.message
    assert orch.called == 0


def test_handler_materialize_conflict_rejected(tmp_path):
    """materialize 不可执行 → REJECTED，不进写作、不调 orchestrator。"""
    session, facts = _session_facts(tmp_path)
    orch = _Orch(None)
    ctx = HandlerContext(session=session, llm=_LLM([]), facts=facts,
                         writing_integration_enabled=True, literature_orchestrator=orch)
    # exact 3 与 min 5 冲突 → finalize 不可执行
    conflict = RawQuotaSpec(constraints=(
        RawCountClause("total", "exact", 3), RawCountClause("total", "min", 5)))
    op = _rewrite_op({"citation_quota": _present(conflict)})
    res = handler_for(Intent.REWRITE).execute(op, ctx)
    assert res.status is OperationStatus.REJECTED
    assert orch.called == 0


def test_handler_orchestrator_needs_input_does_not_write(tmp_path):
    """orchestrator 非 READY（NEEDS_*）→ 返回对应状态、不进写作。"""
    session, facts = _session_facts(tmp_path)
    before = session.read_working()
    outcome = FlowOutcome(state=FlowState.NEEDS_BATCH_CONFIRMATION, message="需确认检索批次")
    orch = _Orch(outcome)
    ctx = HandlerContext(session=session, llm=_LLM(["should not be used"]), facts=facts,
                         writing_integration_enabled=True, literature_orchestrator=orch)
    op = _rewrite_op({"citation_quota": _present(_raw_quota(1))})
    res = handler_for(Intent.REWRITE).execute(op, ctx)
    assert res.status is OperationStatus.REJECTED
    assert orch.called == 1
    assert session.read_working() == before


def test_handler_ready_passes_citation_context_to_execute(tmp_path):
    """orchestrator READY → 组 CitationContext 传给 execute_rewrite 并真实写回。"""
    rec = _rec("r1", "keyA")
    session, facts = _session_facts(tmp_path, [rec])
    quota = _quota(1)
    orch = _Orch(_ready_outcome([rec], quota))
    good = r"Our method attains 0.87 accuracy \cite{key1} \cite{keyA}, beating the baseline."
    ctx = HandlerContext(session=session, llm=_LLM([good]), facts=facts,
                         writing_integration_enabled=True, literature_orchestrator=orch,
                         classifier=_classifier)
    op = _rewrite_op({"citation_quota": _present(_raw_quota(1))})
    res = handler_for(Intent.REWRITE).execute(op, ctx)
    assert res.status is OperationStatus.SUCCEEDED
    assert orch.called == 1
    assert "keyA" in session.read_working()


# ================================================================ Task 4：AST 结构验收

def _src_root():
    return os.path.join(_SRC, "paper_agent")


def _read(rel):
    with open(os.path.join(_src_root(), rel), encoding="utf-8") as fh:
        return fh.read()


def test_literature_does_not_import_tasking_or_application():
    """架构红线：literature/* 不 import tasking/application。"""
    import pathlib

    lit = pathlib.Path(_src_root()) / "literature"
    offenders = []
    for p in lit.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "import paper_agent.tasking" in text or "from paper_agent.tasking" in text:
            offenders.append((p.name, "tasking"))
        if "import paper_agent.application" in text or "from paper_agent.application" in text:
            offenders.append((p.name, "application"))
    assert offenders == []


def test_intents_do_not_manage_retrieval_loop_state():
    """intents/* 只消费 citation_context，不直接驱动 RetrievalLoop。"""
    for mod in ("intents/rewrite.py", "intents/add_section.py"):
        src = _read(mod)
        assert "run_retrieval_loop" not in src
        assert "RetrievalLoop" not in src


def test_citation_context_is_frozen_value_object():
    import dataclasses

    assert dataclasses.is_dataclass(CitationContext)
    params = getattr(CitationContext, "__dataclass_params__")
    assert params.frozen is True
