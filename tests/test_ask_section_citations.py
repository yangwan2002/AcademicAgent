"""`section_citations` 只读工具：问答能回答「哪一章引了哪篇」。

此前 `:ask` 只有正则计数（doc_stats 的"正文引用 N 个不同编号"），答不了归属问题，
模型要么去读正文自己数方括号（不准且贵），要么说看不出来。数据侧的缺口由
`literature.citation_usage` 填（见 tests/test_citation_usage.py），这里测通路。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_agent.application.ask_context import (
    ASK_TOOLS,
    ToolDispatchError,
    capture,
    dispatch,
)
from paper_agent.session.facts import KnownRef, SessionFacts

_MD = """# 1 引言
视觉定位是核心问题[1]，近年进展显著[2]。

# 2 相关工作
早期方法依赖手工特征[2]，后转向深度学习[3]。
"""


class _FakeSession:
    def __init__(self, text: str, ext: str = "md") -> None:
        self._text = text
        self.ext = ext

    def read_working(self) -> str:
        return self._text


@pytest.fixture()
def facts(tmp_path):
    f = SessionFacts(path=os.path.join(str(tmp_path), "facts.json"))
    f.add_ref(KnownRef(id="1", title="视觉定位综述"))
    f.add_ref(KnownRef(id="2", title="手工特征方法"))
    f.add_ref(KnownRef(id="3", title="深度学习定位"))
    return f


def test_tool_is_registered():
    names = [t["function"]["name"] for t in ASK_TOOLS]
    assert "section_citations" in names


def test_reports_per_section_titles(facts):
    ctx = capture(_FakeSession(_MD), facts)
    out = dispatch(ctx, "section_citations", {})

    assert "视觉定位综述" in out and "手工特征方法" in out and "深度学习定位" in out
    assert "1_引言（2 篇）" in out
    assert "2_相关工作（2 篇）" in out


def test_single_section_filter(facts):
    ctx = capture(_FakeSession(_MD), facts)
    out = dispatch(ctx, "section_citations", {"section_id": "2_相关工作"})

    assert "引用了 2 篇" in out
    assert "深度学习定位" in out
    assert "视觉定位综述" not in out       # [1] 只在引言


def test_unknown_section_id_raises_with_hint(facts):
    ctx = capture(_FakeSession(_MD), facts)
    with pytest.raises(ToolDispatchError) as e:
        dispatch(ctx, "section_citations", {"section_id": "不存在"})

    assert "有引用的章节" in str(e.value)   # 报可用值，模型才能自纠


def test_unresolved_tokens_are_surfaced(facts):
    """解析不到的标记必须报出来，否则模型会据"本章只引了 N 篇"下错结论。"""
    ctx = capture(_FakeSession("# 1 引言\n引用[1]与不存在的[9]。\n"), facts)
    out = dispatch(ctx, "section_citations", {})

    assert "视觉定位综述" in out
    assert "解析不到" in out and "9" in out


def test_missing_facts_says_cannot_look_up_not_no_citations():
    """没有 facts ≠ 没有引用。后者会让模型答"这篇没引文献"（假话）。"""
    ctx = capture(_FakeSession(_MD))          # 不传 facts
    out = dispatch(ctx, "section_citations", {})

    assert "没有提供文献数据" in out
    assert "无法回答" in out                   # 明说查不了，而非报告"没有引用"
    assert "没有可解析的引用" not in out       # 不能与"确实没引用"混为一谈
    assert "视觉定位综述" not in out


def test_document_with_no_citations_reports_empty(facts):
    ctx = capture(_FakeSession("# 1 引言\n这一章还没有任何引用。\n"), facts)
    out = dispatch(ctx, "section_citations", {})

    assert "没有可解析的引用" in out


def test_capture_does_not_mutate_facts(facts):
    """`:ask` 必须零副作用：capture 不许写回 citation_usage、不许 save。"""
    before = facts.citation_usage.to_json()
    capture(_FakeSession(_MD), facts)

    assert facts.citation_usage.to_json() == before == []
    assert not os.path.exists(facts.path)     # 没有落盘


def test_bad_section_id_type_raises(facts):
    ctx = capture(_FakeSession(_MD), facts)
    with pytest.raises(ToolDispatchError):
        dispatch(ctx, "section_citations", {"section_id": 3})


def test_tex_document_uses_bibkeys(tmp_path):
    f = SessionFacts(path=os.path.join(str(tmp_path), "facts.json"))
    f.add_ref(KnownRef(id="1", bibkey="zhang2021", title="张的论文"))

    ctx = capture(_FakeSession("\\section{引言}\n见~\\cite{zhang2021}。\n", "tex"), f)
    assert "张的论文" in dispatch(ctx, "section_citations", {})
