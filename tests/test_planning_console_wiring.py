"""chat.py 接线验收：ConsolePlanningInteractor + 三态分派。

- 交互器：确认/否决/修改、未映射三种处置、baseline 保留/回滚、澄清读行
- 分派：off 不接管；shadow 旁路后仍走旧链路（返回 False）；active 接管（返回 True）
- 理解层异常不拖垮 REPL（退回旧流程）
"""

from __future__ import annotations

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.application.console_interactor import ConsolePlanningInteractor
from paper_agent.application.turn_service import PlanningShadowResult, TurnResult
from paper_agent.runtime.plan_executor import KEEP, ROLLBACK
from paper_agent.tasking.contracts import (
    OperationResult,
    OperationStatus,
    PlanExecutionReport,
    PlanningExit,
    PlanStatus,
)


def _interactor(answers):
    fed = list(answers)
    printed: list[str] = []
    it = ConsolePlanningInteractor(
        ask=lambda prompt: fed.pop(0) if fed else "",
        out=printed.append,
    )
    return it, printed


# ============ 交互器 ============

def test_confirm_accept_on_enter():
    it, _ = _interactor([""])
    assert it.confirm_plan("摘要").kind == "accept"


def test_confirm_accept_on_yes():
    it, _ = _interactor(["y"])
    assert it.confirm_plan("摘要").kind == "accept"


def test_confirm_reject_on_n():
    it, _ = _interactor(["n"])
    assert it.confirm_plan("摘要").kind == "reject"


def test_confirm_free_text_is_modify():
    it, _ = _interactor(["再顺便转成 docx"])
    d = it.confirm_plan("摘要")
    assert d.kind == "modify"
    assert d.text == "再顺便转成 docx"


def test_confirm_shows_summary():
    it, printed = _interactor([""])
    it.confirm_plan("计划包含 2 个操作")
    assert any("计划包含 2 个操作" in p for p in printed)


def test_unmapped_supplement():
    it, _ = _interactor(["1", "其实是让我改标题"])
    d = it.resolve_unmapped("帮我订机票")
    assert d.action == "supplement"
    assert d.text == "其实是让我改标题"


def test_unmapped_ignore_default():
    it, _ = _interactor(["2"])
    assert it.resolve_unmapped("x").action == "ignore"
    it2, _ = _interactor([""])          # 空输入 → 忽略
    assert it2.resolve_unmapped("x").action == "ignore"


def test_unmapped_cancel():
    it, _ = _interactor(["3"])
    assert it.resolve_unmapped("x").action == "cancel"


def test_disposition_keep_by_default():
    it, _ = _interactor([""])
    assert it.ask_disposition(["o1"]) == KEEP


def test_disposition_rollback():
    it, _ = _interactor(["r"])
    assert it.ask_disposition(["o1"]) == ROLLBACK


def test_ask_field_strips():
    it, _ = _interactor(["  introduction  "])
    assert it.ask_field("要润色哪一章？") == "introduction"


# ============ chat.py 分派 ============

def _chat_module():
    path = os.path.join(_ROOT, "scripts", "chat.py")
    spec = importlib.util.spec_from_file_location("chat_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeTS:
    def __init__(self, mode, result=None, exc=None):
        self.mode = mode
        self._result = result
        self._exc = exc
        self.calls = 0

    def handle_turn(self, msg, **kw):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result


def test_chat_module_imports():
    mod = _chat_module()
    assert hasattr(mod, "_handle_planned_turn")
    assert hasattr(mod, "main")


def test_shadow_does_not_take_over(capsys):
    mod = _chat_module()
    shadow = PlanningShadowResult(
        turn_id="t", ok=True, operation_intents=("polish", "convert_format"),
        validation_result="ready", matches_legacy=False, added_latency_ms=12.0,
    )
    ts = _FakeTS("shadow", TurnResult(mode="shadow", turn_id="t", shadow=shadow))
    handled = mod._handle_planned_turn(ts, "先润色再转docx", session=None, facts=None,
                                       legacy_intent="convert_format")
    assert handled is False            # 旧链路仍要跑
    assert "[shadow]" in capsys.readouterr().out


def test_active_takes_over_and_prints_report(capsys):
    mod = _chat_module()
    report = PlanExecutionReport(
        plan_status=PlanStatus.DONE,
        operations=(
            OperationResult("o1", OperationStatus.SUCCEEDED, changed_working_copy=True,
                            message="已润色章节「Introduction」"),
            OperationResult("o2", OperationStatus.SUCCEEDED,
                            generated_outputs=("output/paper.docx",)),
        ),
    )
    ts = _FakeTS("active", TurnResult(mode="active", turn_id="t", report=report))
    handled = mod._handle_planned_turn(ts, "先润色再转docx", session=None, facts=None,
                                       legacy_intent="polish")
    assert handled is True             # 接管，不再走旧链路
    out = capsys.readouterr().out
    assert "done" in out
    assert "output/paper.docx" in out


def test_active_planning_exit_printed(capsys):
    mod = _chat_module()
    ts = _FakeTS("active", TurnResult(
        mode="active", turn_id="t",
        planning_exit=PlanningExit(status=PlanStatus.REJECTED, message="用户否决"),
    ))
    handled = mod._handle_planned_turn(ts, "润色", session=None, facts=None,
                                       legacy_intent="polish")
    assert handled is True
    out = capsys.readouterr().out
    assert "rejected" in out and "用户否决" in out


def test_planning_exception_falls_back_to_legacy(capsys):
    mod = _chat_module()
    ts = _FakeTS("active", exc=RuntimeError("boom"))
    handled = mod._handle_planned_turn(ts, "润色", session=None, facts=None,
                                       legacy_intent="polish")
    assert handled is False            # 退回旧流程，不炸退 REPL
    assert "改用原有流程" in capsys.readouterr().out


def test_cancel_is_handled_as_taken_over(capsys):
    from paper_agent.errors import OperationCancelled

    mod = _chat_module()
    ts = _FakeTS("active", exc=OperationCancelled())
    handled = mod._handle_planned_turn(ts, "润色", session=None, facts=None,
                                       legacy_intent="polish")
    assert handled is True             # 取消是明确终态，不重跑旧链路
    assert "已取消" in capsys.readouterr().out


# ============ #15：嵌套提问里的中止 ============

def _raising_interactor(exc):
    printed: list[str] = []

    def _ask(_prompt):
        raise exc

    return ConsolePlanningInteractor(ask=_ask, out=printed.append), printed


def test_ctrl_c_in_clarify_becomes_operation_cancelled():
    """KeyboardInterrupt 不是 Exception，Coordinator 捕获不到 → 会杀掉 REPL。
    必须在交互层转成 OperationCancelled，走既有取消路径。
    """
    import pytest

    from paper_agent.errors import OperationCancelled

    it, _ = _raising_interactor(KeyboardInterrupt())
    with pytest.raises(OperationCancelled):
        it.ask_field("要润色哪一章？")


def test_eof_in_confirm_becomes_operation_cancelled():
    import pytest

    from paper_agent.errors import OperationCancelled

    it, _ = _raising_interactor(EOFError())
    with pytest.raises(OperationCancelled):
        it.confirm_plan("摘要")


def test_quit_command_in_nested_prompt_is_abort_not_answer():
    """:quit 在嵌套提问里不能被当成章节名/约束文本。"""
    import pytest

    from paper_agent.errors import OperationCancelled

    it, _ = _interactor([":quit"])
    with pytest.raises(OperationCancelled):
        it.ask_field("要润色哪一章？")

    it2, _ = _interactor([":q"])
    with pytest.raises(OperationCancelled):
        it2.resolve_unmapped("帮我订机票")


def test_disposition_defaults_to_keep_on_interrupt():
    """处置询问发生在已出错之后：中止就按「保留已完成」，不丢用户成果。"""
    it, printed = _raising_interactor(KeyboardInterrupt())
    assert it.ask_disposition(["o1"]) == KEEP
    assert any("保留已完成" in p for p in printed)


# ============ #18：facts 合并而非覆盖 ============

def test_merge_refs_keeps_verified_retrieved_entries(tmp_path):
    from paper_agent.session.facts import KnownRef, SessionFacts

    facts = SessionFacts(path=str(tmp_path / "facts.json"))
    # 此前经检索 + 核验入库的文献
    facts.add_ref(KnownRef(id="1", bibkey="smith2020deep", title="Deep Nets",
                           verified=True))
    # 源文件扫描只能看到 \cite{key}，标题空、verified=False
    scanned = [
        KnownRef(id="", bibkey="smith2020deep", title="", verified=False),
        KnownRef(id="2", bibkey="jones2021", title="Another", verified=False),
    ]
    added = facts.merge_refs(scanned)

    by_key = {r.bibkey: r for r in facts.known_refs}
    assert added == 1                                   # 只新增 jones2021
    assert by_key["smith2020deep"].verified is True     # 核验状态未被降级
    assert by_key["smith2020deep"].title == "Deep Nets"  # 标题未被清空
    assert "jones2021" in by_key


def test_merge_refs_fills_missing_title():
    from paper_agent.session.facts import KnownRef, SessionFacts

    facts = SessionFacts(path="x")
    facts.add_ref(KnownRef(id="1", bibkey="k", title="", verified=False))
    facts.merge_refs([KnownRef(id="1", bibkey="k", title="From Source")])
    assert facts.known_refs[0].title == "From Source"


def test_merge_numbers_preserves_confirmed_flag():
    from paper_agent.session.facts import KnownNumber, SessionFacts

    facts = SessionFacts(path="x")
    facts.add_number("0.87", source="user_confirmed", confirmed=True)
    added = facts.merge_numbers([KnownNumber(value="0.87", confirmed=False),
                                 KnownNumber(value="0.92")])
    assert added == 1
    by_val = {n.value: n for n in facts.known_numbers}
    assert by_val["0.87"].confirmed is True     # 不被源文件扫描降级
