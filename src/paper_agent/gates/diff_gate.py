"""PolishGate（B4，卡 1）。

polish 的核心护栏：**只改语言，不动事实**。全部为程序确定性校验，不依赖 LLM。

通过条件（全部满足）：
1. 章节/标题结构不变（正文内标题的层级序列逐项相同）
2. 引用集合逐字相同（\\cite{} 键 + [id]）
3. 数字多重集合相同（\\d+(\\.\\d+)?）
4. 公式逐字保留（$..$ / $$..$$ / \\begin{equation}..）
5. 总字符变化 ≤ 阈值（默认 30%）
6. 无占位符（TODO / 待补充 / xxx / [待填] / TBD）

校验失败时返回可读的违规条目，供 Generator 回灌给 LLM 重试。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# ---- 抽取器 ----
_CITE = re.compile(r"\\cite[a-zA-Z]*\s*(?:\[[^\]]*\])?\{([^}]*)\}")
_BRACKET_REF = re.compile(r"\[([0-9][\w:,\-]*)\]")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_DISPLAY_MATH = re.compile(r"\$\$.*?\$\$", re.S)
_ENV_MATH = re.compile(r"\\begin\{(equation\*?|align\*?|gather\*?)\}.*?\\end\{\1\}", re.S)
_INLINE_MATH = re.compile(r"\$[^$]+?\$")
_TEX_HEADING = re.compile(r"\\(section|subsection|subsubsection)\*?\s*\{[^}]*\}")
_MD_HEADING = re.compile(r"(?m)^(#{1,6})[ \t]+.+$")

_PLACEHOLDERS = ("todo", "tbd", "xxx", "待补充", "待填", "[待填]", "具体请补充")


def _citations(text: str) -> Counter:
    tokens: list[str] = []
    for m in _CITE.finditer(text):
        tokens.extend(t.strip() for t in m.group(1).split(",") if t.strip())
    for m in _BRACKET_REF.finditer(text):
        tokens.append(m.group(1).strip())
    return Counter(tokens)


def _numbers(text: str) -> Counter:
    return Counter(_NUMBER.findall(text))


def _formulas(text: str) -> Counter:
    found: list[str] = []
    scratch = text
    for pat in (_DISPLAY_MATH, _ENV_MATH):
        for m in pat.finditer(scratch):
            found.append(m.group(0))
        scratch = pat.sub(" ", scratch)
    for m in _INLINE_MATH.finditer(scratch):
        found.append(m.group(0))
    return Counter(found)


def _heading_levels(text: str, ext: str) -> list[int]:
    ext = ext.lower().lstrip(".")
    levels: list[int] = []
    if ext == "tex":
        order = {"section": 1, "subsection": 2, "subsubsection": 3}
        for m in _TEX_HEADING.finditer(text):
            cmd = re.match(r"\\(\w+)", m.group(0)).group(1)
            levels.append(order.get(cmd, 1))
    elif ext in ("md", "markdown"):
        for m in _MD_HEADING.finditer(text):
            levels.append(len(m.group(1)))
    return levels


@dataclass
class GateResult:
    passed: bool
    violations: list[str] = field(default_factory=list)


class PolishGate:
    """polish 意图的 Diff Gate。"""

    def __init__(self, ext: str, max_char_change_ratio: float = 0.30) -> None:
        self.ext = ext
        self.max_ratio = max_char_change_ratio

    def check(self, original: str, candidate: str) -> GateResult:
        v: list[str] = []

        if _heading_levels(original, self.ext) != _heading_levels(candidate, self.ext):
            v.append("章节/标题结构发生了变化（polish 不允许增删或改变标题层级）。")

        if _citations(original) != _citations(candidate):
            v.append(
                "引用集合发生了变化（polish 不允许新增/删除/改动任何 \\cite 或 [id] 引用）。"
            )

        if _numbers(original) != _numbers(candidate):
            v.append("数字发生了变化（polish 不允许改动任何数字）。")

        if _formulas(original) != _formulas(candidate):
            v.append("公式发生了变化（polish 必须逐字保留所有公式）。")

        base = max(len(original), 1)
        ratio = abs(len(candidate) - len(original)) / base
        if ratio > self.max_ratio:
            v.append(
                f"字数变化 {ratio:.0%} 超过上限 {self.max_ratio:.0%}"
                "（polish 应保持篇幅相近，仅调整语言）。"
            )

        low = candidate.lower()
        hit = [p for p in _PLACEHOLDERS if p in low]
        if hit:
            v.append(f"输出包含占位符：{', '.join(hit)}（不允许留下未完成内容）。")

        return GateResult(passed=not v, violations=v)


def _count_paragraphs(text: str) -> int:
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    return len(blocks)


_REF_CMD = re.compile(r"\\(?:ref|eqref|autoref|label|cref|Cref|pageref)\s*\{[^}]*\}")


def _data_numbers(text: str) -> set[str]:
    """抽取「数据数字」，剔除引用键 / 交叉引用中的数字（如 \\cite{key1} 里的 1）。"""
    scratch = _CITE.sub(" ", text)
    scratch = _REF_CMD.sub(" ", scratch)
    scratch = _BRACKET_REF.sub(" ", scratch)
    return set(_NUMBER.findall(scratch))


def _cite_tokens(text: str, ext: str) -> set[str]:
    """按格式抽取引用 token：.tex 取 \\cite 键，.md/.docx 取 [id]。"""
    tokens: set[str] = set()
    if ext == "tex":
        for m in _CITE.finditer(text):
            tokens.update(t.strip() for t in m.group(1).split(",") if t.strip())
    else:
        for m in _BRACKET_REF.finditer(text):
            tokens.add(m.group(1).strip())
    return tokens


class RewriteGate:
    """rewrite 意图的 Diff Gate（卡 2）。

    只校验候选正文本身（其他章节由 splice 保证逐字不变）：
    1. 不得引入 level ≤ 目标章节的新标题（防越界改到别的 section）
    2. 所有引用都能解析到 facts 的 known_refs（不新增未核验引用）
    3. 无占位符
    4. 新出现的数字必须已知或经用户确认（防编造）
    5. 段落数在 [原-2, 原+5] 之间（防过度膨胀）
    """

    def __init__(self, ext: str, facts, target_level: int) -> None:
        self.ext = ext.lower().lstrip(".")
        self.facts = facts
        self.target_level = target_level

    def check(
        self,
        original: str,
        candidate: str,
        confirmed_numbers: set[str] | None = None,
    ) -> GateResult:
        v: list[str] = []
        confirmed = confirmed_numbers or set()

        # 1. 不得越界新增标题
        bad_headings = [
            lv for lv in _heading_levels(candidate, self.ext) if lv <= self.target_level
        ]
        if bad_headings:
            v.append("正文中出现了与目标章节同级或更高级的标题（rewrite 只能改单章正文）。")

        # 2. 引用必须可解析到 known_refs
        known = self.facts.bibkeys() if self.ext == "tex" else self.facts.ref_ids()
        unknown = _cite_tokens(candidate, self.ext) - known
        if unknown:
            v.append(
                "引用了未核验的文献：" + ", ".join(sorted(unknown))
                + "（请先经搜文献子 SOP 核验入库）。"
            )

        # 3. 占位符
        low = candidate.lower()
        hit = [p for p in _PLACEHOLDERS if p in low]
        if hit:
            v.append(f"输出包含占位符：{', '.join(hit)}。")

        # 4. 新数字必须已知或确认
        orig_nums = _data_numbers(original)
        cand_nums = _data_numbers(candidate)
        allowed = orig_nums | self.facts.number_values() | confirmed
        fabricated = cand_nums - allowed
        if fabricated:
            v.append(
                "出现了来源不明的新数字：" + ", ".join(sorted(fabricated))
                + "（需在表格/已知数字中，或经用户确认）。"
            )

        # 5. 段落数区间
        po, pc = _count_paragraphs(original), _count_paragraphs(candidate)
        if not (po - 2 <= pc <= po + 5):
            v.append(f"段落数从 {po} 变为 {pc}，超出允许区间 [{po - 2}, {po + 5}]。")

        return GateResult(passed=not v, violations=v)


class AddSectionGate:
    """add_section 意图的 Diff Gate（卡 3）。

    校验新生成的章节块（标题 + 正文）：
    1. 标题与访谈一致
    2. 若给了小节骨架（列表式/模仿式），小节标题需出现
    3. 引用可解析到 known_refs
    4. 无占位符
    5. 新数字已知或经确认
    6. 章节长度在合理区间

    「其他章节逐字不变」由插入操作本身保证，不在此校验。
    """

    def __init__(
        self,
        ext: str,
        facts,
        *,
        min_chars: int = 200,
        max_chars: int = 8000,
    ) -> None:
        self.ext = ext.lower().lstrip(".")
        self.facts = facts
        self.min_chars = min_chars
        self.max_chars = max_chars

    def check(
        self,
        section_block: str,
        *,
        title: str,
        expected_subsections: list[str] | None = None,
        confirmed_numbers: set[str] | None = None,
    ) -> GateResult:
        v: list[str] = []
        confirmed = confirmed_numbers or set()

        if title.lower() not in section_block.lower():
            v.append(f"新章节缺少预期标题「{title}」。")

        for sub in expected_subsections or []:
            if sub.lower() not in section_block.lower():
                v.append(f"缺少约定的小节：「{sub}」。")

        known = self.facts.bibkeys() if self.ext == "tex" else self.facts.ref_ids()
        unknown = _cite_tokens(section_block, self.ext) - known
        if unknown:
            v.append("引用了未核验的文献：" + ", ".join(sorted(unknown)) + "。")

        low = section_block.lower()
        hit = [p for p in _PLACEHOLDERS if p in low]
        if hit:
            v.append(f"输出包含占位符：{', '.join(hit)}。")

        fabricated = _data_numbers(section_block) - (
            self.facts.number_values() | confirmed
        )
        if fabricated:
            v.append(
                "出现了来源不明的新数字：" + ", ".join(sorted(fabricated))
                + "（需在已知数字中或经用户确认）。"
            )

        n = len(section_block)
        if n < self.min_chars:
            v.append(f"章节过短（{n} 字符 < {self.min_chars}），可能信息不足或有空洞。")
        elif n > self.max_chars:
            v.append(f"章节过长（{n} 字符 > {self.max_chars}）。")

        return GateResult(passed=not v, violations=v)


#: 供 SOP 复用的「数据数字」抽取（与 gate 判定一致）
data_numbers = _data_numbers

__all__ = [
    "GateResult", "PolishGate", "RewriteGate", "AddSectionGate", "data_numbers",
]
