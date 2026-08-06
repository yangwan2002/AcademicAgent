"""配额驱动的有界 RetrievalLoop（A2c-4 / 任务 3，design D8/D10/D11/D12/D15）。

编排 A2c-3 `run_attempt` 多轮：初始查询 → attempt → 用**拟提交去重记录视图**（D12）经
A2b 评估逐维度 gap → 未满足且预算允许 → QueryPlanner 调整查询/源 → 继续，直到满足/触上限/
无进展/取消。

纪律：配额只由已接受推进；Loop 全程不改 filters/constraints、不改 task_fingerprint、不提问；
trace 隐私安全（经注入 sink，不落原始 query/候选文本）；不落盘、不接 Discovery（留 A2c-5）。
不 import `tasking`、不读全局 config/时钟（注入）。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Callable

from paper_agent.literature.language import classify_record_language
from paper_agent.literature.migration import record_from_dict
from paper_agent.literature.quota import (
    CountOperator,
    QuotaGap,
    QuotaSpec,
    count_by_dimension,
    evaluate_constraints,
)
from paper_agent.literature.retrieval.attempt import AttemptDeps, run_attempt
from paper_agent.literature.retrieval.batch import (
    BatchDisposition,
    BatchStatus,
    RetrievalBatch,
)
from paper_agent.literature.retrieval.budget import (
    BudgetLedger,
    CancelToken,
    RetrievalLimits,
    StopReason,
)
from paper_agent.literature.retrieval.query_planner import (
    DeterministicQueryPlanner,
    PlannerRequest,
    QueryPlanner,
)
from paper_agent.literature.verification import (
    VerificationPolicy,
    VerificationStatus,
    status_under_policy,
)
from paper_agent.observability.current import ambient_publish, ambient_span


@dataclass(frozen=True)
class RetrievalRequest:
    quota: QuotaSpec               # kind=retrieval
    criteria: object               # A2a RelevanceCriteria
    initial_query: str = ""        # legacy 单查询；search_intent 非空时须空或等于首 seed
    task_fingerprint: str = ""
    search_intent: object | None = None   # A2f SearchIntent（多 seed 真相源）

    def __post_init__(self) -> None:
        # 单一查询真相源（D6）：两者同时给且冲突 → 构造失败，不静默忽略其一。
        si = self.search_intent
        if si is not None and self.initial_query:
            first = si.seeds[0].text if getattr(si, "seeds", None) else ""
            if self.initial_query.strip() != first.strip():
                raise ValueError(
                    "RetrievalRequest：search_intent 与 initial_query 冲突"
                    "（search_intent 非空时 initial_query 须为空或等于首 seed）")

    def effective_seeds(self) -> tuple:
        """A2f 检索 seed 序列（带 target）。search_intent 非空 → 其 seeds；否则空。

        legacy `initial_query` 路径不经此方法（Loop 保留原 `[initial_query]` 行为，零回归）。
        """
        si = self.search_intent
        return tuple(si.seeds) if si is not None and getattr(si, "seeds", None) else ()


@dataclass(frozen=True)
class LoopResult:
    batch: RetrievalBatch
    gaps: tuple[QuotaGap, ...]
    unguaranteeable: tuple[str, ...]
    stop_reason: StopReason
    diagnostics: dict = field(default_factory=dict)


def _has_lower_bound(quota: QuotaSpec) -> bool:
    """配额是否含下界目标（min/exact/range）。只含 MAX → 初始即满足，不启动 Loop。"""
    for c in quota.count_constraints:
        if c.operator in (CountOperator.MIN, CountOperator.EXACT, CountOperator.RANGE):
            return True
    return False


def _countable_records(batch: RetrievalBatch, scope, ledger) -> list:
    """拟提交后的去重记录视图（D12）：ACCEPT_NEW/MERGE→proposed、REUSE→Ledger（按 scope）。

    仅计入 strict verified（严格投影，D12）。
    """
    rid_set = set(batch.counted_reference_ids(scope))
    seen: set[str] = set()
    records = []
    for it in batch.items:
        rid = it.target_reference_id
        if not rid or rid not in rid_set or rid in seen:
            continue
        if it.disposition in (BatchDisposition.ACCEPT_NEW, BatchDisposition.MERGE_DUPLICATE):
            if it.proposed_record_snapshot is None:
                continue
            rec = record_from_dict(json.loads(it.proposed_record_snapshot.canonical_record_payload))
        elif it.disposition is BatchDisposition.REUSE_EXISTING:
            rec = ledger.get(rid)
        else:
            continue
        if rec is None:
            continue
        if status_under_policy(rec.verification_events, VerificationPolicy.STRICT_FINAL_V1) \
                is not VerificationStatus.VERIFIED:
            continue
        seen.add(rid)
        records.append(rec)
    return records


def _evaluate_gaps(batch, quota, ledger, thresholds, policy):
    records = _countable_records(batch, quota.scope, ledger)
    counts = count_by_dimension(
        records, quota,
        classifier=lambda r: classify_record_language(r, thresholds),
        policy=policy,
    )
    gaps = evaluate_constraints(counts, quota.count_constraints)
    counted = tuple(sorted(r.reference_id for r in records))
    return gaps, counted


def _sig(counted, gaps) -> tuple:
    return (
        frozenset(counted),
        tuple(sorted((g.dimension.value, g.direction.value, g.amount) for g in gaps)),
    )


def _query_hash(query: str) -> str:
    """query 的 trace 标识。`RETRIEVAL_START` 与 `_trace_record` 必须共用它，
    否则同一次 attempt 的两个事件带不同 hash，无法关联。"""
    return hashlib.sha1(query.encode("utf-8")).hexdigest()[:12]


def _trace_record(query, res, ledger, gaps) -> dict:
    """隐私安全 trace（D15）：只 hash/计数/预算，不落原始 query/候选文本。"""
    throughput = getattr(res, "throughput", None)
    return {
        "query_hash": _query_hash(query),
        "query_len": len(query),
        "candidates": res.stats.candidates,
        "accept_new": res.stats.accept_new,
        "reuse": res.stats.reuse,
        "merge": res.stats.merge,
        "reject": res.stats.reject,
        "failed_sources": res.stats.failed_sources,
        "relevance_calls": res.stats.relevance_calls,
        "relevance_items": getattr(throughput, "relevance_items", 0),
        "relevance_windows": getattr(throughput, "relevance_windows", 0),
        "relevance_llm_calls": getattr(throughput, "relevance_llm_calls", 0),
        "budget_stopped": res.stats.budget_stopped,
        "verify_calls": ledger.verify_calls,
        "search_calls": ledger.search_calls,
        "iterations": ledger.iterations,
        "elapsed_s": round(ledger.elapsed_s, 3),
        "gap_dims": [g.dimension.value for g in gaps],
    }


def _prioritize_seeds(seeds, gaps):
    """seed 排序：命中非零 gap 的 target > ANY > 已满足维度 target；同级保用户确认序。"""
    gap_dims = {g.dimension.value for g in gaps}

    def _rank(seed) -> int:
        t = seed.target.value
        if t == "any":
            return 1
        return 0 if t in gap_dims else 2

    return sorted(seeds, key=_rank)


def run_retrieval_loop(
    request: RetrievalRequest,
    *,
    deps: AttemptDeps,
    limits: RetrievalLimits,
    planner: QueryPlanner,
    clock: Callable[[], float] = time.monotonic,
    cancel_token: CancelToken | None = None,
    trace_sink: Callable[[dict], None] | None = None,
    batch: RetrievalBatch | None = None,
    search_limit: int = 10,
    budget_ledger: BudgetLedger | None = None,
) -> LoopResult:
    quota = request.quota
    criteria = request.criteria
    if batch is None:
        # A2f：batch 身份用 UUID（不再拿 query hash 充当身份，避免同查询任务撞 id）。
        batch = RetrievalBatch(
            batch_id=f"batch_{uuid.uuid4().hex[:12]}",
            task_fingerprint=request.task_fingerprint,
            quota=quota, verification_policy=VerificationPolicy.STRICT_FINAL_V1,
        )

    # 只含 MAX 的配额初始即满足，不启动无意义 Loop
    if not _has_lower_bound(quota):
        _finalize(batch)
        return LoopResult(batch, (), (), StopReason.QUOTA_SATISFIED,
                          {"reason": "no_lower_bound"})

    # A2f：Loop 内 LLM/检索调用与外部（合成/planner）共用同一预算（传入即复用）。
    ledger_budget = budget_ledger if budget_ledger is not None else BudgetLedger(limits, clock=clock)
    attempt_deps = replace(
        deps,
        budget=ledger_budget.verify_budget_view(),
        budget_ledger=ledger_budget,
    )
    source_names = tuple(np.name for np in deps.registry.search_sources)
    thresholds = deps.language_thresholds
    lang_policy = deps.language_policy
    seeds = request.effective_seeds()          # A2f：非空 → 自适应 seed 阶段

    gaps: tuple[QuotaGap, ...] = ()
    counted: tuple = ()
    tried_queries: list[str] = []
    prev_sig = None
    no_progress = 0
    stop: StopReason | None = None

    def _run_one(q: str) -> object:
        """跑一次 attempt + 记账 + trace；返回 AttemptResult。

        观测：每次 attempt 一个 `retrieval_attempt:<n>` span，并把已有的
        `_trace_record`（隐私安全：只 hash/计数/预算）同时发到 ambient bus。
        此前 `trace_sink` 只有 dogfood runner 注入，生产路径恒为 None ⇒
        `RETRIEVAL_START/END` 两个 EventKind 定义了却全仓库无人发布，
        检索这段在 trace.json 里完全不可见。
        """
        attempt_no = ledger_budget.search_calls + 1
        ledger_budget.charge_search()
        with ambient_span(f"retrieval_attempt:{attempt_no}"):
            ambient_publish(
                "RETRIEVAL_START",
                # 与 `_trace_record` 同一套 hash（同函数、同长度）：否则同一 query
                # 在 START/END 上出两个不同 hash，correlate 不起来。
                query_hash=_query_hash(q),
                query_len=len(q),
                attempt=attempt_no,
                search_limit=search_limit,
            )
            res = run_attempt(q, criteria, quota, batch, attempt_deps, limit=search_limit)
            ledger_budget.charge_candidates(res.stats.candidates)
            if res.throughput is None:
                # Legacy relevance is charged after the logical per-item calls.
                # B1/B2 settle or charge calls inside their staged paths.
                ledger_budget.charge_llm(res.stats.relevance_calls)
            tried_queries.append(q)
            record = _trace_record(q, res, ledger_budget, gaps)
            if trace_sink is not None:
                trace_sink(record)
            ambient_publish("RETRIEVAL_END", **record)
        return res

    # ---- A2f 自适应 seed 阶段（仅当 search_intent 提供 seeds）：逐 attempt 重算、满足即停 ----
    if seeds:
        gaps, counted = _evaluate_gaps(batch, quota, deps.ledger, thresholds, lang_policy)
        if not gaps:
            stop = StopReason.QUOTA_SATISFIED
        else:
            ledger_budget.charge_iteration()
            for seed in _prioritize_seeds(seeds, gaps):
                if cancel_token is not None and cancel_token.cancelled:
                    stop = StopReason.CANCELLED
                    break
                if not ledger_budget.call_budget_ok():
                    stop = StopReason.BUDGET_EXHAUSTED
                    break
                gap_dims = {g.dimension.value for g in gaps}
                if seed.target.value != "any" and seed.target.value not in gap_dims:
                    continue                       # 该 target 维度已满足 → 跳过，不浪费 search
                if seed.text in tried_queries:
                    continue
                _run_one(seed.text)
                gaps, counted = _evaluate_gaps(batch, quota, deps.ledger, thresholds, lang_policy)
                if not gaps:
                    stop = StopReason.QUOTA_SATISFIED
                    break

    # ---- planner 续搜（legacy 首轮 [initial_query] 亦走此循环，行为逐字节不变）----
    while stop is None:
        if cancel_token is not None and cancel_token.cancelled:
            stop = StopReason.CANCELLED
            break
        if not ledger_budget.can_continue():
            stop = StopReason.BUDGET_EXHAUSTED
            break

        # 首轮（无 seed 阶段、无 planner 历史）用初始查询；否则 planner 据 gaps 提出
        if not tried_queries and not seeds:
            queries = [request.initial_query]
        else:
            req = PlannerRequest(
                initial_query=request.initial_query,
                topic=getattr(criteria, "topic", ""),
                gap_dimensions=tuple(g.dimension.value for g in gaps),
                tried_queries=tuple(tried_queries),
                available_source_names=source_names,
            )
            plan = planner.propose(req)
            if plan.llm_called:                    # A2f：只有真发了模型调用才计费
                ledger_budget.charge_llm()
            if plan.is_empty:
                plan = DeterministicQueryPlanner().propose(req)   # fallback，不当「已无文献」
            queries = [q for q in plan.queries if q not in tried_queries]
            if not queries:
                stop = StopReason.SOURCE_EXHAUSTED
                break

        ledger_budget.charge_iteration()
        ran_any = False
        round_all_unavailable = True
        for q in queries:
            if not ledger_budget.call_budget_ok():
                break
            res = _run_one(q)
            ran_any = True
            if not (res.stats.failed_sources > 0 and res.stats.candidates == 0):
                round_all_unavailable = False

        gaps, counted = _evaluate_gaps(batch, quota, deps.ledger, thresholds, lang_policy)
        if not gaps:
            stop = StopReason.QUOTA_SATISFIED
            break
        if ran_any and round_all_unavailable:
            stop = StopReason.ALL_SOURCES_UNAVAILABLE
            break

        sig = _sig(counted, gaps)
        if sig == prev_sig:
            no_progress += 1
            if no_progress >= limits.max_no_progress_rounds:
                stop = StopReason.NO_PROGRESS
                break
        else:
            no_progress = 0
        prev_sig = sig

    _finalize(batch)
    unguaranteeable = _unguaranteeable(stop, gaps)
    diagnostics = {
        "stop_reason": stop.value,
        "iterations": ledger_budget.iterations,
        "search_calls": ledger_budget.search_calls,
        "verify_calls": ledger_budget.verify_calls,
        "llm_calls": ledger_budget.llm_calls,
        "candidates": ledger_budget.candidates,
        "elapsed_s": round(ledger_budget.elapsed_s, 3),
        "exhausted_dimensions": list(ledger_budget.exhausted_dimensions()),
        "tried_queries": len(tried_queries),
    }
    return LoopResult(batch, gaps, unguaranteeable, stop, diagnostics)


def _finalize(batch: RetrievalBatch) -> None:
    """把 batch 收束为可审计 STAGED（正常停止/取消/预算触顶都收束）。"""
    if batch.status is BatchStatus.RUNNING:
        batch.stage()


def _unguaranteeable(stop, gaps) -> tuple[str, ...]:
    """受源覆盖不足无法保证的维度（10.5）：非满足停止时，仍有 gap 的语言维度。"""
    if stop is StopReason.QUOTA_SATISFIED:
        return ()
    return tuple(
        g.dimension.value for g in gaps if g.dimension.value.startswith("language.")
    )


__all__ = [
    "RetrievalRequest",
    "LoopResult",
    "run_retrieval_loop",
]
