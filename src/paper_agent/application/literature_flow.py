"""WritingLiteratureOrchestrator：可恢复的写作前文献流状态机（A2d-4 / 任务 4，design D3/D4/D9/D10）。

```text
ASSESSING
  ├─ gap=0 ─────────────────────────────────→ BUILDING_BUNDLE
  └─ gap>0 → RETRIEVING → NEEDS_BATCH_CONFIRMATION
                 ├─ reject ─────────────────→ CANCELLED
                 └─ accept → CONFIRMED → COMMITTED → REASSESSING
                                            ├─ gap=0 → BUILDING_BUNDLE
                                            └─ gap>0 → NEEDS_GAP_DECISION
                                                          ├─ cancel → CANCELLED
                                                          └─ accept → BUILDING_BUNDLE
BUILDING_BUNDLE → (supervised) NEEDS_BUNDLE_CONFIRMATION → READY_TO_WRITE / CANCELLED
```

纪律：
- **commit 后必须重新统计**（Req 13.9），不信 Loop 预览计数。
- **非交互路径绝不默认接受**：port 为 None 或返回 needs_input → 存 checkpoint、返回 NEEDS_*。
- **资产提交不回滚**（D10）：commit 成功后 Bundle 构造/后续失败均不回滚 Ledger。
- 本模块**不调 writer LLM、不写 Working Copy**（属 A2d-5）。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from paper_agent.application.literature_ports import (
    PreflightCheckpoint,
    ThroughputPolicySnapshot,
    checkpoint_to_dict,
    throughput_policy_hash,
)
from paper_agent.literature.evidence import BundleStatus, build_bundle
from paper_agent.literature.preflight import (
    AcceptedQuotaDeviation,
    acquisition_gaps,
    assess_availability,
    derive_retrieval_quota,
    effective_constraints,
    ensure_relevance_active,
    quota_hash,
)
from paper_agent.literature.relevance import relevance_content_hash
from paper_agent.literature.retrieval.batch import render_batch_summary
from paper_agent.literature.retrieval.loop import RetrievalRequest, run_retrieval_loop
from paper_agent.literature.retrieval.transaction import commit_batch


class FlowState(str, Enum):
    ASSESSING = "assessing"
    RETRIEVING = "retrieving"
    NEEDS_BATCH_CONFIRMATION = "needs_batch_confirmation"
    CONFIRMED = "confirmed"
    COMMITTED = "committed"
    REASSESSING = "reassessing"
    NEEDS_GAP_DECISION = "needs_gap_decision"
    BUILDING_BUNDLE = "building_bundle"
    NEEDS_BUNDLE_CONFIRMATION = "needs_bundle_confirmation"
    READY_TO_WRITE = "ready_to_write"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WritingEvidenceRequest:
    """写作证据请求（单一 criteria + fingerprint，design D2）。"""

    criteria: object                     # A2a RelevanceCriteria
    citation_quota: object               # A2b QuotaSpec(kind=CITATION)
    initial_query: str = ""
    operation_id: str = ""
    search_intent: object | None = None  # A2f SearchIntent（多 seed；非空时贯通检索 Loop）
    # F-3：**章节身份**信号（章节标题或规范化桶名），供接受策略解析。
    # 由知道章节身份的那一层（Handler / runner）显式声明——`criteria.section_purpose`
    # 实测是自由文本（常等于整个课题名），不可靠。
    # **不进 task_fingerprint / checkpoint 哈希**：它是策略提示，不是语义判准，
    # 故加此字段不会失效任何已缓存的相关性判定。
    section_kind: str = ""

    @property
    def task_fingerprint(self) -> str:
        return self.criteria.fingerprint()


@dataclass(frozen=True)
class FlowOutcome:
    state: FlowState
    bundle: object | None = None
    effective_constraints: tuple = ()
    deviations: tuple[AcceptedQuotaDeviation, ...] = ()
    availability: object | None = None
    gaps: tuple = ()
    checkpoint: PreflightCheckpoint | None = None
    message: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


class WritingLiteratureOrchestrator:
    def __init__(
        self,
        *,
        deps,                      # A2c-3 AttemptDeps（registry/ledger/judge/store/...）
        limits,                    # A2c-4 RetrievalLimits
        planner,                   # A2c-4 QueryPlanner
        classifier,
        policy: str = "high_only",
        judge_key: str = "",
        content_hasher=relevance_content_hash,
        redundancy_factor: float = 2.0,
        budget_chars: int = 8000,
        supervised: bool = True,
        throughput_policy_snapshot: ThroughputPolicySnapshot | None = None,
        acceptance_resolver=None,
    ) -> None:
        self._deps = deps
        self._limits = limits
        # F-3：`(section_purpose) -> RelevanceAcceptancePolicy`。默认沿用 deps 上装配期
        # 的策略（不传时行为不变）。解析结果**同时**注入 Attempt / availability / bundle，
        # 三处共用同一对象，杜绝「入库了但算不进配额」。
        self._acceptance_resolver = acceptance_resolver
        self._planner = planner
        self._classifier = classifier
        self._policy = policy
        self._judge_key = judge_key
        self._content_hasher = content_hasher
        self._active_relevance_runtime = getattr(
            deps, "active_relevance_runtime", None
        )
        self._redundancy = redundancy_factor
        self._budget_chars = budget_chars
        self._supervised = supervised
        snapshot = throughput_policy_snapshot or ThroughputPolicySnapshot()
        self._throughput_policy = snapshot.to_dict()
        self._throughput_policy_hash = snapshot.canonical_hash
        self._throughput_policy_snapshot_id = snapshot.snapshot_id

    # ---------------------------------------------------------------- 主流程

    def run(
        self,
        request: WritingEvidenceRequest,
        *,
        facts,
        port=None,
        deviations: tuple[AcceptedQuotaDeviation, ...] = (),
        flow_id: str = "",
        preexisting_cited_ids: tuple[str, ...] = (),
        budget_ledger=None,
    ) -> FlowOutcome:
        quota = request.citation_quota
        task_fp = request.task_fingerprint
        qhash = quota_hash(quota)
        fid = flow_id or f"flow_{uuid.uuid4().hex[:10]}"
        devs = tuple(deviations)

        # F-3：**一次解析，三处共用**（Attempt / assess_availability / build_bundle）。
        acceptance = self._resolve_acceptance(request)
        deps = self._deps_with_acceptance(acceptance)

        operation_budget = budget_ledger
        if self._active_relevance_runtime is not None and operation_budget is None:
            from paper_agent.literature.retrieval.budget import BudgetLedger

            operation_budget = BudgetLedger(self._limits)

        si = request.search_intent
        si_id = f"si_{fid}" if si is not None else ""
        si_sem = si.semantic_fingerprint() if si is not None else ""
        si_strat = si.strategy_hash() if si is not None else ""

        def _cp(state: FlowState, *, gaps=(), batch_id="", revision=0) -> PreflightCheckpoint:
            return PreflightCheckpoint(
                flow_id=fid, operation_id=request.operation_id, task_fingerprint=task_fp,
                quota_hash=qhash, state=state.value,
                initial_query_hash=_hash(request.initial_query),
                batch_id=batch_id, revision=revision,
                gaps=tuple((g.dimension.value, g.amount) for g in gaps),
                deviations=devs, created_at=_now(),
                search_intent_id=si_id, semantic_fingerprint=si_sem, strategy_hash=si_strat,
                throughput_policy=dict(self._throughput_policy),
                throughput_policy_hash=self._throughput_policy_hash,
                throughput_policy_snapshot_id=self._throughput_policy_snapshot_id,
            )

        def _pause(state: FlowState, *, gaps=(), batch_id="", availability=None,
                   message="") -> FlowOutcome:
            cp = _cp(state, gaps=gaps, batch_id=batch_id)
            facts.put_literature_flow(checkpoint_to_dict(cp))
            # A2f：暂停时冻结 SearchIntent 原文（会话状态，供恢复重建；不进 trace）。
            if si is not None:
                from paper_agent.literature.retrieval.search_intent import search_intent_to_dict
                facts.put_search_intent(si_id, search_intent_to_dict(si))
            return FlowOutcome(state=state, gaps=tuple(gaps), availability=availability,
                               deviations=devs, checkpoint=cp, message=message)

        # 1) ASSESSING
        eff = effective_constraints(quota, devs, operation_id=request.operation_id,
                                    task_fingerprint=task_fp)
        availability = self._assess(
            quota, request, facts, budget_ledger=operation_budget,
            acceptance=acceptance,
        )
        gaps = acquisition_gaps(availability, eff)

        # 2) RETRIEVING（仅在存在下界缺口时）
        if gaps:
            retrieval_quota = derive_retrieval_quota(
                gaps, quota, redundancy_factor=self._redundancy
            )
            # A2f：有 SearchIntent → 用多 seed；initial_query 置空避免双真相源冲突。
            si = request.search_intent
            loop_res = run_retrieval_loop(
                RetrievalRequest(
                    quota=retrieval_quota, criteria=request.criteria,
                    initial_query="" if si is not None else request.initial_query,
                    task_fingerprint=task_fp, search_intent=si),
                deps=deps, limits=self._limits, planner=self._planner,
                budget_ledger=operation_budget,
            )
            batch = loop_res.batch
            # STAGED batch 先入 pending 区（未确认不进 Ledger）
            from paper_agent.literature.retrieval.batch import batch_to_dict
            facts.pending_batches = [
                pb for pb in facts.pending_batches if pb.get("batch_id") != batch.batch_id
            ] + [batch_to_dict(batch)]

            # 3) NEEDS_BATCH_CONFIRMATION
            if port is None:
                return _pause(FlowState.NEEDS_BATCH_CONFIRMATION, gaps=gaps,
                              batch_id=batch.batch_id, availability=availability,
                              message="需确认检索批次")
            decision = port.confirm_retrieval_batch(render_batch_summary(batch))
            if decision.action == "needs_input":
                return _pause(FlowState.NEEDS_BATCH_CONFIRMATION, gaps=gaps,
                              batch_id=batch.batch_id, availability=availability)
            if decision.action != "accept":
                batch.reject()
                facts.pending_batches = [
                    pb for pb in facts.pending_batches if pb.get("batch_id") != batch.batch_id
                ]
                return FlowOutcome(state=FlowState.CANCELLED, deviations=devs,
                                   message="用户否决检索批次；文献库未改动")
            batch.confirm()
            commit_batch(facts, batch)          # 单 envelope 原子提交（A2c-5）

            # 4) REASSESSING：重新统计（不信 Loop 预览）
            availability = self._assess(
                quota, request, facts, budget_ledger=operation_budget,
                acceptance=acceptance,
            )
            gaps = acquisition_gaps(availability, eff)

            # 5) NEEDS_GAP_DECISION
            if gaps:
                if port is None:
                    return _pause(FlowState.NEEDS_GAP_DECISION, gaps=gaps,
                                  availability=availability, message="需决定是否接受缺口")
                gap_decision = port.resolve_quota_gap(
                    {"gaps": [(g.dimension.value, g.required, g.achieved, g.amount) for g in gaps],
                     "availability_total": availability.total()}
                )
                if gap_decision.action == "needs_input":
                    return _pause(FlowState.NEEDS_GAP_DECISION, gaps=gaps,
                                  availability=availability)
                if gap_decision.action != "accept":
                    return FlowOutcome(state=FlowState.CANCELLED, gaps=tuple(gaps),
                                       deviations=devs, message="用户取消（配额缺口未接受）")
                devs = devs + tuple(
                    AcceptedQuotaDeviation(
                        stage="preflight", dimension=g.dimension,
                        original_lower_bound=(g.achieved + g.amount),
                        achieved_at_decision=g.achieved,
                        effective_lower_bound=g.achieved,     # 冻结为接受时的可用数
                        original_gap=g.amount,
                        task_fingerprint=task_fp, operation_id=request.operation_id,
                        quota_hash=qhash, accepted_at=_now(),
                    )
                    for g in gaps
                )
                eff = effective_constraints(quota, devs, operation_id=request.operation_id,
                                            task_fingerprint=task_fp)

        # 6) BUILDING_BUNDLE
        build = build_bundle(
            [facts.ledger.get(rid) for rid in availability.available_ids],
            criteria=request.criteria, quota=quota,
            relevance_store=facts.relevance_store, effective_constraints=eff,
            budget_chars=self._budget_chars, classifier=self._classifier,
            policy=self._policy, judge_key=self._judge_key,
            content_hasher=self._content_hasher,
            preexisting_cited_ids=preexisting_cited_ids,
            acceptance=acceptance,
        )
        if build.status is BundleStatus.CONTEXT_BUDGET_INSUFFICIENT:
            # 预算连**最省的降级配置**都装不下 effective 下界 → 需决策，不进 Writer Loop。
            # `build_bundle` 已自动试过全部三档（截短摘要 + 按相关性退回题录，X-10 A），
            # 走到这里说明连"只留最相关的几条摘要、其余只给题录"都装不下，降级救不了。
            # 消息带上量化缺口：只说"装不下"，用户无法判断该提预算还是该减篇数。
            shortfall = build.shortfall_chars()
            detail = (
                f"（已试到最省档：摘要上限 {build.min_abstract_limit} 字符 + 多数条目"
                f"只给题录，仍需 {build.required_chars} 字符，预算 {build.budget_chars}，"
                f"差 {shortfall}）"
            ) if build.required_chars else ""
            return _pause(FlowState.NEEDS_GAP_DECISION, gaps=gaps, availability=availability,
                          message=f"上下文预算不足以容纳配额所需证据{detail}："
                                  f"需提高预算（PAPER_LIT_BUNDLE_BUDGET_CHARS）或降低引用数量要求")

        # 7) NEEDS_BUNDLE_CONFIRMATION（supervised 默认；A2c-5 评测门槛）
        if self._supervised:
            if port is None:
                return _pause(FlowState.NEEDS_BUNDLE_CONFIRMATION, gaps=gaps,
                              availability=availability, message="需确认证据集清单")
            # 降级必须**出现在用户确认页**上：证据是截断摘要而用户以为是全文摘要，
            # 会让他对写作质量的预期失真（design：不静默降级）。
            bd = port.confirm_evidence_bundle(
                {"bundle_id": build.bundle.bundle_id,
                 "degradation": build.bundle.degradation,
                 "abstract_limit": build.bundle.abstract_limit,
                 "truncated_entries": build.bundle.truncated_entry_count(),
                 # 退回题录的条数单列：它比截断严重（一点摘要都没给），
                 # 并进 truncated_entries 会掩盖掉。见 X-10 A。
                 "bibliographic_only_entries": build.bundle.dropped_abstract_count(),
                 "entries": [(e.reference_id, e.title, e.language_bucket)
                             for e in build.bundle.entries]}
            )
            if bd.action == "needs_input":
                return _pause(FlowState.NEEDS_BUNDLE_CONFIRMATION, gaps=gaps,
                              availability=availability)
            if bd.action != "accept":
                return FlowOutcome(state=FlowState.CANCELLED, deviations=devs,
                                   message="用户否决证据集")

        facts.drop_literature_flow(fid)
        return FlowOutcome(
            state=FlowState.READY_TO_WRITE, bundle=build.bundle,
            effective_constraints=eff, deviations=devs, availability=availability,
            gaps=tuple(gaps), message="预检完成，可进入写作",
        )

    # ---------------------------------------------------------------- 恢复

    def resume(self, flow_id: str, request: WritingEvidenceRequest, *, facts, port=None,
               preexisting_cited_ids: tuple[str, ...] = ()) -> FlowOutcome:
        """从 checkpoint 继续。作用域不一致（operation/fingerprint/quota_hash）→ 明确拒绝。"""
        from paper_agent.application.literature_ports import checkpoint_from_dict

        row = facts.get_literature_flow(flow_id)
        if row is None:
            return FlowOutcome(state=FlowState.CANCELLED, message=f"找不到 flow：{flow_id}")
        cp = checkpoint_from_dict(row)
        if throughput_policy_hash(cp.throughput_policy) != cp.throughput_policy_hash:
            return FlowOutcome(
                state=FlowState.CANCELLED,
                message="TAMPERED_THROUGHPUT_POLICY：checkpoint 策略哈希无效，拒绝恢复",
            )
        if cp.throughput_policy_hash != self._throughput_policy_hash:
            return FlowOutcome(
                state=FlowState.CANCELLED,
                message="STALE_THROUGHPUT_POLICY：吞吐策略或模型版本已变化，拒绝恢复",
            )
        if (cp.operation_id != request.operation_id
                or cp.task_fingerprint != request.task_fingerprint
                or cp.quota_hash != quota_hash(request.citation_quota)):
            return FlowOutcome(state=FlowState.CANCELLED,
                               message="checkpoint 与当前任务不一致，拒绝恢复")
        # A2f：若 checkpoint 冻结过 SearchIntent，恢复时传入的 intent 必须**同一冻结**
        # （strategy_hash 一致）——不一致即 STALE，拒绝恢复且**不重新采样 LLM**。
        if cp.strategy_hash:
            si = request.search_intent
            if si is None or si.strategy_hash() != cp.strategy_hash:
                return FlowOutcome(state=FlowState.CANCELLED,
                                   message="STALE_SEARCH_INTENT：检索意图与暂存不一致，拒绝恢复")
        return self.run(request, facts=facts, port=port, deviations=cp.deviations,
                        flow_id=flow_id, preexisting_cited_ids=preexisting_cited_ids)

    # ---------------------------------------------------------------- 内部

    def _resolve_acceptance(self, request):
        """按本次请求的**章节信号**解析接受策略（F-3）。

        信号按可靠性排序传入：`section_kind`（显式声明，最可靠）→
        `SearchIntent.section_purpose` → `criteria.section_purpose`
        （后两者实测常是自由文本/课题名，只作兜底）。

        无 resolver（旧调用方）→ 沿用装配期 deps 上的策略，行为不变。
        """
        if self._acceptance_resolver is None:
            return getattr(self._deps, "acceptance_policy", None)
        criteria = getattr(request, "criteria", None)
        intent = getattr(request, "search_intent", None)
        signals = (
            getattr(request, "section_kind", "") or "",
            getattr(intent, "section_purpose", "") or "" if intent is not None else "",
            getattr(criteria, "section_purpose", "") or "" if criteria is not None else "",
        )
        return self._acceptance_resolver(*signals)

    def _deps_with_acceptance(self, acceptance):
        """把解析出的策略写回 deps 副本，使 Attempt 与后两处口径一致。"""
        import dataclasses

        if acceptance is None or getattr(self._deps, "acceptance_policy", None) is acceptance:
            return self._deps
        try:
            return dataclasses.replace(self._deps, acceptance_policy=acceptance)
        except Exception:  # noqa: BLE001 - 非 dataclass deps（测试替身）→ 原样返回
            return self._deps

    def _assess(self, quota, request, facts, *, budget_ledger=None, acceptance=None):
        if self._active_relevance_runtime is not None:
            if budget_ledger is None:
                raise ValueError("active relevance preflight requires BudgetLedger")
            ensure_relevance_active(
                request.criteria,
                facts.ledger.all_records(),
                runtime=self._active_relevance_runtime,
                store=facts.relevance_store,
                budget_ledger=budget_ledger,
                eligibility_filters=quota.eligibility_filters,
                classifier=self._classifier,
                policy=self._policy,
            )
        kwargs = {}
        if acceptance is not None:
            kwargs["acceptance"] = acceptance
        return assess_availability(
            quota, request.criteria, facts.ledger, facts.relevance_store,
            classifier=self._classifier, policy=self._policy, judge_key=self._judge_key,
            content_hasher=self._content_hasher, **kwargs,
        )


__all__ = [
    "FlowState",
    "WritingEvidenceRequest",
    "FlowOutcome",
    "WritingLiteratureOrchestrator",
]
