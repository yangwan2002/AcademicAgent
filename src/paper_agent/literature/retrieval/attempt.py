"""单轮 Retrieval_Attempt 流水线（A2c-3，design D9）。

顺序**先便宜后昂贵、保守不误删**：
SEARCH → 清洗/快照(A2a/A2c-2) → 语言(A2a) → 精确去重(A1) → 资格预筛(A2b filter)
→ 严格核验(A2c-1 CrossVerifier) → 相关性(A2a) → BatchItem(disposition + ProposedRecordSnapshot)。

纪律：核验前只做确定性精确去重；模糊相似度推迟到核验后；只对 strict verified 候选调
相关性 Reviewer；被各阶段拒绝的候选保留 BatchItem + 原因（不静默丢）；配额只由已接受
条目推进；候选文本经 A2a 清洗后才进 LLM。不 import ``tasking``、不读全局 config/时钟（注入）。

本文件 Task 1 部分：接受策略 / 拟提交记录构造 / 两阶段精确去重分类。
``run_attempt`` 主流水线（Task 2）在本模块后续补入。
"""

from __future__ import annotations

from dataclasses import dataclass

from paper_agent.literature import dedup as _dedup
from paper_agent.literature import identifiers as _ids
from paper_agent.literature.bibkey import make_bibkey
from paper_agent.literature.migration import candidate_to_record
from paper_agent.literature.models import ReferenceRecord
from paper_agent.literature.retrieval.batch import (
    BatchDisposition,
    ProposedRecordSnapshot,
)


# ---------------------------------------------------------------- 接受策略

# **已迁至 `literature/relevance.py`**（F-3）：接受语义原先散落三处，其中
# `preflight` 与 `evidence` 硬编码 `("relevant",)`，导致「也收 weakly」只改摄入
# 不改消费。现统一定义在判定词汇的定义地，此处 re-export 保持既有导入不破。
from paper_agent.literature.relevance import (
    STRICT_ACCEPTANCE,
    WEAKLY_ACCEPTANCE,
    RelevanceAcceptancePolicy,
    acceptance_for_purpose,
)


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


__all__ = [
    "RelevanceAcceptancePolicy",
    "STRICT_ACCEPTANCE",
    "WEAKLY_ACCEPTANCE",
    "acceptance_for_purpose",
    "ExactMatch",
    "classify_exact",
    "build_accept_record",
    "proposed_from_record",
    "candidate_to_record",           # re-export（Task 2 / 测试便用）
]


# ===========================================================================
# Task 2：单轮 Attempt 流水线 run_attempt
# ===========================================================================

import uuid as _uuid

from paper_agent.literature.language import (
    DEFAULT_THRESHOLDS,
    LanguageThresholds,
    classify_record_language,
)
from paper_agent.literature.quota import passes_all_filters
from paper_agent.literature.retrieval.batch import BatchItem, CandidateSnapshot
from paper_agent.providers.retrieval.base import RetrievalError


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


def _run_attempt_legacy(
    query: str,
    criteria,
    quota,
    batch,
    deps: AttemptDeps,
    *,
    limit: int = 10,
) -> AttemptResult:
    """执行一轮 Attempt，把新 BatchItem 追加进 ``batch``（RUNNING），返回结果 + 统计。

    不改状态机、不落盘、不 mutate 主 Ledger（去重/构造只读 Ledger）。
    """
    ledger = deps.ledger
    thresholds = deps.language_thresholds
    policy = deps.language_policy

    def _classifier(record):
        return classify_record_language(record, thresholds)

    candidates, failed = _search(deps.registry, query, limit)
    new_items: list[BatchItem] = []
    accepted_records: list[ReferenceRecord] = []
    stats = {"accept_new": 0, "reuse": 0, "merge": 0, "reject": 0, "relevance_calls": 0}

    # 数字别名基线（只读 Ledger + 本轮已接受计数，避免批内碰撞）
    try:
        numeric_base = int(ledger.next_numeric_alias())
    except (ValueError, TypeError):
        numeric_base = 1
    taken_bibkeys = set(ledger.resolvable_bibkeys())

    def _reject(snap, reasons, *, language=None, relevance=None) -> BatchItem:
        stats["reject"] += 1
        return BatchItem(
            _new_item_id(), snap, BatchDisposition.REJECT,
            language_verdict=language, relevance=relevance,
            rejection_reasons=tuple(reasons),
        )

    budget_stopped = False
    for cand in candidates:
        # 轮内预算守卫（A2f 吞吐修复）：核验 + 相关性是 attempt 内最贵的**串行**操作。
        # 一轮多源可取回数十候选，若不在候选粒度检查预算，单个 attempt 会冲过 provider
        # 时间预算/调用上限而「停不下来」（时间预算此前只在每轮迭代开始检查）。预算耗尽
        # （含 provider_time_budget_s / max_provider_calls / max_verify_calls）即停止处理
        # 剩余候选。预算充足时逐字节不变（零回归）。
        if deps.budget.exhausted:
            budget_stopped = True
            break
        snap = CandidateSnapshot.from_candidate(cand)
        language = classify_record_language(candidate_to_record(cand), thresholds)
        cand_record = candidate_to_record(cand)

        exact = classify_exact(
            cand_record, ledger=ledger, accepted_records=tuple(accepted_records)
        )
        if exact.kind == "batch_duplicate":
            new_items.append(_reject(snap, ("batch_duplicate",), language=language))
            continue

        # 资格预筛（明确不满足 → REJECT，保留 item）
        record_for_filter = exact.merged_record or cand_record
        if not passes_all_filters(
            record_for_filter, quota.eligibility_filters,
            classifier=_classifier, policy=policy,
        ):
            new_items.append(_reject(snap, ("filter",), language=language))
            continue

        # 严格核验：仅对 Ledger 无命中的新候选（复用不再核验，先便宜后昂贵）
        assessment = None
        if exact.kind == "none":
            assessment = deps.cross_verifier.assess(
                cand, budget=deps.budget, cache=deps.verify_cache
            )
            if not assessment.meets_strict_threshold:
                new_items.append(_reject(
                    snap, (f"verify:{assessment.strict_status.value}",), language=language
                ))
                continue

        # 相关性（对将计入的候选，当前 TaskFingerprint；REUSE/MERGE 亦需，Req 7.4）。
        # **按解析出的持久 reference_id 入键**（reuse/merge 用现有记录 id，accept_new 用
        # 候选记录的确定性 id），而非候选的 transient id——否则入库后 preflight/build_bundle
        # 按 record.reference_id 查不到,证据集恒空（A2c→A2d seam）。
        resolved_rid = exact.target_reference_id or cand_record.reference_id
        rel = _assess_relevance(cand, criteria, deps, reference_id=resolved_rid)
        stats["relevance_calls"] += 1
        if not deps.acceptance_policy.is_accepted(rel):
            reason = f"relevance:{rel.verdict or rel.error_kind or 'unrelated'}"
            new_items.append(_reject(snap, (reason,), language=language, relevance=rel))
            continue

        # disposition 落定
        if exact.kind == "reuse":
            stats["reuse"] += 1
            new_items.append(BatchItem(
                _new_item_id(), snap, BatchDisposition.REUSE_EXISTING,
                target_reference_id=exact.target_reference_id,
                language_verdict=language, relevance=rel,
            ))
        elif exact.kind == "merge":
            stats["merge"] += 1
            proposed = proposed_from_record(
                exact.merged_record, expected_base_hash=exact.expected_base_hash
            )
            new_items.append(BatchItem(
                _new_item_id(), snap, BatchDisposition.MERGE_DUPLICATE,
                proposed_record_snapshot=proposed,
                target_reference_id=exact.target_reference_id,
                language_verdict=language, relevance=rel,
                verification_events=exact.merged_record.verification_events,
            ))
        else:  # none → ACCEPT_NEW
            numeric = str(numeric_base + stats["accept_new"])
            events = tuple(assessment.events) if assessment is not None else ()
            record = build_accept_record(
                cand_record, events=events, ledger=ledger,
                numeric_alias=numeric, taken_bibkeys=taken_bibkeys,
            )
            taken_bibkeys.add(record.bibkey)
            accepted_records.append(record)
            stats["accept_new"] += 1
            new_items.append(BatchItem(
                _new_item_id(), snap, BatchDisposition.ACCEPT_NEW,
                proposed_record_snapshot=proposed_from_record(record, expected_base_hash=None),
                target_reference_id=record.reference_id,
                language_verdict=language, relevance=rel,
                verification_events=events,
            ))

    batch.items = (*batch.items, *tuple(new_items))
    result_stats = AttemptStats(
        candidates=len(candidates),
        accept_new=stats["accept_new"], reuse=stats["reuse"], merge=stats["merge"],
        reject=stats["reject"], failed_sources=failed, relevance_calls=stats["relevance_calls"],
        budget_stopped=budget_stopped,
    )
    return AttemptResult(items=tuple(new_items), stats=result_stats)


@dataclass(frozen=True)
class _B1Pending:
    ordinal: int
    candidate: object
    snapshot: CandidateSnapshot
    language: object
    candidate_record: ReferenceRecord
    exact: ExactMatch
    verification: object | None
    window_item: object
    work_keys: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class WorkItem:
    """Read-only B2 unit shared by judge, verify and the serial reducer."""

    ordinal: int
    candidate: object
    snapshot: CandidateSnapshot
    language: object
    candidate_record: ReferenceRecord
    relevance_subject: ReferenceRecord
    exact: ExactMatch
    resolved_reference_id: str
    content_hash: str
    group_key: str


@dataclass(frozen=True)
class _PreparedSeed:
    ordinal: int
    candidate: object
    snapshot: CandidateSnapshot
    candidate_record: ReferenceRecord
    exact: ExactMatch


def _metadata_completeness(record: ReferenceRecord) -> int:
    scalar_fields = (
        "title", "abstract", "reference_type", "container_title", "volume",
        "issue", "pages", "article_number", "publisher", "url",
        "conference_name",
    )
    return (
        sum(bool(getattr(record, name, "")) for name in scalar_fields)
        + int(bool(record.authors))
        + int(record.year is not None)
        + len(record.container_identifiers)
    )


def _prepare_b2(candidates, quota, deps: AttemptDeps):
    """Serial prepare: identity grouping, subject construction and filtering."""

    thresholds = deps.language_thresholds

    def classifier(record):
        return classify_record_language(record, thresholds)

    seeds = []
    for ordinal, candidate in enumerate(candidates):
        record = candidate_to_record(candidate)
        seeds.append(_PreparedSeed(
            ordinal=ordinal,
            candidate=candidate,
            snapshot=CandidateSnapshot.from_candidate(candidate),
            candidate_record=record,
            exact=classify_exact(record, ledger=deps.ledger),
        ))

    # Union only deterministic persistent identities: Ledger target or high
    # confidence work identifiers.  Low-confidence title similarity stays out.
    parent = list(range(len(seeds)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    token_owner: dict[tuple[str, str], int] = {}
    for index, seed in enumerate(seeds):
        tokens = set(_work_key_set(seed.candidate_record))
        if seed.exact.target_reference_id:
            tokens.add(("ledger", seed.exact.target_reference_id))
        for token in sorted(tokens):
            previous = token_owner.setdefault(token, index)
            union(index, previous)

    groups: dict[int, list[_PreparedSeed]] = {}
    for index, seed in enumerate(seeds):
        groups.setdefault(find(index), []).append(seed)

    work_items: list[WorkItem] = []
    pre_items: list[tuple[int, BatchItem]] = []
    for members in sorted(groups.values(), key=lambda rows: min(r.ordinal for r in rows)):
        members = sorted(members, key=lambda row: row.ordinal)
        target_ids = sorted({
            row.exact.target_reference_id
            for row in members
            if row.exact.target_reference_id
        })
        if len(target_ids) > 1:
            for row in members:
                language = classifier(row.candidate_record)
                pre_items.append((row.ordinal, BatchItem(
                    _new_item_id(), row.snapshot, BatchDisposition.REJECT,
                    language_verdict=language,
                    rejection_reasons=("identity_conflict",),
                )))
            continue

        representative = max(
            members,
            key=lambda row: (
                int(bool(row.exact.target_reference_id)),
                int(bool(row.candidate_record.abstract)),
                len(row.candidate_record.work_identifiers),
                _metadata_completeness(row.candidate_record),
                -row.ordinal,
            ),
        )
        merge_order = [representative, *(
            row for row in members if row is not representative
        )]

        if target_ids:
            existing = deps.ledger.get(target_ids[0])
            if existing is None:
                for row in members:
                    pre_items.append((row.ordinal, BatchItem(
                        _new_item_id(), row.snapshot, BatchDisposition.REJECT,
                        language_verdict=classifier(row.candidate_record),
                        rejection_reasons=("identity_missing",),
                    )))
                continue
            subject = existing
            for row in merge_order:
                subject = _dedup.merge_records(subject, row.candidate_record).record
            exact = (
                ExactMatch("reuse", target_reference_id=existing.reference_id)
                if subject == existing
                else ExactMatch(
                    "merge",
                    target_reference_id=existing.reference_id,
                    merged_record=subject,
                    expected_base_hash=ProposedRecordSnapshot.hash_of(existing),
                )
            )
            resolved_id = existing.reference_id
            group_key = f"ledger:{resolved_id}"
        else:
            subject = representative.candidate_record
            for row in merge_order[1:]:
                subject = _dedup.merge_records(subject, row.candidate_record).record
            subject = subject.with_changes(reference_id=_dedup.make_reference_id(subject))
            exact = ExactMatch("none")
            resolved_id = subject.reference_id
            work_keys = sorted(_work_key_set(subject))
            group_key = (
                f"work:{work_keys[0][0]}:{work_keys[0][1]}"
                if work_keys else f"candidate:{representative.ordinal}"
            )

        language = classifier(subject)
        if not passes_all_filters(
            subject,
            quota.eligibility_filters,
            classifier=classifier,
            policy=deps.language_policy,
        ):
            pre_items.append((representative.ordinal, BatchItem(
                _new_item_id(), representative.snapshot, BatchDisposition.REJECT,
                target_reference_id=resolved_id,
                language_verdict=language,
                rejection_reasons=("filter",),
            )))
        else:
            if deps.active_relevance_runtime is not None:
                content_hash = deps.active_relevance_runtime.content_hash(
                    subject.title, subject.abstract
                )
            else:
                content_hash = deps.judge.content_hash(
                    subject.title, subject.abstract
                )[2]
            work_items.append(WorkItem(
                ordinal=representative.ordinal,
                candidate=representative.candidate,
                snapshot=representative.snapshot,
                language=language,
                candidate_record=representative.candidate_record,
                relevance_subject=subject,
                exact=exact,
                resolved_reference_id=resolved_id,
                content_hash=content_hash,
                group_key=group_key,
            ))

        for row in members:
            if row is representative:
                continue
            pre_items.append((row.ordinal, BatchItem(
                _new_item_id(), row.snapshot, BatchDisposition.REJECT,
                target_reference_id=resolved_id,
                language_verdict=classifier(row.candidate_record),
                rejection_reasons=("batch_duplicate",),
            )))

    return tuple(sorted(work_items, key=lambda item: item.ordinal)), pre_items


def _judge_b2(work_items, criteria, deps: AttemptDeps):
    """Judge prepared subjects before strict verification; remains serial in B2."""

    import dataclasses

    task_fp = criteria.fingerprint()
    runtime = deps.active_relevance_runtime
    judge_key = runtime.judge_key if runtime is not None else deps.judge.judge_version.key()
    judgements = {}
    uncached: list[WorkItem] = []
    for item in work_items:
        cached = deps.relevance_store.get(
            item.resolved_reference_id, task_fp, item.content_hash, judge_key
        )
        if cached is not None and cached.status == "SUCCESS":
            judgements[item.group_key] = cached
        else:
            uncached.append(item)

    stats = {"items": 0, "windows": 0, "llm_calls": 0}
    budget_stopped = False
    if runtime is None:
        for item in uncached:
            if deps.budget_ledger is not None and not deps.budget_ledger.call_budget_ok():
                budget_stopped = True
                break
            rel = deps.judge.assess(item.relevance_subject, criteria)
            if rel.reference_id != item.resolved_reference_id:
                rel = dataclasses.replace(
                    rel, reference_id=item.resolved_reference_id
                )
            judgements[item.group_key] = rel
            stats["items"] += 1
            stats["windows"] += 1
            stats["llm_calls"] += 1
            if deps.budget_ledger is not None:
                deps.budget_ledger.charge_llm()
        return judgements, stats, budget_stopped

    from paper_agent.literature.retrieval.call_control import ExternalCallControl

    if deps.budget_ledger is None:
        raise ValueError("B1+B2 runtime requires the shared BudgetLedger")
    control = ExternalCallControl(deps.budget_ledger)
    by_key = {f"c{item.ordinal}": item for item in uncached}
    window_items = [
        runtime.make_window_item(
            candidate_key=key,
            reference_id=item.resolved_reference_id,
            title=item.relevance_subject.title,
            abstract=item.relevance_subject.abstract,
        )
        for key, item in by_key.items()
    ]
    for window in runtime.pack(window_items, criteria):
        dimensions = set(deps.budget_ledger.exhausted_dimensions())
        if dimensions & {
            "max_llm_calls", "max_provider_calls", "provider_time_budget_s"
        }:
            budget_stopped = True
            break
        assessed, batch_stats = runtime.assess_window_with_stats(
            window, criteria, call_control=control
        )
        stats["items"] += len(window.items)
        stats["windows"] += 1
        stats["llm_calls"] += (
            batch_stats.first_pass_llm_calls + batch_stats.retry_llm_calls
        )
        for key, rel in assessed.items():
            judgements[by_key[key].group_key] = rel
    return judgements, stats, budget_stopped


def _verify_survivors_b2(work_items, judgements, deps: AttemptDeps):
    """Strictly verify only accepted, genuinely new literature subjects."""

    verifications = {}
    budget_stopped = False
    for item in work_items:
        rel = judgements.get(item.group_key)
        if not deps.acceptance_policy.is_accepted(rel) or item.exact.kind != "none":
            continue
        if deps.budget.exhausted:
            budget_stopped = True
            break
        verifications[item.group_key] = deps.cross_verifier.assess(
            item.relevance_subject,
            budget=deps.budget,
            cache=deps.verify_cache,
        )
    return verifications, budget_stopped


def _verify_all_new_b2(work_items, deps: AttemptDeps):
    """Verify-first B2 reference used to prove reorder semantic equivalence.

    It intentionally performs the extra work B2 removes, but shares the exact
    same prepared subjects and reducer.  It is never selected by feature flags.
    """

    verifications = {}
    budget_stopped = False
    for item in work_items:
        if item.exact.kind != "none":
            continue
        if deps.budget.exhausted:
            budget_stopped = True
            break
        verifications[item.group_key] = deps.cross_verifier.assess(
            item.relevance_subject,
            budget=deps.budget,
            cache=deps.verify_cache,
        )
    return verifications, budget_stopped


def _reduce_b2(
    work_items,
    pre_items,
    judgements,
    verifications,
    batch,
    deps: AttemptDeps,
    *,
    candidates_count: int,
    failed_sources: int,
    judge_stats: dict,
    budget_stopped: bool,
) -> AttemptResult:
    """Single-thread deterministic state/cache reduction for B2."""

    ordered = list(pre_items)
    stats = {
        "accept_new": 0,
        "reuse": 0,
        "merge": 0,
        "reject": len(pre_items),
    }
    try:
        numeric_base = int(deps.ledger.next_numeric_alias())
    except (ValueError, TypeError):
        numeric_base = 1
    taken_bibkeys = set(deps.ledger.resolvable_bibkeys())

    def reject(item: WorkItem, reason: str, rel=None) -> None:
        stats["reject"] += 1
        ordered.append((item.ordinal, BatchItem(
            _new_item_id(), item.snapshot, BatchDisposition.REJECT,
            target_reference_id=item.resolved_reference_id,
            language_verdict=item.language,
            relevance=rel,
            rejection_reasons=(reason,),
        )))

    for item in work_items:
        rel = judgements.get(item.group_key)
        if rel is not None and rel.status == "SUCCESS":
            merge_put = getattr(deps.relevance_store, "merge_put", None)
            (merge_put or deps.relevance_store.put)(rel)
        if rel is None:
            reject(item, "relevance:budget_exhausted")
            continue
        if not deps.acceptance_policy.is_accepted(rel):
            reject(
                item,
                f"relevance:{rel.verdict or rel.error_kind or 'unrelated'}",
                rel,
            )
            continue

        if item.exact.kind == "reuse":
            stats["reuse"] += 1
            ordered.append((item.ordinal, BatchItem(
                _new_item_id(), item.snapshot, BatchDisposition.REUSE_EXISTING,
                target_reference_id=item.resolved_reference_id,
                language_verdict=item.language,
                relevance=rel,
            )))
            continue
        if item.exact.kind == "merge":
            stats["merge"] += 1
            ordered.append((item.ordinal, BatchItem(
                _new_item_id(), item.snapshot, BatchDisposition.MERGE_DUPLICATE,
                proposed_record_snapshot=proposed_from_record(
                    item.relevance_subject,
                    expected_base_hash=item.exact.expected_base_hash,
                ),
                target_reference_id=item.resolved_reference_id,
                language_verdict=item.language,
                relevance=rel,
                verification_events=item.relevance_subject.verification_events,
            )))
            continue

        verification = verifications.get(item.group_key)
        if verification is None:
            reject(item, "verify:budget_exhausted", rel)
            continue
        if not verification.meets_strict_threshold:
            reject(item, f"verify:{verification.strict_status.value}", rel)
            continue

        numeric = str(numeric_base + stats["accept_new"])
        record = build_accept_record(
            item.relevance_subject,
            events=tuple(verification.events),
            ledger=deps.ledger,
            numeric_alias=numeric,
            taken_bibkeys=taken_bibkeys,
        )
        taken_bibkeys.add(record.bibkey)
        stats["accept_new"] += 1
        ordered.append((item.ordinal, BatchItem(
            _new_item_id(), item.snapshot, BatchDisposition.ACCEPT_NEW,
            proposed_record_snapshot=proposed_from_record(
                record, expected_base_hash=None
            ),
            target_reference_id=record.reference_id,
            language_verdict=item.language,
            relevance=rel,
            verification_events=tuple(verification.events),
        )))

    new_items = tuple(item for _, item in sorted(ordered, key=lambda pair: pair[0]))
    batch.items = (*batch.items, *new_items)
    return AttemptResult(
        items=new_items,
        stats=AttemptStats(
            candidates=candidates_count,
            accept_new=stats["accept_new"],
            reuse=stats["reuse"],
            merge=stats["merge"],
            reject=stats["reject"],
            failed_sources=failed_sources,
            relevance_calls=judge_stats["llm_calls"],
            budget_stopped=budget_stopped,
        ),
        throughput=ThroughputAttemptStats(
            relevance_items=judge_stats["items"],
            relevance_windows=judge_stats["windows"],
            relevance_llm_calls=judge_stats["llm_calls"],
        ),
    )


def _run_attempt_b2(
    query: str,
    criteria,
    quota,
    batch,
    deps: AttemptDeps,
    *,
    limit: int = 10,
) -> AttemptResult:
    candidates, failed = _search(deps.registry, query, limit)
    work_items, pre_items = _prepare_b2(candidates, quota, deps)
    judgements, judge_stats, judge_stopped = _judge_b2(
        work_items, criteria, deps
    )
    verifications, verify_stopped = _verify_survivors_b2(
        work_items, judgements, deps
    )
    return _reduce_b2(
        work_items,
        pre_items,
        judgements,
        verifications,
        batch,
        deps,
        candidates_count=len(candidates),
        failed_sources=failed,
        judge_stats=judge_stats,
        budget_stopped=judge_stopped or verify_stopped,
    )


def _run_attempt_b2_verify_first_reference(
    query: str,
    criteria,
    quota,
    batch,
    deps: AttemptDeps,
    *,
    limit: int = 10,
) -> AttemptResult:
    """Non-production reference: same B2 subjects/reducer, verify before judge."""

    candidates, failed = _search(deps.registry, query, limit)
    work_items, pre_items = _prepare_b2(candidates, quota, deps)
    verifications, verify_stopped = _verify_all_new_b2(work_items, deps)
    judgements, judge_stats, judge_stopped = _judge_b2(
        work_items, criteria, deps
    )
    return _reduce_b2(
        work_items,
        pre_items,
        judgements,
        verifications,
        batch,
        deps,
        candidates_count=len(candidates),
        failed_sources=failed,
        judge_stats=judge_stats,
        budget_stopped=judge_stopped or verify_stopped,
    )


def run_attempt(
    query: str,
    criteria,
    quota,
    batch,
    deps: AttemptDeps,
    *,
    limit: int = 10,
) -> AttemptResult:
    """Dispatch to the frozen Legacy path unless B1 runtime is injected."""

    if deps.pipeline_reorder_enabled:
        return _run_attempt_b2(
            query, criteria, quota, batch, deps, limit=limit
        )
    if deps.active_relevance_runtime is None:
        return _run_attempt_legacy(
            query, criteria, quota, batch, deps, limit=limit
        )
    return _run_attempt_b1(query, criteria, quota, batch, deps, limit=limit)


def _run_attempt_b1(
    query: str,
    criteria,
    quota,
    batch,
    deps: AttemptDeps,
    *,
    limit: int = 10,
) -> AttemptResult:
    """B1-only path: preserve verify-first order and batch only relevance.

    Mutable state is reduced in input order after each window.  A candidate
    that could alter exact-dedup semantics causes the preceding window to be
    reduced first, so accepted_records remains equivalent to the serial path.
    """

    from paper_agent.literature.retrieval.call_control import ExternalCallControl

    runtime = deps.active_relevance_runtime
    budget_ledger = deps.budget_ledger
    if budget_ledger is None:
        raise ValueError("B1 active runtime requires the shared BudgetLedger")
    call_control = ExternalCallControl(budget_ledger)
    ledger = deps.ledger
    thresholds = deps.language_thresholds
    language_policy = deps.language_policy

    def _classifier(record):
        return classify_record_language(record, thresholds)

    candidates, failed = _search(deps.registry, query, limit)
    ordered_items: list[tuple[int, BatchItem]] = []
    accepted_records: list[ReferenceRecord] = []
    pending: list[_B1Pending] = []
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

    def _apply(item: _B1Pending, rel) -> None:
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
            # 与 `_judge_b2` 的窗前检查对齐；未判定的候选走下面 `rel is None`
            # 分支得到诚实的 `relevance:budget_exhausted`，且不进 relevance_store。
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
    task_fp = criteria.fingerprint()

    for ordinal, cand in enumerate(candidates):
        while True:
            if _expensive_budget_exhausted():
                _flush()
                budget_stopped = True
                stop_processing = True
                break

            snap = CandidateSnapshot.from_candidate(cand)
            cand_record = candidate_to_record(cand)
            language = classify_record_language(cand_record, thresholds)
            work_keys = frozenset(_work_key_set(cand_record))

            # A pending candidate with the same exact work identity must be
            # reduced first; otherwise classify_exact would observe a future
            # acceptance too early or miss it entirely.
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
            window_item = runtime.make_window_item(
                candidate_key=f"c{ordinal}",
                reference_id=resolved_rid,
                title=cand_record.title,
                abstract=cand_record.abstract,
            )

            # Capacity is decided before strict verification, avoiding an
            # expensive verify for an item that belongs to the next window.
            trial = runtime.pack(
                [*(item.window_item for item in pending), window_item], criteria
            )
            if pending and len(trial) > 1:
                _flush()
                continue

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

            cached = deps.relevance_store.get(
                resolved_rid,
                task_fp,
                window_item.content_hash,
                runtime.judge_key,
            )
            if cached is not None and cached.status == "SUCCESS":
                _flush()
                cached_pending = _B1Pending(
                    ordinal,
                    cand,
                    snap,
                    language,
                    cand_record,
                    exact,
                    verification,
                    window_item,
                    work_keys,
                )
                _apply(cached_pending, cached)
                break

            pending.append(
                _B1Pending(
                    ordinal,
                    cand,
                    snap,
                    language,
                    cand_record,
                    exact,
                    verification,
                    window_item,
                    work_keys,
                )
            )
            if len(pending) >= runtime.window_limits.batch_size:
                _flush()
            break

        if stop_processing:
            break

    if pending:
        _flush()

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
        ),
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


__all__ += [
    "AttemptDeps",
    "AttemptStats",
    "ThroughputAttemptStats",
    "AttemptResult",
    "WorkItem",
    "_run_attempt_legacy",
    "_run_attempt_b2",
    "_run_attempt_b2_verify_first_reference",
    "run_attempt",
]
