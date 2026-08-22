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

from paper_agent.intents.intent import Intent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tasking.clarifier import (
    ConfirmDecision,
    UnmappedDisposition,
    clarify,
    confirm_plan,
    resolve_unmapped,
)
from paper_agent.tasking.contracts import (
    AnalyzeResult,
    AtomicRequirement,
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
        self.confirm_calls = 0

    def ask_field(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._fields.pop(0) if self._fields else ""

    def resolve_unmapped(self, item: str) -> UnmappedDisposition:
        return self._unmapped.pop(0) if self._unmapped else UnmappedDisposition("ignore")

    def confirm_plan(self, summary: str) -> ConfirmDecision:
        self.confirm_calls += 1
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


def _extract_json_with_consumed(slots, consumed):
    return json.dumps({
        "slots": {
            k: {"value": v, "status": "present"} for k, v in slots.items()
        },
        "slot_consumed_requirements": consumed,
    }, ensure_ascii=False)


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


def test_loop_user_modify_reanalysis_gets_original_and_previous_plan():
    """真实 revision_drop："只做前一步"不能被当成一条独立新请求。"""
    original = "先润色引言，再把整篇转换成 docx"
    amendment = "只做前一步，后面不要了"
    seen: list[str] = []

    def context_sensitive_analyze(text, _llm):
        seen.append(text)
        if len(seen) == 1:
            return AnalyzeResult(
                call_status=CallStatus.SUCCESS,
                operations=(
                    RawOperation(Intent.POLISH, "润色引言"),
                    RawOperation(Intent.CONVERT_FORMAT, "转换成 docx"),
                ),
            )
        if (
            original in text
            and amendment in text
            and "上一版计划摘要" in text
            and "convert_format" in text
        ):
            return AnalyzeResult(
                call_status=CallStatus.SUCCESS,
                operations=(RawOperation(Intent.POLISH, "润色引言"),),
            )
        return AnalyzeResult(call_status=CallStatus.SUCCESS)

    llm = MockLLMProvider(scripted=[
        _extract_json({"section_id": "introduction"}),
        _extract_json({"target_ext": "docx"}),
        _extract_json({"section_id": "introduction"}),
    ])
    it = FakeInteractor(confirm=[
        ConfirmDecision("modify", amendment),
        ConfirmDecision("accept"),
    ])
    out = run_planning_loop(
        original,
        llm=llm,
        interactor=it,
        current_sections=["introduction"],
        current_format="tex",
        analyze_fn=context_sensitive_analyze,
    )

    assert isinstance(out, ConfirmedPlan), getattr(out, "message", out)
    assert [op.intent for op in out.plan.operations] == [Intent.POLISH]
    assert out.plan.plan_version == 2
    assert len(seen) == 2


def test_user_modify_filters_composite_labels_but_keeps_real_delta():
    """USER_MODIFY 也做 provenance 过滤，不能把摘要标签弹给用户处置。"""
    original = "润色引言"
    amendment = "不要改数字，保留引用"
    real_req = AtomicRequirement("r_real", "不要改数字", category="constraint")
    label_req = AtomicRequirement(
        "r_label", "计划包含 1 个操作（按执行序）", category="constraint"
    )
    analyze_calls = 0

    def analyze(text, _llm):
        nonlocal analyze_calls
        analyze_calls += 1
        if analyze_calls == 1:
            return AnalyzeResult(
                call_status=CallStatus.SUCCESS,
                operations=(RawOperation(Intent.POLISH, "润色引言"),),
            )
        assert amendment in text
        assert "计划包含 1 个操作（按执行序）" in text
        assert "目标=introduction" in text
        return AnalyzeResult(
            call_status=CallStatus.SUCCESS,
            operations=(RawOperation(
                Intent.POLISH,
                "润色引言，不要改数字",
                ("r_real", "r_label"),
            ),),
            requirements=(real_req, label_req),
            unmapped_text=(
                "保留引用",
                "目标=introduction",
                "未被本次修订明确删除或替换的操作与约束应继续保留。",
            ),
            project_facts=(
                ("research_gap", "目标=introduction", ("r_label",)),
            ),
        )

    class RecordingInteractor(FakeInteractor):
        def __init__(self):
            super().__init__(confirm=[
                ConfirmDecision("modify", amendment),
                ConfirmDecision("accept"),
            ])
            self.unmapped_items = []

        def resolve_unmapped(self, item):
            self.unmapped_items.append(item)
            return UnmappedDisposition("ignore")

    class RecordingFacts:
        def __init__(self):
            self.claims = {}

        def set_claim(self, key, value):
            self.claims[key] = value

        def save(self):
            pass

    it = RecordingInteractor()
    facts = RecordingFacts()
    llm = MockLLMProvider(scripted=[
        _extract_json({"section_id": "introduction"}),
        _extract_json_with_consumed(
            {
                "section_id": "introduction",
                "constraints": ["不要改数字"],
            },
            {"constraints": ["r_real"]},
        ),
    ])
    out = run_planning_loop(
        original,
        llm=llm,
        interactor=it,
        current_sections=["introduction"],
        current_format="tex",
        analyze_fn=analyze,
        facts=facts,
    )

    assert isinstance(out, ConfirmedPlan), getattr(out, "message", out)
    assert [d.requirement_id for d in out.plan.dispositions] == ["r_real"]
    assert out.plan.operations[0].slots["constraints"].value == ["不要改数字"]
    assert it.unmapped_items == ["保留引用"]
    assert facts.claims == {}


def test_loop_unmapped_supplement_reanalysis_keeps_original_operation():
    """补充说明要解释旧计划，不能让已识别的 polish 凭空消失。"""
    original = "润色引言，顺便处理一下票务"
    supplement = "票务不用处理，只保留原来的润色"
    seen: list[str] = []

    def context_sensitive_analyze(text, _llm):
        seen.append(text)
        if len(seen) == 1:
            return AnalyzeResult(
                call_status=CallStatus.SUCCESS,
                operations=(RawOperation(Intent.POLISH, "润色引言"),),
                unmapped_text=("处理一下票务",),
            )
        if (
            original in text
            and supplement in text
            and "上一版计划摘要" in text
            and "polish" in text
        ):
            return AnalyzeResult(
                call_status=CallStatus.SUCCESS,
                operations=(RawOperation(Intent.POLISH, "润色引言"),),
            )
        return AnalyzeResult(call_status=CallStatus.SUCCESS)

    llm = MockLLMProvider(scripted=[
        _extract_json({"section_id": "introduction"}),
        _extract_json({"section_id": "introduction"}),
    ])
    it = FakeInteractor(
        unmapped=[UnmappedDisposition("supplement", supplement)],
        confirm=[ConfirmDecision("accept")],
    )
    out = run_planning_loop(
        original,
        llm=llm,
        interactor=it,
        current_sections=["introduction"],
        current_format="tex",
        analyze_fn=context_sensitive_analyze,
    )

    assert isinstance(out, ConfirmedPlan), getattr(out, "message", out)
    assert [op.intent for op in out.plan.operations] == [Intent.POLISH]
    assert out.plan.plan_version == 2
    assert len(seen) == 2


def test_empty_user_revision_does_not_route_original_write_request_to_readonly():
    llm = MockLLMProvider(scripted=[
        _analyze_json([("polish", "润色引言")]),
        _extract_json({"section_id": "introduction"}),
        _analyze_json([]),
    ])
    it = FakeInteractor(confirm=[ConfirmDecision("modify", "取消现有计划")])
    out = run_planning_loop(
        "润色引言",
        llm=llm,
        interactor=it,
        current_sections=["introduction"],
        current_format="tex",
    )

    assert isinstance(out, PlanningExit)
    assert out.status is PlanStatus.NO_ACTION
    assert out.route_to_readonly is False


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


def test_display_fields_derived_from_slots_after_fill():
    """slots 是唯一真相源：澄清回填后，展示字段自然是新值（无需同步任何副本）。

    历史上 Operation 存过 target/goal/constraints 的快照，回填后不更新 → 确认摘要
    显示「目标=空」。现已删除这些字段，改为实时派生。
    """
    from paper_agent.tasking.clarifier import summarize_plan
    from paper_agent.tasking.plan_builder import operation_target

    plan = build_plan("t", [RawOperation(Intent.POLISH, "润色")],
                      [_ext({})], canonical_requirements=())
    assert operation_target(plan.operations[0].intent, plan.operations[0].slots) == ""
    it = FakeInteractor(field_answers=["introduction"])
    out = clarify(plan, ask=it.ask_field)
    op = out.plan.operations[0]
    assert operation_target(op.intent, op.slots) == "introduction"
    # 确认摘要里也是新值
    assert "introduction" in summarize_plan(out.plan)


# --- 忽略未映射要求时，账本记 ignored（不是凭空消失）---

def test_ignored_unmapped_recorded_in_dispositions():
    from paper_agent.tasking.contracts import (
        AtomicRequirement,
        DispositionStatus,
    )

    reqs = (
        AtomicRequirement(requirement_id="r1", verbatim="润色引言"),
        AtomicRequirement(requirement_id="r2", verbatim="帮我订机票"),
    )
    plan = build_plan(
        "t", [RawOperation(Intent.POLISH, "润色引言")],
        [_ext({"section_id": _present("introduction", req_ids=["r1"])})],
        canonical_requirements=reqs,
    )
    assert plan.unmapped_requirements == ("帮我订机票",)

    it = FakeInteractor(unmapped=[UnmappedDisposition("ignore")])
    out = resolve_unmapped(plan, it)
    assert out.action == "proceed"
    by_id = {d.requirement_id: d for d in out.plan.dispositions}
    assert by_id["r2"].status is DispositionStatus.IGNORED   # 有据可查
    assert by_id["r1"].status is DispositionStatus.MAPPED
    assert out.plan.unmapped_requirements == ()


# ============ P0：请求全部落在五意图之外 → NO_ACTION，不确认不执行 ============

def test_loop_returns_no_action_when_nothing_executable():
    """「帮我查重」这类请求：如实告知做不了，绝不走确认→空执行→报 done。"""
    llm = MockLLMProvider(scripted=[json.dumps({
        "operations": [],
        "requirements": [],
        "unmapped_text": ["帮我查一下这篇论文的重复率"],
    }, ensure_ascii=False)])
    it = FakeInteractor()
    out = run_planning_loop("帮我查一下这篇论文的重复率", llm=llm, interactor=it,
                            current_sections=["introduction"], current_format="tex")
    assert isinstance(out, PlanningExit)
    assert out.status is PlanStatus.NO_ACTION
    assert out.route_to_readonly is True
    assert "重复率" in out.message          # 说清为什么做不了
    assert it.confirm_calls == 0            # 不该请求确认


def test_no_action_message_without_unmapped():
    llm = MockLLMProvider(scripted=[json.dumps({
        "operations": [], "requirements": [], "unmapped_text": [],
    }, ensure_ascii=False)])
    out = run_planning_loop("???", llm=llm, interactor=FakeInteractor(),
                            current_sections=[], current_format="tex")
    assert out.status is PlanStatus.NO_ACTION
    # 没有任何语义证据的空包不等于明确的只读/不支持请求。
    assert out.route_to_readonly is False
    assert "未做任何改动" in out.message


# --- P0：重规划保留原始请求（不把失效原因当成新请求）---

def test_replan_context_carries_original_request():
    from paper_agent.tasking.contracts import ExecutionReplanContext

    llm = MockLLMProvider(scripted=[
        _analyze_json([("polish", "润色引言")]),
        _extract_json({"section_id": "introduction"}),
    ])
    plan0 = build_plan("t", [RawOperation(Intent.POLISH, "润色引言")],
                       [_ext({"section_id": _present("introduction")})],
                       canonical_requirements=())
    replan = ExecutionReplanContext(
        original_request="先润色引言，再加 Discussion",
        previous_plan=plan0,
        remaining_operations=plan0.operations,
        completed_operation_ids=(),
        replan_reason="章节「Discussion」已不存在",
    )
    it = FakeInteractor(confirm=[ConfirmDecision("accept")])
    out = run_planning_loop("先润色引言，再加 Discussion", llm=llm, interactor=it,
                            current_sections=["introduction"], current_format="tex",
                            replan=replan)
    assert isinstance(out, ConfirmedPlan)
    # 送给理解层的文本包含原始请求与失效原因，而不是只有 replan_reason
    prompt = "\n".join(m.content for call in llm.calls for m in call)
    assert "先润色引言，再加 Discussion" in prompt
    assert "章节「Discussion」已不存在" in prompt
    assert out.plan.plan_version == 2         # 基于旧计划的新版本


def test_execution_replan_keeps_value_supplied_during_clarification():
    """澄清答案只在旧计划里；重规划过滤系统提示时仍必须保留它。"""
    from paper_agent.tasking.contracts import ExecutionReplanContext

    clarified_section = SlotValue(
        value="introduction",
        field_status=FieldStatus.PRESENT,
        source=SlotSource.USER_CONFIRMED,
    )
    plan0 = build_plan(
        "t",
        [RawOperation(Intent.POLISH, "帮我润色一下")],
        [_ext({"section_id": clarified_section})],
        canonical_requirements=(),
        source_text="帮我润色一下",
    )
    replan = ExecutionReplanContext(
        original_request="帮我润色一下",
        previous_plan=plan0,
        remaining_operations=plan0.operations,
        completed_operation_ids=(),
        replan_reason="章节索引已变化",
    )
    clarified_req = AtomicRequirement(
        "r_clarified", "introduction", category="target"
    )
    system_req = AtomicRequirement(
        "r_system", "章节索引已变化", category="constraint"
    )

    def analyze(text, _llm):
        assert "目标=introduction" in text
        return AnalyzeResult(
            call_status=CallStatus.SUCCESS,
            operations=(RawOperation(
                Intent.POLISH,
                "润色 introduction",
                ("r_clarified", "r_system"),
            ),),
            requirements=(clarified_req, system_req),
            unmapped_text=("目标=introduction", "章节索引已变化"),
            project_facts=(
                ("research_gap", "章节索引已变化", ("r_system",)),
            ),
        )

    class NoUnmappedInteractor(FakeInteractor):
        def resolve_unmapped(self, item):
            raise AssertionError(f"system scaffold leaked into unmapped: {item}")

    llm = MockLLMProvider(scripted=[
        _extract_json_with_consumed(
            {"section_id": "introduction"},
            {"section_id": ["r_clarified"]},
        ),
    ])
    out = run_planning_loop(
        "帮我润色一下",
        llm=llm,
        interactor=NoUnmappedInteractor(confirm=[ConfirmDecision("accept")]),
        current_sections=["introduction"],
        current_format="tex",
        replan=replan,
        analyze_fn=analyze,
    )

    assert isinstance(out, ConfirmedPlan), getattr(out, "message", out)
    assert [d.requirement_id for d in out.plan.dispositions] == ["r_clarified"]
    retained = out.plan.dispositions[0]
    assert retained.slot_names == ("section_id",)


def test_empty_execution_replan_does_not_route_to_readonly():
    from paper_agent.tasking.contracts import ExecutionReplanContext

    plan0 = build_plan(
        "t",
        [RawOperation(Intent.POLISH, "润色引言")],
        [_ext({"section_id": _present("introduction")})],
        canonical_requirements=(),
    )
    replan = ExecutionReplanContext(
        original_request="润色引言",
        previous_plan=plan0,
        remaining_operations=plan0.operations,
        completed_operation_ids=(),
        replan_reason="章节索引已变化",
    )
    llm = MockLLMProvider(scripted=[_analyze_json([])])
    out = run_planning_loop(
        "润色引言",
        llm=llm,
        interactor=FakeInteractor(),
        current_sections=["introduction"],
        current_format="tex",
        replan=replan,
    )

    assert isinstance(out, PlanningExit)
    assert out.status is PlanStatus.NO_ACTION
    assert out.route_to_readonly is False
