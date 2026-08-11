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

from paper_agent.literature.dialect import is_tex, normalize_ext

# .tex 标题：\section{...} / \subsection{...}（允许带星号变体）
_TEX_HEADING = re.compile(r"\\(section|subsection|subsubsection)\*?\s*\{([^}]*)\}")
# .md 标题：行首 1~6 个 # + 空格 + 标题
_MD_HEADING = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$")

_TEX_LEVELS = {"section": 1, "subsection": 2, "subsubsection": 3}

#: 支持章节解析的格式 —— **唯一真相源**。
#:
#: 其他格式（如 docx 是 zip 二进制）解析不出标题，`SectionIndex` 只能给出空索引，
#: 于是 polish / rewrite / add_section 这三个「按章节定位」的意图在其上全部不可用。
#: 这件事必须在**载入时**和**计划校验时**显式说出来，否则用户只会看到
#: 「找不到章节」——一个把根因（格式不支持）说成表象（章节名不对）的误导性诊断。
# docx 也可解析章节：会话层给出的投影文本里，标题按段落**样式**层级带 `#` 前缀，
# 因此与 md 同一套正则即可定位。层级来自 `Heading N` 样式而非文本约定，比 md 的
# `#` 计数更可靠（md 里 `## 引言### 小节` 挤在一行会被并成一个章节，docx 不会）。
SECTIONABLE_EXTS: frozenset[str] = frozenset({"tex", "md", "markdown", "docx"})


def supports_sections(ext: str) -> bool:
    """该格式能否解析出章节。`_collect_headings` 的分派条件由此收敛为一处。"""
    return normalize_ext(ext) in SECTIONABLE_EXTS


def slugify(title: str) -> str:
    """标题 → 稳定 id：小写、非字母数字（含中文外）转下划线、去首尾下划线。

    **不要为了改善匹配而放宽这个函数** —— 它生成的是持久化 id，被
    `facts.claims` 的键、checkpoint 指纹引用。改它会让已有会话数据失配。
    匹配用的归一在 `match_key()`，那个可以随意激进。
    """
    return _slug_core(title) or "section"


def _slug_core(title: str) -> str:
    """`slugify` 的归一核心，**不含**空结果兜底。

    独立出来是为了让 `match_key` 复用同一套字符规则，又不继承 `"section"`
    兜底值 —— 否则空 anchor 会归一成 `"section"`，进而匹配上真名叫 section
    的章节（"空输入匹配到东西"是比匹配不到更糟的失败）。
    """
    s = title.strip().lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "_", s).strip("_")


#: 章节编号前缀：`1 ` / `2.1` / `3_4_` / `一、`。slugify 之后编号会变成
#: `1_` / `2_1` 这种形式，故在已 slug 化的串上剥离。
_LEADING_ORDINAL = re.compile(
    r"^(?:[0-9]+(?:[._][0-9]+)*|[一二三四五六七八九十]+)[._、\s]*"
)


#: 指代性后缀：用户说「引言**部分**」「方法**这章**」指的都是章节本身。
#: 只在**尾部**剥，且剥完必须还剩东西 —— 免得「部分」这种整词 anchor 归一成空。
_TRAILING_DEIXIS = re.compile(
    r"(?:_?(?:这|那)?(?:一)?(?:部分|章节|小节|章|节|段|内容))+$"
)


def match_key(anchor: str) -> str:
    """章节指代 → **匹配用**归一键。与 `slugify`（id 用）刻意分开。

    做两件 slugify 不该做的事：

    1. **剥掉章节编号**。「`1 引言`」slug 化成 `1_引言`，而用户只会说「引言」，
       编号前缀会让双向包含匹配同时失效（真机 2026-08-01 的 `section_not_found`）。
    2. **剥掉尾部指代词**（部分/这章/小节/内容）。真机 2026-08-06：用户说
       「重写我的引言部分」，标题实际是 `1 引言### （1）空地协同SLAM的视觉关联需求`
       （docx 里 `###` 是粘贴残留的字面文字，同属一个 Heading 2 段落）。去编号后
       章节键是 `引言_1_空地协同slam的视觉关联需求`，`引言部分` 与它双向包含都不
       成立 → 又一次 `section_not_found`。剥掉「部分」后 `引言 ⊂ 章节键` 成立。
    """
    s = str(anchor).strip()
    if ":" in s:                      # 去 `sec:` 之类前缀
        s = s.split(":")[-1]
    key = _LEADING_ORDINAL.sub("", _slug_core(s)).strip("_")
    return (_TRAILING_DEIXIS.sub("", key).strip("_") or key)


def anchor_match_rank(anchor: str, section_id: str, section_title: str) -> int:
    """`anchor` 指代该章节的强度。0 = 不匹配，越大越强。

    **这是「章节指代」的唯一判据**：`SectionIndex.find` 与
    `plan_validator._matches` 必须共用它。二者一旦分叉就会出现
    「计划期校验通过、执行期 find 返回 None」这类错位。

    ==  ================================================================
    5   id 或标题精确相等
    4   去编号后精确相等                「方法」→「3 方法」
    3   标题包含 anchor                 原 `find` 的宽松档
    2   去编号后章节名 ⊂ anchor         「引言部分」→「1 引言」
    1   去编号后 anchor ⊂ 章节名        「引言」→「引言与相关工作」
    0   不匹配
    ==  ================================================================

    档 4 **刻意排在档 3 之上**：去掉编号后完全相等是精确匹配，强于子串包含。
    否则文档同时有「3 方法」和「3.1 方法概述」时，anchor=「方法」的结果将由
    两者在文档中的先后决定 —— 靠排版顺序碰对答案，不是靠判据。
    """
    key = str(anchor).strip().lower()
    if not key:
        return 0
    if section_id == key or section_title.lower() == key:
        return 5

    mk = match_key(anchor)
    # id 与标题各算一次取最大 —— 不能命中一个就 return，那会让同一输入
    # 因迭代序而时高时低（不确定的判据比宽松的判据危险得多）。
    section_keys = [k for k in (match_key(section_id), match_key(section_title)) if k]
    if mk and mk in section_keys:
        return 4
    if key in section_title.lower():
        return 3
    if not mk:
        return 0
    best = 0
    for sk in section_keys:
        if sk in mk:
            best = max(best, 2)
        elif mk in sk:
            best = max(best, 1)
    return best


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
        self.ext = normalize_ext(ext)
        self.sections: list[Section] = self._parse()

    @property
    def supports_sections(self) -> bool:
        """本格式是否可解析章节。空 `sections` 有两种成因，此属性把它们区分开：
        「格式不支持」与「格式支持但文档里确实没有标题」。"""
        return supports_sections(self.ext)

    # ---- 解析 ----
    def _collect_headings(self) -> list[tuple[int, int, str, int]]:
        """返回 [(heading_start, body_start, title, level), ...]，按出现顺序。"""
        out: list[tuple[int, int, str, int]] = []
        if is_tex(self.ext):
            for m in _TEX_HEADING.finditer(self.text):
                level = _TEX_LEVELS.get(m.group(1), 1)
                out.append((m.start(), m.end(), m.group(2).strip(), level))
        elif self.ext in ("md", "markdown", "docx"):
            # docx 走 md 分支：投影文本的标题形态与 md 一致（`#` 前缀）。
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
        """按 id / 标题 / 用户措辞定位章节，取**最强**匹配（判据见 `anchor_match_rank`）。

        取最强而非首个命中：文档里既有「1 引言」又有「引言与相关工作」时，
        anchor=「引言」应该给前者，而它在文档中的位置不该决定结果。
        """
        best: Section | None = None
        best_rank = 0
        for s in self.sections:
            rank = anchor_match_rank(anchor, s.id, s.title)
            if rank > best_rank:            # 严格大于 → 同分保留先出现者
                best, best_rank = s, rank
        return best

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
        if is_tex(self.ext):
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
        if is_tex(self.ext):
            cmd = {1: "section", 2: "subsection", 3: "subsubsection"}.get(level, "section")
            return f"\\{cmd}{{{title}}}"
        return "#" * max(1, level) + " " + title

    def insert_text(self, offset: int, block: str) -> str:
        """在 offset 处插入 block，返回新全文。其余逐字节不变。"""
        return self.text[:offset] + block + self.text[offset:]


__all__ = [
    "Section", "SectionIndex", "slugify",
    "SECTIONABLE_EXTS", "supports_sections",
    "match_key", "anchor_match_rank",
]
