"""Pure benchmark grading, aggregation and fail-closed gates (no LLM calls)."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

SCHEMA = "academic-agent-benchmark-v1"
SUITES = ("planning", "ask", "editing", "relevance")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
                         encoding="utf-8")
    temporary.replace(path)


def wilson(successes, total):
    """95% Wilson interval; repeated trials are not independent tasks."""
    if not total:
        return None
    z = 1.959963984540054
    p = successes / total
    d = 1 + z * z / total
    center = (p + z * z / (2 * total)) / d
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return [max(0.0, center - radius), min(1.0, center + radius)]


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    at = (len(values) - 1) * q
    lo = math.floor(at)
    hi = math.ceil(at)
    return values[lo] + (values[hi] - values[lo]) * (at - lo)


def answer_checks(case, answer):
    """Every required fact must match; substring 'any' is not answer correctness."""
    return {
        **{f"fact_{i + 1}": bool(re.search(p, answer, re.I))
           for i, p in enumerate(case["patterns"])},
        **{f"forbidden_{i + 1}": not re.search(p, answer, re.I)
           for i, p in enumerate(case.get("forbidden", []))},
    }


def tool_checks(case, calls):
    """Semantic read target grading; optional structure lookup and retries allowed.

    Grades model-requested calls; does not claim general tool accuracy outside
    the annotated ask tasks. A required body read cannot be replaced by guessing.
    """
    allowed = {"list_sections", "read_section"}
    valid_names = sum(c["name"] in allowed for c in calls)
    valid_args = 0
    evidence_read = False
    for call in calls:
        args = call["arguments"]
        if call["name"] == "list_sections":
            valid_args += args == {}
        elif call["name"] == "read_section":
            valid = (
                isinstance(args, dict) and set(args) == {"section_id"}
                and args["section_id"] in case["read_sections"]
            )
            valid_args += bool(valid)
            evidence_read |= bool(valid)
    return {
        "requested_calls": len(calls), "valid_tool_names": valid_names,
        "valid_tool_arguments": valid_args, "required_evidence_read": evidence_read,
    }


def editing_checks(before, after, targets):
    """Check actual working-copy artifacts, independently of task status."""
    from paper_agent.gates.diff_gate import _formulas, extract_citation_tokens
    from paper_agent.session.section_index import SectionIndex

    original, changed = SectionIndex(before, "tex"), SectionIndex(after, "tex")
    old_sections, new_sections = original.list(), changed.list()
    checks = {"section_structure": [(s.id, s.title, s.level) for s in old_sections]
              == [(s.id, s.title, s.level) for s in new_sections]}
    for section in old_sections:
        new = changed.find(section.id)
        old_body = original.body_of(section)
        new_body = changed.body_of(new) if new else ""
        if section.id not in targets:
            checks[f"untouched:{section.id}"] = old_body == new_body
            continue
        checks[f"changed:{section.id}"] = old_body.strip() != new_body.strip()
        checks[f"numbers:{section.id}"] = Counter(re.findall(r"\d+(?:\.\d+)?", old_body)) == Counter(
            re.findall(r"\d+(?:\.\d+)?", new_body))
        checks[f"citations:{section.id}"] = extract_citation_tokens(old_body, "tex") == extract_citation_tokens(new_body, "tex")
        checks[f"formulas:{section.id}"] = _formulas(old_body) == _formulas(new_body)
    return checks


def summarize(rows):
    suites = {}
    for suite in SUITES:
        group = [r for r in rows if r["suite"] == suite]
        if not group:
            continue
        n = len(group)
        passed = sum(r["status"] == "PASS" for r in group)
        first_trials = [r for r in group if r["trial"] == 1]
        by_case = {}
        for row in group:
            by_case.setdefault(row["case_id"], []).append(row)
        suites[suite] = {
            "trials": n, "cases": len(by_case), "passed": passed,
            "failed": sum(r["status"] == "FAIL" for r in group),
            "errors": sum(r["status"] == "ERROR" for r in group),
            "inconclusive": sum(r["status"] == "INCONCLUSIVE" for r in group),
            "success_rate": passed / n,
            "first_trial_wilson95": wilson(sum(r["status"] == "PASS" for r in first_trials), len(first_trials)),
            "all_trials_pass_case_rate": sum(all(r["status"] == "PASS" for r in g) for g in by_case.values()) / len(by_case),
            "hard_violations": sum(len(r.get("hard_violations", [])) for r in group),
            "elapsed_p50_s": percentile([r["elapsed_s"] for r in group], 0.5),
            "elapsed_p95_s": percentile([r["elapsed_s"] for r in group], 0.95),
            "logical_llm_calls": sum(r.get("usage", {}).get("calls", 0) for r in group),
        }
        if suite == "ask":
            calls = sum(r.get("tools", {}).get("requested_calls", 0) for r in group)
            for key in ("valid_tool_names", "valid_tool_arguments"):
                suites[suite][key + "_rate"] = (
                    sum(r.get("tools", {}).get(key, 0) for r in group) / calls if calls else None)
        if suite == "relevance":
            tp = sum(r.get("gold_accepted") is True and r.get("accepted") is True for r in group)
            accepted = sum(r.get("accepted") is True for r in group)
            positives = sum(r.get("gold_accepted") is True for r in group)
            negatives = sum(r.get("gold_accepted") is False for r in group)
            suites[suite].update({
                "accepted_precision": tp / accepted if accepted else None,
                "accepted_recall": tp / positives if positives else None,
                "false_acceptance_rate": (accepted - tp) / negatives if negatives else None,
            })
    return {
        "suites": suites,
        "macro_success_rate": sum(s["success_rate"] for s in suites.values()) / len(suites) if suites else None,
        "cost_usd": None,
        "cost_note": "No price assumptions; provider-reported token usage is stored per trial.",
    }


def quality_gate(report, policy, baseline=None, *, require_baseline=False, expected_commit=None):
    """Recompute scores from rows; never trust the report's cached gate/summary."""
    blockers, failures = [], []
    if report.get("schema_version") != SCHEMA:
        blockers.append("unsupported_report_schema")
    if report.get("evaluation_kind") != "live_provider":
        blockers.append("not_real_model_evaluation")
    if report.get("policy_hash") != digest(policy):
        blockers.append("policy_mismatch")
    if not report.get("complete"):
        blockers.append("incomplete_run")
    expected = report.get("expected_trials", [])
    rows = report.get("rows", [])
    keys = [(r["suite"], r["case_id"], r["trial"]) for r in rows]
    if not expected or len(keys) != len(set(keys)) or set(keys) != {tuple(k) for k in expected}:
        blockers.append("missing_duplicate_or_unexpected_trials")
    if any(r["status"] not in {"PASS", "FAIL", "ERROR", "INCONCLUSIVE"} for r in rows):
        blockers.append("invalid_trial_status")
    if any(type(r.get("elapsed_s")) not in {int, float} or not math.isfinite(r["elapsed_s"])
           or r["elapsed_s"] < 0 for r in rows):
        blockers.append("invalid_timing_evidence")
    if any(r["status"] in {"ERROR", "INCONCLUSIVE"} for r in rows):
        blockers.append("trial_or_grader_errors")
    if any(r["status"] == "PASS" and (
        not r.get("checks") or any(v is not True for v in r["checks"].values())
        or r.get("hard_violations")
        or (r["suite"] in {"ask", "editing"} and r.get("judge", {}).get("status") != "PASS")
    ) for r in rows):
        blockers.append("inconsistent_pass_evidence")
    identity = report.get("identity", {})
    if not identity.get("commit") or not identity.get("source_hash") or not identity.get("writer_model"):
        blockers.append("missing_runtime_identity")
    if expected_commit and (identity.get("commit") != expected_commit or identity.get("dirty")):
        blockers.append("release_commit_mismatch_or_dirty_checkout")
    suites_present = {r["suite"] for r in rows}
    if not suites_present or not suites_present <= set(SUITES):
        blockers.append("invalid_suite_selection")
    if expected_commit and suites_present != set(SUITES):
        blockers.append("release_requires_all_suites")
    if expected_commit and not report.get("profile", {}).get("full_selection"):
        blockers.append("release_requires_full_dataset")
    readiness = report.get("label_readiness", {})
    if "relevance" in suites_present and not readiness.get("relevance_adjudicated"):
        blockers.append("relevance_human_adjudication_pending")
    if any(r["suite"] == "relevance" and type(r.get("gold_accepted")) is not bool for r in rows):
        blockers.append("missing_relevance_gold_labels")
    if suites_present & {"ask", "editing"} and not identity.get("independent_judge"):
        blockers.append("independent_judge_required")
    summary = summarize(rows)
    for suite, metrics in summary["suites"].items():
        if metrics["success_rate"] < policy["min_suite_success_rate"][suite]:
            failures.append(f"{suite}:success_rate_below_minimum")
        if metrics["hard_violations"] > policy["max_hard_violations"]:
            failures.append(f"{suite}:hard_constraint_violations")
        if suite == "relevance":
            for metric, threshold in (("accepted_precision", "min_relevance_precision"),
                                      ("accepted_recall", "min_relevance_recall")):
                if metrics[metric] is None or metrics[metric] < policy[threshold]:
                    failures.append(f"relevance:{metric}_below_minimum")
    regression = {}
    if baseline is None and require_baseline:
        blockers.append("baseline_required")
    if baseline is not None:
        if quality_gate(baseline, policy)["status"] != "PASS":
            blockers.append("invalid_baseline")
        if baseline.get("comparison_key") != report.get("comparison_key"):
            blockers.append("incompatible_baseline_dataset_profile_or_judge")
        else:
            old = summarize(baseline["rows"])["suites"]
            old_passes = {(r["suite"], r["case_id"], r["trial"]) for r in baseline["rows"] if r["status"] == "PASS"}
            for suite, metrics in summary["suites"].items():
                delta = (metrics["success_rate"] - old[suite]["success_rate"]) * 100
                regression[suite] = delta
                if delta < -policy["max_regression_pp"] - 1e-9:
                    failures.append(f"{suite}:regression_exceeds_tolerance")
            regression["new_bad_cases"] = [r["case_id"] for r in rows if r["status"] != "PASS"
                                           and (r["suite"], r["case_id"], r["trial"]) in old_passes]
    return {"status": "INCONCLUSIVE" if blockers else "FAIL" if failures else "PASS",
            "blockers": blockers, "failures": failures, "regression": regression}


def markdown_report(report):
    summary = summarize(report["rows"])
    gate = report["gate"]
    lines = ["# AcademicAgent benchmark", "", f"- Run: `{report['run_id']}`",
             f"- Kind: `{report['evaluation_kind']}`", f"- Gate: **{gate['status']}**",
             "- Scope: development benchmark; one synthetic document family, no independent holdout.",
             "- Relevance measures candidate classification, not corpus retrieval Recall@K.",
             "- Confidence intervals use first trials only; shared-document samples remain correlated.",
             "", "| Suite | Cases / trials | Passed | Success | P50 / P95 seconds |",
             "|---|---:|---:|---:|---:|"]
    for suite, m in summary["suites"].items():
        lines.append(f"| {suite} | {m['cases']} / {m['trials']} | {m['passed']} | "
                     f"{m['success_rate']:.1%} | {m['elapsed_p50_s']:.2f} / {m['elapsed_p95_s']:.2f} |")
    lines.extend(["", "## Gate reasons", ""])
    lines.extend(f"- {reason}" for reason in gate["blockers"] + gate["failures"])
    lines.extend(["", "## Bad cases", ""])
    for row in report["rows"]:
        if row["status"] != "PASS":
            reasons = ", ".join(row.get("reasons", []))
            lines.append(f"- `{row['suite']}/{row['case_id']}` trial {row['trial']}: "
                         f"{row['status']} — {reasons}")
    return "\n".join(lines) + "\n"
