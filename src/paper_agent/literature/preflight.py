"""LiteraturePreflight 领域服务（A2d-4 / 任务 1，design-a2d D3/D4）。

**纯领域计算**：可用文献统计、AcquisitionGap、内部 RetrievalQuota 派生、
`AcceptedQuotaDeviation` 与 `effective_constraints`。

本模块**不接 interactor、不 import `tasking`/`application`/`config`**——外部调用与状态推进
由 `application/literature_flow.py` 负责（分层纪律，design D3）。

关键纪律：
- 派生 RetrievalQuota **完整复制** EligibilityFilter、只按 **BELOW** gap × 冗余系数推下界、
  **各维度独立（total/zh/en 不相加）**、原 CitationQuota **frozen 不被修改**（Req 13.6/13.7）。
- 只对 **lower bound** 触发检索；纯 max/above 维度不触发检索（仍由最终 Gate 执行）。
- 偏差是**有数量的记录**：`effective_constraints` 只把**下界**换成 `effective_lower_bound`，
  上界不动；作用域绑 operation_id + task_fingerprint + quota_hash，**不跨任务复用**（Req 13.11）。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace

from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
    GapDirection,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
    count_by_dimension,
    evaluate_constraints,
    passes_all_filters,
)
from paper_agent.literature.relevance import (
    STRICT_ACCEPTANCE,
    relevance_content_hash,
)
from paper_agent.literature.verification import (
    VerificationPolicy,
    VerificationStatus,
    status_under_policy,
)


# ---------------------------------------------------------------- 偏差记录

@dataclass(frozen=True)
class AcceptedQuotaDeviation:
    """用户显式接受的**有数量**偏差（Req 13.11 同步口径，非维度级布尔豁免）。"""

    stage: str                      # preflight | context_budget | final_gate
    dimension: CountDimension
    original_lower_bound: int
    achieved_at_decision: int
    effective_lower_bound: int
    original_gap: int
    task_fingerprint: str
    operation_id: str
    quota_hash: str
    accepted_at: str = ""


def quota_hash(quota: QuotaSpec) -> str:
    """QuotaSpec 的稳定内容哈希（偏差作用域绑定用）。"""
    payload = {
        "kind": quota.kind.value,
        "scope": quota.scope.value,
        "base_year": quota.base_year,
        "filters": sorted(
            (f.field.value, f.operator.value, str(f.value)) for f in quota.eligibility_filters
        ),
        "constraints": sorted(
            (c.dimension.value, c.operator.value, str(c.value)) for c in quota.count_constraints
        ),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- relevance 补判

@dataclass(frozen=True)
class RelevancePreparation:
    assessed: int = 0
    errors: int = 0
    budget_exhausted: bool = False


def ensure_relevance(
    criteria,
    records,
    *,
    judge,
    store,
    max_calls: int = 50,
    judge_key: str = "",
) -> RelevancePreparation:
    """对缺当前 fingerprint 结论的记录补判 relevance（预算内）。

    `status=ERROR` **不伪装 unrelated、也不计可用**（A2a D3）；预算耗尽如实标注。
    """
    task_fp = criteria.fingerprint()
    key = judge_key or judge.judge_version.key()
    assessed = errors = 0
    exhausted = False
    for rec in records:
        if assessed >= max_calls:
            exhausted = True
            break
        if store.get(rec.reference_id, task_fp,
                     relevance_content_hash(rec.title, rec.abstract), key) is not None:
            continue
        asmt = judge.assess(rec, criteria)
        store.put(asmt)
        assessed += 1
        if asmt.status != "SUCCESS":
            errors += 1
    return RelevancePreparation(assessed=assessed, errors=errors, budget_exhausted=exhausted)


def ensure_relevance_active(
    criteria,
    records,
    *,
    runtime,
    store,
    budget_ledger,
    eligibility_filters=(),
    classifier=None,
    policy: str = "high_only",
) -> RelevancePreparation:
    """Batch-backfill missing active-key relevance for usable ledger records.

    Only strict-final, eligibility-passing records enter windows.  ERROR
    assessments are reported but never persisted as successful cache entries.
    """

    from paper_agent.literature.retrieval.call_control import ExternalCallControl

    task_fp = criteria.fingerprint()
    items = []
    for index, rec in enumerate(records):
        if status_under_policy(
            rec.verification_events, VerificationPolicy.STRICT_FINAL_V1
        ) is not VerificationStatus.VERIFIED:
            continue
        if eligibility_filters and not passes_all_filters(
            rec,
            eligibility_filters,
            classifier=classifier,
            policy=policy,
        ):
            continue
        prepared = runtime.prepare(rec.title, rec.abstract)
        cached = store.get(
            rec.reference_id,
            task_fp,
            prepared.content_hash,
            runtime.judge_key,
        )
        if cached is not None and cached.status == "SUCCESS":
            continue
        items.append(
            runtime.make_window_item(
                candidate_key=f"c{index}",
                reference_id=rec.reference_id,
                title=rec.title,
                abstract=rec.abstract,
            )
        )

    if not items:
        return RelevancePreparation()

    control = ExternalCallControl(budget_ledger)
    assessed = errors = 0
    exhausted = False
    for window in runtime.pack(items, criteria):
        results, _stats = runtime.assess_window_with_stats(
            window, criteria, call_control=control
        )
        assessed += len(window.items)
        for item in window.items:
            assessment = results[item.candidate_key]
            if assessment.status == "SUCCESS":
                merge_put = getattr(store, "merge_put", None)
                if merge_put is not None:
                    merge_put(assessment)
                else:
                    store.put(assessment)
            else:
                errors += 1
        dimensions = set(budget_ledger.exhausted_dimensions())
        if dimensions & {
            "max_llm_calls",
            "max_verify_calls",
            "max_provider_calls",
            "provider_time_budget_s",
        }:
            exhausted = True
            break
    return RelevancePreparation(
        assessed=assessed,
        errors=errors,
        budget_exhausted=exhausted,
    )


# ---------------------------------------------------------------- 可用统计

@dataclass(frozen=True)
class Availability:
    counts: object                              # DimensionCounts（A2b）
    available_ids: tuple[str, ...] = ()

    def total(self) -> int:
        return getattr(self.counts, "total", 0)


def assess_availability(
    quota: QuotaSpec,
    criteria,
    ledger,
    relevance_store,
    *,
    classifier,
    policy: str = "high_only",
    judge_key: str = "",
    content_hasher=relevance_content_hash,
    acceptance=STRICT_ACCEPTANCE,
) -> Availability:
    """统计**对本次任务可用**的不同 reference_id（Req 13.2）。

    三条同时满足：STRICT VERIFIED、过全部 EligibilityFilter、当前 task_fingerprint 下
    relevance 已接受（SUCCESS 且 verdict 在接受集合）。

    `acceptance`（F-3）：接受语义由**调用方注入**，与 Attempt / build_bundle 共用同一
    对象。此前本函数硬编码 `("relevant",)`，与可配置的 Attempt 策略分叉，导致
    「也收 weakly」时文献入库却算不进配额。默认严格 → 不传时行为逐字节不变。
    """
    task_fp = criteria.fingerprint()
    usable = []
    for rec in ledger.all_records():
        if status_under_policy(rec.verification_events, VerificationPolicy.STRICT_FINAL_V1) \
                is not VerificationStatus.VERIFIED:
            continue
        if not passes_all_filters(rec, quota.eligibility_filters,
                                  classifier=classifier, policy=policy):
            continue
        asmt = relevance_store.get(rec.reference_id, task_fp,
                                   content_hasher(rec.title, rec.abstract), judge_key)
        if not acceptance.is_accepted(asmt):
            continue
        usable.append(rec)
    counts = count_by_dimension(usable, quota, classifier=classifier, policy=policy)
    return Availability(counts=counts, available_ids=tuple(r.reference_id for r in usable))


def acquisition_gaps(availability: Availability, constraints) -> tuple:
    """AcquisitionGap：**只保留下界缺口**（BELOW）；ABOVE 留给最终 Gate（Req 13.3）。"""
    gaps = evaluate_constraints(availability.counts, constraints)
    return tuple(g for g in gaps if g.direction is GapDirection.BELOW)


# ---------------------------------------------------------------- 派生检索配额

def derive_retrieval_quota(
    gaps,
    citation_quota: QuotaSpec,
    *,
    redundancy_factor: float = 2.0,
    max_target: int = 100,
) -> QuotaSpec:
    """据 BELOW gap 派生**内部** RetrievalQuota（Req 13.5–13.8）。

    - `kind=RETRIEVAL` / `scope=NEW_ONLY`；**完整复制** citation_quota 的 EligibilityFilter
      （不放宽年份/类型）。
    - 每维目标 = `min(ceil(gap.amount * redundancy_factor), max_target)`；
      **各维度独立**（total/zh/en 不相加）。
    - 原 `citation_quota` **不被修改**（返回新 QuotaSpec）。
    """
    factor = max(1.0, float(redundancy_factor))
    constraints = []
    for g in gaps:
        if g.direction is not GapDirection.BELOW:
            continue
        target = min(int(math.ceil(g.amount * factor)), int(max_target))
        if target <= 0:
            continue
        constraints.append(CountConstraint(g.dimension, CountOperator.MIN, target,
                                           verbatim=f"内部检索目标（缺 {g.amount} × 冗余 {factor}）"))
    return QuotaSpec(
        kind=QuotaKind.RETRIEVAL,
        scope=QuotaScope.NEW_ONLY,
        eligibility_filters=tuple(citation_quota.eligibility_filters),   # 完整复制
        count_constraints=tuple(constraints),
        base_year=citation_quota.base_year,
    )


# ---------------------------------------------------------------- effective 约束

def effective_constraints(
    citation_quota: QuotaSpec,
    deviations,
    *,
    operation_id: str = "",
    task_fingerprint: str = "",
) -> tuple[CountConstraint, ...]:
    """把有匹配偏差的维度下界替换为 `effective_lower_bound`（上界不动）。

    匹配要求 dimension + operation_id + task_fingerprint + quota_hash 全一致
    （**不跨任务复用**，Req 13.11）。
    """
    qhash = quota_hash(citation_quota)
    by_dim: dict = {}
    for d in deviations or ():
        if d.quota_hash != qhash:
            continue
        if operation_id and d.operation_id != operation_id:
            continue
        if task_fingerprint and d.task_fingerprint != task_fingerprint:
            continue
        prev = by_dim.get(d.dimension)
        # 同维度多次接受 → 取最小 effective（最新接受的更宽松场景由调用方顺序保证）
        if prev is None or d.effective_lower_bound < prev.effective_lower_bound:
            by_dim[d.dimension] = d

    out: list[CountConstraint] = []
    for c in citation_quota.count_constraints:
        dev = by_dim.get(c.dimension)
        if dev is None:
            out.append(c)
            continue
        eff = int(dev.effective_lower_bound)
        if c.operator is CountOperator.MIN:
            out.append(replace(c, value=eff))
        elif c.operator is CountOperator.EXACT:
            # exact N：下界降为 eff，上界保持 N → 变 range(eff, N)
            out.append(replace(c, operator=CountOperator.RANGE, value=(eff, int(c.value))))
        elif c.operator is CountOperator.RANGE:
            lo, hi = c.value                                    # type: ignore[misc]
            out.append(replace(c, value=(eff, int(hi))))
        else:                                                   # MAX：无下界，不受偏差影响
            out.append(c)
    return tuple(out)


__all__ = [
    "AcceptedQuotaDeviation",
    "quota_hash",
    "RelevancePreparation",
    "ensure_relevance",
    "ensure_relevance_active",
    "Availability",
    "assess_availability",
    "acquisition_gaps",
    "derive_retrieval_quota",
    "effective_constraints",
]
