"""Task 6 offline evaluation-manifest, scorer and protocol-runner tests."""

from __future__ import annotations

import copy
import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest


EVAL = Path(__file__).resolve().parent / "eval" / "relevance_batch"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_is_frozen_balanced_smoke_set_with_honest_label_status():
    runner = _load("relevance_eval_runner_manifest", EVAL / "runner.py")
    manifest = runner.load_manifest(EVAL / "manifest.json")
    distribution = runner.validate_manifest(manifest)
    assert distribution["cases"] == 40
    assert distribution["languages"] == {"en": 20, "zh": 20}
    assert distribution["verdicts"] == {
        "relevant": 15,
        "unrelated": 10,
        "weakly_relevant": 15,
    }
    assert distribution["slices"]["title_only"] >= 4
    assert distribution["slices"]["long_abstract"] >= 4
    assert distribution["slices"]["prompt_injection"] >= 4
    assert all(
        case["label"]["status"] == "draft_pending_human_review"
        and case["label"]["annotators"] == []
        for case in manifest["cases"]
    )
    assert runner.canonical_hash(manifest) == "efb3c4c2f2f4d62b65d5fcf83c0f976d8d67852d9218524cc211f4edc9c8a389"


def test_long_and_title_only_slices_match_actual_expanded_input():
    scorer = _load("relevance_eval_scorer_slices", EVAL / "scorer.py")
    manifest = json.loads((EVAL / "manifest.json").read_text(encoding="utf-8"))
    long_cases = [case for case in manifest["cases"] if "long_abstract" in case["slices"]]
    title_only = [case for case in manifest["cases"] if "title_only" in case["slices"]]
    assert len(long_cases) == 5
    assert all(len(scorer.expand_abstract(case)) > 4000 for case in long_cases)
    assert len(title_only) == 5
    assert all(scorer.expand_abstract(case) == "" for case in title_only)


def test_scorer_reports_precision_and_false_acceptance_regression():
    scorer = _load("relevance_eval_scorer_metrics", EVAL / "scorer.py")
    cases = [
        {
            "id": "positive",
            "candidate": {"abstract": "evidence"},
            "label": {"verdict": "relevant"},
        },
        {
            "id": "negative",
            "candidate": {"abstract": "other"},
            "label": {"verdict": "unrelated"},
        },
    ]
    per_item = {
        "positive": scorer.Prediction("positive", "SUCCESS", "relevant", "title_and_abstract"),
        "negative": scorer.Prediction("negative", "SUCCESS", "unrelated", "title_and_abstract"),
    }
    batch = {
        "positive": scorer.Prediction("positive", "SUCCESS", "relevant", "title_and_abstract"),
        "negative": scorer.Prediction("negative", "SUCCESS", "relevant", "title_and_abstract"),
    }
    metrics = scorer.score_predictions(cases, per_item, batch, [batch])
    assert metrics["verdict_consistency"] == 0.5
    assert metrics["acceptance_consistency"] == 0.5
    assert metrics["batch_accepted_precision"] == 0.5
    assert metrics["batch_false_acceptance_rate"] == 1.0
    assert metrics["precision_drop_pp"] == 50.0
    assert metrics["false_acceptance_increase_pp"] == 100.0
    thresholds = scorer.threshold_results(metrics)
    assert thresholds["verdict_consistency"] is False
    assert thresholds["precision_drop"] is False


def test_manifest_validation_rejects_duplicate_ids_and_missing_slice_coverage():
    runner = _load("relevance_eval_runner_invalid", EVAL / "runner.py")
    manifest = runner.load_manifest(EVAL / "manifest.json")
    duplicate = copy.deepcopy(manifest)
    duplicate["cases"][1]["id"] = duplicate["cases"][0]["id"]
    with pytest.raises(ValueError, match="case ids"):
        runner.validate_manifest(duplicate)

    missing = copy.deepcopy(manifest)
    for case in missing["cases"]:
        case["slices"] = [
            name for name in case["slices"] if name != "prompt_injection"
        ] or ["unrelated"]
    with pytest.raises(ValueError, match="required evaluation slice"):
        runner.validate_manifest(missing)


def test_protocol_smoke_exercises_full_pipeline_but_can_never_claim_go():
    runner = _load("relevance_eval_runner_protocol", EVAL / "runner.py")
    manifest = runner.load_manifest(EVAL / "manifest.json")
    provider = runner.ProtocolOracleProvider(manifest["cases"])
    report = runner.run_evaluation(
        manifest,
        provider,
        provider_name="protocol-oracle-not-a-model",
        model_id="gold-label-fixture",
        evaluation_kind="protocol_fixture",
    )
    assert report["gate_status"] == "NOT_EVALUATED_PROTOCOL_FIXTURE"
    assert report["label_readiness"] == {
        "adjudicated_cases": 0,
        "controlled_40_ready": False,
        "tier2_200_ready": False,
    }
    assert report["metrics"]["verdict_consistency"] == 1.0
    assert report["metrics"]["window_transformation_acceptance_consistency"] == 1.0
    assert report["physical_llm_calls"] == 70
    assert report["call_accounting"] == {
        "logical_structured_calls": 70,
        "provider_attempts": 70,
        "compatibility_retry_overhead": 0,
        "this_run": {
            "logical_structured_calls": 70,
            "provider_attempts": 70,
        },
        "resumed_predictions": 0,
    }
    serialized = json.dumps(report, ensure_ascii=False)
    assert "Evidence-grounded agents for academic manuscript writing" not in serialized


def test_eval_forced_tool_fallback_is_probed_once_not_once_per_call():
    runner = _load("relevance_eval_runner_capability", EVAL / "runner.py")
    manifest = runner.load_manifest(EVAL / "manifest.json")
    oracle = runner.ProtocolOracleProvider(manifest["cases"])

    class CompatibilityProvider:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def complete(self, messages, **opts):
            self.calls += 1
            if "forced_tool_name" in opts:
                raise TypeError("unexpected keyword forced_tool_name")
            return self.inner.complete(messages, **opts)

    provider = CompatibilityProvider(oracle)
    report = runner.run_evaluation(
        manifest,
        provider,
        provider_name="compatibility-fixture",
        model_id="fixture",
        evaluation_kind="protocol_fixture",
    )
    assert report["call_accounting"]["logical_structured_calls"] == 70
    assert report["call_accounting"]["provider_attempts"] == 71
    assert report["call_accounting"]["compatibility_retry_overhead"] == 1
    assert provider.calls == 71


def test_eval_checkpoint_resumes_without_repeating_completed_calls(tmp_path):
    runner = _load("relevance_eval_runner_resume", EVAL / "runner.py")
    manifest = runner.load_manifest(EVAL / "manifest.json")
    checkpoint = tmp_path / "checkpoint.json"
    first_provider = runner.ProtocolOracleProvider(manifest["cases"])
    first = runner.run_evaluation(
        manifest,
        first_provider,
        provider_name="protocol-oracle-not-a-model",
        model_id="gold-label-fixture",
        evaluation_kind="protocol_fixture",
        checkpoint_path=checkpoint,
    )
    assert checkpoint.exists()
    assert first["physical_llm_calls"] == 70

    class MustNotCall:
        def complete(self, messages, **opts):
            raise AssertionError("completed checkpoint must not call provider")

    resumed = runner.run_evaluation(
        manifest,
        MustNotCall(),
        provider_name="protocol-oracle-not-a-model",
        model_id="gold-label-fixture",
        evaluation_kind="protocol_fixture",
        checkpoint_path=checkpoint,
        resume=True,
    )
    assert resumed["physical_llm_calls"] == 70
    assert resumed["call_accounting"]["this_run"] == {
        "logical_structured_calls": 0,
        "provider_attempts": 0,
    }
    assert resumed["call_accounting"]["resumed_predictions"] == 160


def test_live_mode_requires_explicit_paid_provider_opt_in():
    runner = _load("relevance_eval_runner_cli", EVAL / "runner.py")
    with pytest.raises(SystemExit) as exc:
        runner.main(["--live"])
    assert exc.value.code == 2


def test_baseline_report_is_honest_no_go():
    report = json.loads((EVAL / "baseline_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "engineering-ready-quality-not-evaluated"
    assert report["gate_status"] == "NO_GO_MISSING_HUMAN_ADJUDICATION"
    assert report["protocol_smoke"]["quality_claim_allowed"] is False
    assert report["human_adjudicated_cases"] == 0
    assert report["live_provider_evaluation"] == "not-run"


def test_annotation_export_import_requires_two_reviewers_and_never_overwrites_source(tmp_path):
    tool = _load("relevance_eval_annotation_tool", EVAL / "annotation_tool.py")
    source = EVAL / "manifest.json"
    source_before = source.read_bytes()
    sheet = tmp_path / "review.csv"
    output = tmp_path / "manifest.adjudicated.json"
    tool.export_sheet(source, sheet)

    with sheet.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 40
    assert "draft_label" not in rows[0]
    manifest = json.loads(source.read_text(encoding="utf-8"))
    labels = {case["id"]: case["label"]["verdict"] for case in manifest["cases"]}
    for row in rows:
        row["annotator_1_id"] = "reviewer-a"
        row["annotator_2_id"] = "reviewer-b"
        row["annotator_1_verdict"] = labels[row["case_id"]]
        row["annotator_2_verdict"] = labels[row["case_id"]]
        row["adjudicated_verdict"] = labels[row["case_id"]]
        row["adjudication_note"] = "test adjudication"
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tool.FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    updated = tool.import_adjudication(source, sheet, output)
    assert output.exists()
    assert source.read_bytes() == source_before
    assert updated["annotation_protocol"]["status"] == "adjudicated"
    assert all(case["label"]["status"] == "adjudicated" for case in updated["cases"])
    assert all(len(case["label"]["annotators"]) == 2 for case in updated["cases"])


def test_annotation_import_rejects_same_reviewer_twice(tmp_path):
    tool = _load("relevance_eval_annotation_tool_invalid", EVAL / "annotation_tool.py")
    sheet = tmp_path / "review.csv"
    tool.export_sheet(EVAL / "manifest.json", sheet)
    with sheet.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["annotator_1_id"] = "same"
        row["annotator_2_id"] = "same"
        row["annotator_1_verdict"] = "relevant"
        row["annotator_2_verdict"] = "relevant"
        row["adjudicated_verdict"] = "relevant"
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tool.FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="distinct"):
        tool.import_adjudication(
            EVAL / "manifest.json", sheet, tmp_path / "out.json"
        )
