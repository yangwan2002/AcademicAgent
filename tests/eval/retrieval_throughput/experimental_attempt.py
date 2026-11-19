"""Offline-only pipeline reordering experiments.

Production keeps verify-before-review ordering.  The B2 implementation and
its verify-first comparison stay here so evaluation does not enlarge the
production dispatch matrix.
"""

from __future__ import annotations

from dataclasses import dataclass

from paper_agent.literature import dedup as _dedup
from paper_agent.literature.language import classify_record_language
from paper_agent.literature.models import ReferenceRecord
from paper_agent.literature.quota import passes_all_filters
from paper_agent.literature.retrieval.attempt import (
    AttemptDeps,
    AttemptResult,
    AttemptStats,
    ExactMatch,
    ThroughputAttemptStats,
    _new_item_id,
    _search,
    _work_key_set,
    build_accept_record,
    candidate_to_record,
    classify_exact,
    proposed_from_record,
    run_attempt as _run_production_attempt,
)
from paper_agent.literature.retrieval.batch import (
    BatchDisposition,
    BatchItem,
    CandidateSnapshot,
    ProposedRecordSnapshot,
)


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


def run_experimental_attempt(
    query: str,
    criteria,
    quota,
    batch,
    deps: AttemptDeps,
    *,
    limit: int = 10,
) -> AttemptResult:
    """Opt into B2 explicitly; a disabled flag exercises production rollback."""
    if deps.pipeline_reorder_enabled:
        return _run_attempt_b2(query, criteria, quota, batch, deps, limit=limit)
    return _run_production_attempt(query, criteria, quota, batch, deps, limit=limit)


__all__ = [
    "WorkItem",
    "_run_attempt_b2",
    "_run_attempt_b2_verify_first_reference",
    "run_experimental_attempt",
]
