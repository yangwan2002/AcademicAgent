"""单管线 TurnMetrics。"""

from __future__ import annotations

import json

from paper_agent.observability.turn_metrics import TurnMetrics


def test_counts_accumulate():
    m = TurnMetrics()
    m.record_turn()
    m.record_turn()
    m.record_plan_status("done")
    m.record_planning_exit("no_action")
    m.record_planning_failed("RuntimeError")
    m.record_readonly_fallback()
    m.record_cancelled()

    assert m.turns == 2
    assert m.plan_status == {"done": 1}
    assert m.planning_exit == {"no_action": 1}
    assert m.planning_failed == {"RuntimeError": 1}
    assert m.readonly_fallbacks == 1
    assert m.cancelled == 1


def test_summary_has_no_legacy_or_shadow_language():
    m = TurnMetrics(turns=1, readonly_fallbacks=1)
    text = m.summary()
    assert "Planning Turn" in text
    assert "AskLoop" in text
    assert "legacy" not in text.lower()
    assert "shadow" not in text.lower()


def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "metrics.json"
    m = TurnMetrics(
        turns=3,
        plan_status={"done": 2},
        planning_exit={"rejected": 1},
        planning_failed={"ValueError": 1},
        readonly_fallbacks=4,
        cancelled=1,
    )
    assert m.save(str(path)) is True
    assert TurnMetrics.load(str(path)) == m

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "served_by" not in raw
    assert "fallback_to_legacy" not in raw
    assert "shadow" not in raw


def test_load_missing_or_corrupt_is_empty(tmp_path):
    missing = tmp_path / "missing.json"
    assert TurnMetrics.load(str(missing)) == TurnMetrics()
    missing.write_text("not-json", encoding="utf-8")
    assert TurnMetrics.load(str(missing)) == TurnMetrics()


def test_load_ignores_old_dual_pipeline_fields(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps({
            "turns": 2,
            "served_by": {"legacy_fallback": 2},
            "fallback_to_legacy": {"RuntimeError": 2},
            "shadow": {"matches_legacy": 3},
        }),
        encoding="utf-8",
    )
    loaded = TurnMetrics.load(str(path))
    assert loaded.turns == 2
    assert loaded.readonly_fallbacks == 0


def test_save_failure_never_raises(tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    assert TurnMetrics(turns=1).save(str(directory)) is False
