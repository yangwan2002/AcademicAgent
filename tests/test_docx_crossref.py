"""导出后处理：正文 `[n]` → Word 交叉引用域（REF field）。

核心不变式（每条都对应一个真实风险）：

1. **文字保真** —— 含域缓存的可见文字与转换前逐字相同。域把文字搬进了
   `w:fldSimple`，若缓存写错，用户在 Word 里看到的引用就变了。
2. **不原地改写** —— `export_crossrefs` 拒绝 dest == src：原地转换会在中途损坏唯一
   的那份文件。（把域版存回源文件是另一条路，走 `save_to_source`，见
   `test_docx_save_crossref.py`。）
3. **幂等** —— 对已转换文件重跑不叠加第二层域。
4. **断链不制造** —— 正文引用了但文献表没有的编号，保留纯文本而非生成指向空书签
   的域（Word 里会显示「错误！未找到引用源」）。
5. **入站往返** —— 用户提交的文档引用本来就是域时，载入要摊平成纯文本（否则投影层
   看不见引用，模型会自己补、写回时域还会漂移到段尾），导出再升回域并复用原书签。
"""

from __future__ import annotations

import os
import shutil
import zipfile

import pytest

docx = pytest.importorskip("docx")

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from paper_agent.tools.docx_crossref import (
    bookmark_name,
    export_crossrefs,
    visible_text_with_fields,
)

BIB_BEGIN = "<!-- BIBLIOGRAPHY:BEGIN (自动同步，勿手改区块内内容) -->"
BIB_END = "<!-- BIBLIOGRAPHY:END -->"


def _plain_text(path: str) -> str:
    """`para.text` 口径（看不见域）—— 用于反证域确实生效。"""
    d = docx.Document(path)
    return "\n".join(p.text for p in d.paragraphs)


def _document_xml(path: str) -> str:
    return zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")


def _field_count(path: str) -> int:
    """域数量（`fldSimple` + 三段式的 begin）。"""
    xml = _document_xml(path)
    return xml.count("<w:fldSimple") + xml.count('w:fldCharType="begin"')


def _build(tmp_path, body_lines, bib_entries, *, name="src.docx") -> str:
    d = docx.Document()
    for line in body_lines:
        d.add_paragraph(line)
    d.add_paragraph("参考文献")
    d.add_paragraph(BIB_BEGIN)
    for e in bib_entries:
        d.add_paragraph(e)
    d.add_paragraph(BIB_END)
    p = str(tmp_path / name)
    d.save(p)
    return p


@pytest.fixture
def simple(tmp_path):
    src = _build(
        tmp_path,
        ["引言正文，见文献 [1] 与 [3]。", "方法部分参考 [2]。"],
        ["[1] Author A. Title One. 2020.",
         "[2] Author B. Title Two. 2021.",
         "[3] Author C. Title Three. 2022."],
    )
    return src


# ------------------------------------------------------------ 基本转换


def test_converts_body_citations(simple, tmp_path):
    dest = str(tmp_path / "out.docx")
    rep = export_crossrefs(simple, dest)

    assert rep.ok
    assert rep.bookmarks == 3
    assert rep.fields == 3          # [1] [3] [2]
    assert rep.dangling == ()
    assert rep.uncited == ()
    assert os.path.isfile(dest)


def test_text_is_byte_identical(simple, tmp_path):
    """不变式 1：含域缓存的可见文字必须与原文逐字相同。"""
    before = _plain_text(simple)
    dest = str(tmp_path / "out.docx")
    export_crossrefs(simple, dest)
    assert visible_text_with_fields(dest) == before


def test_plain_text_reader_loses_citations(simple, tmp_path):
    """反证域真的生效：`para.text` 看不见域内文字，故引用从纯文本口径消失。

    这正是导出产物不能回灌的原因 —— 主流水线用的就是这个口径。
    """
    dest = str(tmp_path / "out.docx")
    export_crossrefs(simple, dest)
    body = _plain_text(dest).split("参考文献")[0]

    # 方括号与其余文字留在 run 里，只有数字进了域 → 纯文本口径下编号消失
    assert "[1]" not in body
    assert "[3]" not in body
    assert "引言正文，见文献 [] 与 []。" in body


def test_bookmarks_and_fields_in_xml(simple, tmp_path):
    import re
    import zipfile

    dest = str(tmp_path / "out.docx")
    export_crossrefs(simple, dest)
    with zipfile.ZipFile(dest) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")

    assert xml.count("<w:fldSimple") == 3
    assert xml.count("<w:bookmarkStart") == 3
    assert xml.count("<w:bookmarkEnd") == 3
    for n in ("1", "2", "3"):
        assert f'w:name="{bookmark_name(n)}"' in xml
        assert f" REF {bookmark_name(n)} " in xml

    ids = re.findall(r'<w:bookmarkStart w:id="(\d+)"', xml)
    assert len(ids) == len(set(ids)), "书签 id 重复会让 Word 报文档损坏"


# ------------------------------------------------------------ 安全约束


def test_refuses_to_overwrite_source(simple):
    """不变式 2：带域文档回灌会让引用在投影层集体消失。"""
    with pytest.raises(ValueError):
        export_crossrefs(simple, simple)


def test_idempotent(simple, tmp_path):
    """不变式 3：已转换文件重跑不叠加第二层域。"""
    first = str(tmp_path / "a.docx")
    export_crossrefs(simple, first)
    second = str(tmp_path / "b.docx")
    rep = export_crossrefs(first, second)

    assert rep.already_converted
    assert rep.fields == 0
    assert visible_text_with_fields(second) == visible_text_with_fields(first)


def test_dangling_citation_left_as_plain_text(tmp_path):
    """不变式 4：文献表没有 [9]，就不许生成指向空书签的域。"""
    src = _build(
        tmp_path,
        ["正文引用 [1] 和一个不存在的 [9]。"],
        ["[1] Author A. Title. 2020."],
    )
    dest = str(tmp_path / "out.docx")
    rep = export_crossrefs(src, dest)

    assert rep.dangling == ("9",)
    assert not rep.ok                       # 有断链 → 不算完全成功
    assert rep.fields == 1                  # 只转了 [1]
    assert "[9]" in _plain_text(dest)       # 9 仍是纯文本，没进域


# ------------------------------------------------------------ 边界


def test_multi_citation_in_one_bracket(tmp_path):
    """`[12, 21]` 是两条引用 → 两个域，方括号与逗号留作普通文字。"""
    src = _build(
        tmp_path,
        ["联合引用 [12, 21] 见此。"],
        ["[12] Author L. T. 2020.", "[21] Author M. T. 2021."],
    )
    dest = str(tmp_path / "out.docx")
    rep = export_crossrefs(src, dest)

    assert rep.fields == 2
    assert visible_text_with_fields(dest) == _plain_text(src)
    assert "[, ]" in _plain_text(dest)      # 数字都进了域


def test_bibliography_region_itself_not_converted(simple, tmp_path):
    """文献区自身的 `[n]` 是条目编号，不是引用 —— 不得转成域。"""
    dest = str(tmp_path / "out.docx")
    export_crossrefs(simple, dest)
    plain = _plain_text(dest)
    assert "[1] Author A. Title One. 2020." in plain


def test_no_bibliography_region(tmp_path):
    """找不到文献区 → 原样拷出并说明，不静默产出半成品。"""
    d = docx.Document()
    d.add_paragraph("只有正文，引用 [1] 但没有文献表。")
    src = str(tmp_path / "nobib.docx")
    d.save(src)

    dest = str(tmp_path / "out.docx")
    rep = export_crossrefs(src, dest)
    assert rep.fields == 0
    assert rep.warnings
    assert visible_text_with_fields(dest) == _plain_text(src)


def test_uncited_entries_reported(tmp_path):
    """文献表有、正文没引用 → 仅报告。必须在转换前统计，否则会把全部条目算成未引用。"""
    src = _build(
        tmp_path,
        ["正文只引用 [1]。"],
        ["[1] A. T. 2020.", "[2] B. T. 2021.", "[3] C. T. 2022."],
    )
    dest = str(tmp_path / "out.docx")
    rep = export_crossrefs(src, dest)

    assert rep.uncited == ("2", "3")
    assert rep.fields == 1
    assert rep.ok                       # 未引用不是错误


def test_run_formatting_preserved(tmp_path):
    """引用所在段落的段内格式不能因转换而丢失。"""
    d = docx.Document()
    p = d.add_paragraph()
    p.add_run("前缀 ")
    bold = p.add_run("加粗术语")
    bold.bold = True
    p.add_run(" 引用 [1] 后缀。")
    d.add_paragraph("参考文献")
    d.add_paragraph(BIB_BEGIN)
    d.add_paragraph("[1] A. T. 2020.")
    d.add_paragraph(BIB_END)
    src = str(tmp_path / "fmt.docx")
    d.save(src)

    dest = str(tmp_path / "out.docx")
    rep = export_crossrefs(src, dest)
    assert rep.fields == 1

    out = docx.Document(dest)
    bolds = [r.text for r in out.paragraphs[0].runs if r.bold]
    assert "加粗术语" in bolds
    assert visible_text_with_fields(dest) == _plain_text(src)


def test_heading_styles_and_layout_preserved(tmp_path):
    """标题样式与分节属性（页边距等排版）不受转换影响。"""
    d = docx.Document()
    h = d.add_paragraph("一、引言")
    h.style = d.styles["Heading 1"]
    d.add_paragraph("正文引用 [1]。")
    d.add_paragraph("参考文献")
    d.add_paragraph(BIB_BEGIN)
    d.add_paragraph("[1] A. T. 2020.")
    d.add_paragraph(BIB_END)
    src = str(tmp_path / "styled.docx")
    d.save(src)

    before = docx.Document(src).sections[0]
    margins = (before.left_margin, before.right_margin, before.page_width)

    dest = str(tmp_path / "out.docx")
    export_crossrefs(src, dest)

    out = docx.Document(dest)
    assert out.paragraphs[0].style.name == "Heading 1"
    after = out.sections[0]
    assert (after.left_margin, after.right_margin, after.page_width) == margins


def test_bracket_without_number_untouched(tmp_path):
    """非数字方括号（markdown 任务框、`[待填]`）不是引用，不得转域。"""
    src = _build(
        tmp_path,
        ["- [ ] 待办项", "占位 [待填] 与真引用 [1]。"],
        ["[1] A. T. 2020."],
    )
    dest = str(tmp_path / "out.docx")
    rep = export_crossrefs(src, dest)

    assert rep.fields == 1
    plain = _plain_text(dest)
    assert "- [ ] 待办项" in plain
    assert "[待填]" in plain


# ------------------------------------------------------------ 入站：域 → 纯文本


def test_import_flattens_fields_and_projection_sees_citations(tmp_path):
    """用户提交的交叉引用版：摊平后投影层能看见 `[n]`，可见文字逐字不变。

    这是修复前的静默损坏场景 —— 投影看到空的 `[]`，模型会自己补引用。
    """
    from paper_agent.session.docx_document import project_text
    from paper_agent.tools.docx_crossref import import_crossrefs

    src = _build(tmp_path, ["引言 [1] 与 [2]。"], ["[1] A. T. 2020.", "[2] B. T. 2021."])
    user = str(tmp_path / "user.docx")
    export_crossrefs(src, user)
    assert _field_count(user) == 2
    assert "[1]" not in project_text(user).split("参考文献")[0]  # 修复前的症状

    work = str(tmp_path / "work.docx")
    shutil.copyfile(user, work)
    before_visible = visible_text_with_fields(work)
    rep = import_crossrefs(work)

    assert rep.flattened == 2
    assert _field_count(work) == 0
    assert visible_text_with_fields(work) == before_visible   # 文字逐字不变
    body = project_text(work).split("参考文献")[0]
    assert "[1]" in body and "[2]" in body                    # 模型现在看得见
    assert rep.bookmark_map == {"1": "Ref_bib_1", "2": "Ref_bib_2"}


def test_roundtrip_keeps_citations_in_place(tmp_path):
    """域→文本→编辑→域 全程引用不错位。

    修复前：写回时域元素漂移到段尾，`[1]...[2]` 变成 `[]...[12]`。
    """
    from paper_agent.session.docx_document import apply_text, project_text
    from paper_agent.tools.docx_crossref import import_crossrefs

    src = _build(tmp_path, ["引言：关键 [1]，桥接见 [2]。"],
                 ["[1] A. T. 2020.", "[2] B. T. 2021."])
    user = str(tmp_path / "user.docx")
    export_crossrefs(src, user)
    work = str(tmp_path / "work.docx")
    shutil.copyfile(user, work)
    rep = import_crossrefs(work)

    lines = project_text(work).split("\n")
    lines[0] = "引言：核心难点 [1]，桥接方法见 [2]。"
    apply_text(work, "\n".join(lines))

    out = str(tmp_path / "out.docx")
    rep2 = export_crossrefs(work, out, bookmark_map=rep.bookmark_map)
    assert rep2.fields == 2
    assert not rep2.dangling
    first = visible_text_with_fields(out).split("\n")[0]
    assert first == "引言：核心难点 [1]，桥接方法见 [2]。"
    assert "[12]" not in first        # 回归断言：域不再堆到段尾


def test_import_reuses_original_bookmark_names(tmp_path):
    """往返复用用户原书签名 —— Word 自建的 `_Ref...` 不能被改名。

    文档里别处可能有域引用这些书签，改名会让它们断链。
    """
    from paper_agent.tools.docx_crossref import import_crossrefs

    d = docx.Document()
    p = d.add_paragraph()
    p.add_run("引言见 [")
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), " REF _Ref987654321 \\h ")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "1"
    r.append(t)
    fld.append(r)
    p._p.append(fld)
    p.add_run("]。")
    d.add_paragraph("参考文献")
    d.add_paragraph(BIB_BEGIN)
    entry = d.add_paragraph()
    bm = OxmlElement("w:bookmarkStart")
    bm.set(qn("w:id"), "77")
    bm.set(qn("w:name"), "_Ref987654321")
    entry._p.append(bm)
    entry.add_run("[1]")
    bme = OxmlElement("w:bookmarkEnd")
    bme.set(qn("w:id"), "77")
    entry._p.append(bme)
    entry.add_run(" A. T. 2020.")
    d.add_paragraph(BIB_END)
    work = str(tmp_path / "w.docx")
    d.save(work)

    rep = import_crossrefs(work)
    assert rep.bookmark_map == {"1": "_Ref987654321"}

    out = str(tmp_path / "out.docx")
    export_crossrefs(work, out, bookmark_map=rep.bookmark_map)
    xml = _document_xml(out)
    assert "REF _Ref987654321" in xml       # 复用原名
    assert "REF Ref_bib_1" not in xml       # 没有另起新名


def test_foreign_citation_fields_not_flattened(tmp_path):
    """Zotero/EndNote 的域只报告不摊平 —— 动它会切断与文献库的联系。"""
    from paper_agent.tools.docx_crossref import import_crossrefs

    d = docx.Document()
    p = d.add_paragraph()
    p.add_run("引言 ")
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), ' ADDIN ZOTERO_ITEM CSL_CITATION {"x":1} ')
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "(Author, 2020)"
    r.append(t)
    fld.append(r)
    p._p.append(fld)
    d.add_paragraph("参考文献")
    d.add_paragraph(BIB_BEGIN)
    d.add_paragraph("[1] A. T. 2020.")
    d.add_paragraph(BIB_END)
    work = str(tmp_path / "z.docx")
    d.save(work)
    before = visible_text_with_fields(work)

    rep = import_crossrefs(work)
    assert rep.flattened == 0
    assert rep.foreign_fields == 1
    assert rep.warnings
    assert _field_count(work) == 1                     # 域还在
    assert visible_text_with_fields(work) == before


def test_import_is_noop_on_plain_document(tmp_path):
    """纯文本引用的文档：摊平是空操作，文件不被改写。"""
    from paper_agent.tools.docx_crossref import import_crossrefs

    src = _build(tmp_path, ["引言 [1]。"], ["[1] A. T. 2020."])
    before = visible_text_with_fields(src)
    mtime = os.path.getmtime(src)

    rep = import_crossrefs(src)
    assert rep.flattened == 0
    assert not rep.changed
    assert visible_text_with_fields(src) == before
    assert os.path.getmtime(src) == mtime              # 没有落盘


def test_fldchar_form_flattened(tmp_path):
    """Word 原生插入的三段式域（begin/separate/end）也要能摊平。"""
    from paper_agent.session.docx_document import project_text
    from paper_agent.tools.docx_crossref import import_crossrefs

    d = docx.Document()
    p = d.add_paragraph()
    p.add_run("引言见 [")

    def _fldchar(kind):
        r = OxmlElement("w:r")
        fc = OxmlElement("w:fldChar")
        fc.set(qn("w:fldCharType"), kind)
        r.append(fc)
        return r

    p._p.append(_fldchar("begin"))
    ri = OxmlElement("w:r")
    it = OxmlElement("w:instrText")
    it.text = " REF Ref_bib_1 \\h "
    ri.append(it)
    p._p.append(ri)
    p._p.append(_fldchar("separate"))
    rc = OxmlElement("w:r")
    tc = OxmlElement("w:t")
    tc.text = "1"
    rc.append(tc)
    p._p.append(rc)
    p._p.append(_fldchar("end"))
    p.add_run("]。")
    d.add_paragraph("参考文献")
    d.add_paragraph(BIB_BEGIN)
    d.add_paragraph("[1] A. T. 2020.")
    d.add_paragraph(BIB_END)
    work = str(tmp_path / "fc.docx")
    d.save(work)

    rep = import_crossrefs(work)
    assert rep.flattened == 1
    assert rep.bookmark_map == {"1": "Ref_bib_1"}
    assert "引言见 [1]。" in project_text(work)
    xml = _document_xml(work)
    assert "instrText" not in xml and "fldChar" not in xml


# ------------------------------------------------------------ 上标


def test_superscript_covers_brackets(tmp_path):
    """`superscript=True` 时整个 `[n, m]` 连括号一起上标，避免小数字配大括号。"""
    src = _build(tmp_path, ["前文 [1, 2] 后文。"],
                 ["[1] A. T. 2020.", "[2] B. T. 2021."])
    dest = str(tmp_path / "sup.docx")
    rep = export_crossrefs(src, dest, superscript=True)
    assert rep.fields == 2

    out = docx.Document(dest)
    sup = {r.text: r.font.superscript for r in out.paragraphs[0].runs}
    assert sup.get("[") is True
    assert sup.get("]") is True
    assert sup.get(", ") is True
    assert sup.get("前文 ") in (None, False)      # 正文不受影响
    # 域内数字也上标
    xml = _document_xml(dest)
    assert xml.count("superscript") >= 4
    assert visible_text_with_fields(dest) == _plain_text(src)


def test_superscript_off_by_default_keeps_size(tmp_path):
    """默认不上标 —— 原文是行内正文大小的文档不该被擅自改排版。"""
    src = _build(tmp_path, ["前文 [1] 后文。"], ["[1] A. T. 2020."])
    dest = str(tmp_path / "plain.docx")
    export_crossrefs(src, dest)

    out = docx.Document(dest)
    assert all(r.font.superscript in (None, False) for r in out.paragraphs[0].runs)
    assert "superscript" not in _document_xml(dest)


def test_bibliography_entries_never_superscript(tmp_path):
    """上标只作用于正文引用，文献表条目自身保持正常字号。"""
    src = _build(tmp_path, ["前文 [1]。"], ["[1] A. T. 2020."])
    dest = str(tmp_path / "s.docx")
    export_crossrefs(src, dest, superscript=True)

    out = docx.Document(dest)
    entry = [p for p in out.paragraphs if (p.text or "").startswith("[1] A.")][0]
    assert all(r.font.superscript in (None, False) for r in entry.runs)
