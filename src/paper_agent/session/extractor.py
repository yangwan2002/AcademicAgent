"""AutoExtractor（A4，Week 1 极简版）。

从源文件零门槛抽取 Layer 1 facts。Week 1 只抽 ``known_refs``：
- ``.tex``：``\\bibitem{key}``（可选 ``\\bibitem[label]{key}``），题名取其后一行文本。
- ``.md`` ：参考文献区形如 ``[1] Title...`` 或 ``- [1] Title`` 的行。

抽取是尽力而为，抽不到不报错（v2 的低门槛原则）。
"""

from __future__ import annotations

import os
import re

from paper_agent.session.facts import KnownNumber, KnownRef

_TEX_BIBITEM = re.compile(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}([^\n]*)")
_MD_REF_LINE = re.compile(r"(?m)^\s*(?:[-*]\s*)?\[(\w+)\]\s*(.+?)\s*$")

_TABULAR = re.compile(r"\\begin\{tabular\}.*?\\end\{tabular\}", re.S)
_MD_TABLE_ROW = re.compile(r"(?m)^\s*\|.*\|\s*$")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def extract_refs(text: str, ext: str) -> list[KnownRef]:
    ext = ext.lower().lstrip(".")
    refs: list[KnownRef] = []
    if ext == "tex":
        for i, m in enumerate(_TEX_BIBITEM.finditer(text), start=1):
            bibkey = m.group(1).strip()
            title = m.group(2).strip().rstrip(".")
            refs.append(
                KnownRef(id=str(i), bibkey=bibkey, title=title, verified=False)
            )
    elif ext in ("md", "markdown"):
        for m in _MD_REF_LINE.finditer(text):
            rid = m.group(1).strip()
            title = m.group(2).strip()
            refs.append(KnownRef(id=rid, bibkey="", title=title, verified=False))
    return _dedupe(refs)


def extract_numbers(text: str, ext: str) -> list[KnownNumber]:
    """从表格抽取数字作为已知数字（confirmed=True），供 rewrite gate 判定编造。

    - ``.tex``：``\\begin{tabular}...\\end{tabular}`` 块内的数字。
    - ``.md`` ：markdown 表格行（含 ``|``）内的数字。
    """
    ext = ext.lower().lstrip(".")
    blocks: list[str] = []
    if ext == "tex":
        blocks = _TABULAR.findall(text)
    elif ext in ("md", "markdown"):
        blocks = _MD_TABLE_ROW.findall(text)

    seen: set[str] = set()
    out: list[KnownNumber] = []
    for block in blocks:
        for value in _NUMBER.findall(block):
            if value not in seen:
                seen.add(value)
                out.append(KnownNumber(value=value, source="table", confirmed=True))
    return out


def extract_structure_from(path: str, section_name: str) -> list[str]:
    """从参考论文抽取某章节的小节骨架（供 add_section 的「模仿式」结构）。

    返回该章节下一级子标题的标题列表；找不到或无子标题返回空列表。
    """
    from paper_agent.session.section_index import SectionIndex  # 延迟导入避免环依赖

    if not os.path.isfile(path):
        return []
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    with open(path, "r", encoding="utf-8") as fh:
        index = SectionIndex(fh.read(), ext)
    target = index.find(section_name)
    if target is None:
        return []
    subs = [
        s.title for s in index.list()
        if s.level == target.level + 1
        and target.body_start <= s.heading_start < target.body_end
    ]
    return subs


def _dedupe(refs: list[KnownRef]) -> list[KnownRef]:
    seen: set[str] = set()
    out: list[KnownRef] = []
    for r in refs:
        key = r.bibkey or r.id
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


__all__ = ["extract_refs", "extract_numbers", "extract_structure_from"]
