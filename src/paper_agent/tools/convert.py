"""格式转换工具（C4，Week 2）。

转换本质是确定性工具：pandoc 负责格式变换，python-docx 负责应用排版预设。
LLM 不参与内容生成。转换产出**新文件**，不改动 working copy。

Week 2 支持的排版预设：
- ``two_column``       双栏（docx）
- ``three_line_table`` 三线表（docx）
"""

from __future__ import annotations

import os
import shutil
import subprocess

_PRESETS = {
    "two_column": "双栏",
    "three_line_table": "三线表",
}


class ConvertError(Exception):
    """转换失败（pandoc 不可用 / 退出非零 / 文件损坏）。"""


def list_typography_presets() -> dict[str, str]:
    """返回 {preset_key: 中文名}。"""
    return dict(_PRESETS)


def check_pandoc() -> str | None:
    """返回 pandoc 版本首行；不可用返回 None。"""
    if shutil.which("pandoc") is None:
        return None
    try:
        out = subprocess.run(
            ["pandoc", "--version"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.splitlines()[0] if out.stdout else "pandoc"
    except (OSError, subprocess.SubprocessError):
        return None


def convert_document(
    source: str,
    target_ext: str,
    presets: list[str] | None = None,
    *,
    output_dir: str = "output",
) -> str:
    """把 source 转成 target_ext，产出新文件，返回其路径。

    presets 仅对 docx 目标生效。pandoc 不可用或转换失败抛 ``ConvertError``。
    """
    if check_pandoc() is None:
        raise ConvertError(
            "未找到 pandoc。请先安装 pandoc（https://pandoc.org/installing.html）；"
            "学术论文场景不做 LLM「翻译」式转换，以免破坏公式与引用。"
        )
    target_ext = target_ext.lower().lstrip(".")
    os.makedirs(output_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(source))[0]
    output_path = os.path.join(output_dir, f"{stem}.{target_ext}")

    try:
        result = subprocess.run(
            ["pandoc", source, "-o", output_path],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConvertError(f"pandoc 调用失败：{exc}") from exc

    if result.returncode != 0:
        raise ConvertError(
            f"pandoc 退出码 {result.returncode}。stderr：\n{result.stderr.strip()}"
        )
    if not os.path.isfile(output_path):
        raise ConvertError("pandoc 未产出目标文件。")

    if target_ext == "docx" and presets:
        _apply_docx_presets(output_path, presets)

    return output_path


# ---- docx 排版预设 ----
def _apply_docx_presets(path: str, presets: list[str]) -> None:
    import docx

    doc = docx.Document(path)
    for p in presets:
        if p == "two_column":
            _set_two_column(doc)
        elif p == "three_line_table":
            _apply_three_line_tables(doc)
    doc.save(path)


def _set_two_column(doc) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    for section in doc.sections:
        sectPr = section._sectPr
        cols = sectPr.find(qn("w:cols"))
        if cols is None:
            cols = OxmlElement("w:cols")
            sectPr.append(cols)
        cols.set(qn("w:num"), "2")


def _apply_three_line_tables(doc) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def _border(kind: str, present: bool):
        el = OxmlElement(f"w:{kind}")
        el.set(qn("w:val"), "single" if present else "none")
        el.set(qn("w:sz"), "8" if present else "0")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "auto")
        return el

    for table in doc.tables:
        tblPr = table._tbl.tblPr
        # 移除旧边框设置
        old = tblPr.find(qn("w:tblBorders"))
        if old is not None:
            tblPr.remove(old)
        borders = OxmlElement("w:tblBorders")
        borders.append(_border("top", True))
        borders.append(_border("bottom", True))
        borders.append(_border("left", False))
        borders.append(_border("right", False))
        borders.append(_border("insideH", False))
        borders.append(_border("insideV", False))
        tblPr.append(borders)
        # 表头行底部加一条线
        if table.rows:
            header = table.rows[0]
            for cell in header.cells:
                tcPr = cell._tc.get_or_add_tcPr()
                tcBorders = OxmlElement("w:tcBorders")
                tcBorders.append(_border("bottom", True))
                tcPr.append(tcBorders)


__all__ = [
    "ConvertError",
    "check_pandoc",
    "convert_document",
    "list_typography_presets",
]
