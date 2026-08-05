"""Task 5 B1 config, composition and offline end-to-end wiring."""

from __future__ import annotations

import re

import pytest

from paper_agent.application.literature_composition import (
    CompositionUnavailable,
    build_writing_orchestrator,
)
from paper_agent.application.literature_flow import FlowState, WritingEvidenceRequest
from paper_agent.application.literature_ports import (
    checkpoint_from_dict,
    throughput_policy_hash,
)
from paper_agent.config import Config, load_config
from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
)
from paper_agent.literature.relevance import RelevanceCriteria
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.session.facts import SessionFacts
from paper_agent.workspace.models import ReferenceEntry


class _BatchAwareReviewer:
    def __init__(self):
        self.batch_calls = 0
        self.per_item_calls = 0

    def complete(self, messages, **opts):
        tool_name = opts.get("forced_tool_name", "")
        if tool_name == "report_relevance_batch":
            self.batch_calls += 1
            user = "\n".join(m.content for m in messages if m.role == "user")
            keys = re.findall(r"candidate_key=(c\d+)", user)
            arguments = {
                "results": [
                    {
                        "candidate_key": key,
                        "verdict": "relevant",
                        "confidence": "high",
                    }
                    for key in keys
                ]
            }
        else:
            self.per_item_calls += 1
            arguments = {"verdict": "relevant", "confidence": "high"}
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id="1", name=tool_name, arguments=arguments)],
        )


class _Decision:
    def __init__(self, action="accept"):
        self.action = action
        self.note = ""


class _AutoPort:
    def confirm_retrieval_batch(self, summary): return _Decision()
    def resolve_quota_gap(self, report): return _Decision()
    def confirm_evidence_bundle(self, summary): return _Decision()
    def resolve_final_gate(self, report): return _Decision()


def _config(**overrides):
    values = dict(
        literature_writing_integration=True,
        retrieval_provider="mock",
    )
    values.update(overrides)
    return Config(**values)


def _build(tmp_path, config, reviewer=None, *, estimator=None, entries=()):
    reviewer = reviewer or _BatchAwareReviewer()
    facts = SessionFacts(path=str(tmp_path / "facts.json"))
    orchestrator = build_writing_orchestrator(
        config,
        writer_llm=reviewer,
        retrieval_provider=MockRetrievalProvider(entries=entries),
        facts=facts,
        relevance_token_estimator=estimator,
    )
    return orchestrator, facts, reviewer


def test_b1_defaults_are_zero_regression_off():
    config = Config()
    assert config.literature_relevance_batch_enabled is False
    assert config.literature_relevance_batch_size == 1
    assert config.literature_relevance_batch_input_tokens is None
    assert config.literature_batch_retry_limit == 1
    assert config.literature_pipeline_reorder_enabled is False


def test_disabled_b1_does_not_construct_active_runtime(tmp_path):
    orchestrator, _facts, _reviewer = _build(tmp_path, _config())
    assert orchestrator._deps.active_relevance_runtime is None


def test_enabled_b1_builds_one_frozen_active_runtime(tmp_path):
    orchestrator, _facts, _reviewer = _build(
        tmp_path,
        _config(
            literature_relevance_batch_enabled=True,
            literature_relevance_batch_size=4,
        ),
    )
    runtime = orchestrator._deps.active_relevance_runtime
    assert runtime is not None
    assert runtime.window_limits.batch_size == 4
    assert orchestrator._judge_key == runtime.judge_key
    assert "relevance-batch-v1" in runtime.judge_key


def test_enabled_b1_rejects_size_one_and_unbounded_retry(tmp_path):
    with pytest.raises(CompositionUnavailable, match="batch_size"):
        _build(
            tmp_path,
            _config(literature_relevance_batch_enabled=True),
        )
    with pytest.raises(CompositionUnavailable, match="retry_limit"):
        _build(
            tmp_path,
            _config(
                literature_relevance_batch_enabled=True,
                literature_relevance_batch_size=2,
                literature_batch_retry_limit=2,
            ),
        )


def test_token_limit_requires_reliable_injected_estimator(tmp_path):
    config = _config(
        literature_relevance_batch_enabled=True,
        literature_relevance_batch_size=2,
        literature_relevance_batch_input_tokens=4000,
    )
    with pytest.raises(CompositionUnavailable, match="tokenizer"):
        _build(tmp_path, config)
    orchestrator, _facts, _reviewer = _build(
        tmp_path,
        config,
        estimator=lambda text: max(1, len(text) // 4),
    )
    assert orchestrator._deps.active_relevance_runtime.token_estimator is not None


def test_environment_fields_are_wired(monkeypatch):
    monkeypatch.setenv("PAPER_LIT_RELEVANCE_BATCH_ENABLED", "true")
    monkeypatch.setenv("PAPER_LIT_RELEVANCE_BATCH_SIZE", "6")
    monkeypatch.setenv("PAPER_LIT_RELEVANCE_BATCH_INPUT_CHARS", "15000")
    monkeypatch.setenv("PAPER_LIT_RELEVANCE_BATCH_OUTPUT_TOKENS", "3000")
    monkeypatch.setenv("PAPER_LIT_BATCH_RETRY_LIMIT", "0")
    monkeypatch.setenv("PAPER_LIT_PIPELINE_REORDER_ENABLED", "true")
    config = load_config()
    assert config.literature_relevance_batch_enabled is True
    assert config.literature_relevance_batch_size == 6
    assert config.literature_relevance_batch_input_chars == 15000
    assert config.literature_relevance_batch_output_tokens == 3000
    assert config.literature_batch_retry_limit == 0
    assert config.literature_pipeline_reorder_enabled is True


def test_mock_offline_b1_reaches_ready_without_per_item_reviewer(tmp_path):
    entries = [
        ReferenceEntry(
            id="arxiv:1706.03762",
            title="Attention Is All You Need",
            authors=["Vaswani"],
            year=2017,
            source_id="1706.03762",
            source="arxiv",
            abstract="We propose the Transformer based solely on attention mechanisms.",
            work_identifiers={"arxiv": "1706.03762"},
        )
    ]
    reviewer = _BatchAwareReviewer()
    orchestrator, facts, _ = _build(
        tmp_path,
        _config(
            literature_relevance_batch_enabled=True,
            literature_relevance_batch_size=4,
            literature_relevance_batch_output_tokens=2000,
        ),
        reviewer,
        entries=entries,
    )
    quota = QuotaSpec(
        QuotaKind.CITATION,
        QuotaScope.ACTUALLY_CITED,
        count_constraints=(
            CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1),
        ),
    )
    outcome = orchestrator.run(
        WritingEvidenceRequest(
            criteria=RelevanceCriteria(topic="attention transformer"),
            citation_quota=quota,
            initial_query="attention",
            operation_id="op-b1",
        ),
        facts=facts,
        port=_AutoPort(),
    )
    assert outcome.state is FlowState.READY_TO_WRITE
    assert outcome.bundle is not None and outcome.bundle.entries
    assert reviewer.batch_calls == 1
    assert reviewer.per_item_calls == 0


def test_old_checkpoint_without_snapshot_migrates_to_explicit_legacy_policy():
    checkpoint = checkpoint_from_dict(
        {
            "flow_id": "old",
            "operation_id": "op",
            "task_fingerprint": "tf",
            "quota_hash": "qh",
            "state": "needs_input",
        }
    )
    assert checkpoint.throughput_policy["flags"] == {
        "batch": False,
        "reorder": False,
        "concurrency": False,
    }
    assert checkpoint.throughput_policy_hash == throughput_policy_hash(
        checkpoint.throughput_policy
    )


def test_resume_rejects_changed_or_tampered_throughput_snapshot(tmp_path):
    entries = [
        ReferenceEntry(
            id="arxiv:1706.03762",
            title="Attention Is All You Need",
            authors=["Vaswani"],
            year=2017,
            source_id="1706.03762",
            source="arxiv",
            abstract="Transformer evidence with attention mechanisms.",
            work_identifiers={"arxiv": "1706.03762"},
        )
    ]
    first, facts, _reviewer = _build(
        tmp_path,
        _config(
            literature_relevance_batch_enabled=True,
            literature_relevance_batch_size=4,
        ),
        entries=entries,
    )
    quota = QuotaSpec(
        QuotaKind.CITATION,
        QuotaScope.ACTUALLY_CITED,
        count_constraints=(
            CountConstraint(CountDimension.TOTAL, CountOperator.MIN, 1),
        ),
    )
    request = WritingEvidenceRequest(
        criteria=RelevanceCriteria(topic="attention transformer"),
        citation_quota=quota,
        initial_query="attention",
        operation_id="op-snapshot",
    )
    paused = first.run(request, facts=facts, port=None)
    assert paused.state is FlowState.NEEDS_BATCH_CONFIRMATION
    assert paused.checkpoint.throughput_policy["flags"]["batch"] is True

    changed, _same_facts_unused, _reviewer2 = _build(
        tmp_path,
        _config(
            literature_relevance_batch_enabled=True,
            literature_relevance_batch_size=3,
        ),
        entries=entries,
    )
    stale = changed.resume(paused.checkpoint.flow_id, request, facts=facts)
    assert stale.state is FlowState.CANCELLED
    assert "STALE_THROUGHPUT_POLICY" in stale.message

    row = facts.get_literature_flow(paused.checkpoint.flow_id)
    row["throughput_policy"]["batch_size"] = 99
    facts.put_literature_flow(row)
    tampered = first.resume(paused.checkpoint.flow_id, request, facts=facts)
    assert tampered.state is FlowState.CANCELLED
    assert "TAMPERED_THROUGHPUT_POLICY" in tampered.message
