"""章节索引（A2）。

解析 working copy，生成章节锚点索引，供 polish 定位目标章节并做原地替换。

Week 1 支持两种格式：
- ``.tex``：``\\section{...}``（level 1）/ ``\\subsection{...}``（level 2）
- ``.md`` ：``#`` 标题（level = ``#`` 个数）

设计要点：
- 只做「定位 + 切片」，不理解语义。
- 章节边界：一个标题的正文延伸到「下一个 level ≤ 自身的标题」或文件末尾，
  因此 polish 一个 section 时天然包含其下属 subsection。
- 提供 ``splice`` 做纯字符区间替换，保证除目标区间外逐字节不变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# .tex 标题：\section{...} / \subsection{...}（允许带星号变体）
_TEX_HEADING = re.compile(r"\\(section|subsection|subsubsection)\*?\s*\{([^}]*)\}")
# .md 标题：行首 1~6 个 # + 空格 + 标题
_MD_HEADING = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$")

_TEX_LEVELS = {"section": 1, "subsection": 2, "subsubsection": 3}


def slugify(title: str) -> str:
    """标题 → 稳定 id：小写、非字母数字（含中文外）转下划线、去首尾下划线。"""
    s = title.strip().lower()
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "_", s)
    return s.strip("_") or "section"


@dataclass
class Section:
    """一个章节锚点。偏移均为相对 working 全文的字符位置。"""

    id: str
    title: str
    level: int
    heading_start: int   # 标题起始（'\' 或 '#'）
    body_start: int      # 正文起始（标题行之后）
    body_end: int        # 正文结束（下一个 level≤自身的标题起始，或 EOF）


class SectionIndex:
    """working copy 全文的章节索引。"""

    def __init__(self, text: str, ext: str) -> None:
        self.text = text
        self.ext = ext.lower().lstrip(".")
        self.sections: list[Section] = self._parse()

    # ---- 解析 ----
    def _collect_headings(self) -> list[tuple[int, int, str, int]]:
        """返回 [(heading_start, body_start, title, level), ...]，按出现顺序。"""
        out: list[tuple[int, int, str, int]] = []
        if self.ext == "tex":
            for m in _TEX_HEADING.finditer(self.text):
                level = _TEX_LEVELS.get(m.group(1), 1)
                out.append((m.start(), m.end(), m.group(2).strip(), level))
        elif self.ext in ("md", "markdown"):
            for m in _MD_HEADING.finditer(self.text):
                level = len(m.group(1))
                out.append((m.start(), m.end(), m.group(2).strip(), level))
        return out

    def _parse(self) -> list[Section]:
        heads = self._collect_headings()
        sections: list[Section] = []
        for i, (hstart, bstart, title, level) in enumerate(heads):
            # body_end：找之后第一个 level ≤ 当前 level 的标题
            body_end = len(self.text)
            for hstart2, _b2, _t2, level2 in heads[i + 1 :]:
                if level2 <= level:
                    body_end = hstart2
                    break
            sections.append(
                Section(
                    id=self._unique_id(slugify(title), sections),
                    title=title,
                    level=level,
                    heading_start=hstart,
                    body_start=bstart,
                    body_end=body_end,
                )
            )
        return sections

    @staticmethod
    def _unique_id(base: str, existing: list[Section]) -> str:
        ids = {s.id for s in existing}
        if base not in ids:
            return base
        n = 2
        while f"{base}_{n}" in ids:
            n += 1
        return f"{base}_{n}"

    # ---- 查询 ----
    def list(self) -> list[Section]:
        return list(self.sections)

    def find(self, anchor: str) -> Section | None:
        """按 id 或标题（不区分大小写）定位章节。"""
        key = anchor.strip().lower()
        for s in self.sections:
            if s.id == key or s.title.lower() == key:
                return s
        # 宽松：标题包含关系
        for s in self.sections:
            if key and key in s.title.lower():
                return s
        return None

    def body_of(self, section: Section) -> str:
        return self.text[section.body_start : section.body_end]

    def splice(self, section: Section, new_body: str) -> str:
        """用 new_body 替换该章节正文区间，返回新的全文。其余部分逐字节不变。"""
        return (
            self.text[: section.body_start]
            + new_body
            + self.text[section.body_end :]
        )

    # ---- 插入（add_section 用）----
    def _doc_end_offset(self) -> int:
        """新章节可插入的「正文末尾」：tex 取 bibliography/\\end{document} 之前。"""
        if self.ext == "tex":
            for marker in (r"\begin{thebibliography}", r"\end{document}"):
                i = self.text.find(marker)
                if i >= 0:
                    return i
        return len(self.text)

    def insertion_offset(self, position: str) -> int:
        """把位置说明解析为字符偏移。

        position ∈ {"start", "end", "after:<sid>", "before:<sid>"}。
        """
        if position == "start":
            return self.sections[0].heading_start if self.sections else self._doc_end_offset()
        if position.startswith("after:"):
            s = self.find(position[len("after:"):])
            return s.body_end if s else self._doc_end_offset()
        if position.startswith("before:"):
            s = self.find(position[len("before:"):])
            return s.heading_start if s else self._doc_end_offset()
        return self._doc_end_offset()

    def render_heading(self, title: str, level: int = 1) -> str:
        if self.ext == "tex":
            cmd = {1: "section", 2: "subsection", 3: "subsubsection"}.get(level, "section")
            return f"\\{cmd}{{{title}}}"
        return "#" * max(1, level) + " " + title

    def insert_text(self, offset: int, block: str) -> str:
        """在 offset 处插入 block，返回新全文。其余逐字节不变。"""
        return self.text[:offset] + block + self.text[offset:]


__all__ = ["Section", "SectionIndex", "slugify"]
