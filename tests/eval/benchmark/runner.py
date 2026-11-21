"""CLI implementation and importable process target for Windows spawn."""
from __future__ import annotations

import argparse
import json
import multiprocessing
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from uuid import uuid4

from tests.eval.benchmark.core import (
    SCHEMA, SUITES, digest, markdown_report, quality_gate, read_json, summarize, write_json,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def load_dataset(relevance_path=None):
    tasks = read_json(HERE / "tasks.json")
    planning = read_json(HERE.parent / "intent_planning" / "golden.json")
    relevance = read_json(relevance_path or HERE.parent / "relevance_batch" / "manifest.json")
    fixture = (HERE / tasks["fixture"]).read_text(encoding="utf-8")
    criteria = {c["id"]: c for c in relevance["criteria"]}
    cases = [{**c, "suite": "planning"} for c in planning["cases"]]
    cases += tasks["cases"]
    cases += [{**c, "suite": "relevance", "criteria": criteria[c["criteria_id"]]}
              for c in relevance["cases"]]
    seen = set()
    for case in cases:
        key = (case["suite"], case["id"])
        if key in seen or case["suite"] not in SUITES or not re.fullmatch(r"[a-zA-Z0-9_-]+", case["id"]):
            raise ValueError(f"invalid or duplicate case: {key}")
        seen.add(key)
        if case["suite"] == "ask":
            if not case["patterns"] or not case["read_sections"]:
                raise ValueError("ask task must have facts and evidence annotations")
            for pattern in case["patterns"] + case.get("forbidden", []):
                re.compile(pattern)
        elif case["suite"] == "editing":
            from paper_agent.session.section_index import SectionIndex
            index = SectionIndex(fixture, "tex")
            if not case["targets"] or any(index.find(t) is None for t in case["targets"]):
                raise ValueError("editing target missing in fixture")
        elif case["suite"] == "relevance":
            if case["label"]["verdict"] not in {"relevant", "weakly_relevant", "unrelated"}:
                raise ValueError("unknown relevance verdict")
    labels = [c["label"] for c in relevance["cases"]]
    ready = len(labels) >= 40 and all(
        label.get("status") == "adjudicated"
        and isinstance(label.get("annotators"), list)
        and len({a.strip() for a in label["annotators"] if isinstance(a, str) and a.strip()}) >= 2
        for label in labels
    )
    return cases, fixture, {
        "dataset_hash": digest({"tasks": tasks, "fixture": fixture, "planning": planning, "relevance": relevance}),
        "label_readiness": {"relevance_adjudicated": ready,
                            "writing_judge_calibration": "pending_human_review",
                            "independent_holdout": False},
        "counts": dict(Counter(c["suite"] for c in cases)),
    }


def file_tree_hash(folder):
    return digest({str(p.relative_to(folder)).replace("\\", "/"): p.read_text(encoding="utf-8")
                   for p in sorted(folder.rglob("*.py"))})


def runtime_identity(config):
    from paper_agent.providers.factory import resolved_llm_model, reviewer_model_is_diverse

    def git(*args):
        try:
            result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    return {"commit": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=normal", "--",
                              "src", "tests/eval", ".github", "pyproject.toml")),
            "source_hash": file_tree_hash(ROOT / "src"),
            "application_config": {k: v for k, v in asdict(config).items()
                                   if not k.endswith(("base_url", "api_key_env"))},
            "harness_hash": digest({"benchmark": file_tree_hash(HERE),
                                    "planning_scorer": (HERE.parent / "intent_planning" / "scorer.py").read_text(encoding="utf-8")}),
            "writer_provider": config.llm_provider,
            "writer_model": resolved_llm_model(config),
            "judge_provider": config.reviewer_llm_provider or config.llm_provider,
            "judge_model": resolved_llm_model(config, reviewer=True),
            "independent_judge": config.llm_provider.lower() != "mock" and reviewer_model_is_diverse(config),
            # No endpoints or secret values are persisted. Hashes preserve identity.
            "writer_endpoint_hash": digest(config.llm_base_url),
            "judge_endpoint_hash": digest(config.reviewer_llm_base_url or config.llm_base_url)}


def worker(case, fixture, config, profile, policy, identity, mode, directory):
    """One isolated process/trial. Result file survives normal process shutdown."""
    from tests.eval.benchmark.adapters import (
        MeteredProvider, run_document, run_planning, run_relevance, smoke_judge, smoke_provider,
    )
    from paper_agent.providers.factory import build_llm_provider, build_reviewer_llm_provider
    workdir = Path(directory)
    start = time.monotonic()
    writer = judge = None
    row = {"suite": case["suite"], "case_id": case["id"], "status": "ERROR",
           "checks": {}, "hard_violations": []}
    try:
        inner = smoke_provider(case, fixture) if mode == "protocol_smoke" else build_llm_provider(config)
        writer = MeteredProvider(inner, max_calls=profile["max_calls_per_trial"],
                                 deadline=start + profile["trial_timeout_s"])
        if case["suite"] in {"ask", "editing"}:
            inner_judge = smoke_judge() if mode == "protocol_smoke" else (
                build_reviewer_llm_provider(config) if identity["independent_judge"] else None)
            if inner_judge:
                judge = MeteredProvider(inner_judge, max_calls=profile["judge_max_calls"],
                                        deadline=start + profile["trial_timeout_s"])
            result = run_document(case, writer, judge, config, fixture, workdir, policy)
        elif case["suite"] == "planning":
            result = run_planning(case, writer)
        else:
            result = run_relevance(case, writer, identity)
        row.update(result)
        if writer.exhausted or writer.failures or (judge and (judge.exhausted or judge.failures)):
            row.update(status="ERROR", error="provider_failure_or_budget_exhausted")
    except Exception as exc:  # retain harness failures without leaking provider error text/secrets
        row.update(status="ERROR", error=type(exc).__name__,
                   error_frames=[{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                                 for f in traceback.extract_tb(exc.__traceback__)[-5:]])
    finally:
        row["elapsed_s"] = round(time.monotonic() - start, 4)
        row["usage"] = writer.usage() if writer else {"calls": 0}
        row["judge_usage"] = judge.usage() if judge else {"calls": 0}
        row["reasons"] = [k for k, v in row["checks"].items() if not v]
        if row.get("error"):
            row["reasons"].append(row["error"])
        if row.get("judge", {}).get("status") not in {None, "PASS"}:
            row["reasons"].append(row["judge"].get("reason", "judge_below_threshold"))
        # Benchmark data is synthetic/versioned; no hidden reasoning is recorded.
        write_json(workdir / "responses.json", {
            "writer": writer.responses if writer else [], "judge": judge.responses if judge else []})
        write_json(workdir / "result.json", row)


def run_trial(case, fixture, config, profile, policy, identity, mode, directory):
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=worker, args=(case, fixture, config, profile, policy,
                                                   identity, mode, str(directory)))
    start = time.monotonic()
    process.start()
    process.join(profile["trial_timeout_s"])
    timed_out = process.is_alive()
    if timed_out:
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
    path = directory / "result.json"
    if not timed_out and process.exitcode == 0 and path.exists():
        return read_json(path)
    return {"suite": case["suite"], "case_id": case["id"], "status": "ERROR",
            "checks": {}, "hard_violations": [],
            "reasons": ["trial_timeout" if timed_out else "worker_failed"],
            "elapsed_s": round(time.monotonic() - start, 4),
            "usage": {"calls": 0}, "usage_unknown": True}


def run(args):
    from paper_agent.config import Config, load_config
    from paper_agent.utils.dotenv import load_dotenv
    from tests.eval.benchmark.adapters import JUDGE_RUBRIC, JUDGE_VERSION

    cases, fixture, dataset = load_dataset(args.relevance_manifest)
    policy = read_json(args.policy)
    suites = args.suites.split(",")
    if len(suites) != len(set(suites)) or not set(suites) <= set(SUITES):
        raise ValueError("--suites must be a comma-separated subset of planning,ask,editing,relevance")
    selected = []
    for suite in SUITES:
        if suite in suites:
            group = [c for c in cases if c["suite"] == suite]
            selected.extend(group[:args.limit] if args.limit else group)
    if not selected:
        raise ValueError("empty benchmark selection")
    mode = "live_provider" if args.live else "protocol_smoke"
    if args.live:
        if not args.allow_paid_provider:
            raise ValueError("--live requires --allow-paid-provider")
        load_dotenv()
        config = load_config()
        if config.llm_provider.lower() == "mock":
            raise ValueError("live benchmark cannot use mock provider")
    else:
        config = Config(llm_provider="mock", llm_model="protocol-fixture")
    config = replace(config, llm_max_retries=0, llm_request_timeout_s=args.request_timeout,
                     trace_enabled=False, stream_enabled=False, langfuse_enabled=False,
                     planning_timeout_s=args.request_timeout,
                     planning_provider_time_budget_s=args.trial_timeout)
    identity = runtime_identity(config)
    identity.update(judge_rubric=JUDGE_VERSION, judge_rubric_hash=digest(JUDGE_RUBRIC))
    profile = {"repeats": args.repeats, "max_calls_per_trial": args.max_calls,
               "judge_max_calls": 2, "trial_timeout_s": args.trial_timeout,
               "request_timeout_s": args.request_timeout, "temperature": 0.0,
               "sdk_retry_note": "application retries disabled; SDK internal retries may still occur",
               "cache": "fresh_process_and_session_per_trial",
               "suites": sorted(suites), "limit_per_suite": args.limit,
               "full_selection": len(selected) == len(cases),
               "max_total_logical_calls": args.max_total_calls}
    expected = [[c["suite"], c["id"], trial] for c in selected for trial in range(1, args.repeats + 1)]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = args.output or ROOT / "runs" / "benchmark" / run_id
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    # Same dataset/protocol/judge, different writer/code versions are comparable.
    comparison_key = digest({"dataset": dataset["dataset_hash"], "profile": profile,
                             "expected": expected, "harness": identity["harness_hash"],
                             "judge": [identity[k] for k in ("judge_provider", "judge_model", "judge_endpoint_hash", "judge_rubric_hash")],
                             "policy": policy})
    report = {"schema_version": SCHEMA, "run_id": run_id, "evaluation_kind": mode,
              "identity": identity, "profile": profile, "dataset_hash": dataset["dataset_hash"],
              "label_readiness": dataset["label_readiness"], "policy_hash": digest(policy),
              "comparison_key": comparison_key, "expected_trials": expected,
              "complete": False, "rows": []}
    baseline = read_json(args.baseline) if args.baseline else None

    def save():
        report["summary"] = summarize(report["rows"])
        report["gate"] = quality_gate(report, policy, baseline, require_baseline=args.require_baseline)
        write_json(output / "report.json", report)
        (output / "report.md").write_text(markdown_report(report), encoding="utf-8")
        with (output / "bad_cases.jsonl").open("w", encoding="utf-8") as handle:
            for row in report["rows"]:
                if row["status"] != "PASS":
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    save()
    used_calls = 0
    reserve = args.max_calls + profile["judge_max_calls"]
    for case in selected:
        for trial in range(1, args.repeats + 1):
            print(f"[{len(report['rows']) + 1}/{len(expected)}] {case['suite']}/{case['id']} trial={trial}", flush=True)
            directory = output / "trials" / case["suite"] / f"{case['id']}-{trial}"
            directory.mkdir(parents=True)
            if used_calls + reserve > args.max_total_calls:
                row = {"suite": case["suite"], "case_id": case["id"], "status": "ERROR",
                       "checks": {}, "hard_violations": [], "elapsed_s": 0,
                       "reasons": ["run_call_budget_exhausted"], "usage": {"calls": 0}}
            else:
                row = run_trial(case, fixture, config, profile, policy, identity, mode, directory)
                used_calls += (reserve if row.get("usage_unknown") else
                               row["usage"]["calls"] + row.get("judge_usage", {}).get("calls", 0))
            # Failed/timeout positives must remain in the recall denominator.
            if case["suite"] == "relevance":
                row["gold_verdict"] = case["label"]["verdict"]
                row["gold_accepted"] = (case["label"]["verdict"] == "relevant"
                                         and bool(case["candidate"].get("abstract")))
            row.update(trial=trial, artifact_dir=str(directory.relative_to(output)).replace("\\", "/"))
            report["rows"].append(row)
            save()
    report["complete"] = True
    report["call_budget_consumed_or_reserved"] = used_calls
    save()
    print(f"Gate: {report['gate']['status']}\nReport: {output / 'report.md'}", flush=True)
    if mode == "protocol_smoke":
        # Smoke deliberately includes failed model outcomes. Harness errors fail CI.
        return 1 if any(r["status"] in {"ERROR", "INCONCLUSIVE"} for r in report["rows"]) else 0
    return 0 if report["gate"]["status"] == "PASS" else 2


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="AcademicAgent task benchmark")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate datasets without network")
    validate.add_argument("--relevance-manifest", type=Path)
    run_parser = commands.add_parser("run")
    modes = run_parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--smoke", action="store_true")
    modes.add_argument("--live", action="store_true")
    run_parser.add_argument("--allow-paid-provider", action="store_true")
    run_parser.add_argument("--suites", default=",".join(SUITES))
    run_parser.add_argument("--limit", type=positive)
    run_parser.add_argument("--repeats", type=positive, default=1)
    run_parser.add_argument("--max-calls", type=positive, default=24)
    run_parser.add_argument("--max-total-calls", type=positive, default=3000)
    run_parser.add_argument("--request-timeout", type=positive, default=60)
    run_parser.add_argument("--trial-timeout", type=positive, default=240)
    run_parser.add_argument("--output", type=Path)
    run_parser.add_argument("--relevance-manifest", type=Path)
    run_parser.add_argument("--policy", type=Path, default=HERE / "policy.json")
    run_parser.add_argument("--baseline", type=Path)
    run_parser.add_argument("--require-baseline", action="store_true")
    for name in ("gate", "baseline"):
        command = commands.add_parser(name)
        command.add_argument("--report", type=Path, required=True)
        command.add_argument("--policy", type=Path, default=HERE / "policy.json")
        if name == "gate":
            command.add_argument("--baseline", type=Path)
            command.add_argument("--require-baseline", action="store_true")
            command.add_argument("--expected-commit")
        else:
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            _, _, dataset = load_dataset(args.relevance_manifest)
            print(json.dumps(dataset, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            return run(args)
        report, policy = read_json(args.report), read_json(args.policy)
        baseline = read_json(args.baseline) if getattr(args, "baseline", None) else None
        gate = quality_gate(report, policy, baseline,
                            require_baseline=getattr(args, "require_baseline", False),
                            expected_commit=getattr(args, "expected_commit", None))
        print(json.dumps(gate, ensure_ascii=False, indent=2))
        if gate["status"] != "PASS":
            return 2
        if args.command == "baseline":
            # Explicit promotion only; never overwrite a baseline during evaluation.
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Benchmark configuration/report error: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())