"""参考文献区同步（真机缺陷:正文标了 [6]，文末没有任何条目）。

原因不是 bug 而是功能缺失:`tools/bibliography.py` 只支持 .tex，
rewrite/add_section 提交时也从不碰文末。账本里元数据是齐的，没人负责渲染。
"""
from __future__ import annotations

import pytest

from paper_agent.literature.bibliography_sync import (
    apply_bibliography,
    render_bibliography,
)
from paper_agent.literature.citation_styles import (
    CitationStyle,
    available_styles,
    get_style,
    register,
)
from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import record_from_dict


def _rec(n: int, **kw):
    d = {
        "reference_id": f"r{n:012x}",
        "bibkey": f"key{n}",
        "title": f"Title {n}",
        "authors": [f"Author {n}A", f"Author {n}B"],
        "year": 2020 + (n % 5),
        "numeric_aliases": [str(n)],
        "legacy_verified": True,
    }
    d.update(kw)
    return record_from_dict(d)


# ---------------- 只列正文真的引用了的 ----------------

def test_lists_only_cited_references():
    """账本 5 条、正文只引 2 条 ⇒ 文献区只列 2 条。多余条目和缺条目一样是错。"""
    recs = [_rec(i) for i in range(1, 6)]
    ledger = ReferenceLedger(recs)
    body, report = render_bibliography(("2", "4"), ledger, "md")
    assert len(report.listed) == 2
    assert "Title 2" in body and "Title 4" in body
    assert "Title 1" not in body and "Title 5" not in body


def test_numeric_order_is_numeric_not_lexical():
    """`[2]` 必须排在 `[10]` 前 —— 字符串序会把 10 排到 2 前面。"""
    ledger = ReferenceLedger([_rec(2), _rec(10)])
    body, _ = render_bibliography(("10", "2"), ledger, "md")
    assert body.index("[2]") < body.index("[10]")


def test_unresolved_marker_reported_not_fabricated():
    """账本查不到的标记 ⇒ 进 unresolved，绝不编造条目掩盖悬空引用。"""
    ledger = ReferenceLedger([_rec(1)])
    body, report = render_bibliography(("1", "99"), ledger, "md")
    assert report.unresolved == ["99"]
    assert len(report.listed) == 1
    assert "99" not in body.replace("[1]", "")


def test_duplicate_citations_listed_once():
    ledger = ReferenceLedger([_rec(3)])
    body, report = render_bibliography(("3", "3", "3"), ledger, "md")
    assert len(report.listed) == 1
    assert body.count("Title 3") == 1


# ---------------- 字段不全时优雅退化 ----------------

def test_degrades_without_venue_fields():
    """真机账本 container_title/volume/pages 覆盖率为 0 ⇒ 不能留 `. , (): .` 空骨架。"""
    ledger = ReferenceLedger([_rec(1)])
    body, report = render_bibliography(("1",), ledger, "md")
    assert "Title 1" in body and "2021" in body
    assert "()" not in body and ": ." not in body
    assert ",," not in body
    assert report.incomplete == [_rec(1).reference_id]   # 如实报告缺字段


def test_full_fields_render_complete_gbt():
    """字段齐全时给出完整国标形态。"""
    rec = _rec(1, reference_type="journal-article", container_title="机器人学报",
               volume="45", issue="3", pages="112-120")
    ledger = ReferenceLedger([rec])
    body, report = render_bibliography(("1",), ledger, "md")
    assert "[J]" in body
    assert "机器人学报" in body
    assert "45(3)" in body and "112-120" in body
    assert report.incomplete == []


def test_three_plus_authors_use_deng():
    """国标:3 名以上作者写「前三名, 等」。"""
    rec = _rec(1, authors=["A", "B", "C", "D", "E"])
    body, _ = render_bibliography(("1",), ReferenceLedger([rec]), "md")
    assert "等" in body
    assert "E" not in body.replace("Title", "")


# ---------------- 方言 ----------------

def test_tex_uses_bibitem_and_bibkey():
    ledger = ReferenceLedger([_rec(7)])
    body, _ = render_bibliography(("key7",), ledger, "tex")
    assert body.startswith("\\bibitem{key7}")
    assert "[7]" not in body


def test_md_uses_numeric_marker():
    ledger = ReferenceLedger([_rec(7)])
    body, _ = render_bibliography(("7",), ledger, "md")
    assert body.startswith("[7]")


# ---------------- 区块重建（幂等 + 不越界） ----------------

def test_rebuild_is_idempotent():
    """同步两次结果逐字相同 —— 增量追加会留下僵尸条目。"""
    ledger = ReferenceLedger([_rec(1), _rec(2)])
    text = "# 引言\n\n见[1]与[2]。\n"
    once, _ = apply_bibliography(text, "md", ledger)
    twice, _ = apply_bibliography(once, "md", ledger)
    assert once == twice


def test_removed_citation_drops_from_bibliography():
    """引用被删后重建 ⇒ 对应条目消失，不留僵尸。"""
    ledger = ReferenceLedger([_rec(1), _rec(2)])
    with_both, _ = apply_bibliography("见[1]与[2]。", "md", ledger)
    assert "Title 2" in with_both

    edited = with_both.replace("见[1]与[2]。", "只见[1]。")
    rebuilt, _ = apply_bibliography(edited, "md", ledger)
    assert "Title 1" in rebuilt
    assert "Title 2" not in rebuilt


def test_body_outside_block_untouched():
    """标记之外的内容逐字不动。"""
    ledger = ReferenceLedger([_rec(1)])
    text = "# 引言\n\n正文见[1]。\n\n# 结论\n\n结论段落。\n"
    new, _ = apply_bibliography(text, "md", ledger)
    assert "# 引言" in new and "# 结论" in new
    assert "结论段落。" in new
    assert new.index("正文见[1]") < new.index("## 参考文献")


def test_no_citations_leaves_text_alone():
    ledger = ReferenceLedger([_rec(1)])
    text = "# 引言\n\n没有任何引用。\n"
    new, report = apply_bibliography(text, "md", ledger)
    assert new == text
    assert report is None


def test_none_ledger_is_noop():
    """不带文献功能的旧路径照旧可用。"""
    text = "见[1]。"
    new, report = apply_bibliography(text, "md", None)
    assert new == text and report is None


# ---------------- 格式可扩展 ----------------

def test_custom_style_registrable():
    """用户要「某期刊格式」时:加一个类 + register 一行，不碰同步逻辑。"""

    class _Journal(CitationStyle):
        name = "testjournal"
        label = "测试期刊格式"
        required = ("title",)

        def render_text(self, record):
            return f"<<{record.title}>>"

    register(_Journal())
    assert get_style("testjournal").name == "testjournal"
    assert ("testjournal", "测试期刊格式") in available_styles()

    ledger = ReferenceLedger([_rec(1)])
    body, _ = render_bibliography(("1",), ledger, "md", style_name="testjournal")
    assert "<<Title 1>>" in body


def test_unknown_style_falls_back_to_gbt():
    assert get_style("no_such_style").name == "gbt7714"
    assert get_style("").name == "gbt7714"


@pytest.mark.parametrize("alias", ["gbt7714", "GB/T 7714", "国标", "", "default"])
def test_gbt_aliases(alias):
    assert get_style(alias).name == "gbt7714"
