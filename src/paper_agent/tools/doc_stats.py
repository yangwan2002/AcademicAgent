"""文档统计（Week 2）：跨格式统计章节/引用/公式/表格/图的数量。

用于 convert_format 的「转换后校验」：比较转换前后各项数量是否一致。

跨格式计数天然是近似的（pandoc 会改变某些元素的表示），因此：
- 章节数、公式数 视为**关键**指标（不一致 → 校验不通过）；
- 引用数、表格数、图数 视为**参考**指标（不一致 → 如实告知，不判失败）。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass

# ---- 文本格式（tex / md）正则 ----
_TEX = {
    "sections": re.compile(r"\\(?:sub)*section\*?\s*\{"),
    "citations": re.compile(r"\\cite[a-zA-Z]*\s*(?:\[[^\]]*\])?\{"),
    "tables": re.compile(r"\\begin\{table\*?\}"),
    "figures": re.compile(r"\\begin\{figure\*?\}"),
}
_MD = {
    "sections": re.compile(r"(?m)^#{1,6}[ \t]+\S"),
    "citations": re.compile(r"\[\d+\]|\[@[^\]]+\]"),
    "tables": re.compile(r"(?m)^\s*\|?[ :\-|]+\|[ :\-|]*$"),
    "figures": re.compile(r"!\[[^\]]*\]\([^)]*\)"),
}
_DISPLAY_MATH = re.compile(r"\$\$.*?\$\$", re.S)
_ENV_MATH = re.compile(r"\\begin\{(equation\*?|align\*?|gather\*?)\}.*?\\end\{\1\}", re.S)
_INLINE_MATH = re.compile(r"\$[^$]+?\$")


@dataclass
class DocStats:
    sections: int = 0
    citations: int = 0
    formulas: int = 0
    tables: int = 0
    figures: int = 0

    def as_dict(self) -> dict:
        return {
            "sections": self.sections,
            "citations": self.citations,
            "formulas": self.formulas,
            "tables": self.tables,
            "figures": self.figures,
        }


def _count_formulas_text(text: str) -> int:
    scratch = text
    n = 0
    for pat in (_DISPLAY_MATH, _ENV_MATH):
        n += len(pat.findall(scratch))
        scratch = pat.sub(" ", scratch)
    n += len(_INLINE_MATH.findall(scratch))
    return n


def stats_from_text(text: str, ext: str) -> DocStats:
    ext = ext.lower().lstrip(".")
    table = _TEX if ext == "tex" else _MD
    return DocStats(
        sections=len(table["sections"].findall(text)),
        citations=len(table["citations"].findall(text)),
        formulas=_count_formulas_text(text),
        tables=len(table["tables"].findall(text)),
        figures=len(table["figures"].findall(text)),
    )


def stats_from_docx(path: str) -> DocStats:
    """用 python-docx + 原始 XML 统计 docx 的各项数量。"""
    import docx  # 延迟导入，未装 python-docx 时不影响其他功能

    doc = docx.Document(path)
    sections = sum(
        1 for p in doc.paragraphs
        if (p.style and p.style.name or "").lower().startswith("heading")
    )
    full_text = "\n".join(p.text for p in doc.paragraphs)
    citations = len(re.findall(r"\[\d+\]", full_text))
    tables = len(doc.tables)
    try:
        figures = len(doc.inline_shapes)
    except Exception:  # noqa: BLE001
        figures = 0
    # 公式：直接数 document.xml 里的 OMML 节点
    formulas = 0
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "ignore")
        formulas = xml.count("<m:oMath ") + xml.count("<m:oMath>")
    except Exception:  # noqa: BLE001
        formulas = 0
    return DocStats(
        sections=sections,
        citations=citations,
        formulas=formulas,
        tables=tables,
        figures=figures,
    )


def stats_for(path: str, ext: str) -> DocStats:
    ext = ext.lower().lstrip(".")
    if ext == "docx":
        return stats_from_docx(path)
    with open(path, "r", encoding="utf-8") as fh:
        return stats_from_text(fh.read(), ext)


# ---- 转换后校验 ----
_CRITICAL = ("sections", "formulas")
_ADVISORY = ("citations", "tables", "figures")


@dataclass
class ConvertValidation:
    ok: bool
    source: DocStats
    target: DocStats
    lines: list[str]


def validate_conversion(source: DocStats, target: DocStats) -> ConvertValidation:
    lines: list[str] = []
    ok = True
    s, t = source.as_dict(), target.as_dict()
    for metric in _CRITICAL:
        matched = s[metric] == t[metric]
        mark = "✓" if matched else "✗"
        lines.append(f"  {mark} {metric}：{s[metric]} → {t[metric]}（关键）")
        if not matched:
            ok = False
    for metric in _ADVISORY:
        matched = s[metric] == t[metric]
        mark = "✓" if matched else "≈"
        note = "" if matched else "（参考，跨格式常有差异）"
        lines.append(f"  {mark} {metric}：{s[metric]} → {t[metric]}{note}")
    return ConvertValidation(ok=ok, source=source, target=target, lines=lines)


__all__ = [
    "DocStats",
    "ConvertValidation",
    "stats_from_text",
    "stats_from_docx",
    "stats_for",
    "validate_conversion",
]
