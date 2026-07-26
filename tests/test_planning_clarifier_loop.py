"""intent-planning Task 17 验收：Clarifier + resolve_unmapped + Confirmation + 循环。

- 已给字段不问；多缺口一次问清；≤2 轮转 needs_input
- unavailable 用「自动理解失败」措辞
- unmapped 强制处置（补充/忽略/取消）；未处置不 ready
- 确认/否决/修改；修改→新版本重建不丢原请求；否决不产 ConfirmedPlan
- 有界循环：happy → ConfirmedPlan；超限 → revision_limit_exceeded
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
from paper_agent.tasking.clarifier import (
    ConfirmDecision,
    UnmappedDisposition,
    clarify,
    confirm_plan,
    resolve_unmapped,
)
from paper_agent.tasking.contracts import (
    CallStatus,
    ConfirmedPlan,
    ExtractionResult,
    FieldStatus,
    PlanningExit,
    PlanStatus,
    RawOperation,
    SlotSource,
    SlotValue,
)
from paper_agent.tasking.plan_builder import build_plan
from paper_agent.tasking.planning_loop import run_planning_loop


def _present(value, req_ids=()):
    return SlotValue(value=value, field_status=FieldStatus.PRESENT,
                     source=SlotSource.USER_EXPLICIT, requirement_ids=tuple(req_ids))


def _ext(slots):
    return ExtractionResult(call_status=CallStatus.SUCCESS, slots=slots)


# --- Fake interactor ---

class FakeInteractor:
    def __init__(self, *, field_answers=None, unmapped=None, confirm=None):
        self._fields = list(field_answers or [])
        self._unmapped = list(unmapped or [])
        self._confirm = list(confirm or [])
        self.prompts: list[str] = []

    def ask_field(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._fields.pop(0) if self._fields else ""

    def resolve_unmapped(self, item: str) -> UnmappedDisposition:
        return self._unmapped.pop(0) if self._unmapped else UnmappedDisposition("ignore")

    def confirm_plan(self, summary: str) -> ConfirmDecision:
        return self._confirm.pop(0) if self._confirm else ConfirmDecision("accept")


# ============ Clarifier ============

def test_clarify_does_not_ask_present_fields():
    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色引言")],
                      [_ext({"section_id": _present("introduction")})],
                      canonical_requirements=())
    it = FakeInteractor()
    out = clarify(plan, ask=it.ask_field)
    assert out.resolved is True
    assert it.prompts == []            # 已给 section_id，不追问


def test_clarify_asks_all_gaps_in_one_round():
    # rewrite 缺 section_id + direction（都 required+must_ask）
    plan = build_plan("t", [RawOperation(Intent.REWRITE, "改写")],
                      [_ext({})], canonical_requirements=())
    it = FakeInteractor(field_answers=["introduction", "补充论证"])
    out = clarify(plan, ask=it.ask_field)
    assert out.resolved is True
    assert out.rounds_used == 1         # 一轮问清两个缺口
    assert len(it.prompts) == 2


def test_clarify_gives_up_after_two_rounds():
    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色")],
                      [_ext({})], canonical_requirements=())
    it = FakeInteractor(field_answers=["", "", "", ""])  # 一直空 → 补不上
    out = clarify(plan, ask=it.ask_field, max_rounds=2)
    assert out.resolved is False
    assert out.rounds_used == 2


def test_clarify_unavailable_uses_autofail_wording():
    # section_id 因抽取失败 → unavailable（required）
    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色引言")],
                      [_ext({"section_id": SlotValue(field_status=FieldStatus.UNAVAILABLE)})],
                      canonical_requirements=())
    it = FakeInteractor(field_answers=["introduction"])
    clarify(plan, ask=it.ask_field)
    assert any("自动理解暂时失败" in p for p in it.prompts)


def test_clarify_refill_marks_user_confirmed():
    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色")],
                      [_ext({})], canonical_requirements=())
    it = FakeInteractor(field_answers=["introduction"])
    out = clarify(plan, ask=it.ask_field)
    sid = out.plan.operations[0].slots["section_id"]
    assert sid.field_status is FieldStatus.PRESENT
    assert sid.source is SlotSource.USER_CONFIRMED


# ============ resolve_unmapped ============

def _plan_with_unmapped():
    p = build_plan("t", [RawOperation(Intent.POLISH, "润色引言")],
                   [_ext({"section_id": _present("introduction")})],
                   canonical_requirements=(), unmapped_text=["帮我订机票"])
    return p


def test_resolve_unmapped_ignore_clears_and_proceeds():
    it = FakeInteractor(unmapped=[UnmappedDisposition("ignore")])
    out = resolve_unmapped(_plan_with_unmapped(), it)
    assert out.action == "proceed"
    assert out.plan.unmapped_requirements == ()


def test_resolve_unmapped_supplement():
    it = FakeInteractor(unmapped=[UnmappedDisposition("supplement", "其实是让我改标题")])
    out = resolve_unmapped(_plan_with_unmapped(), it)
    assert out.action == "supplement"
    assert "改标题" in out.supplement_text


def test_resolve_unmapped_cancel():
    it = FakeInteractor(unmapped=[UnmappedDisposition("cancel")])
    out = resolve_unmapped(_plan_with_unmapped(), it)
    assert out.action == "cancel"


# ============ Confirmation ============

def test_confirm_disabled_auto_accepts():
    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色引言")],
                      [_ext({"section_id": _present("introduction")})],
                      canonical_requirements=())
    d = confirm_plan(plan, FakeInteractor(), require_confirmation=False)
    assert d.kind == "accept"


def test_confirm_reject():
    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色引言")],
                      [_ext({"section_id": _present("introduction")})],
                      canonical_requirements=())
    it = FakeInteractor(confirm=[ConfirmDecision("reject")])
    d = confirm_plan(plan, it)
    assert d.kind == "reject"


# ============ 有界循环 ============

def _analyze_json(ops):
    return json.dumps({
        "operations": [{"intent": i, "verbatim": v, "requirement_ids": []} for i, v in ops],
        "requirements": [], "unmapped_text": [],
    }, ensure_ascii=False)


def _extract_json(slots):
    return json.dumps({"slots": {k: {"value": v, "status": "present"} for k, v in slots.items()}},
                      ensure_ascii=False)


def test_loop_happy_path_returns_confirmed_plan():
    llm = MockLLMProvider(scripted=[
        _analyze_json([("polish", "润色引言")]),
        _extract_json({"section_id": "introduction"}),
    ])
    it = FakeInteractor(confirm=[ConfirmDecision("accept")])
    out = run_planning_loop("润色引言", llm=llm, interactor=it,
                            current_sections=["introduction"], current_format="tex")
    assert isinstance(out, ConfirmedPlan)
    assert [op.intent for op in out.plan.operations] == [Intent.POLISH]
    assert out.plan.status is PlanStatus.CONFIRMED


def test_loop_modify_rebuilds_without_losing_original():
    # 第一轮只 polish；用户修改「再转成 docx」→ 第二轮 polish+convert（不丢原请求）
    llm = MockLLMProvider(scripted=[
        _analyze_json([("polish", "润色引言")]),
        _extract_json({"section_id": "introduction"}),
        _analyze_json([("polish", "润色引言"), ("convert_format", "转成 docx")]),
        _extract_json({"section_id": "introduction"}),
        _extract_json({"target_ext": "docx"}),
    ])
    it = FakeInteractor(confirm=[ConfirmDecision("modify", "再转成 docx"),
                                 ConfirmDecision("accept")])
    out = run_planning_loop("润色引言", llm=llm, interactor=it,
                            current_sections=["introduction"], current_format="tex")
    assert isinstance(out, ConfirmedPlan)
    assert [op.intent for op in out.plan.operations] == [Intent.POLISH, Intent.CONVERT_FORMAT]
    assert out.plan.plan_version == 2      # 修订重建版本+1


def test_loop_reject_returns_exit_no_write():
    llm = MockLLMProvider(scripted=[
        _analyze_json([("polish", "润色引言")]),
        _extract_json({"section_id": "introduction"}),
    ])
    it = FakeInteractor(confirm=[ConfirmDecision("reject")])
    out = run_planning_loop("润色引言", llm=llm, interactor=it,
                            current_sections=["introduction"], current_format="tex")
    assert isinstance(out, PlanningExit)
    assert out.status is PlanStatus.REJECTED


def test_loop_revision_limit_exceeded():
    # 每轮都 modify → 3 轮后超限
    scripted = []
    for _ in range(3):
        scripted.append(_analyze_json([("polish", "润色引言")]))
        scripted.append(_extract_json({"section_id": "introduction"}))
    llm = MockLLMProvider(scripted=scripted)
    it = FakeInteractor(confirm=[ConfirmDecision("modify", "再改改"),
                                 ConfirmDecision("modify", "再改改"),
                                 ConfirmDecision("modify", "再改改")])
    out = run_planning_loop("润色引言", llm=llm, interactor=it,
                            current_sections=["introduction"], current_format="tex",
                            max_plan_revisions=3)
    assert isinstance(out, PlanningExit)
    assert out.status is PlanStatus.REVISION_LIMIT_EXCEEDED


def test_loop_clarifies_missing_then_confirms():
    # analyze 出 polish 但没给章节 → 抽取器也没给 → 循环内澄清补 section_id
    llm = MockLLMProvider(scripted=[
        _analyze_json([("polish", "润色一下")]),
        _extract_json({}),                          # 无 section_id
    ])
    it = FakeInteractor(field_answers=["introduction"],
                        confirm=[ConfirmDecision("accept")])
    out = run_planning_loop("润色一下", llm=llm, interactor=it,
                            current_sections=["introduction"], current_format="tex")
    assert isinstance(out, ConfirmedPlan)
    assert out.plan.operations[0].slots["section_id"].value == "introduction"


def test_loop_rejects_convert_not_terminal():
    llm = MockLLMProvider(scripted=[
        _analyze_json([("convert_format", "转 docx"), ("polish", "润色引言")]),
        _extract_json({"target_ext": "docx"}),
        _extract_json({"section_id": "introduction"}),
    ])
    it = FakeInteractor(confirm=[ConfirmDecision("accept")])
    out = run_planning_loop("先转docx再润色", llm=llm, interactor=it,
                            current_sections=["introduction"], current_format="tex")
    assert isinstance(out, PlanningExit)
    assert out.status is PlanStatus.REJECTED


# ============ clarify 加固：结构化回填 / 人性化问法 / target 刷新 ============

def test_clarify_prompt_no_internal_slot_name_leak():
    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色")],
                      [_ext({})], canonical_requirements=())
    it = FakeInteractor(field_answers=["introduction"])
    clarify(plan, ask=it.ask_field)
    joined = " ".join(it.prompts)
    assert "section_id" not in joined          # 不泄露内部槽名
    assert "章节" in joined                     # 用人类标签


def test_clarify_structure_kind_maps_natural_language_to_enum():
    # add_section 缺 structure_kind（枚举）；用户答「列表式」→ 映射为 list
    plan = build_plan("t", [RawOperation(Intent.ADD_SECTION, "加章节")],
                      [_ext({"title": _present("Discussion"),
                             "purpose": _present("讨论")})],
                      canonical_requirements=())
    it = FakeInteractor(field_answers=["列表式"])
    out = clarify(plan, ask=it.ask_field)
    assert out.resolved is True
    sk = out.plan.operations[0].slots["structure_kind"]
    assert sk.value == "list"
    assert sk.field_status is FieldStatus.PRESENT
    assert sk.source is SlotSource.USER_CONFIRMED


def test_clarify_structure_kind_choice_letter():
    plan = build_plan("t", [RawOperation(Intent.ADD_SECTION, "加章节")],
                      [_ext({"title": _present("Discussion"),
                             "purpose": _present("讨论")})],
                      canonical_requirements=())
    it = FakeInteractor(field_answers=["a"])   # a → desc
    out = clarify(plan, ask=it.ask_field)
    assert out.plan.operations[0].slots["structure_kind"].value == "desc"


def test_clarify_structure_kind_invalid_then_valid():
    plan = build_plan("t", [RawOperation(Intent.ADD_SECTION, "加章节")],
                      [_ext({"title": _present("Discussion"),
                             "purpose": _present("讨论")})],
                      canonical_requirements=())
    it = FakeInteractor(field_answers=["流程图", "b"])  # 第一轮非法，第二轮 list
    out = clarify(plan, ask=it.ask_field, max_rounds=2)
    assert out.resolved is True
    assert out.plan.operations[0].slots["structure_kind"].value == "list"
    # 第二轮的提示应带「没法用」措辞
    assert any("没法用" in p for p in it.prompts)


def test_clarify_format_operations_parsed_to_format_ops():
    from paper_agent.tools.docx_format import FormatOp
    plan = build_plan("t", [RawOperation(Intent.FORMAT_DOCX, "排版")],
                      [_ext({})], canonical_requirements=())
    it = FakeInteractor(field_answers=["正文宋体小四，标题居中"])
    out = clarify(plan, ask=it.ask_field)
    assert out.resolved is True
    ops = out.plan.operations[0].slots["operations"].value
    assert isinstance(ops, list) and ops
    assert all(isinstance(o, FormatOp) for o in ops)


def test_clarify_refreshes_target_after_fill():
    # section_id 缺失、澄清补上后，op.target 应刷新为该章节（不再陈旧为空）
    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色")],
                      [_ext({})], canonical_requirements=())
    assert plan.operations[0].target == ""      # 建计划时缺 section_id → target 空
    it = FakeInteractor(field_answers=["introduction"])
    out = clarify(plan, ask=it.ask_field)
    assert out.plan.operations[0].target == "introduction"   # 已刷新
