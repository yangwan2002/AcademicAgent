"""B-4 验收：Turn 归属计数（TurnMetrics + chat.py 接线）。

核心断言：**静默降级到旧栈必须留痕**，否则 E2E 数据分不清
「新栈处理了」与「新栈挂了、旧栈兜的」，实验结论无效。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.application.turn_service import PlanningShadowResult, TurnResult
from paper_agent.errors import OperationCancelled
from paper_agent.observability.turn_metrics import TurnMetrics
from paper_agent.tasking.contracts import (
    OperationResult,
    OperationStatus,
    PlanExecutionReport,
    PlanningExit,
    PlanStatus,
)


def _chat_module():
    path = os.path.join(_ROOT, "scripts", "chat.py")
    spec = importlib.util.spec_from_file_location("chat_metrics_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeTS:
    def __init__(self, mode, result=None, exc=None):
        self.mode = mode
        self._result = result
        self._exc = exc

    def handle_turn(self, msg, **kw):
        if self._exc is not None:
            raise self._exc
        return self._result


def _call(mod, ts, metrics):
    return mod._handle_planned_turn(
        ts, "润色引言", session=None, facts=None,
        legacy_intent="polish", metrics=metrics,
    )


# ============ TurnMetrics 本体 ============

def test_clean_when_no_fallback():
    m = TurnMetrics()
    m.record_turn()
    m.record_served_by("active")
    m.record_plan_status("done")
    assert m.fallback_count == 0
    assert m.attribution_is_clean is True
    assert "归属干净" in m.summary()


def test_fallback_makes_attribution_dirty():
    m = TurnMetrics()
    m.record_fallback("RuntimeError")
    assert m.fallback_count == 1
    assert m.attribution_is_clean is False


def test_summary_warns_loudly_on_fallback():
    """降级过就必须显式说「本次统计不可用于评估新栈」，不能让读者自己推。"""
    m = TurnMetrics()
    m.record_turn()
    m.record_fallback("ValueError")
    text = m.summary()
    assert "静默降级" in text
    assert "不可直接用于评估新栈" in text


def test_counts_accumulate_by_category():
    m = TurnMetrics()
    m.record_fallback("RuntimeError")
    m.record_fallback("RuntimeError")
    m.record_fallback("KeyError")
    assert m.fallback_to_legacy == {"RuntimeError": 2, "KeyError": 1}
    assert m.fallback_count == 3


def test_shadow_divergence_counted():
    m = TurnMetrics()
    m.record_shadow(ok=True, matches_legacy=True)
    m.record_shadow(ok=True, matches_legacy=False)
    m.record_shadow(ok=False, matches_legacy=None)
    assert m.shadow_matches_legacy == 1
    assert m.shadow_differs_from_legacy == 1
    assert m.shadow_incomplete == 1


def test_unknown_category_does_not_crash():
    m = TurnMetrics()
    m.record_fallback("")
    m.record_served_by(None)
    assert m.fallback_to_legacy == {"unknown": 1}
    assert m.served_by == {"unknown": 1}


# ============ 落盘 ============

def test_save_writes_atomic_json(tmp_path):
    m = TurnMetrics()
    m.record_turn()
    m.record_served_by("active")
    m.record_fallback("RuntimeError")
    path = str(tmp_path / "nested" / "turn_metrics.json")
    assert m.save(path) is True

    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["turns"] == 1
    assert data["fallback_count"] == 1
    assert data["attribution_is_clean"] is False
    assert data["fallback_to_legacy"] == {"RuntimeError": 1}
    assert not os.path.exists(path + ".tmp")      # 临时文件已被 replace


def test_save_failure_never_raises(tmp_path):
    """观测失败不拖垮 REPL：路径不可写只返回 False。"""
    m = TurnMetrics()
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    # 把文件当目录用 → 必然失败
    assert m.save(str(blocker / "sub" / "x.json")) is False


# ============ chat.py 接线 ============

def test_exception_records_fallback_with_category():
    """B-4 核心：理解层异常 → 旧栈接手，且必须记下类别。"""
    mod = _chat_module()
    m = TurnMetrics()
    handled = _call(mod, _FakeTS("active", exc=RuntimeError("boom")), m)

    assert handled is False                                  # 旧链路仍要跑
    assert m.fallback_to_legacy == {"RuntimeError": 1}
    assert m.served_by == {"legacy_fallback": 1}
    assert m.attribution_is_clean is False


def test_active_success_is_not_counted_as_fallback():
    mod = _chat_module()
    m = TurnMetrics()
    report = PlanExecutionReport(
        plan_status=PlanStatus.DONE,
        operations=(OperationResult("o1", OperationStatus.SUCCEEDED),),
    )
    handled = _call(mod, _FakeTS("active", TurnResult(
        mode="active", turn_id="t", report=report)), m)

    assert handled is True
    assert m.served_by == {"active": 1}
    assert m.plan_status == {"done": 1}
    assert m.attribution_is_clean is True


def test_active_planning_exit_counted_as_exit_not_failure():
    """明确出口（needs_input 等）是诚实终态，不是失败、也不是降级。"""
    mod = _chat_module()
    m = TurnMetrics()
    handled = _call(mod, _FakeTS("active", TurnResult(
        mode="active", turn_id="t",
        planning_exit=PlanningExit(status=PlanStatus.NEEDS_INPUT, message="理解失败"),
    )), m)

    assert handled is True
    assert m.planning_exit == {"needs_input": 1}
    assert m.fallback_count == 0
    assert m.active_failed == {}


def test_active_internal_failure_counted_separately_from_fallback():
    """新栈内部异常（Coordinator 捕获、未降级）与「降级到旧栈」是两件事。"""
    mod = _chat_module()
    m = TurnMetrics()
    report = PlanExecutionReport(plan_status=PlanStatus.FAILED)
    handled = _call(mod, _FakeTS("active", TurnResult(
        mode="active", turn_id="t", report=report,
        run_error_category="ZeroDivisionError",
    )), m)

    assert handled is True                        # 没降级，新栈自己出了报告
    assert m.active_failed == {"ZeroDivisionError": 1}
    assert m.fallback_count == 0                  # 关键：不算降级
    assert m.plan_status == {"failed": 1}


def test_cancel_counted_as_cancel_not_fallback():
    mod = _chat_module()
    m = TurnMetrics()
    handled = _call(mod, _FakeTS("active", exc=OperationCancelled()), m)

    assert handled is True
    assert m.cancelled == 1
    assert m.fallback_count == 0


def test_shadow_records_divergence_and_returns_false():
    mod = _chat_module()
    m = TurnMetrics()
    shadow = PlanningShadowResult(
        turn_id="t", ok=True, operation_intents=("polish", "convert_format"),
        validation_result="ready", matches_legacy=False,
    )
    handled = _call(mod, _FakeTS("shadow", TurnResult(
        mode="shadow", turn_id="t", shadow=shadow)), m)

    assert handled is False                       # shadow 只旁路，旧链路继续
    assert m.served_by == {"shadow": 1}
    assert m.shadow_differs_from_legacy == 1
    assert m.fallback_count == 0                  # shadow 不是降级


def test_missing_shadow_result_is_treated_as_fallback():
    """shadow 结果缺失 → 旧栈接手，同样要留痕（否则又是一条暗路）。"""
    mod = _chat_module()
    m = TurnMetrics()
    handled = _call(mod, _FakeTS("shadow", TurnResult(
        mode="shadow", turn_id="t", shadow=None)), m)

    assert handled is False
    assert m.fallback_to_legacy == {"shadow_result_missing": 1}


def test_metrics_is_optional_backward_compatible():
    """不传 metrics 时行为与改造前一致（既有调用点/测试不受影响）。"""
    mod = _chat_module()
    handled = mod._handle_planned_turn(
        _FakeTS("active", exc=RuntimeError("boom")),
        "润色", session=None, facts=None, legacy_intent="polish",
    )
    assert handled is False
