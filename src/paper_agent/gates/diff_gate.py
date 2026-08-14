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

from paper_agent.literature.dialect import citable_tokens, is_tex, normalize_ext

# ---- 抽取器 ----
_CITE = re.compile(r"\\cite[a-zA-Z]*\s*(?:\[[^\]]*\])?\{([^}]*)\}")
# 首字符必须允许字母：ledger 的 reference_id 是 `"r" + sha1[:12]`（dedup.py），
# 旧式 `[0-9]...` 开头**一个真实 id 都抽不到** ⇒ ① 引用不算引用、配额恒为 0，
# ② id 里的数字被 _data_numbers 当成「编造的新数字」。两个 Gate 同时误判。
# 仍要求以字母/数字开头，避免把 markdown 链接 `[文字](url)`、任务框 `[ ]` 吞进来。
#
# 允许**内部空白**：真机里多引用写作 `[12, 21]`（逗号后带空格），旧式 `[\w:,\-]*` 不含空格
# ⇒ 整个 `[12, 21]` 匹配不上 ⇒ ① 这条引用不算引用、配额白算，② `_data_numbers` 剔不掉它，
# 12 与 21 被当成「编造的新数字」逐个弹窗问用户。tex 侧因 `_CITE` 显式 split(",") 无此问题，
# 这是 md 方言少做了拆分。末字符不允许空白，避免把 `[ ]`（任务框）吞进来。
#
# 允许方括号被**反斜杠转义**（`\[6\]`）：pandoc 从 docx 转回 markdown 时会转义方括号，
# 不认这形态则 docx 来源的文稿「引用一条都抽不到 + 每个引用编号都被当成编造数字」，
# 与上面那条缺陷同一签名。
_BRACKET_REF = re.compile(
    r"\\?\[([0-9A-Za-z][\w:,\-]*(?:[ \t]*,[ \t]*[\w:\-]+)*)\\?\](?!\()"
)
#: `[n]` 标记语法的**单一来源**。`bibliography_sync` 顺序重编号要就地改写正文标记，
#: 必须和 `extract_citation_tokens` 认的是同一套语法 —— 各自写一份正则必然漂移
#: （抽取认得 `[12, 21]` / `\[7\]`，改写认不得，就会漏改成悬空引用）。
CITATION_BRACKET_RE = _BRACKET_REF
# markdown 任务框 `- [x]` / `* [ ]`：`[x]` 会被 _BRACKET_REF 当成 token `x`
# ⇒ 草稿里的待办清单变成「新增引用无法解析：x」。只在**列表项开头**这个位置剔除，
# 不影响正文里合法的单字母 bibkey。（`[ ]` 因首字符须字母/数字本就不匹配。）
_TASK_BOX = re.compile(r"(?m)^[ \t]*[-*+][ \t]+\[[ xX]\]")
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
    ext = normalize_ext(ext)
    levels: list[int] = []
    if is_tex(ext):
        order = {"section": 1, "subsection": 2, "subsubsection": 3}
        for m in _TEX_HEADING.finditer(text):
            cmd = re.match(r"\\(\w+)", m.group(0)).group(1)
            levels.append(order.get(cmd, 1))
    elif ext in ("md", "markdown", "docx"):
        # docx 与 md 同分支：docx 走**投影文本**，标题形态就是 `#` 前缀
        # （见 `section_index._collect_headings`）。漏掉 docx 会让下面的标题结构
        # 检查对 docx 静默变成空操作 —— 同一处「标题被吞」，.md 拦下、.docx 放行。
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
    scratch = _BRACKET_REF.sub(" ", _TASK_BOX.sub(" ", scratch))
    return set(_NUMBER.findall(scratch))


def extract_citation_tokens(text: str, ext: str) -> tuple[str, ...]:
    """按格式抽取引用 token（**公开**、确定性、保序去重）。

    `.tex` 取 `\\cite{}` 键（逗号分隔）；`.md`/`.docx` 取 `[id]`。A2d 起由
    CitationQuotaGate 与既有 Diff Gate 共用同一解析口径（不再各自私有实现）。

    ext 经 `dialect.is_tex` 归一化：原先裸比 `ext == "tex"`，传 `".tex"` / `"TEX"` 会静默
    落到方括号分支 ⇒ `\\cite{}` 一个都抽不到、引用全被判未核验。
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        t = tok.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    if is_tex(ext):
        for m in _CITE.finditer(text):
            for t in m.group(1).split(","):
                _add(t)
    else:
        # 与 tex 侧同样按逗号拆分：`[12, 21]` 是**两条**引用，不是一个 token "12,21"。
        # 不拆会让配额只算一条、且该 token 在账本里恒解析不到。
        for m in _BRACKET_REF.finditer(_TASK_BOX.sub(" ", text)):
            for t in m.group(1).split(","):
                _add(t)
    return tuple(out)


def _cite_tokens(text: str, ext: str) -> set[str]:
    """既有内部入口（薄包公开函数，行为逐字不变）。"""
    return set(extract_citation_tokens(text, ext))


class RewriteGate:
    """rewrite 意图的 Diff Gate（卡 2）。

    只校验候选正文本身（其他章节由 splice 保证逐字不变）：
    1. 不得引入 level ≤ 目标章节的新标题（防越界改到别的 section）
    2. 所有引用都能解析到 facts 的 known_refs（不新增未核验引用）
    3. 无占位符
    4. 新出现的数字必须已知或经用户确认（防编造）
    5. 段落数在 [原-2, 原+5] 之间（防过度膨胀）

    第 5 条的默认窗口假设「等量改写」。**扩写类请求**（把概述扩写成正文、且要求
    引用 N 篇）在结构上就需要更多段落：每个子标题下要铺开论述，段落数常翻倍。
    这时上限由调用方按需求放宽（`expand_budget`），否则 Gate 与用户的明确指令直接
    矛盾 —— 模型无论怎么写都过不了，重试 3 次全废。
    """

    def __init__(self, ext: str, facts, target_level: int,
                 *, expand_budget: int = 0) -> None:
        self.ext = normalize_ext(ext)
        self.facts = facts
        self.target_level = target_level
        self.expand_budget = max(0, int(expand_budget))

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
        known = citable_tokens(self.facts, self.ext)
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

        # 5. 段落数区间（扩写请求按 expand_budget 放宽上限，下限不动：仍防「越改越少」）
        po, pc = _count_paragraphs(original), _count_paragraphs(candidate)
        lo, hi = po - 2, po + 5 + self.expand_budget
        if not (lo <= pc <= hi):
            v.append(f"段落数从 {po} 变为 {pc}，超出允许区间 [{lo}, {hi}]。")

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
        self.ext = normalize_ext(ext)
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

        known = citable_tokens(self.facts, self.ext)
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
