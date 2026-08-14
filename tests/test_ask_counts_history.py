"""计数口径与编辑历史的测试（P-4 第二步：按真机评测实测缺陷补的修复）。

这些用例**每一条都对应一次真机错答**，不是补覆盖率。真机 24 问评测跑完后逐条读
答案，发现四个缺陷（fixture 为 `tests/eval/ask_readonly/fixtures/uav_slam.md`）：

1. 问"全文有多少条引用"答 **18** —— 把正文出现次数（10，含重复）与文献表条目
   （8）相加了。真值是 8。模型加了"正则近似值"的免责声明，但那是概念错误，
   不是精度问题，免责声明替代不了正确答案。
2. 问"有几个公式"答 **6** —— 1 个块公式 + 5 个行内 `$I_a$`/`$c_p$` 这类**变量
   符号**。变量符号不是公式。
3. 问"哪一章最长"答"**参考文献** 621 字" —— 文献表被当成正文章节参与比较，模型
   自己意识到不对还得找补一句"若只看正文则是引言"。
4. 历史类 3 问（"我刚才改了什么"等）全部答"无法查看修改历史" —— 而信息一直在
   session 的 git commit 里（`polish <id>` 等），缺的只是读取通路。

所以断言都钉**具体数字与具体口径**，不是"字段存在"。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_agent.application.ask_context import (
    ToolDispatchError,
    capture,
    dispatch,
)
from paper_agent.tools.doc_stats import (
    is_non_body_heading,
    split_bibliography,
    stats_from_text,
)

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "eval", "ask_readonly", "fixtures", "uav_slam.md"
)


def _fixture_text() -> str:
    with open(_FIXTURE, encoding="utf-8") as fh:
        return fh.read()


class _FakeSession:
    def __init__(self, text: str, ext: str = "md") -> None:
        self._text = text
        self.ext = ext

    def read_working(self) -> str:
        return self._text


# ---- 缺陷 1：引用计数 ----


def test_citations_not_summed_across_body_and_bibliography():
    """真机答"引用 18"的那份文档，正文唯一编号必须是 8、文献表条目 8。

    18 = 10（正文含重复）+ 8（文献表）。这两个数**不能相加**。
    """
    stats = stats_from_text(_fixture_text(), "md").as_dict()
    assert stats["unique_citations"] == 8
    assert stats["bib_entries"] == 8


def test_repeated_citation_counted_once():
    doc = "# 1 引言\n见 [1] 与 [2]，又见 [1]。\n\n# 参考文献\n[1] A\n[2] B\n"
    stats = stats_from_text(doc, "md").as_dict()
    assert stats["unique_citations"] == 2  # [1] 出现两次只算一个编号
    assert stats["bib_entries"] == 2


def test_citation_ranges_and_groups_expand():
    doc = "# 1 引言\n如 [1,3] 与 [5-7] 所述。\n\n# 参考文献\n[1] A\n"
    stats = stats_from_text(doc, "md").as_dict()
    assert stats["unique_citations"] == 5  # 1,3,5,6,7


def test_year_range_not_mistaken_for_citation_range():
    """`[2020-2024]` 不该展开成上千个编号。"""
    doc = "# 1 引言\n时间跨度 [2020-2024] 内。\n"
    stats = stats_from_text(doc, "md").as_dict()
    assert stats["unique_citations"] == 0


def test_bib_entries_zero_when_no_bibliography():
    """没有文献表就报 0 条，不拿正文引用充数。"""
    doc = "# 1 引言\n见 [1]。\n"
    stats = stats_from_text(doc, "md").as_dict()
    assert stats["unique_citations"] == 1
    assert stats["bib_entries"] == 0


# ---- 缺陷 2：公式计数 ----


def test_inline_math_not_counted_as_formula():
    """真机答"公式 6"的那份文档：块级 1、行内 5，两者必须分开。"""
    stats = stats_from_text(_fixture_text(), "md").as_dict()
    assert stats["display_formulas"] == 1
    assert stats["inline_math"] == 5


def test_variable_symbols_are_not_formulas():
    doc = "# 3 方法\n设 $I_a$ 与 $I_g$，变换为 $T_{ag}$。\n"
    stats = stats_from_text(doc, "md").as_dict()
    assert stats["display_formulas"] == 0
    assert stats["inline_math"] == 3


def test_tex_equation_env_counts_as_display_formula():
    doc = r"\section{方法}\begin{equation}z = fb/d\end{equation}"
    stats = stats_from_text(doc, "tex").as_dict()
    assert stats["display_formulas"] == 1


def test_legacy_formula_and_citation_fields_unchanged():
    """`convert.py` 的转换校验拿 `formulas` 当**关键**指标、`citations` 当参考指标。

    修复只新增字段，旧字段口径保持原样 —— 否则会牵动那条校验的判定。
    """
    stats = stats_from_text(_fixture_text(), "md").as_dict()
    assert stats["formulas"] == 6      # 旧口径：块级 + 行内
    assert stats["citations"] == 18    # 旧口径：全文 [n] 出现次数


# ---- 缺陷 3：非正文章节 ----


@pytest.mark.parametrize(
    "title",
    ["参考文献", "References", "REFERENCES", "Bibliography", "致谢",
     "附录", "Appendix", "Acknowledgements", "6 参考文献", "参考文献："],
)
def test_non_body_headings_recognised(title):
    assert is_non_body_heading(title)


@pytest.mark.parametrize("title", ["引言", "方法", "实验", "3.2 语义引导的候选生成",
                                   "参考文献综述", "Introduction"])
def test_body_headings_not_flagged(title):
    """"参考文献综述"是正文章节 —— 只有整个标题就是文献表时才算非正文。"""
    assert not is_non_body_heading(title)


def test_bibliography_excluded_from_body_sections():
    """真机答"最长的是参考文献 621 字"：文献表必须被标出且不算正文章节。"""
    ctx = capture(_FakeSession(_fixture_text()))
    assert len(ctx.sections) == 14
    assert len(ctx.body_sections) == 13
    assert [s.id for s in ctx.sections if s.non_body] == ["参考文献"]


def test_longest_body_section_is_not_bibliography():
    """直接钉住那个错答：正文最长章节是引言，不是 621 字的文献表。"""
    ctx = capture(_FakeSession(_fixture_text()))
    longest = max(ctx.body_sections, key=lambda s: s.chars)
    assert longest.title == "1 引言"


def test_subsections_of_appendix_are_non_body():
    doc = "# 1 引言\n正文。\n\n# 附录\n\n## A.1 推导\n细节。\n\n"
    ctx = capture(_FakeSession(doc))
    non_body = {s.title for s in ctx.sections if s.non_body}
    assert non_body == {"附录", "A.1 推导"}


def test_section_after_appendix_at_same_level_is_body_again():
    """附录之后同级的标题要脱离"非正文"状态，不能一路标到文末。"""
    doc = "# 附录\n\n## A.1\nx\n\n# 6 结语\n收尾。\n"
    ctx = capture(_FakeSession(doc))
    flags = {s.title: s.non_body for s in ctx.sections}
    assert flags["6 结语"] is False


def test_digest_reports_body_section_count():
    digest = capture(_FakeSession(_fixture_text())).facts_digest()
    assert "共 13 个正文章节" in digest
    assert "（非正文）" in digest


def test_digest_states_count_semantics_separately():
    """digest 里不能出现合并后的"引用 18"，且要点明行内符号不是公式。"""
    digest = capture(_FakeSession(_fixture_text())).facts_digest()
    assert "正文引用 8 个不同编号" in digest
    assert "文献表 8 条" in digest
    assert "块级公式 1" in digest
    assert "引用 18" not in digest


def test_split_bibliography_finds_boundary():
    body, bib = split_bibliography(_fixture_text(), "md")
    assert "[1]" in body and "参考文献" not in body
    assert bib.lstrip().startswith("# 参考文献")


# ---- 缺陷 4：编辑历史 ----


class _HistorySession(_FakeSession):
    def __init__(self, text: str, entries, git_enabled: bool = True) -> None:
        super().__init__(text)
        self._entries = entries
        self.git_enabled = git_enabled

    def history(self, limit: int = 20):
        return list(self._entries)[:limit]


def _entry(msg, rel="2 minutes ago", added=3, removed=1):
    return {
        "commit": "abc12345", "message": msg, "relative": rel,
        "lines_added": added, "lines_removed": removed,
    }


def test_history_renders_section_titles_not_ids():
    """commit 里存的是 `polish 1_引言`，给用户看必须是标题。"""
    session = _HistorySession(
        _fixture_text(),
        [_entry("polish 1_引言"), _entry("rewrite 3_2_语义引导的候选生成")],
    )
    out = dispatch(capture(session), "edit_history", {})
    assert "润色「1 引言」" in out
    assert "改写「3.2 语义引导的候选生成」" in out
    assert "1_引言" not in out  # 原始 id 不该漏给用户


def test_history_answers_how_many_edits():
    session = _HistorySession(
        _fixture_text(), [_entry("polish 1_引言"), _entry("rewrite 5_结论")]
    )
    out = dispatch(capture(session), "edit_history", {})
    assert "共 2 次编辑" in out


def test_history_orders_newest_first():
    session = _HistorySession(
        _fixture_text(),
        [_entry("rewrite 5_结论", rel="1 minute ago"),
         _entry("polish 1_引言", rel="1 hour ago")],
    )
    out = dispatch(capture(session), "edit_history", {})
    assert out.index("改写") < out.index("润色")


def test_history_limit_respected_and_clamped():
    entries = [_entry("polish 1_引言", rel=f"{i} minutes ago") for i in range(12)]
    ctx = capture(_HistorySession(_fixture_text(), entries))
    assert dispatch(ctx, "edit_history", {"limit": 3}).count("润色") == 3
    # 上限收敛，不因为要了 999 条就把上下文撑爆
    assert dispatch(ctx, "edit_history", {"limit": 999}).count("润色") == 12


def test_history_bad_limit_raises_dispatch_error():
    ctx = capture(_HistorySession(_fixture_text(), [_entry("polish 1_引言")]))
    with pytest.raises(ToolDispatchError):
        dispatch(ctx, "edit_history", {"limit": "很多"})


def test_history_distinguishes_never_edited_from_unavailable():
    """"没改过"和"读不到"是两回事，措辞必须不同。"""
    never = capture(_HistorySession(_fixture_text(), []))
    assert "还没有任何编辑记录" in dispatch(never, "edit_history", {})

    no_git = capture(_HistorySession(_fixture_text(), [], git_enabled=False))
    assert "未启用 git" in dispatch(no_git, "edit_history", {})


def test_history_absent_when_session_lacks_method():
    """老 session / 测试替身没有 history() 时，如实说不支持，不假装没改过。"""
    ctx = capture(_FakeSession(_fixture_text()))
    out = dispatch(ctx, "edit_history", {})
    assert "不支持历史查询" in out


def test_history_survives_session_raising():
    class _Boom(_FakeSession):
        def history(self, limit: int = 20):
            raise RuntimeError("git 炸了")

    ctx = capture(_Boom(_fixture_text()))  # 捕获不该抛
    assert "读取编辑历史失败" in dispatch(ctx, "edit_history", {})


def test_history_keeps_unknown_section_id_verbatim():
    """章节改名/删除后 id 对不上，保留原样，不猜也不静默丢。"""
    session = _HistorySession(_fixture_text(), [_entry("polish 9_不存在的章节")])
    out = dispatch(capture(session), "edit_history", {})
    assert "9_不存在的章节" in out


def test_edit_history_is_registered_as_tool():
    from paper_agent.application.ask_context import ASK_TOOLS

    names = {t["function"]["name"] for t in ASK_TOOLS}
    assert "edit_history" in names


def test_doc_stats_tool_output_separates_counts():
    ctx = capture(_FakeSession(_fixture_text()))
    out = dispatch(ctx, "doc_stats", {})
    assert "正文引用 8 个不同编号" in out
    assert "文献表 8 条" in out
    assert "正文章节 13" in out


def test_ask_context_still_holds_no_session():
    """只读红线：加了 history 之后快照仍不得持有 session。"""
    session = _HistorySession(_fixture_text(), [_entry("polish 1_引言")])
    ctx = capture(session)
    assert session not in [getattr(ctx, f) for f in ctx.__dataclass_fields__]
    assert all(
        not hasattr(v, "write_working")
        for v in (getattr(ctx, f) for f in ctx.__dataclass_fields__)
    )
