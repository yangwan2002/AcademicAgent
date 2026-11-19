"""Run one AcademicAgent benchmark case using EvalCaliper's file protocol.

Input JSON:
  {"suite": "planning", "case_id": "<id>", "mode": "live_provider" | "protocol_smoke"}
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / "src", ROOT):
    path = str(candidate)
    if path not in sys.path:
        sys.path.insert(0, path)

from paper_agent.config import Config, load_config
from tests.eval.benchmark.adapters import JUDGE_RUBRIC, JUDGE_VERSION
from tests.eval.benchmark.core import digest, read_json, write_json
from tests.eval.benchmark.runner import load_dataset, run_trial, runtime_identity

SUITES = {"planning", "ask", "editing", "relevance"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--trace", default="/eval/trace.jsonl")
    return parser.parse_args()


def select_case(cases: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    case_id = str(payload.get("case_id") or payload.get("benchmark_case_id") or "")
    suite = str(payload.get("suite") or "")

    if suite and suite not in SUITES:
        raise ValueError(f"unknown suite: {suite}")

    if case_id and suite:
        for case in cases:
            if case["suite"] == suite and case["id"] == case_id:
                return case
        raise ValueError(f"case {suite}/{case_id} not found")

    if case_id:
        matches = [case for case in cases if case["id"] == case_id]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"case {case_id} not found")
        raise ValueError(f"case_id {case_id} is ambiguous; specify suite")

    if suite:
        matches = [case for case in cases if case["suite"] == suite]
        if not matches:
            raise ValueError(f"no cases for suite {suite}")
        return matches[0]

    raise ValueError("input must specify case_id or suite")


def build_output(row: dict[str, Any], profile: dict[str, Any], identity: dict[str, Any], dataset_meta: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "ERROR")
    passed = status == "PASS"
    return {
        "status": status,
        "passed": passed,
        "score": 1 if passed else 0,
        "suite": row.get("suite"),
        "case_id": row.get("case_id"),
        "reasons": row.get("reasons", []),
        "error_frames": row.get("error_frames", []),
        "hard_violations": row.get("hard_violations", []),
        "checks": row.get("checks", {}),
        "judge": row.get("judge", {}),
        "usage": row.get("usage", {}),
        "elapsed_s": row.get("elapsed_s"),
        "artifact_dir": row.get("artifact_dir"),
        "dataset_hash": dataset_meta.get("dataset_hash"),
        "label_readiness": dataset_meta.get("label_readiness"),
        "comparison_key": digest(
            {
                "dataset": dataset_meta.get("dataset_hash"),
                "profile": profile,
                "judge": [
                    identity.get("judge_provider"),
                    identity.get("judge_model"),
                    identity.get("judge_endpoint_hash"),
                    identity.get("judge_rubric_hash"),
                ],
                "policy": digest(policy),
            }
        ),
    }


def write_error_output(path: Path, message: str, frames: list[dict[str, Any]] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        path,
        {
            "status": "ERROR",
            "passed": False,
            "score": 0,
            "reasons": [message],
            "error_frames": frames or [],
        },
    )


def main() -> int:
    args = parse_args()
    output_path = Path(args.output)
    trace_path = Path(args.trace)

    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        cases, fixture, dataset_meta = load_dataset()
        case = select_case(cases, payload)

        mode = str(payload.get("mode") or "live_provider")
        if mode not in {"live_provider", "protocol_smoke"}:
            raise ValueError("mode must be live_provider or protocol_smoke")

        request_timeout = int(payload.get("request_timeout", 60))
        trial_timeout = int(payload.get("timeout_seconds", 240))
        max_calls = int(payload.get("max_calls", 24))

        if mode == "protocol_smoke":
            config = Config(llm_provider="mock", llm_model="protocol-fixture")
        else:
            config = load_config()

        config = replace(
            config,
            llm_max_retries=0,
            llm_request_timeout_s=request_timeout,
            trace_enabled=False,
            stream_enabled=False,
            langfuse_enabled=False,
            planning_timeout_s=request_timeout,
            planning_provider_time_budget_s=trial_timeout,
        )

        identity = runtime_identity(config)
        identity.update(judge_rubric=JUDGE_VERSION, judge_rubric_hash=digest(JUDGE_RUBRIC))
        profile = {
            "repeats": 1,
            "max_calls_per_trial": max_calls,
            "judge_max_calls": 2,
            "trial_timeout_s": trial_timeout,
            "request_timeout_s": request_timeout,
        }
        policy = read_json(ROOT / "tests" / "eval" / "benchmark" / "policy.json")

        with tempfile.TemporaryDirectory(prefix="evalcaliper-academic-") as tmp:
            row = run_trial(
                case=case,
                fixture=fixture,
                config=config,
                profile=profile,
                policy=policy,
                identity=identity,
                mode=mode,
                directory=Path(tmp),
            )

        output = build_output(row, profile, identity, dataset_meta, policy)
        write_json(output_path, output)

        trace_events = [
            {
                "type": "benchmark_result",
                "suite": output["suite"],
                "case_id": output["case_id"],
                "status": output["status"],
                "score": output["score"],
                "reasons": output["reasons"],
            }
        ]
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in trace_events) + "\n",
            encoding="utf-8",
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - adapter must always write EvalCaliper output
        frames = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)[-8:]
        ]
        write_error_output(output_path, f"{type(exc).__name__}: {exc}", frames)
        try:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_path.write_text(
                json.dumps({"type": "adapter_error", "message": str(exc)}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())