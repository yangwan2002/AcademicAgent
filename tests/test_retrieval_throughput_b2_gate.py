"""Task 8 offline paired gate and frozen report tests."""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path


EVAL = Path(__file__).resolve().parent / "eval" / "retrieval_throughput"


def _load(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


def test_b2_gate_small_run_is_honest_about_replay_minimum():
    gate = _load("retrieval_b2_gate_small", EVAL / "b2_gate.py")
    report = gate.build_b2_gate_report(runs=2)
    assert report["network_used"] is False
    assert report["production_modes"] == ["single_item", "b1"]
    assert report["experimental_modes"] == ["b1_b2", "b2_reference"]
    assert report["paired"]["final_semantics_equal"] is True
    assert report["paired"]["b1_b2_verify_calls"] < report["paired"]["b1_verify_calls"]
    assert report["checks"]["replay_minimum_met"] is False
    assert report["engineering_gate"] == "B2_ENGINEERING_NO_GO"


def test_b2_gate_frozen_report_passes_engineering_but_not_human_release():
    report = json.loads((EVAL / "b2_gate_report.json").read_text(encoding="utf-8"))
    assert report["runs_per_mode"] >= 30
    assert all(report["checks"].values())
    assert report["engineering_gate"] == "B2_ENGINEERING_GO_DEFAULT_OFF"
    assert report["release_gate"] == {
        "status": "NO_GO_B1_HUMAN_ADJUDICATION_PENDING",
        "human_adjudicated_cases": 0,
        "required_cases": 40,
    }
    assert report["paired"]["zero_survivor_verify_calls"] == 0
    assert report["modes"]["legacy"]["quota_completion_rate"] == 1.0
    assert report["modes"]["b1"]["quota_completion_rate"] == 1.0
    assert report["modes"]["b1_b2"]["quota_completion_rate"] == 1.0


def test_b2_reorder_matches_reference_for_100_deterministic_shuffles():
    gate = _load("retrieval_b2_gate_property", EVAL / "b2_gate.py")
    source = list(gate._candidates())
    for seed in range(100):
        candidates = list(source)
        random.Random(seed).shuffle(candidates)
        single_item = gate._run_mode("single_item", candidates)
        b1 = gate._run_mode("b1", candidates)
        b2 = gate._run_mode("b1_b2", candidates)
        reference = gate._run_mode("b2_reference", candidates)
        assert single_item["semantic"] == b1["semantic"] == b2["semantic"] == reference["semantic"]
        assert b2["accepted"] == reference["accepted"] == 20
        assert b2["calls"]["verify"] <= reference["calls"]["verify"]
