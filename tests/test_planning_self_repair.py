"""静态校验拒绝 → 带确定性提示自修复一次。

真机复现（2026-08-01）：用户对已有「1 引言」章节说「帮我完善我的引言」，
理解层产出 `add_section(title="引言")`，校验器回：

    rejected（section_exists:章节「引言」已存在，应改用 rewrite。）

校验器**已经知道正确做法**（消息原文就写着「应改用 rewrite」），却只是拒绝，
把纠错工作推给用户去改措辞。这与 backlog B-2 里 `fix_bibliography` 是同一性质的
责任转移：系统有足够信息自行完成，却要求用户先学会系统的内部规则。

本文件锁住：
1. 可修复问题 → 回灌提示重规划一次并成功（不是直接拒绝）
2. 不可修复问题 → 仍然立刻拒绝，不浪费 LLM 调用
3. 混合问题 → 整体拒绝（不做半修复）
4. 自修复有界，且用尽后报告的是**原始**校验问题，不是「超过修订次数」
5. 提示只用确定性信息；初次 analyze 仍不看文档状态
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tasking.clarifier import ConfirmDecision, UnmappedDisposition
from paper_agent.tasking.contracts import (
    ConfirmedPlan,
    PlanningExit,
    PlanStatus,
    RevisionReason,
    ValidationIssue,
    ValidationOutcome,
)
from paper_agent.tasking.plan_validator import (
    CONVERT_NOT_TERMINAL,
    FORMAT_LACKS_SECTIONS,
    SECTION_EXISTS,
    SECTION_NOT_FOUND,
    UNSUPPORTED_INTENT,
)
from paper_agent.tasking.planning_loop import (
    _MAX_VALIDATION_REPAIRS,
    _repair_hint,
    run_planning_loop,
)

SECTIONS = ["1_引言", "2_相关工作", "1 引言", "2 相关工作"]


class _It:
    def __init__(self, *, field_answers=None, confirm=None):
        self._fields = list(field_answers or [])
        self._confirm = list(confirm or [])
        self.confirm_calls = 0

    def ask_field(self, prompt: str) -> str:
        return self._fields.pop(0) if self._fields else ""

    def resolve_unmapped(self, item: str) -> UnmappedDisposition:
        return UnmappedDisposition("ignore")

    def confirm_plan(self, summary: str) -> ConfirmDecision:
        self.confirm_calls += 1
        return self._confirm.pop(0) if self._confirm else ConfirmDecision("accept")


def _analyze_json(ops):
    return json.dumps({
        "operations": [
            {"intent": i, "verbatim": v, "requirement_ids": []} for i, v in ops
        ],
        "requirements": [], "unmapped_text": [],
    }, ensure_ascii=False)


def _extract_json(slots):
    return json.dumps(
        {"slots": {k: {"value": v, "status": "present"} for k, v in slots.items()}},
        ensure_ascii=False,
    )


def _outcome(*codes):
    return ValidationOutcome(
        result="rejected",
        issues=tuple(
            ValidationIssue(f"op{n}", c, message=f"msg-{c}")
            for n, c in enumerate(codes, 1)
        ),
    )


# ============================================ _repair_hint 判据


def test_hint_for_section_exists_names_rewrite():
    hint = _repair_hint(_outcome(SECTION_EXISTS), SECTIONS)
    assert hint
    assert "rewrite" in hint
    assert "add_section" in hint


def test_hint_for_section_not_found_lists_existing_sections():
    hint = _repair_hint(_outcome(SECTION_NOT_FOUND), SECTIONS)
    assert hint
    assert "1 引言" in hint          # 给出可选项，而不是让 LLM 继续猜
    assert "2 相关工作" in hint


def test_hint_for_empty_document_says_so():
    hint = _repair_hint(_outcome(SECTION_NOT_FOUND), [])
    assert "没有任何章节" in hint


def test_convert_not_terminal_is_deliberately_not_repairable():
    """满足「校验器知道答案」但**不满足「不改变用户要什么」**，故必须排除。

    可选的修复只有「挪到最后」（改了操作顺序，而顺序有语义：
    「转 docx 再润色」≠「润色再转 docx」）或「本轮不做转换」（丢掉一条要求）。
    这种取舍必须由用户做，系统替他决定就是静默改需求。
    """
    assert _repair_hint(_outcome(CONVERT_NOT_TERMINAL), SECTIONS) == ""


def test_no_hint_for_unrepairable():
    """格式不支持 / 意图不存在：重规划改不了，不得浪费调用。"""
    assert _repair_hint(_outcome(FORMAT_LACKS_SECTIONS), SECTIONS) == ""
    assert _repair_hint(_outcome(UNSUPPORTED_INTENT), SECTIONS) == ""


def test_no_hint_for_mixed_issues():
    """混了不可修复的 → 整体拒绝，不做半修复。"""
    assert _repair_hint(
        _outcome(SECTION_EXISTS, FORMAT_LACKS_SECTIONS), SECTIONS
    ) == ""


def test_no_hint_when_no_issues():
    assert _repair_hint(_outcome(), SECTIONS) == ""


def test_hint_carries_original_messages():
    """修复提示必须带上原始问题原文，否则理解层不知道具体哪里错了。"""
    hint = _repair_hint(_outcome(SECTION_EXISTS), SECTIONS)
    assert f"msg-{SECTION_EXISTS}" in hint


# ============================================ 循环行为（真机场景）


def test_regression_add_section_on_existing_section_self_repairs():
    """真机场景：LLM 先给 add_section(引言) → 自修复成 rewrite(引言) → 成功。"""
    llm = MockLLMProvider(scripted=[
        # 第一轮：错的
        _analyze_json([("add_section", "完善我的引言")]),
        _extract_json({"title": "引言"}),
        # 第二轮：收到修复提示后改对
        _analyze_json([("rewrite", "完善我的引言")]),
        _extract_json({"section_id": "引言", "direction": "扩写成正式引言"}),
    ])
    it = _It(confirm=[ConfirmDecision("accept")])
    out = run_planning_loop("帮我完善我的引言", llm=llm, interactor=it,
                            current_sections=SECTIONS, current_format="md")
    assert isinstance(out, ConfirmedPlan), getattr(out, "message", out)
    assert [op.intent for op in out.plan.operations] == [Intent.REWRITE]


def test_self_repair_is_observable_in_trace():
    class _Bus:
        def __init__(self):
            self.events = []

        def publish(self, kind, **payload):
            self.events.append((getattr(kind, "value", str(kind)), payload))

    llm = MockLLMProvider(scripted=[
        _analyze_json([("add_section", "完善我的引言")]),
        _extract_json({"title": "引言"}),
        _analyze_json([("rewrite", "完善我的引言")]),
        _extract_json({"section_id": "引言", "direction": "扩写"}),
    ])
    bus = _Bus()
    run_planning_loop("帮我完善我的引言", llm=llm, interactor=_It(),
                      current_sections=SECTIONS, current_format="md", bus=bus)
    phases = [p.get("phase") for _k, p in bus.events if "phase" in p]
    assert "self_repair" in phases


def test_unrepairable_rejects_without_extra_llm_call():
    """不支持章节的格式 + rewrite：不可修复，必须一次就拒，不能再烧一轮理解调用。

    原先用 docx 举例；docx 已支持章节级就地编辑（见 test_docx_inplace.py），
    故改用 pdf —— 待验证的是"不可修复即刻拒绝"，与具体格式无关。
    """
    llm = MockLLMProvider(scripted=[
        _analyze_json([("rewrite", "改写引言")]),
        _extract_json({"section_id": "引言", "direction": "扩写"}),
    ])
    out = run_planning_loop("改写引言", llm=llm, interactor=_It(),
                            current_sections=[], current_format="pdf")
    assert isinstance(out, PlanningExit)
    assert out.status is PlanStatus.REJECTED
    assert FORMAT_LACKS_SECTIONS in out.message


def test_repair_is_bounded_and_reports_original_issue():
    """修复后仍不对 → 报**原始**校验问题，不是「超过修订次数」。"""
    llm = MockLLMProvider(scripted=[
        _analyze_json([("add_section", "完善我的引言")]),
        _extract_json({"title": "引言"}),
        _analyze_json([("add_section", "完善我的引言")]),   # 还是错的
        _extract_json({"title": "引言"}),
    ])
    out = run_planning_loop("帮我完善我的引言", llm=llm, interactor=_It(),
                            current_sections=SECTIONS, current_format="md")
    assert isinstance(out, PlanningExit)
    assert out.status is PlanStatus.REJECTED
    assert SECTION_EXISTS in out.message
    assert "修订次数" not in out.message


def test_repair_budget_is_one():
    assert _MAX_VALIDATION_REPAIRS == 1


def test_revision_reason_exists():
    assert RevisionReason.VALIDATION_REPAIR.value == "validation_repair"


def test_valid_plan_never_triggers_repair():
    """校验通过时不得有任何自修复行为（不改变既有 happy path）。"""
    llm = MockLLMProvider(scripted=[
        _analyze_json([("rewrite", "改写引言")]),
        _extract_json({"section_id": "引言", "direction": "扩写"}),
    ])
    it = _It(confirm=[ConfirmDecision("accept")])
    out = run_planning_loop("改写引言", llm=llm, interactor=it,
                            current_sections=SECTIONS, current_format="md")
    assert isinstance(out, ConfirmedPlan)
    assert it.confirm_calls == 1
