"""章节指代匹配：用户措辞 → 真实章节。

真机复现（2026-08-01，用户真实骨架 `examples/airground_skeleton.md`）：
输入「我需要去写我的引言部分」→ LLM 填 `section_id="引言部分"` →
`rejected(section_not_found: 计划期找不到要修改的章节：引言部分)`。

根因不是章节名写错，是**章节编号前缀**让双向包含匹配同时失效：
    slugify("1 引言")   = "1_引言"
    slugify("引言部分") = "引言部分"
    "引言部分" in "1_引言"  → False
    "1_引言" in "引言部分"  → False        公共部分是「引言」，但互不完整包含

设计约束决定了修复位置：按设计 LLM **拿不到章节清单**（analyze 是消息的纯函数，
`current_sections` 只用于校验投影、从不参与生成），所以它只能照抄用户原话。
⇒ 容忍用户措辞是**匹配层**的责任，不是抽取层的。

本文件锁住：
1. 真机失败用例现在能匹配上（find 与 validator 两条路径都要）
2. `find` 与 `_matches` 判据一致 —— 这是最要紧的一条，二者分叉会导致
   「计划期通过、执行期 find 返回 None」
3. 编号剥离不引入误匹配
4. `slugify` 未被放宽（它生成持久化 id，改动会让已有会话数据失配）
"""

from __future__ import annotations

import pytest

from paper_agent.intents.router import Intent
from paper_agent.session.section_index import (
    SectionIndex,
    anchor_match_rank,
    match_key,
    slugify,
)
from paper_agent.tasking.contracts import (
    FieldStatus,
    Operation,
    SlotValue,
    Task_Plan,
)
from paper_agent.tasking.plan_validator import SECTION_NOT_FOUND, validate_plan

# 用户真实骨架的标题结构（截取，保留编号形态）
SKELETON = """## 1 引言

### （1）空地协同SLAM的视觉关联需求

正文一。

### （4）本文研究思路与贡献

正文二。

## 2 相关工作

### 2.1跨视角图像匹配研究

正文三。

## 3 方法

### 3.1 问题定义与总体框架

正文四。

## 4 实验与结果分析

正文五。
"""


@pytest.fixture
def index() -> SectionIndex:
    return SectionIndex(SKELETON, "md")


def _sections_as_turn_service_does(index: SectionIndex) -> list[str]:
    """复刻 turn_service：id 与 title 两份都进 current_sections。"""
    return [s.id for s in index.list()] + [s.title for s in index.list()]


def _validate(anchor: str, index: SectionIndex):
    plan = Task_Plan(
        plan_id="p1", source_turn_id="t1",
        operations=(Operation(
            operation_id="op1", intent=Intent.REWRITE,
            slots={"section_id": SlotValue(
                value=anchor, field_status=FieldStatus.PRESENT)},
        ),),
    )
    return validate_plan(
        plan, current_sections=_sections_as_turn_service_does(index),
        current_format="md",
    )


# ------------------------------------------------------------ 真机失败用例


def test_regression_yinyanbufen_resolves(index):
    """LLM 填「引言部分」必须解析到「1 引言」。"""
    s = index.find("引言部分")
    assert s is not None
    assert s.id == "1_引言"


def test_regression_yinyanbufen_passes_validation(index):
    outcome = _validate("引言部分", index)
    assert SECTION_NOT_FOUND not in [i.code for i in outcome.issues]
    assert outcome.result == "ready"


@pytest.mark.parametrize(
    "anchor, expected_id",
    [
        # 真机这次的输入
        ("引言部分", "1_引言"),
        # 其他自然措辞
        ("引言", "1_引言"),
        ("引言这一章", "1_引言"),
        ("1 引言", "1_引言"),
        ("1_引言", "1_引言"),
        ("相关工作", "2_相关工作"),
        ("相关工作部分", "2_相关工作"),
        ("方法", "3_方法"),
        ("方法章节", "3_方法"),
        ("实验与结果分析", "4_实验与结果分析"),
        ("实验", "4_实验与结果分析"),
        # 带编号的子节
        ("跨视角图像匹配研究", "2_1跨视角图像匹配研究"),
        ("2.1跨视角图像匹配研究", "2_1跨视角图像匹配研究"),
        ("问题定义与总体框架", "3_1_问题定义与总体框架"),
        # sec: 前缀
        ("sec:引言", "1_引言"),
    ],
)
def test_natural_phrasings_resolve(index, anchor, expected_id):
    s = index.find(anchor)
    assert s is not None, f"{anchor!r} 未解析到任何章节"
    assert s.id == expected_id


# -------------------------------------------- find 与 validator 判据一致性


@pytest.mark.parametrize(
    "anchor",
    [
        "引言部分", "引言", "引言这一章", "1 引言", "1_引言", "sec:引言",
        "相关工作", "相关工作部分", "方法", "方法章节",
        "实验", "实验与结果分析", "跨视角图像匹配研究",
        # 应当都匹配不上的
        "致谢", "参考文献列表", "附录A", "conclusion", "",
    ],
)
def test_find_and_validator_agree(index, anchor):
    """两条路径必须同时可解析或同时不可解析。

    分叉的后果不对称且都很糟：
    - validator 松、find 紧 → 计划期放行，执行期 `章节不存在` 运行时失败
    - validator 紧、find 松 → 明明能改却在计划期被拒（本次 bug 的形态）
    """
    resolvable_by_find = index.find(anchor) is not None
    outcome = _validate(anchor, index)
    accepted_by_validator = SECTION_NOT_FOUND not in [
        i.code for i in outcome.issues
    ]
    assert resolvable_by_find == accepted_by_validator, (
        f"{anchor!r}: find={resolvable_by_find} validator={accepted_by_validator}"
    )


# ------------------------------------------------------------ 不引入误匹配


@pytest.mark.parametrize("anchor", ["致谢", "附录", "conclusion", "参考文献"])
def test_unrelated_anchors_still_unresolved(index, anchor):
    assert index.find(anchor) is None


def test_empty_anchor_unresolved(index):
    assert index.find("") is None
    assert index.find("   ") is None


def test_prefers_strongest_match_not_first_occurrence():
    """anchor=「引言」时，精确的「引言」应胜过靠前出现的「引言与相关工作」。"""
    text = "## 引言与相关工作\n\n甲。\n\n## 引言\n\n乙。\n"
    idx = SectionIndex(text, "md")
    s = idx.find("引言")
    assert s is not None
    assert s.title == "引言"       # rank 5 精确 > rank 4 包含


def test_numbering_only_title_does_not_match_everything():
    """标题只有编号时 match_key 会变空串，不得因此匹配上任意 anchor。"""
    idx = SectionIndex("## 1.2\n\n正文。\n\n## 引言\n\n正文。\n", "md")
    assert match_key("1.2") == ""
    s = idx.find("引言")
    assert s is not None and s.title == "引言"


# ------------------------------------------------------------ match_key 行为


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1 引言", "引言"),
        ("1_引言", "引言"),
        ("2.1跨视角图像匹配研究", "跨视角图像匹配研究"),
        ("2_1跨视角图像匹配研究", "跨视角图像匹配研究"),
        ("3.4 跨平台几何约束构建与UGV轨迹优化", "跨平台几何约束构建与ugv轨迹优化"),
        ("一、引言", "引言"),
        ("引言部分", "引言部分"),
        ("sec:引言", "引言"),
        ("Introduction", "introduction"),
        ("", ""),
    ],
)
def test_match_key(raw, expected):
    assert match_key(raw) == expected


def test_slugify_not_loosened():
    """slugify 生成持久化 id（facts.claims 键 / checkpoint 指纹），必须保持原样。"""
    assert slugify("1 引言") == "1_引言"          # 编号仍保留在 id 里
    assert slugify("2.1跨视角图像匹配研究") == "2_1跨视角图像匹配研究"
    assert slugify("") == "section"


# ------------------------------------------------------------ rank 分级


def test_rank_levels():
    assert anchor_match_rank("1_引言", "1_引言", "1 引言") == 5   # id 精确
    assert anchor_match_rank("1 引言", "1_引言", "1 引言") == 5   # 标题精确
    assert anchor_match_rank("引言", "1_引言", "1 引言") == 4     # 去编号后精确
    assert anchor_match_rank("引言部分", "1_引言", "1 引言") == 2  # 章节名 ⊂ anchor
    assert anchor_match_rank("引言", "引言与相关工作", "引言与相关工作") == 3
    assert anchor_match_rank("致谢", "1_引言", "1 引言") == 0
    assert anchor_match_rank("", "1_引言", "1 引言") == 0


def test_exact_after_numbering_beats_substring():
    """档 4 必须强于档 3：靠判据而不是靠章节在文档里的先后。"""
    assert (anchor_match_rank("方法", "3_方法", "3 方法")
            > anchor_match_rank("方法", "3_1_方法概述", "3.1 方法概述"))
    idx = SectionIndex("## 3.1 方法概述\n\n甲。\n\n## 3 方法\n\n乙。\n", "md")
    assert idx.find("方法").title == "3 方法"


def test_rank_is_deterministic():
    """id 与标题各算一次取最大；不得因迭代序而时高时低。"""
    for _ in range(50):
        assert anchor_match_rank("引言", "1_引言", "1 引言") == 4
