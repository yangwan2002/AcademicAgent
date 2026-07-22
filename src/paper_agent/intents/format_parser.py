"""format_docx 指令解析（B5，Week 5）。

把自然语言排版需求解析为 FormatOp 列表。采用规则解析（确定性、可测、零 LLM
依赖）——排版是确定性操作，规则比 LLM 更可靠。未识别的需求交给上层走 Layer 3
逃生舱或如实告知。

按逗号/顿号/分号切成子句，每个子句自带作用范围 + 若干指令。
"""

from __future__ import annotations

import re

from paper_agent.tools.docx_format import CJK_SIZE, FormatOp

_FONTS = [
    "宋体", "黑体", "楷体", "仿宋", "微软雅黑", "华文中宋", "华文宋体",
    "Times New Roman", "Arial", "Calibri", "Cambria",
]
_ALIGN_KW = {
    "居中": "center", "两端对齐": "justify", "左对齐": "left", "右对齐": "right",
    "justify": "justify", "center": "center",
}
_SPACING_WORD = {"双倍": 2.0, "单倍": 1.0, "double": 2.0, "single": 1.0}
_NUM = r"(\d+(?:\.\d+)?)"


def _scope(clause: str) -> str:
    if "一级标题" in clause or "1级标题" in clause:
        return "heading1"
    if "标题" in clause or "heading" in clause.lower():
        return "headings"
    if "正文" in clause:
        return "body"
    return "all"


def _clauses(message: str) -> list[str]:
    return [c.strip() for c in re.split(r"[，,、;；\n]", message) if c.strip()]


def parse_format_ops(message: str) -> list[FormatOp]:
    ops: list[FormatOp] = []
    for clause in _clauses(message):
        scope = _scope(clause)
        low = clause.lower()

        # 字体
        for fam in _FONTS:
            if fam.lower() in low:
                ops.append(FormatOp("set_font_family", fam, scope,
                                    f"字体→{fam}（{scope}）", "font.name"))
                break

        # 字号（中文字号优先，其次数字+磅/pt/号）
        size = None
        for word, pt in CJK_SIZE.items():
            if word in clause:
                size = pt
                break
        if size is None:
            m = re.search(_NUM + r"\s*(?:pt|磅|号)", clause, re.IGNORECASE)
            if m:
                size = float(m.group(1))
        if size is not None:
            ops.append(FormatOp("set_font_size", size, scope,
                                f"字号→{size}（{scope}）", "font.size"))

        # 行距
        spacing = None
        for word, val in _SPACING_WORD.items():
            if word in low:
                spacing = val
                break
        if spacing is None:
            m = re.search(_NUM + r"\s*倍", clause)
            if m and ("行距" in clause or "倍" in clause):
                spacing = float(m.group(1))
        if spacing is not None:
            ops.append(FormatOp("set_line_spacing", spacing, scope,
                                f"行距→{spacing}（{scope}）", "pPr.spacing"))

        # 对齐
        for kw, val in _ALIGN_KW.items():
            if kw in low:
                ops.append(FormatOp("set_alignment", val, scope,
                                    f"对齐→{val}（{scope}）", "pPr.jc"))
                break

        # 分栏
        if "单栏" in clause:
            ops.append(FormatOp("set_columns", 1, "all", "单栏", "sectPr.cols"))
        elif "双栏" in clause or "两栏" in clause:
            ops.append(FormatOp("set_columns", 2, "all", "双栏", "sectPr.cols"))
        elif "三栏" in clause:
            ops.append(FormatOp("set_columns", 3, "all", "三栏", "sectPr.cols"))

        # 三线表
        if "三线" in clause:
            ops.append(FormatOp("apply_three_line_tables", None, "all",
                                "三线表", "tblBorders"))

        # 首行缩进
        if "首行缩进" in clause:
            m = re.search(_NUM + r"\s*(字符|字|cm|厘米)", clause)
            if m and m.group(2) in ("cm", "厘米"):
                cm = float(m.group(1))
            elif m:
                cm = float(m.group(1)) * 0.37   # 约每字符 0.37cm
            else:
                cm = 0.74                        # 默认 2 字符
            ops.append(FormatOp("set_first_line_indent", cm, scope,
                                f"首行缩进→{cm:.2f}cm（{scope}）", "pPr.ind"))

        # 加粗 / 斜体
        if "加粗" in clause or "bold" in low:
            ops.append(FormatOp("set_bold", True, scope, f"加粗（{scope}）", "font.bold"))
        if "斜体" in clause or "italic" in low:
            ops.append(FormatOp("set_italic", True, scope, f"斜体（{scope}）", "font.italic"))

        # 页面方向
        if "横向" in clause or "landscape" in low:
            ops.append(FormatOp("set_page_orientation", "landscape", "all",
                                "横向", "sectPr.orient"))
        elif "纵向" in clause or "portrait" in low:
            ops.append(FormatOp("set_page_orientation", "portrait", "all",
                                "纵向", "sectPr.orient"))

    return ops


__all__ = ["parse_format_ops"]
