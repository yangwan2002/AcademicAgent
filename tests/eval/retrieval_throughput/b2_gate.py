"""Offline paired gate: unified single-item vs batch vs experimental reorder.

The replay uses deterministic providers and a simulated monotonic clock. It
never touches network services and persists no raw query or candidate text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SRC = ROOT / "src"
for path in (str(SRC), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from baseline import build_single_item_snapshot
from experimental_attempt import (
    _run_attempt_b2_verify_first_reference, run_experimental_attempt,
)
from paper_agent.config import Config
from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.quota import (
    CountConstraint, CountDimension, CountOperator, EligibilityFilter,
    FilterField, FilterOperator, QuotaKind, QuotaScope, QuotaSpec,
)
from paper_agent.literature.relevance import (
    JudgeVersion, RelevanceCriteria, RelevanceJudge, RelevanceStore,
)
from paper_agent.literature.relevance_batch import (
    BatchRelevanceJudge, make_batch_judge_version,
)
from paper_agent.literature.relevance_runtime import (
    ActiveRelevanceRuntime, RelevanceTextPolicy,
)
from paper_agent.literature.retrieval.attempt import (
    AttemptDeps, RelevanceAcceptancePolicy,
    run_attempt,
)
from paper_agent.literature.retrieval.batch import (
    BatchDisposition, RetrievalBatch,
)
from paper_agent.literature.retrieval.budget import (
    BudgetLedger, RetrievalLimits,
)
from paper_agent.literature.retrieval.cross_verify import (
    CrossVerifier, VerificationCache,
)
from paper_agent.literature.retrieval.registry import ProviderRegistry, named
from paper_agent.literature.retrieval.window_packer import (
    BatchInputBudget, WindowLimits,
)
from paper_agent.literature.verification import VerificationPolicy
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.providers.retrieval.capabilities import (
    FetchResult, FetchStatus,
)
from paper_agent.workspace.models import RetrievalCandidate


PROFILE_PATH = HERE / "benchmark_profile.json"
REPORT_PATH = HERE / "b2_experiment_report.json"
GOLDEN_PATH = HERE / "legacy_golden.json"
RELEVANCE_MANIFEST = HERE.parent / "relevance_batch" / "manifest.json"


def canonical_hash(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Search:
    def __init__(self, candidates, clock: _Clock, events: list) -> None:
        self.candidates = tuple(candidates)
        self.clock = clock
        self.events = events
        self.calls = 0

    def search(self, query, *, limit=100, filters=()):
        self.calls += 1
        self.clock.advance(0.2)
        self.events.append(("search", self.calls))
        return list(self.candidates[:limit])


class _Verify:
    def __init__(self, missing, clock: _Clock, events: list) -> None:
        self.missing = set(missing)
        self.clock = clock
        self.events = events
        self.calls = 0

    def fetch_metadata(self, identifier_type, identifier):
        self.calls += 1
        self.clock.advance(0.05)
        self.events.append(("verify", identifier))
        if identifier in self.missing:
            return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE)
        candidate = RetrievalCandidate(
            id=f"verified:{identifier}", title="", authors=[], year=None,
            source_id=identifier, source="crossref",
        )
        return FetchResult(FetchStatus.FOUND, candidate=candidate)

    def search_bibliographic(self, query, *, limit=5):
        return []


class _Reviewer:
    def __init__(self, verdict_by_key: dict[str, str], clock: _Clock, events: list) -> None:
        self.verdict_by_key = verdict_by_key
        self.clock = clock
        self.events = events
        self.calls = 0

    def complete(self, messages, **opts):
        self.calls += 1
        self.clock.advance(0.5)
        tool_name = opts.get("forced_tool_name", "")
        self.events.append(("judge", tool_name or "json"))
        user = "\n".join(
            message.content for message in messages if message.role == "user"
        )
        if tool_name == "report_relevance_batch":
            keys = re.findall(r"candidate_key=(c\d+)", user)
            arguments = {"results": [
                {
                    "candidate_key": key,
                    "verdict": self.verdict_by_key[key],
                    "confidence": "high",
                }
                for key in keys
            ]}
        else:
            arguments = {
                "verdict": (
                    "unrelated" if "Unrelated Replay Study" in user else "relevant"
                ),
                "confidence": "high",
            }
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(
                id=f"replay-{self.calls}", name=tool_name, arguments=arguments,
            )],
        )


def _candidates():
    rows = []
    for index in range(20):
        rows.append(RetrievalCandidate(
            id=f"relevant:{index}",
            title=f"Relevant Replay Study {index}", authors=["Author"], year=2024,
            source_id=f"10.3000/relevant-{index}", source="openalex",
            abstract="Direct evidence for academic writing agents and citations.",
            language="zh" if index < 5 else "en",
            work_identifiers={"doi": f"10.3000/relevant-{index}"},
        ))
    for index in range(6):
        rows.append(RetrievalCandidate(
            id=f"unrelated:{index}",
            title=f"Unrelated Replay Study {index}", authors=["Author"], year=2024,
            source_id=f"10.3000/unrelated-{index}", source="openalex",
            abstract="An unrelated agricultural sensor deployment.", language="en",
            work_identifiers={"doi": f"10.3000/unrelated-{index}"},
        ))
    for index in range(2):
        rows.append(RetrievalCandidate(
            id=f"invalid:{index}",
            title=f"Relevant But Unverified Replay Study {index}",
            authors=["Author"], year=2024,
            source_id=f"10.3000/invalid-{index}", source="openalex",
            abstract="Direct evidence whose identifier fails strict verification.",
            language="en",
            work_identifiers={"doi": f"10.3000/invalid-{index}"},
        ))
    for index in range(2):
        rows.append(RetrievalCandidate(
            id=f"filtered:{index}", title=f"Filtered Old Replay Study {index}",
            authors=["Author"], year=2010,
            source_id=f"10.3000/filtered-{index}", source="openalex",
            abstract="Old study excluded by the frozen year filter.", language="en",
            work_identifiers={"doi": f"10.3000/filtered-{index}"},
        ))
    return tuple(rows)


def _quota() -> QuotaSpec:
    return QuotaSpec(
        QuotaKind.RETRIEVAL,
        QuotaScope.NEW_ONLY,
        eligibility_filters=(
            EligibilityFilter(FilterField.YEAR, FilterOperator.GTE, 2020),
        ),
        count_constraints=(
            CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 20),
            CountConstraint(CountDimension.LANGUAGE_ZH, CountOperator.MIN, 5),
            CountConstraint(CountDimension.LANGUAGE_EN, CountOperator.MIN, 15),
        ),
    )


def _limits() -> RetrievalLimits:
    return RetrievalLimits(
        max_iterations=4, max_provider_calls=200, max_search_calls=20,
        max_verify_calls=100, max_discovery_calls=10, max_llm_calls=100,
        max_candidates=100, provider_time_budget_s=1000,
    )


def _runtime(reviewer) -> ActiveRelevanceRuntime:
    policy = RelevanceTextPolicy(version="b2-gate-text-v1")
    budget = BatchInputBudget(
        max_input_chars=40_000, max_output_chars=12_000, max_output_tokens=4_000,
    )
    evaluator = BatchRelevanceJudge(
        reviewer,
        judge_version=make_batch_judge_version(
            provider="offline-replay", model_id="deterministic-fixture",
            text_policy=policy, input_budget=budget,
        ),
        text_policy=policy, input_budget=budget, retry_limit=0,
        now_iso=lambda: "2026-07-31T00:00:00+00:00",
    )
    return ActiveRelevanceRuntime(
        evaluator=evaluator, text_policy=policy,
        window_limits=WindowLimits(batch_size=5, input_budget=budget),
    )


def _run_mode(mode: str, candidates=None) -> dict:
    candidates = tuple(candidates or _candidates())
    clock = _Clock()
    events: list[tuple[str, object]] = []
    search = _Search(candidates, clock, events)
    verify = _Verify(
        {"10.3000/invalid-0", "10.3000/invalid-1"}, clock, events
    )
    registry = ProviderRegistry(
        search_sources=(named("openalex", search),),
        verify_sources=(named("crossref", verify),),
    )
    verdict_by_key = {
        f"c{index}": (
            "unrelated" if candidate.title.startswith("Unrelated") else "relevant"
        )
        for index, candidate in enumerate(candidates)
    }
    reviewer = _Reviewer(verdict_by_key, clock, events)
    budget = BudgetLedger(_limits(), clock=clock)
    runtime = _runtime(reviewer) if mode != "single_item" else None
    deps = AttemptDeps(
        registry=registry, ledger=ReferenceLedger(),
        cross_verifier=CrossVerifier(
            registry.verify_pairs(), clock=clock,
            now_iso=lambda: "2026-07-31T00:00:00+00:00",
        ),
        judge=RelevanceJudge(
            reviewer,
            judge_version=JudgeVersion(
                provider="offline-replay", model_id="deterministic-fixture"
            ),
        ),
        relevance_store=RelevanceStore(),
        budget=budget.verify_budget_view(), verify_cache=VerificationCache(),
        acceptance_policy=RelevanceAcceptancePolicy(),
        active_relevance_runtime=runtime, budget_ledger=budget,
        pipeline_reorder_enabled=mode in {"b1_b2", "b2_reference"},
    )
    batch = RetrievalBatch(
        batch_id=f"replay-{mode}", task_fingerprint="b2-gate-task",
        quota=_quota(), verification_policy=VerificationPolicy.STRICT_FINAL_V1,
    )
    criteria = RelevanceCriteria(
        topic="academic writing agents",
        research_question="How do agents retrieve and verify citation evidence?",
        section_purpose="introduction_background",
    )
    if mode == "b2_reference":
        run = _run_attempt_b2_verify_first_reference
    elif mode == "b1_b2":
        run = run_experimental_attempt
    else:
        run = run_attempt
    result = run(
        "redacted-replay-query", criteria, _quota(), batch, deps, limit=100
    )

    reasons = Counter(
        reason.split(":", 1)[0]
        for item in result.items for reason in item.rejection_reasons
    )
    accepted = [
        (candidate, item)
        # strict=True：items 应与 candidates 一一对应，不等长说明上游丢了候选，
        # 该炸出来而不是悄悄少统计几条。
        for candidate, item in zip(candidates, result.items, strict=True)
        if item.disposition in {
            BatchDisposition.ACCEPT_NEW,
            BatchDisposition.REUSE_EXISTING,
            BatchDisposition.MERGE_DUPLICATE,
        }
    ]
    languages = Counter(candidate.language for candidate, _item in accepted)
    semantic = [
        {
            "disposition": item.disposition.value,
            "rejection_reasons": list(item.rejection_reasons),
            # Reject target ids are diagnostic only: single-item review leaves them empty,
            # while B2 knows the persistent subject id during prepare.  They do
            # not affect commit, quota, strictness or user-visible disposition.
            "target_reference_id": (
                item.target_reference_id or ""
                if item.disposition is not BatchDisposition.REJECT else ""
            ),
        }
        for item in result.items
    ]
    survivors = sum(
        deps.acceptance_policy.is_accepted(item.relevance) for item in result.items
    )
    errors = sum(
        item.relevance is not None and item.relevance.status == "ERROR"
        for item in result.items
    )
    return {
        "mode": mode, "active_elapsed_s": round(clock.value, 6),
        "calls": {"search": search.calls, "llm": reviewer.calls, "verify": verify.calls},
        "candidates": len(candidates), "accepted": len(accepted),
        "accepted_languages": dict(sorted(languages.items())),
        "quota_complete": (
            len(accepted) >= 20 and languages.get("zh", 0) >= 5
            and languages.get("en", 0) >= 15
        ),
        "attrition": {
            "filter": reasons.get("filter", 0),
            "relevance": reasons.get("relevance", 0),
            "verify": reasons.get("verify", 0),
            "other": sum(reasons.values()) - reasons.get("filter", 0)
            - reasons.get("relevance", 0) - reasons.get("verify", 0),
        },
        "survivors": survivors, "relevance_errors": errors,
        "provider_errors": 0, "timeouts": 0,
        "budget_stopped": result.stats.budget_stopped,
        "semantic": semantic,
        "event_kinds": [kind for kind, _value in events],
        "judge_key": (
            runtime.judge_key if runtime is not None else deps.judge.judge_version.key()
        ),
    }


def _percentile(values, percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _aggregate(rows: list[dict]) -> dict:
    elapsed = [row["active_elapsed_s"] for row in rows]
    first = rows[0]
    return {
        "runs": len(rows),
        "active_elapsed_s": {
            "p50": statistics.median(elapsed), "p95": _percentile(elapsed, 0.95),
        },
        "calls_per_run": first["calls"],
        "candidates_per_run": first["candidates"],
        "accepted_per_run": first["accepted"],
        "accepted_languages": first["accepted_languages"],
        "quota_completion_rate": sum(row["quota_complete"] for row in rows) / len(rows),
        "attrition_per_run": first["attrition"],
        "survivors_per_run": first["survivors"],
        "relevance_errors": sum(row["relevance_errors"] for row in rows),
        "provider_errors": sum(row["provider_errors"] for row in rows),
        "timeouts": sum(row["timeouts"] for row in rows),
        "budget_stops": sum(row["budget_stopped"] for row in rows),
    }


def _adjudicated_count() -> int:
    manifest = json.loads(RELEVANCE_MANIFEST.read_text(encoding="utf-8"))
    return sum(
        case.get("label", {}).get("status") == "adjudicated"
        and len(case.get("label", {}).get("annotators", [])) >= 2
        for case in manifest["cases"]
    )


def build_b2_gate_report(profile_path: Path = PROFILE_PATH, *, runs: int | None = None) -> dict:
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    run_count = runs or int(profile["runs"]["replay_min"])
    mode_rows = {
        mode: [_run_mode(mode) for _ in range(run_count)]
        for mode in ("single_item", "b1", "b1_b2")
    }
    reference_rows = [_run_mode("b2_reference") for _ in range(run_count)]
    paired_semantics = all(
        single_item["semantic"] == b1["semantic"] == b2["semantic"] == reference["semantic"]
        # strict=True：四臂必须等长。少了一臂就静默截断的话，"配对语义一致"会在
        # 只比了前几轮的情况下报 True —— 这是 gate 结论失真，不是小瑕疵。
        for single_item, b1, b2, reference in zip(
            mode_rows["single_item"], mode_rows["b1"],
            mode_rows["b1_b2"], reference_rows, strict=True,
        )
    )
    b1_verify = mode_rows["b1"][0]["calls"]["verify"]
    b2_verify = mode_rows["b1_b2"][0]["calls"]["verify"]
    b2_survivors = mode_rows["b1_b2"][0]["survivors"]
    unrelated = tuple(
        row for row in _candidates() if row.title.startswith("Unrelated")
    )
    zero_survivor = _run_mode("b1_b2", unrelated)
    rollback_probe = _run_mode("b1_b2")
    rollback_b1 = _run_mode("b1")
    rollback_ok = (
        rollback_probe["event_kinds"].index("judge")
        < rollback_probe["event_kinds"].index("verify")
        and rollback_b1["event_kinds"].index("verify")
        < rollback_b1["event_kinds"].index("judge")
        and rollback_b1["judge_key"] == mode_rows["b1"][0]["judge_key"]
    )
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    single_item_hash = canonical_hash(build_single_item_snapshot())
    checks = {
        "paired_final_semantics_equal": paired_semantics,
        "b2_verify_calls_not_above_survivors": b2_verify <= b2_survivors,
        "b2_verify_calls_reduced": b2_verify < b1_verify,
        "zero_survivor_zero_verify": zero_survivor["calls"]["verify"] == 0,
        "strict_quota_complete_all_modes": all(
            row["quota_complete"] for rows in mode_rows.values() for row in rows
        ),
        "no_relevance_errors_or_budget_stops": all(
            row["relevance_errors"] == 0 and not row["budget_stopped"]
            for rows in mode_rows.values() for row in rows
        ),
        "rollback_restores_b1_order_and_key": rollback_ok,
        "legacy_golden_unchanged": single_item_hash == golden["snapshot_sha256"],
        "b2_default_off": Config().literature_pipeline_reorder_enabled is False,
        "replay_minimum_met": run_count >= int(profile["runs"]["replay_min"]),
    }
    engineering_go = all(checks.values())
    adjudicated = _adjudicated_count()
    return {
        "schema_version": "retrieval-throughput-b2-gate-report-v2",
        "network_used": False, "profile_id": profile["profile_id"],
        "production_modes": ["single_item", "b1"],
        "experimental_modes": ["b1_b2", "b2_reference"],
        "historical_profile_mode_mapping": {"legacy": "single_item"},
        "profile_hash": canonical_hash(profile), "runs_per_mode": run_count,
        "workload": {
            "candidates": 30, "expected_accepted": 20,
            "expected_languages": {"en": 15, "zh": 5},
            "raw_text_persisted": False,
            "timing_basis": "deterministic_simulated_monotonic",
        },
        "modes": {mode: _aggregate(rows) for mode, rows in mode_rows.items()},
        "paired": {
            "final_semantics_equal": paired_semantics,
            "b1_verify_calls": b1_verify, "b1_b2_verify_calls": b2_verify,
            "verify_calls_saved": b1_verify - b2_verify,
            "verify_reduction_ratio": round((b1_verify - b2_verify) / b1_verify, 6),
            "active_time_reduction_ratio_vs_b1": round(
                (
                    mode_rows["b1"][0]["active_elapsed_s"]
                    - mode_rows["b1_b2"][0]["active_elapsed_s"]
                ) / mode_rows["b1"][0]["active_elapsed_s"],
                6,
            ),
            "zero_survivor_verify_calls": zero_survivor["calls"]["verify"],
        },
        "checks": checks,
        "engineering_gate": (
            "B2_ENGINEERING_GO_DEFAULT_OFF" if engineering_go
            else "B2_ENGINEERING_NO_GO"
        ),
        "release_gate": {
            "status": (
                "CONTROLLED_DOGFOOD_ELIGIBLE"
                if engineering_go and adjudicated >= 40
                else "NO_GO_B1_HUMAN_ADJUDICATION_PENDING"
            ),
            "human_adjudicated_cases": adjudicated, "required_cases": 40,
        },
    }


def _write_atomic(path: Path, value: dict) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline B2 throughput gate")
    parser.add_argument("--profile", type=Path, default=PROFILE_PATH)
    parser.add_argument("--runs", type=int)
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args(argv)
    report = build_b2_gate_report(args.profile, runs=args.runs)
    if args.write_report:
        _write_atomic(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["engineering_gate"] == "B2_ENGINEERING_GO_DEFAULT_OFF" else 2


if __name__ == "__main__":
    raise SystemExit(main())
