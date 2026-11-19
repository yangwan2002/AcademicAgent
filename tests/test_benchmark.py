"""Benchmark acceptance tests: corrupt evidence, regression and real app paths."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import time

import pytest

from paper_agent.config import Config
from paper_agent.providers.llm.base import LLMError, LLMResponse, ToolCall
from paper_agent.providers.llm.mock import MockLLMProvider
from tests.eval.benchmark.adapters import (
    MeteredProvider, grade_task, run_document, smoke_judge, smoke_provider,
)
from tests.eval.benchmark.core import (
    SCHEMA, answer_checks, digest, editing_checks, quality_gate, read_json,
    summarize, tool_checks, wilson,
)
from tests.eval.benchmark.runner import HERE, load_dataset, main, run_trial


@pytest.fixture
def policy():
    return read_json(HERE / "policy.json")


def evidence(policy, *, count=20):
    rows = [{"suite": "planning", "case_id": f"case_{i}", "trial": 1,
             "status": "PASS", "checks": {"expected_intent": True},
             "hard_violations": [], "elapsed_s": 1.0} for i in range(count)]
    return {"schema_version": SCHEMA, "evaluation_kind": "live_provider", "complete": True,
            "identity": {"commit": "abc", "source_hash": "source", "writer_model": "model"},
            "policy_hash": digest(policy), "comparison_key": "same-profile",
            "expected_trials": [[r["suite"], r["case_id"], 1] for r in rows], "rows": rows}


def test_dataset_has_production_path_tasks_and_honest_labels():
    cases, fixture, metadata = load_dataset()
    assert metadata["counts"] == {"planning": 41, "ask": 10, "editing": 10, "relevance": 40}
    assert not metadata["label_readiness"]["relevance_adjudicated"]
    assert not metadata["label_readiness"]["independent_holdout"]
    assert len([c for c in cases if len(c.get("targets", [])) > 1]) == 2
    assert "0.87" in fixture


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r.update(evaluation_kind="protocol_smoke"), "not_real_model_evaluation"),
    (lambda r: r.update(complete=False), "incomplete_run"),
    (lambda r: r["rows"].pop(), "missing_duplicate_or_unexpected_trials"),
    (lambda r: r["rows"].append(deepcopy(r["rows"][0])), "missing_duplicate_or_unexpected_trials"),
    (lambda r: r["rows"][0].update(status="ERROR"), "trial_or_grader_errors"),
    (lambda r: r["rows"][0].update(status="SKIP"), "invalid_trial_status"),
    (lambda r: r["rows"][0].update(checks={"expected_intent": False}), "inconsistent_pass_evidence"),
    (lambda r: r.update(policy_hash="other-policy"), "policy_mismatch"),
    (lambda r: r["identity"].pop("writer_model"), "missing_runtime_identity"),
])
def test_gate_fails_closed(policy, mutation, reason):
    report = evidence(policy)
    mutation(report)
    gate = quality_gate(report, policy)
    assert gate["status"] == "INCONCLUSIVE"
    assert reason in gate["blockers"]


def test_baseline_comparison_and_explicit_requirement(policy):
    baseline = evidence(policy)
    candidate = deepcopy(baseline)
    for row in candidate["rows"][:2]:
        row.update(status="FAIL", checks={"expected_intent": False})
    gate = quality_gate(candidate, policy, baseline)
    assert gate["status"] == "FAIL"  # 90% is above 85%, but regressed by 10 pp
    assert gate["regression"]["planning"] == pytest.approx(-10)
    assert gate["regression"]["new_bad_cases"] == ["case_0", "case_1"]
    assert quality_gate(baseline, policy, require_baseline=True)["status"] == "INCONCLUSIVE"
    baseline["comparison_key"] = "old-dataset"
    assert "incompatible_baseline_dataset_profile_or_judge" in quality_gate(candidate, policy, baseline)["blockers"]


def test_hard_violation_cannot_be_offset_by_high_average(policy):
    report = evidence(policy, count=100)
    report["rows"][0].update(status="FAIL", hard_violations=["silent_loss"])
    assert quality_gate(report, policy)["status"] == "FAIL"


def test_incorrect_cached_summary_and_gate_are_ignored(policy):
    report = evidence(policy)
    report.update(summary={"macro_success_rate": 1}, gate={"status": "PASS"})
    for row in report["rows"]:
        row.update(status="FAIL", checks={"expected_intent": False})
    assert quality_gate(report, policy)["status"] == "FAIL"


def test_release_checks_commit_cleanliness_and_full_selection(policy):
    report = evidence(policy)
    gate = quality_gate(report, policy, expected_commit="different")
    assert "release_commit_mismatch_or_dirty_checkout" in gate["blockers"]
    assert "release_requires_all_suites" in gate["blockers"]
    assert "release_requires_full_dataset" in gate["blockers"]


def test_no_acceptance_is_not_perfect_retrieval():
    rows = [{"suite": "relevance", "case_id": str(i), "trial": 1, "status": "FAIL",
             "elapsed_s": 0, "gold_accepted": True, "accepted": False} for i in range(2)]
    metrics = summarize(rows)["suites"]["relevance"]
    assert metrics["accepted_precision"] is None
    assert metrics["accepted_recall"] == 0


def test_errors_remain_in_denominator_and_repeats_not_independent(policy):
    report = evidence(policy, count=2)
    report["rows"][1]["status"] = "ERROR"
    rows = report["rows"] + [{**r, "trial": 2} for r in report["rows"]]
    summary = summarize(rows)["suites"]["planning"]
    assert summary["success_rate"] == 0.5
    assert summary["first_trial_wilson95"] == wilson(1, 2)
    assert summary["all_trials_pass_case_rate"] == 0.5


def test_answer_grader_requires_all_facts_and_numeric_boundaries():
    cases, _, _ = load_dataset()
    case = next(c for c in cases if c["id"] == "ask_training")
    assert all(answer_checks(case, "20 轮，学习率 0.001").values())
    assert not all(answer_checks(case, "训练 120 轮，学习率 0.001").values())
    assert not all(answer_checks(case, "20 轮").values())


def test_tool_grader_accepts_alternative_paths_rejects_wrong_arguments():
    case = {"read_sections": ["introduction", "results"]}
    direct = [{"name": "read_section", "arguments": {"section_id": "results"}}]
    assert tool_checks(case, direct)["required_evidence_read"]
    assert tool_checks(case, [{"name": "list_sections", "arguments": {}}] + direct)["valid_tool_arguments"] == 2
    assert not tool_checks(case, [{"name": "read_section", "arguments": {"section_id": "method"}}])["required_evidence_read"]
    assert tool_checks(case, [{"name": "write_section", "arguments": {}}])["valid_tool_names"] == 0


def test_document_checks_find_data_corruption_and_noop():
    _, fixture, _ = load_dataset()
    unchanged = editing_checks(fixture, fixture, ["introduction"])
    assert unchanged["changed:introduction"] is False
    corrupted = editing_checks(fixture, fixture.replace("0.87 accuracy", "0.97 accuracy"), ["introduction"])
    assert corrupted["numbers:introduction"] is False
    unrelated = editing_checks(fixture, fixture.replace("3 layers", "4 layers"), ["introduction"])
    assert unrelated["untouched:method"] is False
    formula = editing_checks(fixture, fixture.replace("L_c", "L_z"), ["method"])
    assert formula["formulas:method"] is False


def meter(inner, calls=24):
    return MeteredProvider(inner, max_calls=calls, deadline=time.monotonic() + 30)


def test_call_budget_prevents_extra_provider_call():
    raw = MockLLMProvider(scripted=["one", "two"])
    provider = meter(raw, calls=1)
    provider.complete([])
    with pytest.raises(LLMError):
        provider.complete([])
    assert len(raw.calls) == 1
    assert provider.exhausted


def test_missing_token_usage_stays_unknown():
    provider = meter(MockLLMProvider(scripted=["ok"]))
    provider.complete([])
    assert provider.usage()["prompt_tokens"] is None


@pytest.mark.parametrize("bad", [True, 0, 6, "5", None])
def test_judge_invalid_scores_do_not_pass(bad):
    judge = meter(MockLLMProvider(scripted=[ToolCall("x", "grade_academic_task", {
        "correctness": bad, "completeness": 5, "clarity": 5, "rationale": "test"})]))
    result = grade_task(judge, source="source", task="task", output="answer", min_score=4)
    assert result["status"] == "INCONCLUSIVE"


@pytest.mark.parametrize("case_id", ["ask_training", "edit_intro_method", "edit_results_conclusion"])
def test_real_turn_service_pipeline_with_protocol_provider(tmp_path, policy, case_id):
    cases, fixture, _ = load_dataset()
    case = next(c for c in cases if c["id"] == case_id)
    result = run_document(case, meter(smoke_provider(case, fixture)), meter(smoke_judge()),
                          Config(llm_provider="mock", trace_enabled=False), fixture, tmp_path, policy)
    assert result["status"] == "PASS", result
    assert all(result["checks"].values())
    if case["suite"] == "editing":
        assert result["write_sections"] == [[target] for target in case["targets"]]
    assert (tmp_path / "input.tex").read_text(encoding="utf-8") == fixture


def test_smoke_report_cannot_be_promoted(tmp_path, policy):
    from tests.eval.benchmark.core import write_json
    report = evidence(policy)
    report["evaluation_kind"] = "protocol_smoke"
    source, dest = tmp_path / "report.json", tmp_path / "baseline.json"
    write_json(source, report)
    assert main(["baseline", "--report", str(source), "--output", str(dest)]) == 2
    assert not dest.exists()


def test_baseline_never_overwrites_existing_file(tmp_path, policy):
    from tests.eval.benchmark.core import write_json
    source, dest = tmp_path / "report.json", tmp_path / "baseline.json"
    write_json(source, evidence(policy))
    dest.write_text("existing-approved-baseline", encoding="utf-8")
    assert main(["baseline", "--report", str(source), "--output", str(dest)]) == 2
    assert dest.read_text(encoding="utf-8") == "existing-approved-baseline"


def test_spawn_worker_preserves_app_result(tmp_path, policy):
    cases, fixture, _ = load_dataset()
    case = next(c for c in cases if c["id"] == "ask_accuracy")
    result = run_trial(case, fixture, replace(Config(), llm_provider="mock"),
                       {"max_calls_per_trial": 24, "judge_max_calls": 2, "trial_timeout_s": 20},
                       policy, {"independent_judge": False}, "protocol_smoke", tmp_path)
    assert result["status"] == "PASS", result
    assert result["usage"]["calls"] >= 3  # analyze, body tool, final answer


def test_meter_records_provider_failure():
    class Broken:
        def complete(self, messages, **opts):
            raise LLMError("test failure")
    provider = meter(Broken())
    with pytest.raises(LLMError):
        provider.complete([])
    assert provider.usage()["provider_error_count"] == 1


def test_meter_preserves_reported_tokens():
    class Known:
        def complete(self, messages, **opts):
            return LLMResponse("ok", prompt_tokens=10, completion_tokens=4)
    provider = meter(Known())
    provider.complete([])
    assert provider.usage()["prompt_tokens"] == 10


def test_hard_process_deadline_marks_unknown_usage(tmp_path, policy):
    cases, fixture, _ = load_dataset()
    result = run_trial(cases[0], fixture, Config(llm_provider="mock"),
                       {"max_calls_per_trial": 24, "judge_max_calls": 2, "trial_timeout_s": 0.001},
                       policy, {}, "protocol_smoke", tmp_path)
    assert result["status"] == "ERROR"
    assert result["usage_unknown"] is True
    assert result["reasons"] == ["trial_timeout"]


def test_planning_correct_intent_wrong_target_is_failure():
    from tests.eval.benchmark.adapters import run_planning
    import json
    cases, _, _ = load_dataset()
    case = next(c for c in cases if c["id"] == "polish_method")
    provider = meter(MockLLMProvider(scripted=[json.dumps({
        "operations": [{"intent": "polish", "verbatim": "polish Method", "requirement_ids": []}],
        "requirements": [], "unmapped_text": []}), json.dumps({"slots": {
            "section_id": {"value": "introduction", "status": "present"},
            "goal": {"value": "clarity", "status": "present"}}})]))
    result = run_planning(case, provider)
    assert result["checks"]["intent_sequence"] is True
    assert result["checks"]["target_1"] is False


def test_duplicate_annotator_ids_do_not_make_gold_ready(tmp_path):
    from tests.eval.benchmark.core import write_json
    data = read_json(HERE.parent / "relevance_batch" / "manifest.json")
    for case in data["cases"]:
        case["label"].update(status="adjudicated", annotators=["person-a", "person-a"])
    path = tmp_path / "manifest.json"
    write_json(path, data)
    _, _, metadata = load_dataset(path)
    assert not metadata["label_readiness"]["relevance_adjudicated"]
