"""文献检索主流程：检索 → 精确去重/资格过滤 → 严格核验 → 相关性评审 → 结果归并。

单篇与批量评审共用候选循环和结果归并，只替换评审策略与缓存身份。
本模块生成拟提交的 BatchItem，不修改主 Ledger；管线重排仅供独立评测使用。
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass

from paper_agent.literature import dedup as _dedup
from paper_agent.literature import identifiers as _ids
from paper_agent.literature.bibkey import make_bibkey
from paper_agent.literature.language import (
    DEFAULT_THRESHOLDS,
    LanguageThresholds,
    classify_record_language,
)
from paper_agent.literature.migration import candidate_to_record
from paper_agent.literature.models import ReferenceRecord
from paper_agent.literature.quota import passes_all_filters
from paper_agent.literature.relevance import (
    STRICT_ACCEPTANCE,
    WEAKLY_ACCEPTANCE,
    RelevanceAcceptancePolicy,
    acceptance_for_purpose,
)
from paper_agent.literature.retrieval.batch import (
    BatchDisposition,
    BatchItem,
    CandidateSnapshot,
    ProposedRecordSnapshot,
)
from paper_agent.providers.retrieval.base import RetrievalError


# ---------------------------------------------------------------- 精确去重分类

@dataclass(frozen=True)
class ExactMatch:
    """确定性精确去重结果。kind ∈ none | reuse | merge | batch_duplicate。"""

    kind: str
    target_reference_id: str = ""
    merged_record: ReferenceRecord | None = None       # merge 时的合并后记录
    expected_base_hash: str | None = None              # merge 时目标当前哈希


def _work_key_set(record: ReferenceRecord) -> set[tuple[str, str]]:
    return _ids.work_keys(record.work_identifiers)


def classify_exact(
    candidate_record: ReferenceRecord,
    *,
    ledger,
    accepted_records: tuple[ReferenceRecord, ...] = (),
) -> ExactMatch:
    """两阶段确定性精确去重：先批内已接受，再对 Ledger（仅 high 置信 work-id 命中）。

    - 批内已 ACCEPT_NEW 有 work-key 同值 → ``batch_duplicate``（不产第二个 ACCEPT_NEW）。
    - Ledger high 命中：合并后记录 == 现有 → ``reuse``；否则 ``merge``（附 expected_base_hash
      = 现有记录当前哈希 + 合并后记录）。
    - 无命中 → ``none``。
    """
    cand_keys = _work_key_set(candidate_record)
    if cand_keys:
        for rec in accepted_records:
            if cand_keys & _work_key_set(rec):
                return ExactMatch("batch_duplicate", target_reference_id=rec.reference_id)

    match = _dedup.find_duplicate(candidate_record, ledger.all_records(), allow_low=False)
    if match is None or match.confidence != "high":
        return ExactMatch("none")

    existing = ledger.get(match.reference_id)
    if existing is None:
        return ExactMatch("none")
    merged = _dedup.merge_records(existing, candidate_record).record
    if merged == existing:
        return ExactMatch("reuse", target_reference_id=existing.reference_id)
    return ExactMatch(
        "merge",
        target_reference_id=existing.reference_id,
        merged_record=merged,
        expected_base_hash=ProposedRecordSnapshot.hash_of(existing),
    )


# ---------------------------------------------------------------- 拟提交记录构造

def build_accept_record(
    candidate_record: ReferenceRecord,
    *,
    events: tuple,
    ledger,
    numeric_alias: str,
    taken_bibkeys: set[str],
) -> ReferenceRecord:
    """为 ACCEPT_NEW 构造拟提交记录：挂核验事件 + 分配 bibkey / 数字别名。

    只**读**取 ledger 的既有键做冲突规避，**不 mutate** 主 Ledger。
    """
    rec = candidate_record.with_changes(
        verification_events=(*candidate_record.verification_events, *events)
    )
    changes: dict = {}
    if not rec.bibkey:
        changes["bibkey"] = make_bibkey(rec, taken_bibkeys)
    if not rec.numeric_aliases:
        changes["numeric_aliases"] = (numeric_alias,)
    return rec.with_changes(**changes) if changes else rec


def proposed_from_record(record: ReferenceRecord, *, expected_base_hash: str | None) -> ProposedRecordSnapshot:
    return ProposedRecordSnapshot(
        target_reference_id=record.reference_id,
        expected_base_hash=expected_base_hash,
        canonical_record_payload=ProposedRecordSnapshot.payload_of(record),
    )


@dataclass
class AttemptDeps:
    """run_attempt 的注入依赖（组合层组装；不读全局 config/时钟）。"""

    registry: object                       # ProviderRegistry
    ledger: object                         # ReferenceLedger（只读）
    cross_verifier: object                 # A2c-1 CrossVerifier
    judge: object                          # A2a RelevanceJudge
    relevance_store: object                # A2a RelevanceStore
    budget: object                         # A2c-1 VerifyBudget
    verify_cache: object                   # A2c-1 VerificationCache
    acceptance_policy: RelevanceAcceptancePolicy = STRICT_ACCEPTANCE
    language_thresholds: LanguageThresholds = DEFAULT_THRESHOLDS
    language_policy: str = "high_only"
    active_relevance_runtime: object | None = None
    budget_ledger: object | None = None
    pipeline_reorder_enabled: bool = False


@dataclass(frozen=True)
class AttemptStats:
    candidates: int = 0
    accept_new: int = 0
    reuse: int = 0
    merge: int = 0
    reject: int = 0
    failed_sources: int = 0
    relevance_calls: int = 0
    budget_stopped: bool = False       # True → 轮内预算耗尽，剩余候选未处理


@dataclass(frozen=True)
class ThroughputAttemptStats:
    relevance_items: int = 0
    relevance_windows: int = 0
    relevance_llm_calls: int = 0


@dataclass(frozen=True)
class AttemptResult:
    items: tuple[BatchItem, ...]
    stats: AttemptStats
    throughput: ThroughputAttemptStats | None = None


def _new_item_id() -> str:
    return f"it_{_uuid.uuid4().hex[:10]}"


def _search(registry, query: str, limit: int) -> tuple[list, int]:
    """汇总 search_sources 的候选；单源失败跳过并计数（Req 11.3）。"""
    out: list = []
    failed = 0
    for np in registry.search_sources:
        try:
            out.extend(np.provider.search(query, limit=limit))
        except RetrievalError:
            failed += 1
    return out, failed


@dataclass(frozen=True)
class _PendingCandidate:
    ordinal: int
    candidate: object
    snapshot: CandidateSnapshot
    language: object
    candidate_record: ReferenceRecord
    exact: ExactMatch
    verification: object | None
    window_item: object
    work_keys: frozenset[tuple[str, str]]


def run_attempt(
    query: str,
    criteria,
    quota,
    batch,
    deps: AttemptDeps,
    *,
    limit: int = 10,
) -> AttemptResult:
    """按稳定输入序核验、评审并归并候选。

    未注入批量 runtime 时，使用原单篇 judge 和缓存键立即评审。
    批量模式只改变评审窗口；遇到待评审的同身份候选，先归并前窗再去重。
    """
    if deps.pipeline_reorder_enabled:
        raise ValueError(
            "Pipeline reordering is experimental; use "
            "tests.eval.retrieval_throughput.experimental_attempt."
            "run_experimental_attempt instead"
        )

    runtime = deps.active_relevance_runtime
    budget_ledger = deps.budget_ledger
    call_control = None
    if runtime is not None:
        from paper_agent.literature.retrieval.call_control import ExternalCallControl

        if budget_ledger is None:
            raise ValueError("Batch relevance runtime requires the shared BudgetLedger")
        call_control = ExternalCallControl(budget_ledger)
    ledger = deps.ledger
    thresholds = deps.language_thresholds
    language_policy = deps.language_policy

    def _classifier(record):
        return classify_record_language(record, thresholds)

    candidates, failed = _search(deps.registry, query, limit)
    ordered_items: list[tuple[int, BatchItem]] = []
    accepted_records: list[ReferenceRecord] = []
    pending: list[_PendingCandidate] = []
    stats = {
        "accept_new": 0,
        "reuse": 0,
        "merge": 0,
        "reject": 0,
        "relevance_calls": 0,
        "relevance_items": 0,
        "relevance_windows": 0,
        "relevance_llm_calls": 0,
    }

    try:
        numeric_base = int(ledger.next_numeric_alias())
    except (ValueError, TypeError):
        numeric_base = 1
    taken_bibkeys = set(ledger.resolvable_bibkeys())

    def _reject(snap, reasons, *, language=None, relevance=None) -> BatchItem:
        stats["reject"] += 1
        return BatchItem(
            _new_item_id(),
            snap,
            BatchDisposition.REJECT,
            language_verdict=language,
            relevance=relevance,
            rejection_reasons=tuple(reasons),
        )

    def _emit(ordinal: int, item: BatchItem) -> None:
        ordered_items.append((ordinal, item))

    def _remember_success(rel) -> None:
        if rel.status != "SUCCESS":
            return
        merge_put = getattr(deps.relevance_store, "merge_put", None)
        if merge_put is not None:
            merge_put(rel)
        else:
            deps.relevance_store.put(rel)

    def _apply(item: _PendingCandidate, rel) -> None:
        if not deps.acceptance_policy.is_accepted(rel):
            reason = f"relevance:{rel.verdict or rel.error_kind or 'unrelated'}"
            _emit(
                item.ordinal,
                _reject(
                    item.snapshot,
                    (reason,),
                    language=item.language,
                    relevance=rel,
                ),
            )
            return

        exact = item.exact
        if exact.kind == "reuse":
            stats["reuse"] += 1
            _emit(
                item.ordinal,
                BatchItem(
                    _new_item_id(),
                    item.snapshot,
                    BatchDisposition.REUSE_EXISTING,
                    target_reference_id=exact.target_reference_id,
                    language_verdict=item.language,
                    relevance=rel,
                ),
            )
        elif exact.kind == "merge":
            stats["merge"] += 1
            proposed = proposed_from_record(
                exact.merged_record, expected_base_hash=exact.expected_base_hash
            )
            _emit(
                item.ordinal,
                BatchItem(
                    _new_item_id(),
                    item.snapshot,
                    BatchDisposition.MERGE_DUPLICATE,
                    proposed_record_snapshot=proposed,
                    target_reference_id=exact.target_reference_id,
                    language_verdict=item.language,
                    relevance=rel,
                    verification_events=exact.merged_record.verification_events,
                ),
            )
        else:
            numeric = str(numeric_base + stats["accept_new"])
            events = (
                tuple(item.verification.events)
                if item.verification is not None
                else ()
            )
            record = build_accept_record(
                item.candidate_record,
                events=events,
                ledger=ledger,
                numeric_alias=numeric,
                taken_bibkeys=taken_bibkeys,
            )
            taken_bibkeys.add(record.bibkey)
            accepted_records.append(record)
            stats["accept_new"] += 1
            _emit(
                item.ordinal,
                BatchItem(
                    _new_item_id(),
                    item.snapshot,
                    BatchDisposition.ACCEPT_NEW,
                    proposed_record_snapshot=proposed_from_record(
                        record, expected_base_hash=None
                    ),
                    target_reference_id=record.reference_id,
                    language_verdict=item.language,
                    relevance=rel,
                    verification_events=events,
                ),
            )

    def _flush() -> None:
        nonlocal budget_stopped
        if not pending:
            return
        assert runtime is not None
        frozen_pending = tuple(pending)
        windows = runtime.pack(
            [item.window_item for item in frozen_pending], criteria
        )
        by_key = {item.window_item.candidate_key: item for item in frozen_pending}
        for window in windows:
            # 开窗前先看预算：预算耗尽时 `BudgetAwareLLMProvider` 会抛
            # `ExternalCallRejected`，而 `structured_call` 的宽 `except Exception`
            # 会把它压成 `PROVIDER_FAILED` ⇒ **整窗** batch_size 条被判 ERROR、
            # 落成 `relevance:provider_failed`（谎报「模型失败」，实为预算停止）。
            # 单篇路径下这只损失 1 条，size=10 时损失 10 条 —— 爆炸半径随 size 放大。
            # 未评审的候选显式记录预算停止，不进入 relevance_store。
            if _expensive_budget_exhausted():
                budget_stopped = True
                # 未判定的候选必须显式落 REJECT：`pending.clear()` 会把它们
                # 一并丢掉 ⇒ 连 disposition 都不产生（candidates 计数与处置
                # 分布对不上，比理由写错更难查）。
                for window_item in window.items:
                    item = by_key[window_item.candidate_key]
                    _emit(
                        item.ordinal,
                        _reject(
                            item.snapshot,
                            ("relevance:budget_exhausted",),
                            language=item.language,
                        ),
                    )
                continue
            assessments, batch_stats = runtime.assess_window_with_stats(
                window, criteria, call_control=call_control
            )
            stats["relevance_windows"] += 1
            stats["relevance_items"] += len(window.items)
            physical_calls = (
                batch_stats.first_pass_llm_calls + batch_stats.retry_llm_calls
            )
            stats["relevance_llm_calls"] += physical_calls
            stats["relevance_calls"] += physical_calls
            for window_item in window.items:
                rel = assessments[window_item.candidate_key]
                _remember_success(rel)
                _apply(by_key[window_item.candidate_key], rel)
        pending.clear()

    def _expensive_budget_exhausted() -> bool:
        if runtime is None:
            return deps.budget.exhausted
        dimensions = set(budget_ledger.exhausted_dimensions())
        return bool(
            dimensions
            & {
                "max_verify_calls",
                "max_llm_calls",
                "max_provider_calls",
                "provider_time_budget_s",
            }
        )

    budget_stopped = False
    stop_processing = False
    task_fp = criteria.fingerprint() if runtime is not None else None

    for ordinal, cand in enumerate(candidates):
        while True:
            # 1. 预算检查与候选快照。
            if _expensive_budget_exhausted():
                _flush()
                budget_stopped = True
                stop_processing = True
                break

            snap = CandidateSnapshot.from_candidate(cand)
            cand_record = candidate_to_record(cand)
            language = classify_record_language(cand_record, thresholds)
            work_keys = frozenset(_work_key_set(cand_record))

            # 2. 先归并同身份的前窗，再检查精确重复与资格。
            if pending and work_keys and any(
                work_keys & item.work_keys for item in pending
            ):
                _flush()
                continue

            exact = classify_exact(
                cand_record,
                ledger=ledger,
                accepted_records=tuple(accepted_records),
            )
            if exact.kind == "batch_duplicate":
                _emit(
                    ordinal,
                    _reject(snap, ("batch_duplicate",), language=language),
                )
                break

            record_for_filter = exact.merged_record or cand_record
            if not passes_all_filters(
                record_for_filter,
                quota.eligibility_filters,
                classifier=_classifier,
                policy=language_policy,
            ):
                _emit(
                    ordinal,
                    _reject(snap, ("filter",), language=language),
                )
                break

            resolved_rid = exact.target_reference_id or cand_record.reference_id
            window_item = None
            if runtime is not None:
                window_item = runtime.make_window_item(
                    candidate_key=f"c{ordinal}",
                    reference_id=resolved_rid,
                    title=cand_record.title,
                    abstract=cand_record.abstract,
                )

                # 在严格核验前决定窗口容量，避免为下一窗口提前消耗预算。
                trial = runtime.pack(
                    [*(item.window_item for item in pending), window_item], criteria
                )
                if pending and len(trial) > 1:
                    _flush()
                    continue

            # 3. 仅核验尚未在 Ledger 命中的新候选。
            verification = None
            if exact.kind == "none":
                verification = deps.cross_verifier.assess(
                    cand, budget=deps.budget, cache=deps.verify_cache
                )
                if not verification.meets_strict_threshold:
                    _emit(
                        ordinal,
                        _reject(
                            snap,
                            (f"verify:{verification.strict_status.value}",),
                            language=language,
                        ),
                    )
                    break

            # 4. 单篇立即评审，批量缓存命中立即复用，其余进入有界窗口。
            item = _PendingCandidate(
                ordinal, cand, snap, language, cand_record, exact,
                verification, window_item, work_keys,
            )
            if runtime is None:
                rel = _assess_relevance(
                    cand, criteria, deps, reference_id=resolved_rid
                )
                stats["relevance_calls"] += 1
                _apply(item, rel)
                break

            cached = deps.relevance_store.get(
                resolved_rid, task_fp, window_item.content_hash, runtime.judge_key,
            )
            if cached is not None and cached.status == "SUCCESS":
                _flush()
                _apply(item, cached)
                break

            pending.append(item)
            if len(pending) >= runtime.window_limits.batch_size:
                _flush()
            break

        if stop_processing:
            break

    if pending:
        _flush()

    # 5. 按输入序提交本轮结果；批量策略的物理调用已在窗口内记账。
    new_items = tuple(item for _, item in sorted(ordered_items, key=lambda pair: pair[0]))
    batch.items = (*batch.items, *new_items)
    result_stats = AttemptStats(
        candidates=len(candidates),
        accept_new=stats["accept_new"],
        reuse=stats["reuse"],
        merge=stats["merge"],
        reject=stats["reject"],
        failed_sources=failed,
        relevance_calls=stats["relevance_calls"],
        budget_stopped=budget_stopped,
    )
    return AttemptResult(
        items=new_items,
        stats=result_stats,
        throughput=ThroughputAttemptStats(
            relevance_items=stats["relevance_items"],
            relevance_windows=stats["relevance_windows"],
            relevance_llm_calls=stats["relevance_llm_calls"],
        ) if runtime is not None else None,
    )


def _assess_relevance(cand, criteria, deps, *, reference_id: str = ""):
    """查 store（content_hash + judge_key + 当前 task_fp）→ 命中复用，否则评审并存储。

    ``reference_id``：入键用的**持久记录身份**（由调用方按 disposition 解析）。留空时
    退回候选自身 id（向后兼容）。评审结果统一改写 ``reference_id`` 后入库,使入库记录
    在 preflight/build_bundle 按 ``record.reference_id`` 可命中同一结论。
    """
    import dataclasses

    judge = deps.judge
    rid = reference_id or getattr(cand, "reference_id", "") or getattr(cand, "id", "")
    task_fp = criteria.fingerprint()
    _, _, chash, _ = judge.content_hash(
        getattr(cand, "title", ""), getattr(cand, "abstract", "")
    )
    cached = deps.relevance_store.get(rid, task_fp, chash, judge.judge_version.key())
    if cached is not None:
        return cached
    rel = judge.assess(cand, criteria)
    if rel.reference_id != rid:
        rel = dataclasses.replace(rel, reference_id=rid)
    deps.relevance_store.put(rel)
    return rel


__all__ = [
    "RelevanceAcceptancePolicy",
    "STRICT_ACCEPTANCE",
    "WEAKLY_ACCEPTANCE",
    "acceptance_for_purpose",
    "ExactMatch",
    "classify_exact",
    "build_accept_record",
    "proposed_from_record",
    "candidate_to_record",
    "AttemptDeps",
    "AttemptStats",
    "ThroughputAttemptStats",
    "AttemptResult",
    "run_attempt",
]
