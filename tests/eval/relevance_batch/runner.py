"""Offline-first B1 relevance quality-gate runner.

Default mode validates the frozen manifest only.  ``--protocol-smoke`` uses a
label-oracle stub solely to exercise the complete per-item/batch/metamorphic
pipeline and can never produce a release GO.  Real provider use requires both
``--live`` and ``--allow-paid-provider``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SRC = ROOT / "src"
for path in (str(SRC), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from paper_agent.literature.relevance import (
    JudgeVersion,
    RelevanceCriteria,
    RelevanceJudge,
)
from paper_agent.literature.relevance_batch import (
    BatchRelevanceJudge,
    make_batch_judge_version,
)
from paper_agent.literature.relevance_runtime import (
    ActiveRelevanceRuntime,
    RelevanceTextPolicy,
)
from paper_agent.literature.retrieval.budget import (
    BudgetLedger,
    RetrievalLimits,
)
from paper_agent.literature.retrieval.call_control import (
    BudgetAwareLLMProvider,
    ExternalCallControl,
)
from paper_agent.literature.retrieval.window_packer import (
    BatchInputBudget,
    WindowLimits,
)
from paper_agent.providers.llm.base import LLMResponse, ToolCall

_SCORER_SPEC = importlib.util.spec_from_file_location(
    "relevance_batch_eval_scorer", HERE / "scorer.py"
)
assert _SCORER_SPEC is not None and _SCORER_SPEC.loader is not None
scorer = importlib.util.module_from_spec(_SCORER_SPEC)
sys.modules[_SCORER_SPEC.name] = scorer
_SCORER_SPEC.loader.exec_module(scorer)


MANIFEST_PATH = HERE / "manifest.json"
REPORT_PATH = HERE / "last_report.json"
CHECKPOINT_PATH = HERE / "progress_checkpoint.json"


@dataclass(frozen=True)
class EvalCandidate:
    reference_id: str
    title: str
    abstract: str


def canonical_hash(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _checkpoint_identity(
    manifest: dict,
    *,
    provider_name: str,
    model_id: str,
    evaluation_kind: str,
) -> dict:
    return {
        "manifest_hash": canonical_hash(manifest),
        "profile_hash": canonical_hash(manifest["profile"]),
        "provider": provider_name,
        "model_id": model_id,
        "evaluation_kind": evaluation_kind,
    }


def _load_checkpoint(path: Path, expected_identity: dict) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "relevance-batch-eval-checkpoint-v1":
        raise ValueError("unsupported relevance evaluation checkpoint schema")
    if payload.get("identity") != expected_identity:
        raise ValueError("relevance evaluation checkpoint identity mismatch")
    return payload


def _restore_predictions(rows: dict | None) -> dict[str, scorer.Prediction]:
    return {
        key: scorer.Prediction(**value)
        for key, value in (rows or {}).items()
    }


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict) -> dict:
    if manifest.get("schema_version") != "relevance-batch-eval-v1":
        raise ValueError("unsupported relevance eval manifest schema")
    criteria = manifest.get("criteria", [])
    criteria_ids = [row.get("id") for row in criteria]
    if len(criteria_ids) != len(set(criteria_ids)) or not all(criteria_ids):
        raise ValueError("criteria ids must be unique and non-empty")
    cases = manifest.get("cases", [])
    ids = [case.get("id") for case in cases]
    if len(ids) != len(set(ids)) or not all(ids):
        raise ValueError("case ids must be unique and non-empty")
    if len(cases) < 40:
        raise ValueError("controlled smoke manifest requires at least 40 cases")
    for case in cases:
        if case.get("criteria_id") not in criteria_ids:
            raise ValueError(f"unknown criteria_id in {case.get('id')}")
        if case.get("language") not in ("zh", "en"):
            raise ValueError(f"invalid language in {case.get('id')}")
        if case.get("label", {}).get("verdict") not in scorer.VERDICTS:
            raise ValueError(f"invalid verdict in {case.get('id')}")
        if not isinstance(case.get("slices"), list) or not case["slices"]:
            raise ValueError(f"missing slices in {case.get('id')}")
        if not case.get("candidate", {}).get("title"):
            raise ValueError(f"missing title in {case.get('id')}")

    languages = Counter(case["language"] for case in cases)
    verdicts = Counter(case["label"]["verdict"] for case in cases)
    slices = Counter(slice_name for case in cases for slice_name in case["slices"])
    required_slices = {
        "title_only",
        "long_abstract",
        "adjacent_topic",
        "include_exclude",
        "prompt_injection",
    }
    if min(languages.get("zh", 0), languages.get("en", 0)) < 15:
        raise ValueError("manifest must substantially cover both zh and en")
    if any(verdicts.get(verdict, 0) < 8 for verdict in scorer.VERDICTS):
        raise ValueError("manifest must cover every verdict with at least 8 cases")
    if any(slices.get(name, 0) < 4 for name in required_slices):
        raise ValueError("manifest lacks a required evaluation slice")
    return {
        "cases": len(cases),
        "languages": dict(sorted(languages.items())),
        "verdicts": dict(sorted(verdicts.items())),
        "slices": dict(sorted(slices.items())),
    }


def _criteria(row: dict) -> RelevanceCriteria:
    return RelevanceCriteria(
        topic=row.get("topic", ""),
        research_question=row.get("research_question", ""),
        section_purpose=row.get("section_purpose", ""),
        query_intent=row.get("query_intent", ""),
        include_conditions=tuple(row.get("include_conditions", [])),
        exclude_conditions=tuple(row.get("exclude_conditions", [])),
    )


def _prediction(case_id: str, assessment) -> scorer.Prediction:
    return scorer.Prediction(
        case_id=case_id,
        status=assessment.status,
        verdict=assessment.verdict,
        basis=assessment.basis,
        error_kind=assessment.error_kind,
    )


class _FixedOptionsProvider:
    def __init__(self, provider, *, temperature: float) -> None:
        self.provider = provider
        self.temperature = temperature

    def complete(self, messages, **opts):
        opts.setdefault("temperature", self.temperature)
        return self.provider.complete(messages, **opts)

    @property
    def structured_call_capability_owner(self):
        return self.provider


class ProtocolOracleProvider:
    """Protocol-only stub; its gold-label access makes it invalid for quality claims."""

    def __init__(self, cases: list[dict]) -> None:
        self._by_id = {case["id"]: case["label"]["verdict"] for case in cases}
        self._by_title = {
            case["candidate"]["title"]: case["label"]["verdict"] for case in cases
        }
        self.calls = 0

    def complete(self, messages, **opts):
        self.calls += 1
        tool_name = opts.get("forced_tool_name", "")
        user = "\n".join(message.content for message in messages if message.role == "user")
        if tool_name == "report_relevance_batch":
            keys = re.findall(r"candidate_key=([A-Za-z0-9_.:-]+)", user)
            arguments = {
                "results": [
                    {
                        "candidate_key": key,
                        "verdict": self._by_id[key],
                        "confidence": "high",
                    }
                    for key in reversed(keys)
                ]
            }
        else:
            matches = [title for title in self._by_title if title in user]
            if len(matches) != 1:
                raise RuntimeError("protocol oracle could not identify exactly one title")
            arguments = {
                "verdict": self._by_title[matches[0]],
                "confidence": "high",
            }
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id=f"eval-{self.calls}", name=tool_name, arguments=arguments)],
        )


def _runtime(provider, profile: dict, *, provider_name: str, model_id: str):
    policy_cfg = profile["text_policy"]
    policy = RelevanceTextPolicy(
        version=policy_cfg["version"],
        max_title_chars=policy_cfg["max_title_chars"],
        max_abstract_chars=policy_cfg["max_abstract_chars"],
    )
    budget_cfg = profile["input_budget"]
    budget = BatchInputBudget(
        max_input_chars=budget_cfg["max_input_chars"],
        max_input_tokens=budget_cfg.get("max_input_tokens"),
        max_output_chars=budget_cfg["max_output_chars"],
        max_output_tokens=budget_cfg.get("max_output_tokens"),
    )
    evaluator = BatchRelevanceJudge(
        provider,
        judge_version=make_batch_judge_version(
            provider=provider_name,
            model_id=model_id,
            text_policy=policy,
            input_budget=budget,
        ),
        text_policy=policy,
        input_budget=budget,
        retry_limit=profile["retry_limit"],
    )
    return ActiveRelevanceRuntime(
        evaluator=evaluator,
        text_policy=policy,
        window_limits=WindowLimits(
            batch_size=profile["batch_size"], input_budget=budget
        ),
    )


def run_evaluation(
    manifest: dict,
    provider,
    *,
    provider_name: str,
    model_id: str,
    evaluation_kind: str,
    checkpoint_path: Path | None = None,
    resume: bool = False,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    profile = manifest["profile"]
    identity = _checkpoint_identity(
        manifest,
        provider_name=provider_name,
        model_id=model_id,
        evaluation_kind=evaluation_kind,
    )
    if resume and checkpoint_path is None:
        raise ValueError("resume requires checkpoint_path")
    checkpoint = (
        _load_checkpoint(checkpoint_path, identity)
        if resume and checkpoint_path is not None
        else {}
    )
    checkpoint_predictions = checkpoint.get("predictions", {})
    phase_predictions: dict[str, dict[str, scorer.Prediction]] = {
        phase: _restore_predictions(checkpoint_predictions.get(phase))
        for phase in ("per_item", "batch", "reverse_order", "rotate_one")
    }
    prior_accounting = checkpoint.get("call_accounting", {})
    prior_logical_calls = int(prior_accounting.get("logical_calls_completed", 0))
    prior_provider_attempts = int(
        prior_accounting.get("provider_attempts_completed", 0)
    )
    resumed_predictions = sum(len(rows) for rows in phase_predictions.values())
    logical_calls_this_run = 0
    fixed_provider = _FixedOptionsProvider(
        provider, temperature=profile["temperature"]
    )
    runtime = _runtime(
        fixed_provider, profile, provider_name=provider_name, model_id=model_id
    )
    limits = RetrievalLimits(
        max_iterations=100,
        max_provider_calls=1000,
        max_search_calls=1000,
        max_verify_calls=1000,
        max_discovery_calls=1000,
        max_llm_calls=1000,
        max_candidates=1000,
        provider_time_budget_s=3600,
    )
    ledger = BudgetLedger(limits)
    control = ExternalCallControl(ledger)
    per_item_provider = BudgetAwareLLMProvider(
        fixed_provider, control, provider_name=provider_name
    )
    per_item_judge = RelevanceJudge(
        per_item_provider,
        judge_version=JudgeVersion(provider=provider_name, model_id=model_id),
    )
    criteria_by_id = {
        row["id"]: _criteria(row) for row in manifest["criteria"]
    }
    cases = manifest["cases"]

    def save_checkpoint(stage: str, *, complete: bool = False) -> None:
        if checkpoint_path is not None:
            _write_json_atomic(
                checkpoint_path,
                {
                    "schema_version": "relevance-batch-eval-checkpoint-v1",
                    "identity": identity,
                    "complete": complete,
                    "stage": stage,
                    "call_accounting": {
                        "logical_calls_completed": (
                            prior_logical_calls + logical_calls_this_run
                        ),
                        "provider_attempts_completed": (
                            prior_provider_attempts + ledger.llm_calls
                        ),
                    },
                    "predictions": {
                        phase: {
                            key: asdict(value) for key, value in rows.items()
                        }
                        for phase, rows in phase_predictions.items()
                    },
                },
            )
        if progress is not None:
            progress({
                "stage": stage,
                "completed_predictions": sum(
                    len(rows) for rows in phase_predictions.values()
                ),
                "total_predictions": len(cases) * 4,
                "logical_calls_this_run": logical_calls_this_run,
                "provider_attempts_this_run": ledger.llm_calls,
                "resumed_predictions": resumed_predictions,
            })

    per_item = phase_predictions["per_item"]
    for case in cases:
        if case["id"] in per_item:
            continue
        candidate = EvalCandidate(
            reference_id=case["id"],
            title=case["candidate"]["title"],
            abstract=scorer.expand_abstract(case),
        )
        assessment = per_item_judge.assess(candidate, criteria_by_id[case["criteria_id"]])
        per_item[case["id"]] = _prediction(case["id"], assessment)
        logical_calls_this_run += 1
        save_checkpoint("per_item")

    groups: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        groups[case["criteria_id"]].append(case)

    def run_batch_variant(
        phase: str, transform
    ) -> dict[str, scorer.Prediction]:
        nonlocal logical_calls_this_run
        predictions = phase_predictions[phase]
        for criteria_id, original_group in groups.items():
            group = transform(list(original_group))
            criteria = criteria_by_id[criteria_id]
            items = [
                runtime.make_window_item(
                    candidate_key=case["id"],
                    reference_id=case["id"],
                    title=case["candidate"]["title"],
                    abstract=scorer.expand_abstract(case),
                )
                for case in group
            ]
            for window in runtime.pack(items, criteria):
                keys = tuple(item.candidate_key for item in window.items)
                completed = [key in predictions for key in keys]
                if all(completed):
                    continue
                if any(completed):
                    raise ValueError(
                        f"checkpoint contains partial relevance window for {phase}"
                    )
                assessments, batch_stats = runtime.assess_window_with_stats(
                    window, criteria, call_control=control
                )
                logical_calls_this_run += 1 + int(
                    batch_stats.retry_llm_calls > 0
                )
                for key, assessment in assessments.items():
                    predictions[key] = _prediction(key, assessment)
                save_checkpoint(phase)
        return predictions

    batch = run_batch_variant("batch", lambda group: group)
    transformed = [
        run_batch_variant("reverse_order", lambda group: list(reversed(group))),
        run_batch_variant("rotate_one", lambda group: group[1:] + group[:1]),
    ]
    metrics = scorer.score_predictions(cases, per_item, batch, transformed)
    thresholds = scorer.threshold_results(metrics)

    adjudicated = [
        case
        for case in cases
        if case["label"].get("status") == "adjudicated"
        and len(case["label"].get("annotators", [])) >= 2
    ]
    controlled_labels_ready = len(cases) >= 40 and len(adjudicated) == len(cases)
    tier2_labels_ready = len(cases) >= 200 and len(adjudicated) == len(cases)
    real_evaluation = evaluation_kind == "live_provider"
    all_thresholds = all(thresholds.values())
    if not real_evaluation:
        gate_status = "NOT_EVALUATED_PROTOCOL_FIXTURE"
    elif not controlled_labels_ready:
        gate_status = "NO_GO_MISSING_HUMAN_ADJUDICATION"
    elif not all_thresholds:
        gate_status = "NO_GO_QUALITY_THRESHOLDS"
    else:
        gate_status = "CONTROLLED_DOGFOOD_GO"

    logical_calls = prior_logical_calls + logical_calls_this_run
    provider_attempts = prior_provider_attempts + ledger.llm_calls
    report = {
        "schema_version": "relevance-batch-eval-report-v1",
        "evaluation_kind": evaluation_kind,
        "dataset_id": manifest["dataset_id"],
        "manifest_hash": canonical_hash(manifest),
        "provider": provider_name,
        "model_id": model_id,
        "profile": profile,
        "distribution": validate_manifest(manifest),
        "label_readiness": {
            "adjudicated_cases": len(adjudicated),
            "controlled_40_ready": controlled_labels_ready,
            "tier2_200_ready": tier2_labels_ready,
        },
        "metrics": metrics,
        "thresholds": thresholds,
        "physical_llm_calls": provider_attempts,
        "call_accounting": {
            "logical_structured_calls": logical_calls,
            "provider_attempts": provider_attempts,
            "compatibility_retry_overhead": max(
                0, provider_attempts - logical_calls
            ),
            "this_run": {
                "logical_structured_calls": logical_calls_this_run,
                "provider_attempts": ledger.llm_calls,
            },
            "resumed_predictions": resumed_predictions,
        },
        "gate_status": gate_status,
        "predictions": {
            "per_item": {key: asdict(value) for key, value in per_item.items()},
            "batch": {key: asdict(value) for key, value in batch.items()},
        },
    }
    save_checkpoint("complete", complete=True)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="B1 relevance quality gate")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--protocol-smoke", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-paid-provider", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-progress", action="store_true")
    args = parser.parse_args(argv)
    if args.protocol_smoke and args.live:
        parser.error("choose only one of --protocol-smoke or --live")

    manifest = load_manifest(args.manifest)
    distribution = validate_manifest(manifest)
    if not args.protocol_smoke and not args.live:
        print(json.dumps({
            "status": "manifest-valid",
            "manifest_hash": canonical_hash(manifest),
            "distribution": distribution,
            "network_used": False,
        }, ensure_ascii=False, indent=2))
        return 0

    if args.live:
        if not args.allow_paid_provider:
            parser.error("--live requires explicit --allow-paid-provider")
        from paper_agent.config import load_config
        from paper_agent.providers.factory import (
            build_llm_provider,
            build_reviewer_llm_provider,
            resolved_llm_model,
        )
        from paper_agent.utils.dotenv import load_dotenv

        load_dotenv()
        config = load_config()
        provider = build_reviewer_llm_provider(config) or build_llm_provider(config)
        provider_name = config.reviewer_llm_provider or config.llm_provider
        model_id = resolved_llm_model(config, reviewer=True) or resolved_llm_model(config)
        if provider_name.lower() == "mock":
            parser.error("live evaluation cannot use mock provider")
        kind = "live_provider"
    else:
        provider = ProtocolOracleProvider(manifest["cases"])
        provider_name = "protocol-oracle-not-a-model"
        model_id = "gold-label-fixture"
        kind = "protocol_fixture"

    checkpoint_path = args.checkpoint
    if checkpoint_path is None and args.live and args.write_report:
        checkpoint_path = CHECKPOINT_PATH

    def emit_progress(event: dict) -> None:
        print(
            "[relevance-eval] "
            f"stage={event['stage']} "
            f"predictions={event['completed_predictions']}/"
            f"{event['total_predictions']} "
            f"logical_this_run={event['logical_calls_this_run']} "
            f"provider_attempts_this_run={event['provider_attempts_this_run']} "
            f"resumed={event['resumed_predictions']}",
            file=sys.stderr,
            flush=True,
        )

    report = run_evaluation(
        manifest,
        provider,
        provider_name=provider_name,
        model_id=model_id,
        evaluation_kind=kind,
        checkpoint_path=checkpoint_path,
        resume=args.resume,
        progress=(None if args.no_progress or not args.live else emit_progress),
    )
    if args.write_report:
        _write_json_atomic(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["gate_status"] in {
        "NOT_EVALUATED_PROTOCOL_FIXTURE",
        "CONTROLLED_DOGFOOD_GO",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
