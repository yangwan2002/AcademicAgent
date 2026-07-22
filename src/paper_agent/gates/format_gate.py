"""FormatGate（B4，卡 5）。

format_docx 的核心红线：**正文文本前后必须完全一致**（只允许改格式属性）。
校验方式：提取 docx 正文可见文本 → SHA-256，操作前后必须相同；另校验段落/表格/
图数量不变、文件可打开。任一不符 → 由上层立即 git 回滚。
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass

from paper_agent.gates.diff_gate import GateResult


@dataclass
class DocxSnapshot:
    text_hash: str
    paragraphs: int
    tables: int
    figures: int


def _visible_text(doc) -> str:
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def snapshot_docx(path: str) -> DocxSnapshot:
    """提取 docx 正文快照（不含页眉/页脚——那不属于正文）。"""
    import docx

    doc = docx.Document(path)
    text = _visible_text(doc)
    try:
        figures = len(doc.inline_shapes)
    except Exception:  # noqa: BLE001
        figures = 0
    formula_omml = 0
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "ignore")
        formula_omml = xml.count("<m:oMath")
    except Exception:  # noqa: BLE001
        pass
    return DocxSnapshot(
        text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        paragraphs=len(doc.paragraphs),
        tables=len(doc.tables),
        figures=figures + formula_omml,
    )


class FormatGate:
    def check(self, before: DocxSnapshot, after_path: str) -> GateResult:
        v: list[str] = []
        try:
            after = snapshot_docx(after_path)
        except Exception as exc:  # noqa: BLE001
            return GateResult(passed=False, violations=[f"文件无法打开或损坏：{exc}"])

        if before.text_hash != after.text_hash:
            v.append("正文文本发生了变化（format 操作只能改格式，不能动一个字）。")
        if before.paragraphs != after.paragraphs:
            v.append(f"段落数从 {before.paragraphs} 变为 {after.paragraphs}。")
        if before.tables != after.tables:
            v.append(f"表格数从 {before.tables} 变为 {after.tables}。")
        if before.figures != after.figures:
            v.append(f"图/公式数从 {before.figures} 变为 {after.figures}。")
        return GateResult(passed=not v, violations=v)


__all__ = ["DocxSnapshot", "FormatGate", "snapshot_docx"]
