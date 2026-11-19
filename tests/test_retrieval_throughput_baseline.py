"""Unified single-item behavior against the frozen historical semantic golden."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
EVAL = HERE / "eval" / "retrieval_throughput"


def _load_module(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


def _canonical_hash(value: dict) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _semantic_summary(snapshot: dict) -> dict:
    summary = {}
    for name, value in snapshot["scenarios"].items():
        if "stats" in value:
            summary[name] = {
                "stats": value["stats"],
                "calls": value["calls"],
                "dispositions": [item["disposition"] for item in value["items"]],
            }
        else:
            summary[name] = {
                sub_name: {
                    "stats": sub_value["stats"],
                    "calls": sub_value["calls"],
                    "dispositions": [item["disposition"] for item in sub_value["items"]],
                }
                for sub_name, sub_value in value.items()
            }
    return summary


def test_benchmark_profile_has_frozen_validity_and_targets():
    profile = json.loads((EVAL / "benchmark_profile.json").read_text(encoding="utf-8"))
    assert profile["objective"] == {
        "citation_total_min": 20,
        "language_zh_min": 5,
        "language_en_min": 15,
        "requires_quota_completion": True,
    }
    assert profile["runs"]["replay_min"] >= 30
    assert profile["runs"]["live_paired_min"] >= 5
    assert profile["validity"]["fast_incomplete_is_success"] is False
    assert profile["targets"]["live_median_active_elapsed_s_max"] == 600
    assert profile["privacy"]["persist_raw_query_or_candidate_text"] is False


def test_profile_hash_is_canonical_and_stable():
    runner = _load_module("throughput_runner", EVAL / "runner.py")
    _, first = runner.load_profile(EVAL / "benchmark_profile.json")
    _, second = runner.load_profile(EVAL / "benchmark_profile.json")
    assert first == second and len(first) == 64


def test_unified_single_item_characterization_matches_historical_golden():
    baseline = _load_module("throughput_baseline", EVAL / "baseline.py")
    actual = baseline.build_single_item_snapshot()
    golden = json.loads((EVAL / "legacy_golden.json").read_text(encoding="utf-8"))
    assert _canonical_hash(actual) == golden["snapshot_sha256"]
    assert _semantic_summary(actual) == golden["semantic_summary"]
    assert actual["normalization"]["excluded"] == [
        "BatchItem.item_id",
        "VerificationEvent.event_id",
        "VerificationEvent.checked_at",
        "RelevanceAssessment.assessed_at",
        "provider elapsed time",
    ]


def test_runner_is_offline_and_uses_same_golden_snapshot():
    runner = _load_module("throughput_runner_report", EVAL / "runner.py")
    report = runner.build_report(EVAL / "benchmark_profile.json")
    assert report["network_used"] is False
    assert report["mode"] == "single-item-characterization"
    assert report["snapshot_hash"] == _canonical_hash(report["result"])
    assert report["snapshot_hash"] == json.loads(
        (EVAL / "legacy_golden.json").read_text(encoding="utf-8")
    )["snapshot_sha256"]


def test_baseline_report_does_not_claim_incomplete_live_run():
    report = json.loads((EVAL / "baseline_report.json").read_text(encoding="utf-8"))
    assert report["historical_live_dogfood"]["status"] == "invalid-incomplete"
    assert report["live_baseline"]["status"] == "not-run"
