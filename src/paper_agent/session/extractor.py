"""AutoExtractor（A4，Week 1 极简版）。

从源文件零门槛抽取 Layer 1 facts。Week 1 只抽 ``known_refs``：
- ``.tex``：``\\bibitem{key}``（可选 ``\\bibitem[label]{key}``），题名取其后一行文本。
- ``.md`` ：参考文献区形如 ``[1] Title...`` 或 ``- [1] Title`` 的行。
- ``.docx``：同 ``.md`` —— 投影文本里文献条目就是 ``[1] Title`` 这样的整行。
  调用方（``chat.py`` 的 ``_TEXT_EXT``）一直包含 docx，但这里以前没有对应分支，
  于是 docx 初稿的参考文献静默抽到 0 条。

抽取是尽力而为，抽不到不报错（v2 的低门槛原则）。

**抽取 ≠ 核验**：这里产出的 ``KnownRef.verified`` 恒为 ``False``。它只表示
「这篇稿子里出现过这条引用」，不表示该文献真实存在、题名作者年份准确。
真正的核验是检索链路（``literature.retrieval.cross_verify``）的职责，
只对经系统搜索入库的文献生效；用户手写的条目从不进核验。
"""

from __future__ import annotations

import os
import re

from paper_agent.literature.dialect import is_tex, normalize_ext
from paper_agent.session.facts import KnownNumber, KnownRef

_TEX_BIBITEM = re.compile(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}([^\n]*)")
_MD_REF_LINE = re.compile(r"(?m)^\s*(?:[-*]\s*)?\[(\w+)\]\s*(.+?)\s*$")

_TABULAR = re.compile(r"\\begin\{tabular\}.*?\\end\{tabular\}", re.S)
_MD_TABLE_ROW = re.compile(r"(?m)^\s*\|.*\|\s*$")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def extract_refs(text: str, ext: str) -> list[KnownRef]:
    ext = normalize_ext(ext)
    refs: list[KnownRef] = []
    if is_tex(ext):
        for i, m in enumerate(_TEX_BIBITEM.finditer(text), start=1):
            bibkey = m.group(1).strip()
            title = m.group(2).strip().rstrip(".")
            refs.append(
                KnownRef(id=str(i), bibkey=bibkey, title=title, verified=False)
            )
    elif ext in ("md", "markdown", "docx"):
        # 只在参考文献区内抽，避免把正文里的 `[1]` 引用标记当成条目。
        # md 的行内引用（`见 [1]。`）不会被 `_MD_REF_LINE` 误配（它要求行首），
        # 但 docx 投影是「一段一行」，正文段落若以引用开头就会撞上。
        region, has_bib = _bib_region(text, ext)
        for m in _MD_REF_LINE.finditer(region):
            rid = m.group(1).strip()
            title = m.group(2).strip()
            refs.append(KnownRef(id=rid, bibkey="", title=title, verified=False))
        if not refs and has_bib:
            # 文献表用 Word 自动编号列表时，编号由 numPr 渲染、**不在文本流里**，
            # 投影只剩 `Zhang X. Visual SLAM. 2021.` —— `_MD_REF_LINE` 要求行首有
            # `[n]`，于是一条也抽不到。此时按段落顺序补编号。
            #
            # 只在「显式编号一条都没抽到」时才走这条路：混着用会让同一篇稿子里的
            # 编号来源不一致（一部分来自文本、一部分来自位置），正文 `[n]` 到底
            # 指哪条就说不清了。宁可整篇按一种口径。
            #
            # 还要求 `has_bib`：切不出文献区时 region 是**全文**，此时按位置补编号
            # 会把正文段落整段当成文献（无标题 docx 上实测抽出 3 条正文句子）。
            # 没有文献区就是没有文献表，抽 0 条才是对的。
            refs = _positional_refs(region)
    return _dedupe(refs)


#: 自动编号列表回退时，判定「这段像一条文献著录」的最短长度。
_MIN_ENTRY_CHARS = 12


def _positional_refs(region: str) -> list[KnownRef]:
    """把参考文献区里无编号的段落按顺序当作条目（自动编号列表的回退路径）。

    编号按出现顺序从 1 起给 —— Word 自动编号默认也是 1 起、按顺序排，因此位置序
    与用户在 Word 里看到的编号一致，正文 `[n]` 能对上。

    过滤掉标题行（投影里带 `#` 前缀）和过短的行，避免把区内的说明文字当成文献。
    """
    out: list[KnownRef] = []
    for line in region.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if len(s) < _MIN_ENTRY_CHARS:
            continue
        out.append(
            KnownRef(id=str(len(out) + 1), bibkey="", title=s, verified=False)
        )
    return out


def _bib_region(text: str, ext: str) -> tuple[str, bool]:
    """取参考文献区文本，并告知是否真切出了区。

    切不出来就退回全文（保持旧行为，显式 `[n]` 编号的条目不因此少抽），但第二个
    返回值为 False —— 位置回退必须凭它把两种情形分开，否则会在无文献区的文档上
    把正文当文献。
    """
    from paper_agent.tools.doc_stats import split_bibliography  # 延迟导入避免环依赖

    _, region = split_bibliography(text, ext)
    return (region, True) if region else (text, False)


def extract_numbers(text: str, ext: str) -> list[KnownNumber]:
    """从表格抽取数字作为已知数字（confirmed=True），供 rewrite gate 判定编造。

    - ``.tex``：``\\begin{tabular}...\\end{tabular}`` 块内的数字。
    - ``.md`` ：markdown 表格行（含 ``|``）内的数字。
    """
    ext = normalize_ext(ext)
    blocks: list[str] = []
    if is_tex(ext):
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
