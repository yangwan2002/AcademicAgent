"""X-11 回归：requirement 记账的三处缺陷。

真机场景（一句话同时交代课题、结构约束、引用配额与改写目标）此前被弹三次
「无法安排」窗口，其中两次是虚假警报、一次是真丢数据：

- r1 课题（「课题是面向空地协同SLAM…」）：**真丢** —— REWRITE_SPEC 七个槽都是
  「本次操作要做什么」，没有承载「这篇论文在研究什么」的位置 → 落 facts.claims。
- r3/r4 引用配额：**假警报** —— 配额槽是嵌套 dict，序列化后用户原话里相邻的两句被
  dict 语法切开，子串匹配要求连续 ⇒ 任何嵌套槽位恒定 100% 漏报。
- 确认摘要**完全不显示配额** —— 让用户「据此核对」，却不显示要核对的东西。
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.intent import Intent
from paper_agent.tasking.clarifier import summarize_plan
from paper_agent.tasking.contracts import (
    AtomicRequirement,
    CallStatus,
    DispositionStatus,
    ExtractionResult,
    FieldStatus,
    RawOperation,
    SlotSource,
    SlotValue,
)
from paper_agent.tasking.extractors.base import _infer_consumption, _slot_texts
from paper_agent.tasking.operation_analyzer import _coerce_result
from paper_agent.tasking.plan_builder import build_plan, operation_quota

_U = SlotSource.USER_EXPLICIT

# 真机上 LLM 填进 citation_quota 的实际结构（每 clause 自带 verbatim）。
_QUOTA = {
    "constraints": [
        {"dimension": "total", "operator": "bare", "value": 10,
         "verbatim": "需要引用 10 篇参考文献"},
        {"dimension": "language.en", "operator": "bare", "value": 8,
         "verbatim": "英文 8 篇"},
        {"dimension": "language.zh", "operator": "bare", "value": 2,
         "verbatim": "中文 2 篇"},
    ]
}


def _present(value, req_ids=(), verbatim=""):
    return SlotValue(value=value, field_status=FieldStatus.PRESENT,
                     source=_U, requirement_ids=tuple(req_ids), verbatim=verbatim)


# --- 结构化槽位分片（修 r3/r4 的机制）---------------------------------------

def test_structured_slot_yields_per_clause_fragments():
    """嵌套槽位按 clause 的 verbatim 分片，**不**吞 dict 语法。"""
    frags = _slot_texts(_present(_QUOTA))
    assert "英文8篇" in frags
    assert "中文2篇" in frags
    # 关键：不能出现 dict 语法残渣，否则又回到「一坨」的老路
    assert not any("dimension" in f for f in frags)


def test_nested_quota_requirements_are_claimed_by_inference():
    """配额要求可被推断认领。此前恒定漏报：原话相邻两句被 dict 语法切断。"""
    reqs = (
        AtomicRequirement(requirement_id="r3", verbatim="需要引用 10 篇参考文献"),
        AtomicRequirement(requirement_id="r4", verbatim="其中英文 8 篇、中文 2 篇"),
    )
    slots = {"citation_quota": _present(_QUOTA)}
    new_slots, consumed = _infer_consumption(slots, reqs)
    assert set(consumed) == {"r3", "r4"}
    assert set(new_slots["citation_quota"].requirement_ids) == {"r3", "r4"}


def test_plain_string_slots_still_matched():
    """分片改造不得回退字符串槽位的既有能力。"""
    reqs = (AtomicRequirement(requirement_id="r5", verbatim="改写引言这一章"),)
    slots = {"section_id": _present("引言", verbatim="改写引言这一章")}
    _new, consumed = _infer_consumption(slots, reqs)
    assert consumed == ["r5"]


# --- 项目级事实（修 r1：唯一真丢数据的一条）---------------------------------

def test_analyzer_extracts_project_facts_with_key_whitelist():
    raw = {
        "operations": [{"intent": "rewrite", "verbatim": "改写引言"}],
        "requirements": [{"requirement_id": "r1", "verbatim": "课题是空地协同SLAM"}],
        "unmapped_text": [],
        "project_facts": [
            {"key": "research_topic", "value": "空地协同SLAM图像匹配",
             "requirement_ids": ["r1"]},
            {"key": "自造字段", "value": "应被丢弃"},        # 白名单外
            {"key": "novelty", "value": ""},                  # 空值
        ],
    }
    ar = _coerce_result(raw, "改写引言，课题是空地协同SLAM")
    assert ar.project_facts == (("research_topic", "空地协同SLAM图像匹配", ("r1",)),)


def test_project_fact_requirement_is_project_memory_not_unmapped():
    """课题类陈述记 PROJECT_MEMORY，不弹窗 —— 它确实被消费了，只是消费方是项目记忆。"""
    reqs = [
        AtomicRequirement(requirement_id="r1", verbatim="课题是面向空地协同SLAM的图像匹配研究"),
        AtomicRequirement(requirement_id="r5", verbatim="改写引言这一章"),
    ]
    ext = ExtractionResult(
        call_status=CallStatus.SUCCESS,
        slots={"section_id": _present("引言", ("r5",))},
    )
    raw_op = RawOperation(intent=Intent.REWRITE, verbatim="改写引言这一章",
                          requirement_ids=("r5",))
    plan = build_plan("t1", [raw_op], [ext], reqs,
                      project_fact_requirement_ids=("r1",))
    by_id = {d.requirement_id: d for d in plan.dispositions}
    assert by_id["r1"].status is DispositionStatus.PROJECT_MEMORY
    assert plan.unmapped_requirements == ()


def test_slot_consumption_wins_over_project_memory():
    """同一条既进槽又进 claims 时记 mapped（槽位消费优先，真的影响执行）。"""
    reqs = [AtomicRequirement(requirement_id="r1", verbatim="改写引言")]
    ext = ExtractionResult(
        call_status=CallStatus.SUCCESS,
        slots={"section_id": _present("引言", ("r1",))},
    )
    raw_op = RawOperation(intent=Intent.REWRITE, verbatim="改写引言")
    plan = build_plan("t1", [raw_op], [ext], reqs,
                      project_fact_requirement_ids=("r1",))
    assert plan.dispositions[0].status is DispositionStatus.MAPPED


def test_persist_project_facts_writes_and_saves_claims():
    """落盘不能依赖写作成功：计划被驳回时用户交代的课题也不该白说。"""
    from paper_agent.tasking.planning_loop import _persist_project_facts

    class _Facts:
        def __init__(self):
            self.claims, self.saved = {}, 0

        def set_claim(self, k, v):
            self.claims[k] = v

        def save(self):
            self.saved += 1

    facts = _Facts()
    _persist_project_facts((("research_topic", "空地协同SLAM", ("r1",)),), facts, None)
    assert facts.claims == {"research_topic": "空地协同SLAM"}
    assert facts.saved == 1


def test_persist_project_facts_tolerates_no_facts():
    """无会话/测试路径 facts 为 None：项目记忆是增强项，缺它不该让规划失败。"""
    from paper_agent.tasking.planning_loop import _persist_project_facts

    _persist_project_facts((("research_topic", "x", ()),), None, None)  # 不抛


# --- 确认摘要显示配额（修 3）-------------------------------------------------

def test_operation_quota_renders_counts_and_filters():
    slots = {"citation_quota": _present(dict(
        _QUOTA, filters=[
            {"field": "year", "operator": "recent_years", "value": 5},
            {"field": "language", "operator": "in", "value": ["zh", "en"]},
        ]))}
    rendered = operation_quota(slots)
    assert "共10篇" in rendered
    assert "英文8篇" in rendered
    assert "中文2篇" in rendered
    assert "近5年" in rendered


def test_operation_quota_absent_slot_is_empty():
    assert operation_quota({}) == ()


# --- 课题真正抵达检索合成（r1 修复的**目的**，不只是别弹窗）------------------

_SKELETON_MD = "## 引言\n### 研究背景\n内容\n## 相关工作\n## 方法\n## 实验\n"


class _Sess:
    ext = "md"                      # 注意 SECTIONABLE_EXTS 存的是无点扩展名

    def __init__(self, text=_SKELETON_MD):
        self._text = text

    def read_working(self):
        return self._text


class _Facts:
    claims = {"research_topic": "面向空地协同SLAM的大视角大尺度差图像匹配研究"}


def test_paper_title_falls_back_to_research_topic_on_skeleton():
    """四个同级 `##` 的骨架文档恒推不出题目 → 用 claims.research_topic。"""
    from paper_agent.runtime.handlers import _paper_context

    class Ctx:
        session = _Sess()
        facts = _Facts()

    assert _paper_context(Ctx(), "引言")["paper_title"] == _Facts.claims["research_topic"]


def test_real_title_is_not_overwritten_by_claims():
    """有唯一顶层标题时以文档为准，claims 只做兜底。"""
    from paper_agent.runtime.handlers import _paper_context

    class Ctx:
        session = _Sess("# 真题目\n## 引言\n正文\n")
        facts = _Facts()

    assert _paper_context(Ctx(), "引言")["paper_title"] == "真题目"


def test_paper_context_without_facts_degrades_to_empty():
    from paper_agent.runtime.handlers import _paper_context

    class Ctx:
        session = _Sess()
        facts = None

    assert _paper_context(Ctx(), "引言")["paper_title"] == ""


def test_operation_quota_absent_or_malformed_is_empty():
    assert operation_quota({}) == ()
    assert operation_quota({"citation_quota": _present("不是dict")}) == ()


def test_summary_shows_quota_so_user_can_verify():
    """此前摘要里「10 篇/英 8/中 2」一个字都没有。"""
    ext = ExtractionResult(
        call_status=CallStatus.SUCCESS,
        slots={"section_id": _present("引言"), "citation_quota": _present(_QUOTA)},
    )
    plan = build_plan("t1", [RawOperation(intent=Intent.REWRITE, verbatim="改写引言")],
                      [ext], [])
    text = summarize_plan(plan)
    assert "10篇" in text and "英文8篇" in text and "中文2篇" in text
