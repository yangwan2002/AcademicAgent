"""docx 排版工具（C5，Week 5，Layer 1）。

只改格式属性（rPr / pPr / sectPr / tblPr），**绝不动正文文本**。每个操作声明
自己修改的字段类别，供审计与展示。所有操作都是确定性的 python-docx 变换。

作用范围（scope）：
- ``all``      全部段落
- ``body``     正文段落（排除 Heading 样式）
- ``headings`` 所有标题
- ``heading1`` 一级标题
"""

from __future__ import annotations

from dataclasses import dataclass

from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

# 中文字号 → 磅
CJK_SIZE = {
    "初号": 42, "小初": 36, "一号": 26, "小一": 24, "二号": 22, "小二": 18,
    "三号": 16, "小三": 15, "四号": 14, "小四": 12, "五号": 10.5, "小五": 9,
    "六号": 7.5,
}
_ALIGN = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}


@dataclass
class FormatOp:
    name: str
    value: object = None
    scope: str = "all"
    label: str = ""              # 展示用中文说明
    changed_fields: str = ""     # 审计：本操作改动的字段类别


# ---- scope 解析 ----
def _is_heading(p) -> bool:
    return (p.style and p.style.name or "").lower().startswith("heading")


def _paragraphs(doc, scope: str):
    ps = list(doc.paragraphs)
    if scope == "body":
        return [p for p in ps if not _is_heading(p)]
    if scope == "headings":
        return [p for p in ps if _is_heading(p)]
    if scope == "heading1":
        return [p for p in ps if (p.style and p.style.name or "").lower() == "heading 1"]
    return ps


# ---- 字符级 ----
def _set_run_font(run, family: str) -> None:
    run.font.name = family
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), family)


def op_set_font_family(doc, scope, family):
    for p in _paragraphs(doc, scope):
        for r in p.runs:
            _set_run_font(r, family)


def op_set_font_size(doc, scope, size_pt):
    for p in _paragraphs(doc, scope):
        for r in p.runs:
            r.font.size = Pt(float(size_pt))


def op_set_bold(doc, scope, on):
    for p in _paragraphs(doc, scope):
        for r in p.runs:
            r.font.bold = bool(on)


def op_set_italic(doc, scope, on):
    for p in _paragraphs(doc, scope):
        for r in p.runs:
            r.font.italic = bool(on)


# ---- 段落级 ----
def op_set_alignment(doc, scope, align):
    val = _ALIGN.get(str(align).lower())
    for p in _paragraphs(doc, scope):
        p.alignment = val


def op_set_line_spacing(doc, scope, value):
    for p in _paragraphs(doc, scope):
        p.paragraph_format.line_spacing = float(value)


def op_set_first_line_indent(doc, scope, cm):
    for p in _paragraphs(doc, scope):
        p.paragraph_format.first_line_indent = Cm(float(cm))


# ---- 结构级 ----
def op_set_columns(doc, scope, n):
    for section in doc.sections:
        sectPr = section._sectPr
        cols = sectPr.find(qn("w:cols"))
        if cols is None:
            cols = OxmlElement("w:cols")
            sectPr.append(cols)
        cols.set(qn("w:num"), str(int(n)))


def op_set_margins(doc, scope, cm):
    m = Cm(float(cm))
    for section in doc.sections:
        section.top_margin = section.bottom_margin = m
        section.left_margin = section.right_margin = m


def op_set_page_orientation(doc, scope, orient):
    want = WD_ORIENT.LANDSCAPE if str(orient).lower().startswith("land") else WD_ORIENT.PORTRAIT
    for section in doc.sections:
        if section.orientation != want:
            section.orientation = want
            section.page_width, section.page_height = section.page_height, section.page_width


# ---- 表格 ----
def _border(kind, present):
    el = OxmlElement(f"w:{kind}")
    el.set(qn("w:val"), "single" if present else "none")
    el.set(qn("w:sz"), "8" if present else "0")
    el.set(qn("w:space"), "0")
    el.set(qn("w:color"), "auto")
    return el


def op_apply_three_line_tables(doc, scope, value):
    for table in doc.tables:
        tblPr = table._tbl.tblPr
        old = tblPr.find(qn("w:tblBorders"))
        if old is not None:
            tblPr.remove(old)
        borders = OxmlElement("w:tblBorders")
        for kind, present in (("top", True), ("bottom", True), ("left", False),
                              ("right", False), ("insideH", False), ("insideV", False)):
            borders.append(_border(kind, present))
        tblPr.append(borders)
        if table.rows:
            for cell in table.rows[0].cells:
                tcBorders = OxmlElement("w:tcBorders")
                tcBorders.append(_border("bottom", True))
                cell._tc.get_or_add_tcPr().append(tcBorders)


REGISTRY = {
    "set_font_family": op_set_font_family,
    "set_font_size": op_set_font_size,
    "set_bold": op_set_bold,
    "set_italic": op_set_italic,
    "set_alignment": op_set_alignment,
    "set_line_spacing": op_set_line_spacing,
    "set_first_line_indent": op_set_first_line_indent,
    "set_columns": op_set_columns,
    "set_margins": op_set_margins,
    "set_page_orientation": op_set_page_orientation,
    "apply_three_line_tables": op_apply_three_line_tables,
}


def apply_ops(path: str, ops: list[FormatOp]) -> None:
    """按顺序对 docx 文件应用所有格式操作并保存。"""
    import docx

    doc = docx.Document(path)
    for op in ops:
        func = REGISTRY.get(op.name)
        if func is None:
            raise ValueError(f"未知格式操作：{op.name}")
        func(doc, op.scope, op.value)
    doc.save(path)


__all__ = ["FormatOp", "REGISTRY", "apply_ops", "CJK_SIZE"]
