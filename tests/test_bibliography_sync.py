"""参考文献区同步（真机缺陷:正文标了 [6]，文末没有任何条目）。

原因不是 bug 而是功能缺失:`tools/bibliography.py` 只支持 .tex，
rewrite/add_section 提交时也从不碰文末。账本里元数据是齐的，没人负责渲染。
"""
from __future__ import annotations

import pytest

from paper_agent.literature.bibliography_sync import (
    apply_bibliography,
    render_bibliography,
    sync_bibliography,
)
from paper_agent.literature.citation_styles import (
    CitationStyle,
    available_styles,
    get_style,
    register,
)
from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import record_from_dict
from paper_agent.session.file_session import FileBackedSession

# 正文与文献区的分界，供顺序编码测试切开两段分别断言。
_MD_BEGIN_MARK = "<!-- BIBLIOGRAPHY:BEGIN"


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


# ---------------- 顺序编码（md/docx）----------------
#
# 真机症状:库里 8 篇被引 4 篇,账本别名是**入库流水号**,于是文献表输出
# `[1] [3] [5] [7]` —— 跳号,不能交付。要求正文与文献表都是连续的 1..N。


def test_body_renumbered_to_sequential_order():
    """正文引用 7,3,1,5 ⇒ 就地重编为 1,2,3,4，按首次出现顺序。"""
    ledger = ReferenceLedger([_rec(i) for i in range(1, 9)])
    new, _ = apply_bibliography("先[7]，再[3]，又[7]与[1]，末[5]。", "md", ledger)
    body = new.split(_MD_BEGIN_MARK)[0]
    assert "先[1]，再[2]，又[1]与[3]，末[4]。" in body


def test_bibliography_has_no_gaps():
    """文献表编号必须连续 1..N —— 跳号（[1][3][5][7]）是原始故障。"""
    ledger = ReferenceLedger([_rec(i) for i in range(1, 9)])
    new, report = apply_bibliography("见[7]、[3]、[1]、[5]。", "md", ledger)
    block = new.split(_MD_BEGIN_MARK)[1]
    markers = [ln.split("]")[0] + "]" for ln in block.splitlines() if ln.startswith("[")]
    assert markers == ["[1]", "[2]", "[3]", "[4]"]
    assert len(report.listed) == 4


def test_body_and_bibliography_markers_agree():
    """同一个标记在正文与文献表里指向同一条文献：正文 `[2]` 就是表里 `[2]` 的著录。"""
    ledger = ReferenceLedger([_rec(i) for i in range(1, 9)])
    new, _ = apply_bibliography("首引[7]，次引[3]。", "md", ledger)
    body, block = new.split(_MD_BEGIN_MARK)
    # 正文首个标记是 [1]，它对应原 [7] ⇒ 表里 [1] 必须是 Title 7
    assert "首引[1]，次引[2]。" in body
    line1 = next(ln for ln in block.splitlines() if ln.startswith("[1]"))
    line2 = next(ln for ln in block.splitlines() if ln.startswith("[2]"))
    assert "Title 7" in line1
    assert "Title 3" in line2


def test_body_marker_resolves_to_same_record_after_renumber():
    """核心不变量：重编号后 `resolve_token("n")` 仍回到正文 `[n]` 指的那条记录。

    只改正文不改账本就会破掉这条 —— 正文 `[7]`→`[1]` 而账本 `1` 属于另一条，
    第二次同步整表错位（真实症状）。这条测试是防它复发的守门。
    """
    ledger = ReferenceLedger([_rec(i) for i in range(1, 9)])
    apply_bibliography("先[7]，再[3]，末[5]。", "md", ledger)
    for token, title in (("1", "Title 7"), ("2", "Title 3"), ("3", "Title 5")):
        res = ledger.resolve_token(token, ext="md")
        assert res.status.name == "RESOLVED"
        assert ledger.get(res.reference_id).title == title


def test_uncited_records_yield_their_numbers():
    """未被引记录顺延到 N+1..，不占 1..N —— 否则被引者与它撞号成 AMBIGUOUS。"""
    ledger = ReferenceLedger([_rec(i) for i in range(1, 9)])
    apply_bibliography("只引[7]与[3]。", "md", ledger)
    aliases = [r.numeric_aliases for r in ledger.all_records()]
    assert all(len(a) == 1 for a in aliases), "每条只留一个数字别名，旧号不留"
    flat = sorted(int(a[0]) for a in aliases)
    assert flat == list(range(1, 9)), "别名应是 1..M 的一个全排列，无重复无空洞"
    for tok in (str(k) for k in range(1, 9)):
        assert ledger.resolve_token(tok, ext="md").status.name == "RESOLVED"


def test_unresolved_marker_not_renumbered():
    """悬空标记原样留下：重编号它只会把一个错标记换成另一个错标记。"""
    ledger = ReferenceLedger([_rec(1), _rec(2)])
    new, report = apply_bibliography("见[2]与[99]。", "md", ledger)
    body = new.split(_MD_BEGIN_MARK)[0]
    assert "见[1]与[99]。" in body
    assert report.unresolved == ["99"]


def test_mixed_group_renumbered_per_item():
    """`[99, 2]` 里 2 照常重编、99 原样 —— 整组不改会让正文出现两个含义的 [2]。"""
    ledger = ReferenceLedger([_rec(1), _rec(2), _rec(3)])
    new, _ = apply_bibliography("见[3]和[99, 2]。", "md", ledger)
    body = new.split(_MD_BEGIN_MARK)[0]
    assert "见[1]和[99, 2]。" in body


def test_multi_citation_group_renumbered():
    """`[12, 21]` 是两条引用，逐项重编后仍是一组。"""
    ledger = ReferenceLedger([_rec(12), _rec(21)])
    new, _ = apply_bibliography("并列见[21, 12]。", "md", ledger)
    body = new.split(_MD_BEGIN_MARK)[0]
    assert "并列见[1, 2]。" in body


def test_sequential_renumber_is_idempotent():
    """重编号后再跑一次逐字不变 —— 否则每次改写都会漂移一轮编号。"""
    ledger = ReferenceLedger([_rec(i) for i in range(1, 9)])
    once, _ = apply_bibliography("见[7]、[3]、[5]。", "md", ledger)
    twice, _ = apply_bibliography(once, "md", ledger)
    assert once == twice


def test_tex_numbering_untouched():
    """tex 的 `\\cite{bibkey}` 编号由 LaTeX 自己排，改它反而错。"""
    ledger = ReferenceLedger([_rec(7), _rec(3)])
    text = "见 \\cite{key7} 与 \\cite{key3}。"
    new, _ = apply_bibliography(text, "tex", ledger)
    assert "\\cite{key7}" in new and "\\cite{key3}" in new
    assert "\\bibitem{key7}" in new


def test_existing_bibliography_block_not_counted_as_body():
    """已有文献区里的 `[n]` 不参与出现顺序 —— 否则编号由上一轮的表决定而非正文。"""
    ledger = ReferenceLedger([_rec(i) for i in range(1, 9)])
    first, _ = apply_bibliography("正文只引[5]。", "md", ledger)
    assert "正文只引[1]。" in first
    # 表里已有 [1]；再加一个引用后，新标记应按正文顺序拿到 [2]，而不是被表干扰
    edited = first.replace("正文只引[1]。", "正文引[1]，又引[8]。")
    second, _ = apply_bibliography(edited, "md", ledger)
    body = second.split(_MD_BEGIN_MARK)[0]
    assert "正文引[1]，又引[2]。" in body


def test_renumber_survives_docx_ext():
    """docx 与 md 同走顺序编码（投影文本口径一致）。"""
    ledger = ReferenceLedger([_rec(4), _rec(9)])
    new, _ = apply_bibliography("见[9]与[4]。", "docx", ledger)
    assert "见[1]与[2]。" in new.split(_MD_BEGIN_MARK)[0]


def test_renumbered_aliases_survive_json_roundtrip():
    """别名重排必须能存进 facts.json 再读回来。

    重编号把身份放进了账本，若它不落盘，下次开会话正文 `[1]` 又指回旧记录。
    """
    ledger = ReferenceLedger([_rec(i) for i in range(1, 6)])
    apply_bibliography("见[4]与[2]。", "md", ledger)
    reloaded = ReferenceLedger([record_from_dict(d) for d in ledger.to_json()])
    for token, title in (("1", "Title 4"), ("2", "Title 2")):
        res = reloaded.resolve_token(token, ext="md")
        assert res.status.name == "RESOLVED"
        assert reloaded.get(res.reference_id).title == title


def test_undo_after_sync_restores_body_and_aliases_together(tmp_path):
    """真会话上验证 `:undo` 的成对回退：正文与别名一起回到同步前。

    只回正文的话旧号会解析到别的文献（`[4]` 原指 T4，会变成 T3）。
    """
    src = tmp_path / "p.md"
    src.write_text("见[4]与[2]。", encoding="utf-8")
    session = FileBackedSession(
        source_path=str(src), workspace_root=str(tmp_path / "ws"), use_git=False)
    ledger = ReferenceLedger([_rec(i) for i in range(1, 6)])

    sync_bibliography(session, ledger)
    assert "见[1]与[2]。" in session.read_working()

    assert session.undo() is True
    ledger.restore_numeric_aliases(session.take_undo_state() or {})
    assert session.read_working().strip() == "见[4]与[2]。"
    res = ledger.resolve_token("4", ext="md")
    assert res.status.name == "RESOLVED"
    assert ledger.get(res.reference_id).title == "Title 4"


def test_undo_state_not_consumed_when_undo_fails(tmp_path):
    """正文没退成，配对状态也不能退 —— 否则两边永久错位。"""
    src = tmp_path / "p.md"
    src.write_text("见[4]。", encoding="utf-8")
    session = FileBackedSession(
        source_path=str(src), workspace_root=str(tmp_path / "ws"), use_git=False)
    assert session.undo() is False          # 没有任何写入，无历史
    assert session.take_undo_state() is None


def test_alias_snapshot_lets_undo_restore_identity():
    """`:undo` 回退正文时必须一并回退别名，否则旧号指向别的文献。

    实测症状：正文 `[4]` 原指 T4，只回退正文后解析成 T3。
    """
    ledger = ReferenceLedger([_rec(i) for i in range(1, 6)])
    snapshot = ledger.numeric_alias_snapshot()
    apply_bibliography("见[4]与[2]。", "md", ledger)
    ledger.restore_numeric_aliases(snapshot)   # 模拟 :undo
    for k in range(1, 6):
        res = ledger.resolve_token(str(k), ext="md")
        assert res.status.name == "RESOLVED"
        assert ledger.get(res.reference_id).title == f"Title {k}"


def test_alias_restore_keeps_records_added_after_snapshot():
    """撤销一次编辑不该抹掉这期间新入库文献 —— 快照里没有的记录保持不动。"""
    ledger = ReferenceLedger([_rec(1), _rec(2)])
    snapshot = ledger.numeric_alias_snapshot()
    added = _rec(3)
    ledger.add_record(added)
    ledger.restore_numeric_aliases(snapshot)
    assert ledger.get(added.reference_id) is not None
    res = ledger.resolve_token("3", ext="md")
    assert res.status.name == "RESOLVED"
    assert res.reference_id == added.reference_id


def test_sync_writes_renumbered_body_even_if_block_unchanged():
    """正文重编号了、但文献区渲染结果恰好不变时，仍必须落盘。

    `_replace_block` 的 `changed` 只看区块，漏掉这种情况就会把重编号后的正文丢掉。
    """
    class _FakeSession:
        ext = "md"

        def __init__(self, text):
            self._text = text
            self.writes = 0

        def read_working(self):
            return self._text

        def write_working(self, text, message="edit", **kw):
            self._text = text
            self.writes += 1

    ledger = ReferenceLedger([_rec(i) for i in range(1, 5)])
    first, _ = apply_bibliography("见[3]与[1]。", "md", ledger)
    # 构造：正文换回旧号，但账本已是重编后的状态 ⇒ 区块渲染不变、正文需要改
    session = _FakeSession(first.replace("见[1]与[2]。", "见[2]与[1]。"))
    sync_bibliography(session, ledger)
    assert session.writes == 1
    assert "见[1]与[2]。" in session.read_working().split(_MD_BEGIN_MARK)[0]
