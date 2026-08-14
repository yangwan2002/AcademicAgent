"""文档统计（Week 2）：跨格式统计章节/引用/公式/表格/图的数量。

**两类消费者，两套口径 —— 不要混用。**

1. `convert_format` 的「转换后校验」：只关心**转换前后是否一致**，对绝对值语义
   不敏感。用 `sections` / `citations` / `formulas` / `tables` / `figures`。
   跨格式计数天然近似（pandoc 会改变元素表示），因此：
   - 章节数、公式数 视为**关键**指标（不一致 → 校验不通过）；
   - 引用数、表格数、图数 视为**参考**指标（不一致 → 如实告知，不判失败）。

2. 只读问答：用户问的是**绝对值**（"全文有多少条引用"）。这里要
   `unique_citations` / `bib_entries` / `display_formulas` / `inline_math` /
   `body_sections`。

第 2 组是真机评测后补的。原先问答直接复用第 1 组，导致三类错答：把正文引用出现
次数（10，含重复）与文献表条目（8）相加报成"引用 18"；把 `$I_a$` 这类变量符号
算进公式报成"公式 6"；把 621 字的参考文献表当成"最长的一章"。

第 1 组的口径**刻意保持原样**：改它会牵动转换校验的判定，而那条校验比的是一致性，
本来就不需要绝对值正确。
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

# ---- 「非正文」章节标题 ----
#
# 参考文献/致谢/附录不该混进"正文有几章""哪章最长"的口径：一个 600 字的文献表会
# 把这类判断带偏（实测：问"哪一章最长"答"参考文献 621 字"）。
_REF_HEADING = re.compile(
    r"^\s*(?:\d+[.、\s]*)?"
    r"(?:参考文献|参考资料|引用文献|参考书目|文献列表"
    r"|references?|bibliography)\s*[:：]?\s*$",
    re.I,
)
_NON_BODY_HEADING = re.compile(
    r"^\s*(?:\d+[.、\s]*)?"
    r"(?:参考文献|参考资料|引用文献|参考书目|文献列表|致谢|谢辞|鸣谢|附录"
    r"|references?|bibliography|acknowledg(?:e?ments?|ements)|appendix|appendices)"
    r"\s*[:：]?\s*$",
    re.I,
)

_MD_HEADING_LINE = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$")
_TEX_HEADING_LINE = re.compile(r"(?m)^\s*\\(?:sub)*section\*?\s*\{([^}]*)\}")

#: 正文里的数字引用标记。`[1]`、`[1,2]`、`[1-3]` 都算。
_NUM_CITE = re.compile(r"\[(\d+(?:\s*[,，]\s*\d+)*(?:\s*[-–—]\s*\d+)?)\]")
_KEY_CITE = re.compile(r"\[@([^\]]+)\]")
_BIB_ENTRY_MD = re.compile(r"(?m)^\s*\[(\d+)\]")
_BIB_ENTRY_TEX = re.compile(r"\\bibitem")
_TEX_CITE_KEYS = re.compile(r"\\cite[a-zA-Z]*\s*(?:\[[^\]]*\])?\{([^}]*)\}")
_TEX_BIB_START = re.compile(r"\\begin\{thebibliography\}|\\bibliography\s*\{|\\printbibliography")


def is_reference_heading(title: str) -> bool:
    """标题是否是参考文献表。"""
    return bool(_REF_HEADING.match(title or ""))


def is_non_body_heading(title: str) -> bool:
    """标题是否属于非正文章节（参考文献/致谢/附录）。"""
    return bool(_NON_BODY_HEADING.match(title or ""))


def split_bibliography(text: str, ext: str) -> tuple[str, str]:
    """按参考文献标题把全文切成 (正文, 文献表)。

    找不到文献表就返回 ``(text, "")`` —— 此时"正文引用"等于全文引用，不虚构分割。
    """
    ext = (ext or "").lower().lstrip(".")
    if ext == "tex":
        m = _TEX_BIB_START.search(text)
        if m:
            return text[: m.start()], text[m.start():]
        for m in _TEX_HEADING_LINE.finditer(text):
            if is_reference_heading(m.group(1)):
                return text[: m.start()], text[m.start():]
        return text, ""

    for m in _MD_HEADING_LINE.finditer(text):
        if is_reference_heading(m.group(2)):
            return text[: m.start()], text[m.start():]
    return text, ""


#: 引用编号的合理上限。真实论文的文献编号不会到四位数，而 `[2020-2024]`（年份区间）
#: 会。用**量级**排除年份，不能用区间跨度 —— `2020-2024` 跨度只有 4，跨度判据拦不住。
_MAX_CITE_NUM = 999


def _expand_cite_group(group: str) -> list[int]:
    """把 `1,2` / `1-3` 展开成编号列表。超出合理编号量级的（年份等）整组丢弃。"""
    out: list[int] = []
    for part in re.split(r"[,，]", group):
        part = part.strip()
        if not part:
            continue
        rng = re.match(r"^(\d+)\s*[-–—]\s*(\d+)$", part)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            if lo <= hi and hi <= _MAX_CITE_NUM:
                out.extend(range(lo, hi + 1))
            continue
        if part.isdigit() and int(part) <= _MAX_CITE_NUM:
            out.append(int(part))
    return out


@dataclass
class DocStats:
    """各项计数。

    ``citations`` / ``formulas`` 的口径**保持原样不动**：`convert.py` 的转换校验拿
    ``formulas`` 当关键指标、``citations`` 当参考指标，比的是"转换前后是否一致"，
    对绝对值语义不敏感。改它们会牵动那条校验。

    问答需要的是"有多少条引用"这种**绝对值**问题，所以另开字段：
    ``unique_citations`` / ``bib_entries`` / ``display_formulas`` / ``inline_math``。
    实测教训：把正文引用次数（10）和文献表条目（8）相加报成 18，是概念错误，
    加"正则近似值"的免责声明并不能替代正确答案。
    """

    sections: int = 0
    citations: int = 0
    formulas: int = 0
    tables: int = 0
    figures: int = 0
    #: 正文里出现的**唯一**引用编号数（`[1]` 出现两次只算一次）。
    unique_citations: int = 0
    #: 参考文献表里的条目数。
    bib_entries: int = 0
    #: 块级公式数（`$$...$$`、equation/align/gather 环境）。
    display_formulas: int = 0
    #: 行内数学片段数（`$x$` 这类**变量符号**，不是公式）。
    inline_math: int = 0
    #: 正文章节数（不含参考文献/致谢/附录）。
    body_sections: int = 0

    def as_dict(self) -> dict:
        return {
            "sections": self.sections,
            "citations": self.citations,
            "formulas": self.formulas,
            "tables": self.tables,
            "figures": self.figures,
            "unique_citations": self.unique_citations,
            "bib_entries": self.bib_entries,
            "display_formulas": self.display_formulas,
            "inline_math": self.inline_math,
            "body_sections": self.body_sections,
        }


def _count_formulas_text(text: str) -> int:
    scratch = text
    n = 0
    for pat in (_DISPLAY_MATH, _ENV_MATH):
        n += len(pat.findall(scratch))
        scratch = pat.sub(" ", scratch)
    n += len(_INLINE_MATH.findall(scratch))
    return n


def _count_math_split(text: str) -> tuple[int, int]:
    """返回 (块级公式数, 行内数学数)。

    行内 `$x$` 是**变量符号**不是公式，两者必须分开数 —— 混起来会把
    `$I_a$ $I_g$ $T_{ag}$` 报成 3 个公式。
    """
    scratch = text
    display = 0
    for pat in (_DISPLAY_MATH, _ENV_MATH):
        display += len(pat.findall(scratch))
        scratch = pat.sub(" ", scratch)
    return display, len(_INLINE_MATH.findall(scratch))


def _count_citations_split(text: str, ext: str) -> tuple[int, int]:
    """返回 (正文唯一引用数, 文献表条目数)。"""
    body, bib = split_bibliography(text, ext)
    if ext == "tex":
        keys: set[str] = set()
        for m in _TEX_CITE_KEYS.finditer(body):
            keys.update(k.strip() for k in m.group(1).split(",") if k.strip())
        return len(keys), len(_BIB_ENTRY_TEX.findall(bib or text))

    nums: set[int] = set()
    for m in _NUM_CITE.finditer(body):
        nums.update(_expand_cite_group(m.group(1)))
    labels = {m.group(1).strip() for m in _KEY_CITE.finditer(body)}
    entries = len(_BIB_ENTRY_MD.findall(bib)) if bib else 0
    return len(nums) + len(labels), entries


def _count_body_sections(text: str, ext: str) -> int:
    """正文章节数：排除参考文献/致谢/附录及其子节。"""
    pat = _TEX_HEADING_LINE if ext == "tex" else _MD_HEADING_LINE
    total = 0
    skip_level: int | None = None
    for m in pat.finditer(text):
        if ext == "tex":
            title, level = m.group(1), 1
        else:
            title, level = m.group(2), len(m.group(1))
        if skip_level is not None:
            if level > skip_level:
                continue  # 非正文章节的子节，一并跳过
            skip_level = None
        if is_non_body_heading(title):
            skip_level = level
            continue
        total += 1
    return total


def stats_from_text(text: str, ext: str) -> DocStats:
    ext = ext.lower().lstrip(".")
    table = _TEX if ext == "tex" else _MD
    display, inline = _count_math_split(text)
    unique_cites, bib_entries = _count_citations_split(text, ext)
    return DocStats(
        sections=len(table["sections"].findall(text)),
        citations=len(table["citations"].findall(text)),
        formulas=_count_formulas_text(text),
        tables=len(table["tables"].findall(text)),
        figures=len(table["figures"].findall(text)),
        unique_citations=unique_cites,
        bib_entries=bib_entries,
        display_formulas=display,
        inline_math=inline,
        body_sections=_count_body_sections(text, ext),
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

    # docx 也给分开的口径：段落文本按 md 规则切文献表即可（标题在段落文本里）。
    unique_cites, bib_entries = _count_citations_split(full_text, "md")
    heading_titles = [
        p.text for p in doc.paragraphs
        if (p.style and p.style.name or "").lower().startswith("heading")
    ]
    body_sections = 0
    for title in heading_titles:
        if is_non_body_heading(title):
            break  # 文献表之后的都不算正文（docx 拿不到可靠层级，用"到此为止"近似）
        body_sections += 1
    return DocStats(
        sections=sections,
        citations=citations,
        formulas=formulas,
        tables=tables,
        figures=figures,
        unique_citations=unique_cites,
        bib_entries=bib_entries,
        display_formulas=formulas,  # OMML 节点就是块级公式，docx 无行内/块之分
        inline_math=0,
        body_sections=body_sections,
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
    "is_non_body_heading",
    "is_reference_heading",
    "split_bibliography",
]
