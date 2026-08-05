"""约束保真度：用户表达的修饰语必须到达抽取器（spec `constraint-fidelity`）。

真机缺陷（2026-08-01，active 模式）：

    用户：我目前小论文只写了一个框架，我现在需要去写我的引言部分，
          你能帮我完善我的引言部分吗，要求参考文献10，英文8，中文2
    系统：有一条要求我没能安排进任何操作：「参考文献10」  → 忽略
          有一条要求我没能安排进任何操作：「英文8」       → 忽略
          有一条要求我没能安排进任何操作：「中文2」       → 忽略
          计划包含 1 个操作：rewrite，目标=引言部分

三条配额全部落 unmapped，计划里没有 `citation_quota`，执行会产出零引用的引言。

**根因不是缺槽位**（实测 `REWRITE_SPEC.slots` 含 `citation_quota` 且有
`execution_binding`，也在 tool schema 里），而是 `RawOperation.requirement_ids`
—— analyzer 一直在填「本操作覆盖哪些要求」，下游一处都没读。抽取器只看到
analyzer 裁过的 `verbatim` 切片「完善我的引言部分」，里面一个数字都没有。

本文件锁住：
1. 路由：归属于操作的要求必须进入抽取器视野（Property 1）
2. 隔离：不属于本操作的要求**不得**进入视野（Property 4，**修复前不成立**）
3. 降级：analyzer 未声明归属时不得比现状更差
4. 空壳：object 型槽位的空容器不算「有取值」，否则验证器会被哄骗
5. 记账规则未被改动（Slot 消费粒度）
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tasking.contracts import (
    AtomicRequirement,
    CallStatus,
    FieldStatus,
    RawOperation,
    SlotSource,
)
from paper_agent.tasking.extractors import build_extractor
from paper_agent.tasking.extractors.base import (
    build_extractor_user_text,
    build_tool_schema,
)
from paper_agent.tasking.intent_spec import (
    POLISH_SPEC,
    REWRITE_SPEC,
    CITATION_QUOTA_SLOT_SPEC,
)
from paper_agent.tasking.operation_analyzer import (
    ATTRIBUTION_CLAIMED,
    ATTRIBUTION_FALLBACK_ALL,
    ATTRIBUTION_SOLE_OPERATION,
    requirements_for,
)
from paper_agent.tasking.plan_builder import build_plan

# 真机原话与 analyzer 实际的切分结果
REAL_MESSAGE = (
    "我目前小论文只写了一个框架，我现在需要去写我的引言部分，"
    "你能帮我完善我的引言部分吗，要求参考文献10，英文8，中文2"
)
QUOTA_REQS = (
    AtomicRequirement(requirement_id="r1", verbatim="参考文献10"),
    AtomicRequirement(requirement_id="r2", verbatim="英文8"),
    AtomicRequirement(requirement_id="r3", verbatim="中文2"),
)


def _op(intent=Intent.REWRITE, verbatim="完善我的引言部分", req_ids=()):
    return RawOperation(intent=intent, verbatim=verbatim,
                        requirement_ids=tuple(req_ids))


# ============================================ 槽位本来就在（排除错误假设）


def test_rewrite_has_citation_quota_slot_with_binding():
    """「rewrite 没有文献相关槽位」这个假设不成立——缺的是输入，不是槽位。"""
    slot = REWRITE_SPEC.slots.get("citation_quota")
    assert slot is not None
    assert slot.execution_binding is not None
    assert slot.value_type == "object"


def test_citation_quota_reaches_the_model_schema():
    props = build_tool_schema(REWRITE_SPEC, "rewrite")["function"]["parameters"]["properties"]
    assert "citation_quota" in props
    dims = props["citation_quota"]["properties"]["constraints"]["items"]["properties"]
    assert set(dims["dimension"]["enum"]) == {"total", "language.zh", "language.en"}


def test_polish_deliberately_lacks_citation_quota():
    """对照：polish 确实没有配额槽，故「润色引言要引10篇」落 unmapped 是正确行为。"""
    assert "citation_quota" not in POLISH_SPEC.slots


# ============================================ Property 1：归属要求进入视野


def test_regression_quota_text_reaches_extractor():
    """真机场景：三条配额原话必须出现在抽取器的 user 消息里。"""
    text = build_extractor_user_text(_op(req_ids=["r1", "r2", "r3"]), QUOTA_REQS)
    assert "完善我的引言部分" in text
    for v in ("参考文献10", "英文8", "中文2"):
        assert v in text, f"{v!r} 没能到达抽取器"


def test_user_text_without_requirements_is_unchanged():
    """无要求时逐字等于 verbatim（不引入额外文本）。"""
    assert build_extractor_user_text(_op(), ()) == "完善我的引言部分"
    assert build_extractor_user_text(_op(), None) == "完善我的引言部分"


def test_user_text_dedups_when_already_in_verbatim():
    """analyzer 已把修饰语摘进切片时不重复追加。"""
    op = _op(verbatim="完善引言，参考文献10")
    text = build_extractor_user_text(op, (QUOTA_REQS[0],))
    assert text.count("参考文献10") == 1
    assert text == "完善引言，参考文献10"      # 无追加


def test_user_text_no_double_punctuation():
    op = _op(verbatim="完善我的引言部分，")
    text = build_extractor_user_text(op, (QUOTA_REQS[0],))
    assert "。。" not in text
    assert "，。" not in text


def test_user_text_handles_empty_verbatim():
    text = build_extractor_user_text(_op(verbatim=""), QUOTA_REQS)
    assert text.startswith("要求：")
    assert "参考文献10" in text


def test_user_text_skips_blank_requirement_verbatim():
    reqs = (AtomicRequirement(requirement_id="r9", verbatim="   "),)
    assert build_extractor_user_text(_op(), reqs) == "完善我的引言部分"


# ============================================ Property 4：多操作隔离


def test_multi_operation_isolation():
    """不属于本操作的要求**不得**出现在它的视野里。

    这条属性在修复**前不成立**（`_understand` 把全部 requirements 传给每个抽取器），
    故它是新建立的不变量，不属于保持性检查。
    """
    reqs = (
        AtomicRequirement(requirement_id="a1", verbatim="别改公式"),
        AtomicRequirement(requirement_id="b1", verbatim="篇幅和现在差不多"),
    )
    op_a = _op(Intent.POLISH, "润色引言，别改公式", req_ids=["a1"])
    op_b = _op(Intent.REWRITE, "改写结论", req_ids=["b1"])

    owned_a, _ = requirements_for(op_a, reqs, 2)
    owned_b, _ = requirements_for(op_b, reqs, 2)
    text_a = build_extractor_user_text(op_a, owned_a)
    text_b = build_extractor_user_text(op_b, owned_b)

    assert "别改公式" in text_a and "篇幅和现在差不多" not in text_a
    assert "篇幅和现在差不多" in text_b and "别改公式" not in text_b


# ============================================ 归属三档与降级


def test_attribution_claimed_filters_by_id():
    owned, why = requirements_for(_op(req_ids=["r2"]), QUOTA_REQS, 2)
    assert why == ATTRIBUTION_CLAIMED
    assert [r.requirement_id for r in owned] == ["r2"]


def test_attribution_filters_hallucinated_ids():
    """引用了不存在的 requirement → 忽略，与 plan_builder 同口径。"""
    owned, why = requirements_for(_op(req_ids=["r1", "nope"]), QUOTA_REQS, 2)
    assert why == ATTRIBUTION_CLAIMED
    assert [r.requirement_id for r in owned] == ["r1"]


def test_attribution_all_hallucinated_falls_back():
    owned, why = requirements_for(_op(req_ids=["x", "y"]), QUOTA_REQS, 2)
    assert why == ATTRIBUTION_FALLBACK_ALL
    assert len(owned) == 3


def test_attribution_sole_operation_takes_everything():
    """单操作 + analyzer 未声明 → 全部归它（真机场景，无歧义）。"""
    owned, why = requirements_for(_op(), QUOTA_REQS, 1)
    assert why == ATTRIBUTION_SOLE_OPERATION
    assert len(owned) == 3


def test_attribution_multi_operation_fallback_is_not_worse_than_before():
    """多操作 + 未声明 → 退回现状（列全部），不得让抽取器什么都看不到。"""
    owned, why = requirements_for(_op(), QUOTA_REQS, 3)
    assert why == ATTRIBUTION_FALLBACK_ALL
    assert len(owned) == 3


def test_attribution_reasons_are_distinct():
    assert len({ATTRIBUTION_CLAIMED, ATTRIBUTION_SOLE_OPERATION,
                ATTRIBUTION_FALLBACK_ALL}) == 3


# ============================================ 空壳对象不算有取值


def _extract_slot(llm_payload: dict, spec_intent=Intent.REWRITE):
    """跑一次真实抽取路径（mock LLM），返回 slots。"""
    llm = MockLLMProvider(scripted=[json.dumps(llm_payload, ensure_ascii=False)])
    ex = build_extractor(spec_intent, llm)
    res = ex.pre_extract(_op(spec_intent), ())
    assert res.call_status is CallStatus.SUCCESS
    return res.slots


def test_empty_quota_shell_is_not_present():
    """`{"filters": [], "constraints": []}` 语义上等于「没有配额」。

    若记 present，「该要求是否被承载」的判定会对空壳返回 true —— 验证器会报
    「已修复」而实际没有配额。
    """
    slots = _extract_slot({
        "section_id": "引言", "direction": "扩写",
        "citation_quota": {"filters": [], "constraints": []},
    })
    cq = slots["citation_quota"]
    # citation_quota 声明了 clarify_if_user_invalid → 模型试过但产出空壳 → INVALID
    assert CITATION_QUOTA_SLOT_SPEC.clarify_if_user_invalid is True
    assert cq.field_status is FieldStatus.INVALID


def test_non_empty_quota_is_present():
    slots = _extract_slot({
        "section_id": "引言", "direction": "扩写",
        "citation_quota": {
            "filters": [],
            "constraints": [{"dimension": "total", "operator": "bare", "value": 10}],
        },
    })
    assert slots["citation_quota"].field_status is FieldStatus.PRESENT


def test_absent_quota_key_is_user_absent():
    slots = _extract_slot({"section_id": "引言", "direction": "扩写"})
    assert slots["citation_quota"].field_status is FieldStatus.USER_ABSENT


@pytest.mark.parametrize("shell", [
    {},
    {"filters": [], "constraints": [], "explicit_scope": ""},
    {"filters": None, "constraints": []},
])
def test_various_empty_shells(shell):
    slots = _extract_slot({"section_id": "引言", "direction": "扩写",
                           "citation_quota": shell})
    assert slots["citation_quota"].field_status is not FieldStatus.PRESENT


def test_array_slots_keep_existing_behavior():
    """**刻意不覆盖 array 型槽位**：空列表仍记 present/user_explicit。

    改判 user_absent 会让最终 source 从 user_explicit 变 defaulted ——
    可观测的行为变化，与本次缺陷无关，会破坏保持性比对。
    """
    llm = MockLLMProvider(scripted=[json.dumps(
        {"section_id": "引言", "goal": "保守润色", "constraints": []},
        ensure_ascii=False)])
    ex = build_extractor(Intent.POLISH, llm)
    slots = ex.pre_extract(_op(Intent.POLISH), ()).slots
    assert slots["constraints"].field_status is FieldStatus.PRESENT
    assert slots["constraints"].value == []
    assert slots["constraints"].source is SlotSource.USER_EXPLICIT


# ============================================ 记账规则未被改动


def test_accounting_still_slot_granular():
    """路由改了，**记账不变**：只有落进 SlotValue.requirement_ids 才算 mapped。

    「仅挂在 RawOperation.requirement_ids 上」依然不算——那只是路由信息。
    """
    from paper_agent.tasking.contracts import (
        DispositionStatus, ExtractionResult, SlotValue,
    )

    raw = _op(req_ids=["r1", "r2", "r3"])       # 操作声明覆盖三条
    ext = ExtractionResult(
        call_status=CallStatus.SUCCESS,
        slots={"section_id": SlotValue(value="引言",
                                       field_status=FieldStatus.PRESENT,
                                       requirement_ids=("r1",))},  # 只有一条落进槽
    )
    plan = build_plan("t", [raw], [ext], QUOTA_REQS)
    by_id = {d.requirement_id: d.status for d in plan.dispositions}
    assert by_id["r1"] is DispositionStatus.MAPPED
    assert by_id["r2"] is DispositionStatus.UNMAPPED    # 挂在操作上不算
    assert by_id["r3"] is DispositionStatus.UNMAPPED


# ============================================ 端到端（mock LLM，走真实理解链路）


def test_end_to_end_quota_survives_understanding():
    """真机原话 → 理解链路 → 计划里有 citation_quota、零 unmapped。

    mock LLM 模拟一个**遵守 schema 的**模型：analyzer 声明归属，抽取器在看到
    配额文本后填槽。这条测试锁住「链路能把配额送到底」，模型是否配合由 golden
    真机评测回答。
    """
    from paper_agent.tasking.planning_loop import run_planning_loop
    from paper_agent.tasking.clarifier import ConfirmDecision, UnmappedDisposition
    from paper_agent.tasking.contracts import ConfirmedPlan

    analyze_json = json.dumps({
        "operations": [{
            "intent": "rewrite",
            "verbatim": "完善我的引言部分",
            "requirement_ids": ["r1", "r2", "r3"],
        }],
        "requirements": [
            {"requirement_id": "r1", "verbatim": "参考文献10"},
            {"requirement_id": "r2", "verbatim": "英文8"},
            {"requirement_id": "r3", "verbatim": "中文2"},
        ],
        "unmapped_text": [],
    }, ensure_ascii=False)

    extract_json = json.dumps({
        "section_id": {"value": "引言", "status": "present"},
        "direction": {"value": "扩写成正式引言", "status": "present"},
        "citation_quota": {
            "value": {
                "filters": [],
                "constraints": [
                    {"dimension": "total", "operator": "bare", "value": 10,
                     "verbatim": "参考文献10"},
                    {"dimension": "language.en", "operator": "bare", "value": 8,
                     "verbatim": "英文8"},
                    {"dimension": "language.zh", "operator": "bare", "value": 2,
                     "verbatim": "中文2"},
                ],
            },
            "status": "present",
            "requirement_ids": ["r1", "r2", "r3"],
        },
    }, ensure_ascii=False)

    class _It:
        def __init__(self):
            self.unmapped_asks = 0

        def ask_field(self, prompt):
            return ""

        def resolve_unmapped(self, item):
            self.unmapped_asks += 1
            return UnmappedDisposition("ignore")

        def confirm_plan(self, summary):
            return ConfirmDecision("accept")

    it = _It()
    out = run_planning_loop(
        REAL_MESSAGE,
        llm=MockLLMProvider(scripted=[analyze_json, extract_json]),
        interactor=it,
        current_sections=["1_引言", "1 引言"],
        current_format="md",
    )
    assert isinstance(out, ConfirmedPlan), getattr(out, "message", out)
    assert it.unmapped_asks == 0, "不应再追问已被承载的要求"

    quota = out.plan.operations[0].slots["citation_quota"]
    assert quota.field_status is FieldStatus.PRESENT
    dims = {c["dimension"]: c["value"] for c in quota.value["constraints"]}
    assert dims == {"total": 10, "language.en": 8, "language.zh": 2}
    assert out.plan.unmapped_requirements == ()


def test_extractor_actually_receives_quota_text():
    """直接检查发给模型的 user 消息内容（不是只看结果）。"""
    llm = MockLLMProvider(scripted=[json.dumps(
        {"section_id": "引言", "direction": "扩写"}, ensure_ascii=False)])
    ex = build_extractor(Intent.REWRITE, llm)
    ex.pre_extract(_op(req_ids=["r1", "r2", "r3"]), QUOTA_REQS)

    user_msgs = [m.content for call in llm.calls for m in call if m.role == "user"]
    assert user_msgs, "没有 user 消息"
    joined = "\n".join(user_msgs)
    for v in ("参考文献10", "英文8", "中文2"):
        assert v in joined, f"{v!r} 没进 user 消息"


# ============================================ 检索意图的论文上下文
#
# 真机（2026-08-01）：用户对已有引言骨架说「完善我的引言部分」，合成器收到的
# user 消息只有两行 ——
#     写作操作：rewrite
#     改写方向：完善引言部分
# 于是 LLM 只能产出「引言这一章通常引什么」的通用模板：
#     semantic_topic='研究背景、问题动机与现有方法的局限性分析'
#     seeds=[research gap identification, limitations of existing approaches, ...]
# 那是它在该输入下能给出的最好结果 —— 模型没错，输入是空的。
#
# 需要的信息全在工作副本里：目标章节正文写着「UAV与UGV感知范围互补」
# 「Relay中间视角」「主共视支撑域」，章节标题写着「跨视角图像匹配研究」。


def _search_ctx(**kw):
    from paper_agent.literature.retrieval.search_intent import SearchContext
    base = dict(operation_kind="rewrite", target_title="引言",
                direction="完善引言部分")
    base.update(kw)
    return SearchContext(**base)


def test_paper_context_reaches_synthesizer_prompt():
    """论文题目 / 章节清单 / 目标章节正文都必须进 LLM 的 user 消息。"""
    from paper_agent.tasking.query_synthesizer import LLMSearchIntentSynthesizer

    ctx = _search_ctx(
        paper_title="面向空地协同SLAM的大视角大尺度差图像匹配研究",
        paper_section_titles=("1 引言", "2.1跨视角图像匹配研究", "3 方法"),
        target_section_body="从UAV与UGV感知范围和观测精度互补这一背景出发，"
                            "说明跨平台视觉关联…Relay中间视角…主共视支撑域预测。",
    )
    msgs = LLMSearchIntentSynthesizer(MockLLMProvider(scripted=["{}"]))._build_messages(ctx)
    user = "\n".join(m.content for m in msgs if m.role == "user")

    assert "面向空地协同SLAM" in user
    assert "跨视角图像匹配研究" in user
    assert "主共视支撑域" in user, "目标章节正文没进 prompt"


def test_paper_title_comes_first_in_prompt():
    """论文主题决定检索什么，写作动作只决定侧面 —— 故题目排在 operation 之前。"""
    from paper_agent.tasking.query_synthesizer import LLMSearchIntentSynthesizer

    ctx = _search_ctx(paper_title="空地协同图像匹配")
    msgs = LLMSearchIntentSynthesizer(MockLLMProvider(scripted=["{}"]))._build_messages(ctx)
    user = "\n".join(m.content for m in msgs if m.role == "user")
    assert user.index("空地协同图像匹配") < user.index("写作操作")


def test_prompt_unchanged_when_no_paper_context():
    """新字段默认空 ⇒ 不传时 prompt 与既有逐字一致（保持性）。"""
    from paper_agent.tasking.query_synthesizer import LLMSearchIntentSynthesizer

    msgs = LLMSearchIntentSynthesizer(MockLLMProvider(scripted=["{}"]))._build_messages(_search_ctx())
    user = "\n".join(m.content for m in msgs if m.role == "user")
    assert user.startswith("写作操作：rewrite")
    assert "论文题目" not in user and "全文章节" not in user


def test_language_hint_is_actionable_not_jargon():
    """旧版只印内部枚举名（language.zh），模型无从知道该做什么 → 中文配额填不满。"""
    from paper_agent.tasking.query_synthesizer import LLMSearchIntentSynthesizer

    ctx = _search_ctx(requested_dimensions=("language.zh", "language.en"))
    msgs = LLMSearchIntentSynthesizer(MockLLMProvider(scripted=["{}"]))._build_messages(ctx)
    user = "\n".join(m.content for m in msgs if m.role == "user")
    assert "中文" in user and "英文" in user
    assert "关键词本身要用中文" in user


def test_requested_dimensions_derived_from_quota():
    """`requested_dimensions` 此前全仓库无人填写。只传语言维度，不传篇数。"""
    from paper_agent.literature.quota import RawCountClause, RawQuotaSpec
    from paper_agent.runtime.handlers import _requested_language_dimensions

    spec = RawQuotaSpec(constraints=(
        RawCountClause(dimension="total", operator="bare", value=10),
        RawCountClause(dimension="language.en", operator="bare", value=8),
        RawCountClause(dimension="language.zh", operator="bare", value=2),
    ))
    dims = _requested_language_dimensions(spec)
    assert set(dims) == {"language.en", "language.zh"}
    assert "total" not in dims, "篇数维度不得外泄给合成器（design D2 越权红线）"


def test_requested_dimensions_tolerates_none():
    from paper_agent.runtime.handlers import _requested_language_dimensions
    assert _requested_language_dimensions(None) == ()


# --- 确定性投影：章节标题进 seed 池，且通用章节名不得使整个请求崩掉 ---


def test_deterministic_projection_filters_generic_section_titles():
    """章节标题天然含「引言」「Introduction」。

    `SearchSeed` 构造函数对通用章节名**直接抛异常**，而 `build_seeds` 的过滤契约
    只在它自己构造时生效。原实现用列表推导先构造再传入 ⇒ 既有崩溃路径
    （`structure_terms` 含「Introduction」时整个请求 failed）。
    """
    from paper_agent.tasking.query_synthesizer import (
        DeterministicSearchIntentSynthesizer,
    )

    ctx = _search_ctx(
        paper_title="面向空地协同SLAM的大视角大尺度差图像匹配研究",
        paper_section_titles=("1 引言", "Introduction", "Related Work",
                              "2.1跨视角图像匹配研究"),
    )
    res = DeterministicSearchIntentSynthesizer().synthesize(ctx)   # 不得抛
    assert res.intent is not None
    texts = [s.text for s in res.intent.seeds]
    assert not any(t.strip().lower() in ("introduction", "related work", "引言")
                   for t in texts), f"通用章节名未被剔除：{texts}"
    assert any("跨视角图像匹配" in t for t in texts), f"有信息的标题被丢了：{texts}"


def test_deterministic_projection_unchanged_without_paper_fields():
    """不传新字段时行为与既有一致。"""
    from paper_agent.tasking.query_synthesizer import (
        DeterministicSearchIntentSynthesizer,
    )

    res = DeterministicSearchIntentSynthesizer().synthesize(_search_ctx())
    assert res.intent is not None
    assert res.intent.semantic_topic == "完善引言部分"
    assert res.llm_called is False


# --- 论文题目的保守启发式 ---


def test_paper_title_only_when_unique_top_level():
    """多个同级顶层标题 = 那是章节不是题目 ⇒ 宁可留空，不猜。"""
    from paper_agent.runtime.handlers import _paper_context

    class _S:
        ext = "md"

        def __init__(self, text):
            self._t = text

        def read_working(self):
            return self._t

    class _C:
        def __init__(self, text):
            self.session = _S(text)

    # 用户真实骨架的形态：四个 `##`，没有题目
    four_tops = "## 1 引言\n\n甲\n\n## 2 相关工作\n\n乙\n\n## 3 方法\n\n丙\n\n## 4 实验\n\n丁\n"
    got = _paper_context(_C(four_tops), "引言")
    assert got["paper_title"] == ""
    assert "1 引言" in got["paper_section_titles"]
    assert "甲" in got["target_section_body"]

    # 唯一顶层标题 = 题目
    with_title = "# 空地协同图像匹配\n\n## 1 引言\n\n甲\n"
    assert _paper_context(_C(with_title), "引言")["paper_title"] == "空地协同图像匹配"


def test_paper_context_degrades_to_empty_on_error():
    """读文档失败不得拖垮写作请求（降级回「只有 operation 槽位」的旧行为）。"""
    from paper_agent.runtime.handlers import _paper_context

    class _Boom:
        ext = "md"

        def read_working(self):
            raise OSError("disk gone")

    class _C:
        session = _Boom()

    assert _paper_context(_C(), "引言") == {
        "paper_title": "", "paper_section_titles": (), "target_section_body": "",
    }


def test_paper_context_skips_non_sectionable_format():
    from paper_agent.runtime.handlers import _paper_context

    class _S:
        ext = "docx"

        def read_working(self):
            raise AssertionError("docx 不该被当文本读")

    class _C:
        session = _S()

    assert _paper_context(_C(), "引言")["paper_section_titles"] == ()
