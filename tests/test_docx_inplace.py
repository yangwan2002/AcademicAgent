"""docx 就地编辑：working copy 是 docx 副本，排版与正文互不牺牲。

替代早前的 pandoc 桥接方案（docx→md 编辑→md→docx）。那条路有结构性缺陷：最终
文件由 markdown 重建，`--reference-doc` 只带回**样式级**定义，而 `docx_format.py`
是逐 run 设 `font.size`。于是「丢来 docx 要求调格式」会排版成功、Gate 通过、
`:save` 报已保存，打开却发现字号没变 —— 报告成功但结果错误。

本文件的核心断言就是钉住那个场景，以及"改正文不该损失排版"。
"""
from __future__ import annotations

import os

import pytest

from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.section_index import SectionIndex, supports_sections

docx = pytest.importorskip("docx")
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.shared import Inches, Pt  # noqa: E402


HEADER_TEXT = "某某大学硕士学位论文"


@pytest.fixture
def paper(tmp_path):
    """一篇**已排好版**的论文：页边距、页眉、居中标题、12pt 正文、跨 run 段落。

    排版细节是刻意加的 —— 只有它们才能验证"改正文不动排版"。
    """
    path = str(tmp_path / "论文.docx")
    doc = docx.Document()
    sec = doc.sections[0]
    sec.left_margin = Inches(1.25)
    sec.right_margin = Inches(1.25)
    sec.header.paragraphs[0].text = HEADER_TEXT

    doc.add_heading("1 引言", 1).alignment = WD_ALIGN_PARAGRAPH.CENTER
    p = doc.add_paragraph("位姿")          # 刻意切碎：跨 run 的"位姿图优化"
    p.add_run("图优")
    p.add_run("化是核心问题[6]。精度 0.87 m。")
    for r in p.runs:
        r.font.size = Pt(12)

    doc.add_heading("1.1 研究背景", 2)
    doc.add_paragraph("本文方法基于图优化框架[12]。").runs[0].font.size = Pt(12)
    doc.add_heading("2 相关工作", 1)
    doc.add_paragraph("多机器人系统[14]的既有工作。").runs[0].font.size = Pt(12)
    doc.save(path)
    return path


def _session(path, tmp_path, *, use_git=True):
    return FileBackedSession(
        path, workspace_root=str(tmp_path / "ws"), use_git=use_git)


def _layout(path):
    """抽出一组排版指纹，用于「改正文前后逐项相同」的比对。"""
    d = docx.Document(path)
    s = d.sections[0]
    return {
        "left": s.left_margin,
        "right": s.right_margin,
        "header": s.header.paragraphs[0].text,
        "align0": d.paragraphs[0].alignment,
        "sizes": [r.font.size for p in d.paragraphs for r in p.runs if r.font.size],
        "styles": [p.style.name for p in d.paragraphs],
    }


# ---------------- working copy 形态 ----------------

def test_working_copy_is_binary_docx(paper, tmp_path):
    """working 必须仍是 docx —— 这是"排版有地方待"的前提。"""
    s = _session(paper, tmp_path)
    assert s.ext == "docx"
    assert s.working_path.endswith(".docx")
    with open(s.working_path, "rb") as fh:
        assert fh.read(2) == b"PK"


def test_source_untouched_until_save(paper, tmp_path):
    before = open(paper, "rb").read()
    s = _session(paper, tmp_path)
    s.write_working(s.read_working().replace("位姿图优化", "因子图优化"))
    assert open(paper, "rb").read() == before


# ---------------- 投影与章节 ----------------

def test_docx_supports_sections():
    assert supports_sections("docx")


def test_projection_marks_headings_from_style(paper, tmp_path):
    """标题层级来自段落**样式**，不是猜文本约定。"""
    text = _session(paper, tmp_path).read_working()
    assert "# 1 引言" in text
    assert "## 1.1 研究背景" in text
    assert "# 2 相关工作" in text


def test_sections_parse_with_levels(paper, tmp_path):
    s = _session(paper, tmp_path)
    idx = SectionIndex(s.read_working(), s.ext)
    assert [(x.title, x.level) for x in idx.list()] == [
        ("1 引言", 1), ("1.1 研究背景", 2), ("2 相关工作", 1),
    ]


def test_projection_is_not_a_file(paper, tmp_path):
    """投影是只读视图：读两次结果一致，且不产生 markdown 落盘文件。"""
    s = _session(paper, tmp_path)
    assert s.read_working() == s.read_working()
    assert not [f for f in os.listdir(s.session_dir) if f.endswith(".md")]


# ---------------- 纯排版（旧方案在此静默失效）----------------

def test_format_only_survives_save(paper, tmp_path):
    """核心回归：丢来 docx、只要求调格式，字号必须活到最终文件。

    旧的 pandoc 往返在这里会静默丢掉 run 级字号 —— 排版报成功、文件却没变。
    """
    from paper_agent.intents.format_docx import execute_format

    s = _session(paper, tmp_path)
    if not s.git_enabled:
        pytest.skip("需要 git")
    assert execute_format(s, "全文改成四号字，1.5倍行距").ok

    dest = s.save_to_source()
    sizes = {r.font.size.pt
             for p in docx.Document(dest).paragraphs
             for r in p.runs if r.font.size}
    assert sizes == {14.0}, f"排版未落到最终文件：{sizes}"


def test_format_target_is_working_copy(paper, tmp_path):
    from paper_agent.intents.format_docx import _format_target

    s = _session(paper, tmp_path)
    assert _format_target(s) == s.working_path


def test_save_overwrites_source_in_place(paper, tmp_path):
    """就地编辑无保真损失，故默认覆盖源文件（与 md/tex 一致），不再另存。"""
    s = _session(paper, tmp_path)
    assert s.save_to_source() == paper


# ---------------- 正文改动不伤排版 ----------------

def test_cross_run_rewrite_works(paper, tmp_path):
    """跨 run 的字符串也能改 —— 朴素逐 run replace 在此必败。

    `位姿图优化` 在 XML 里被切成 ['位姿','图优','化…']，run 边界由格式/拼写检查/
    编辑历史决定，与语义无关。以**段落**为编辑单元才绕开这点。
    """
    s = _session(paper, tmp_path)
    text = s.read_working()
    assert "位姿图优化是核心问题" in text        # 投影里是完整的
    s.write_working(text.replace("位姿图优化", "因子图优化"))
    assert "因子图优化是核心问题[6]。精度 0.87 m。" in [
        p.text for p in docx.Document(s.working_path).paragraphs]


def test_text_edit_preserves_layout(paper, tmp_path):
    """改正文后，排版指纹逐项不变。"""
    s = _session(paper, tmp_path)
    before = _layout(s.working_path)
    s.write_working(s.read_working().replace("位姿图优化", "因子图优化"))
    assert _layout(s.working_path) == before


def test_rewrite_keeps_paragraph_font(paper, tmp_path):
    """整段改写保留首 run 字体属性（新建 run 会退回样式默认值）。"""
    s = _session(paper, tmp_path)
    idx = SectionIndex(s.read_working(), s.ext)
    sec = idx.find("1.1 研究背景")
    s.write_working(idx.splice(sec, "\n改写后的正文[12]，含新表述。"))
    para = [p for p in docx.Document(s.working_path).paragraphs
            if "改写后的正文" in p.text][0]
    assert [r.font.size.pt for r in para.runs] == [12.0]


def test_add_paragraph_inherits_neighbour_format(paper, tmp_path):
    """新增段落继承邻段排版，而不是以 Normal 默认样式突兀出现。"""
    s = _session(paper, tmp_path)
    idx = SectionIndex(s.read_working(), s.ext)
    sec = idx.find("2 相关工作")
    s.write_working(idx.splice(
        sec, "\n多机器人系统[14]的既有工作。\n新增的一段正文[21]。"))
    doc = docx.Document(s.working_path)
    texts = [p.text for p in doc.paragraphs]
    assert "新增的一段正文[21]。" in texts
    assert texts.index("新增的一段正文[21]。") == texts.index("多机器人系统[14]的既有工作。") + 1
    para = [p for p in doc.paragraphs if "新增的一段" in p.text][0]
    assert [r.font.size.pt for r in para.runs] == [12.0]


def test_added_heading_gets_heading_style(paper, tmp_path):
    """新增的 `## 标题` 必须真的成为 Heading 样式。

    否则下次投影认不出它，用户新增的章节等于凭空消失。
    """
    s = _session(paper, tmp_path)
    idx = SectionIndex(s.read_working(), s.ext)
    sec = idx.find("2 相关工作")
    s.write_working(idx.splice(
        sec, "\n多机器人系统[14]的既有工作。\n## 2.1 新小节\n小节正文。"))
    doc = docx.Document(s.working_path)
    para = [p for p in doc.paragraphs if p.text == "2.1 新小节"][0]
    assert para.style.name.startswith(("Heading", "标题"))
    # 往返闭合：新章节能被再次解析出来
    again = SectionIndex(s.read_working(), s.ext)
    assert ("2.1 新小节", 2) in [(x.title, x.level) for x in again.list()]


def test_delete_paragraph(paper, tmp_path):
    s = _session(paper, tmp_path)
    idx = SectionIndex(s.read_working(), s.ext)
    sec = idx.find("1.1 研究背景")
    s.write_working(idx.splice(sec, "\n"))
    assert "本文方法基于图优化框架" not in [
        p.text for p in docx.Document(s.working_path).paragraphs]


def test_insert_at_document_start(paper, tmp_path):
    """插到文首要落在**开头**，不能落到文末（早前实现会错位）。"""
    s = _session(paper, tmp_path)
    s.write_working("文首新增行。\n" + s.read_working())
    assert docx.Document(s.working_path).paragraphs[0].text == "文首新增行。"


# ---------------- 排版 + 正文并存 ----------------

def test_format_then_edit_keeps_both(paper, tmp_path):
    """先排版再改正文：两者都在。这是旧方案做不到的（保存时必牺牲其一）。"""
    from paper_agent.intents.format_docx import execute_format

    s = _session(paper, tmp_path)
    if not s.git_enabled:
        pytest.skip("需要 git")
    assert execute_format(s, "全文改成四号字").ok
    s.write_working(s.read_working().replace("位姿图优化", "因子图优化"))

    doc = docx.Document(s.save_to_source())
    assert any("因子图优化" in p.text for p in doc.paragraphs)      # 正文改动在
    assert {r.font.size.pt for p in doc.paragraphs
            for r in p.runs if r.font.size} == {14.0}              # 排版也在


def test_edit_then_format_keeps_both(paper, tmp_path):
    """反序同样成立 —— 两条链路作用于同一个文件，不存在覆盖顺序问题。"""
    from paper_agent.intents.format_docx import execute_format

    s = _session(paper, tmp_path)
    if not s.git_enabled:
        pytest.skip("需要 git")
    s.write_working(s.read_working().replace("位姿图优化", "因子图优化"))
    assert execute_format(s, "全文改成四号字").ok

    doc = docx.Document(s.save_to_source())
    assert any("因子图优化" in p.text for p in doc.paragraphs)
    assert {r.font.size.pt for p in doc.paragraphs
            for r in p.runs if r.font.size} == {14.0}


# ---------------- undo ----------------

def test_undo_restores_binary_without_git(paper, tmp_path):
    """无 git 时快照必须存**二进制**，否则 undo 会把排版一并丢掉。"""
    s = _session(paper, tmp_path, use_git=False)
    before = _layout(s.working_path)
    s.write_working(s.read_working().replace("位姿图优化", "因子图优化"))
    assert s.undo()
    doc = docx.Document(s.working_path)
    assert any("位姿图优化" in p.text for p in doc.paragraphs)
    assert _layout(s.working_path) == before


def test_undo_after_format_without_git(paper, tmp_path):
    s = _session(paper, tmp_path, use_git=False)
    s.write_working(s.read_working().replace("位姿图优化", "因子图优化"))
    assert s.undo()
    with open(s.working_path, "rb") as fh:
        assert fh.read(2) == b"PK"          # 仍是合法 docx，不是文本


# ---------------- 引用抽取 ----------------

def test_citation_tokens_from_docx(paper, tmp_path):
    """投影不带 pandoc 的 `\\[6\\]` 转义，引用抽取直接可用。"""
    from paper_agent.gates.diff_gate import extract_citation_tokens

    s = _session(paper, tmp_path)
    text = s.read_working()
    assert "\\[" not in text
    assert set(extract_citation_tokens(text, s.ext)) >= {"6", "12", "14"}


# ---------------- 覆盖范围如实说明 ----------------

def test_table_text_is_reported_not_silently_ignored(tmp_path):
    """表格文字投影覆盖不到 —— 要能查出来告知用户，而不是假装处理了。"""
    from paper_agent.session.docx_document import table_text_ignored

    path = str(tmp_path / "t.docx")
    doc = docx.Document()
    doc.add_paragraph("正文段落。")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "表头"
    table.cell(1, 1).text = "0.95"
    doc.save(path)

    assert table_text_ignored(path) == 2
    s = _session(path, tmp_path, use_git=False)
    assert "0.95" not in s.read_working()        # 确实看不见，故需如实告知


# ---------------- 段内局部格式 ----------------

def _bold_doc(tmp_path):
    """`前缀 + **加粗词** + 后缀`：段内局部格式承载语义（术语强调、定理名）。"""
    path = str(tmp_path / "b.docx")
    doc = docx.Document()
    p = doc.add_paragraph("前缀")
    p.add_run("加粗词").bold = True
    p.add_run("后缀。")
    for r in p.runs:
        r.font.size = Pt(12)
    doc.save(path)
    return path


@pytest.mark.parametrize("new_text,expect_bold", [
    ("新前缀加粗词后缀。", "加粗词"),      # 改前缀 → 加粗词不受影响
    ("前缀加粗词新后缀。", "加粗词新"),    # 改后缀 → 变化落在跨界 run 上
    ("前缀强调词后缀。", "强调词"),        # 改加粗词本身 → 加粗属性跟着新词
])
def test_intra_paragraph_bold_survives(tmp_path, new_text, expect_bold):
    """改一处不该抹平整段格式。

    朴素做法（清空段落、用首 run 重写）会把加粗全丢掉。这里只重写**变化区**所覆盖
    的 run，未变化区的 run 连 XML 都不碰。
    """
    from paper_agent.session.docx_document import apply_text

    path = _bold_doc(tmp_path)
    apply_text(path, new_text)
    para = docx.Document(path).paragraphs[0]
    assert para.text == new_text                       # 文本正确性优先
    assert [r.text for r in para.runs if r.bold] == [expect_bold]


def test_full_paragraph_rewrite_keeps_first_run_font(tmp_path):
    """全段皆变时退化为保留首 run 属性 —— 字号不该退回样式默认值。"""
    from paper_agent.session.docx_document import apply_text

    path = _bold_doc(tmp_path)
    apply_text(path, "完全不同的一句话。")
    para = docx.Document(path).paragraphs[0]
    assert para.text == "完全不同的一句话。"
    assert [r.font.size.pt for r in para.runs] == [12.0]


def test_text_correctness_under_random_edits(tmp_path):
    """随机 run 切分 + 随机编辑下，写回文本必须逐字等于输入。

    run 边界与语义无关，公共前后缀的算术很容易在跨界 run 上丢字（开发中确实丢过）。
    这条用随机组合把它钉死。
    """
    import random

    from paper_agent.session.docx_document import apply_text

    rng = random.Random(7)
    chars = "甲乙丙丁戊己庚辛"
    path = str(tmp_path / "r.docx")
    for _ in range(120):
        doc = docx.Document()
        para = doc.add_paragraph("")
        for _k in range(rng.randint(1, 4)):
            run = para.add_run("".join(rng.choice(chars)
                                      for _ in range(rng.randint(1, 4))))
            run.bold = rng.choice([True, None])
        if para.runs and not para.runs[0].text:
            para.runs[0]._element.getparent().remove(para.runs[0]._element)
        doc.save(path)

        old = docx.Document(path).paragraphs[0].text
        if not old:
            continue
        i = rng.randint(0, len(old) - 1)
        j = rng.randint(i, len(old))
        new = old[:i] + "".join(rng.choice(chars)
                                for _ in range(rng.randint(0, 4))) + old[j:]
        if not new:
            continue
        apply_text(path, new)
        assert docx.Document(path).paragraphs[0].text == new
