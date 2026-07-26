"""Interview（B2，Week 1 polish）。

polish 的访谈很轻：
- a1（仅当章节未定位）：让用户从章节清单里选一章。
- 目标（可选）：润色程度/偏好，回车走保守默认。

通过注入 ``ask(prompt) -> str`` 回调与 I/O 解耦，便于测试与复用（REPL / 批处理）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from paper_agent.session.section_index import SectionIndex

Ask = Callable[[str], str]


@dataclass
class PolishPlan:
    section_id: str
    goal: str = ""
    # 用户显式硬约束（如「不改数字」「保留引用」）。由 intent-planning 的
    # polish.constraints 槽填入，经 execute_polish → ContextBuilder 真正影响生成。
    # 旧访谈路径不填 → 空 list → prompt 与改造前逐字节一致。
    constraints: list[str] = field(default_factory=list)


@dataclass
class ConvertPlan:
    target_ext: str
    presets: list[str] = field(default_factory=list)


@dataclass
class RewritePlan:
    section_id: str
    direction: str
    details: str = ""
    want_search: bool = False
    search_query: str = ""
    search_limit: int = 5


_DIRECTION_MAP = {
    "a": "补充论证 / 加论据",
    "b": "调整段落顺序 / 结构",
    "c": "更换叙述角度",
}

# 通常需要引用文献的章节类型
_CITATION_HEAVY = ("introduction", "related", "background", "discussion", "相关", "引言", "讨论")


@dataclass
class AddSectionPlan:
    title: str
    purpose: str
    position: str = "end"                 # start / end / after:<sid> / before:<sid>
    section_type: str = ""
    structure_kind: str = "desc"          # desc / list / mimic
    structure_desc: str = ""
    subsections: list[str] = field(default_factory=list)
    want_search: bool = False
    search_query: str = ""
    search_limit: int = 5


class InterviewError(Exception):
    """访谈无法定位到有效目标（章节 / 格式）。"""


_FORMAT_ALIASES = {
    "docx": "docx", "word": "docx",
    "latex": "tex", "tex": "tex", ".tex": "tex",
    "markdown": "md", "md": "md", ".md": "md",
}


def interview_convert(
    target_ext: str | None,
    presets: list[str],
    ask: Ask,
) -> ConvertPlan:
    """convert 访谈：仅当目标格式缺失时追问一次。"""
    ext = target_ext
    if ext is None:
        answer = ask("转成什么格式？（docx / latex / markdown）\n> ").strip().lower()
        ext = _FORMAT_ALIASES.get(answer.lstrip("."))
        if ext is None:
            raise InterviewError(f"无法识别的目标格式：{answer!r}")
    return ConvertPlan(target_ext=ext, presets=list(presets))


def interview_polish(
    section_id: str | None,
    index: SectionIndex,
    ask: Ask,
) -> PolishPlan:
    """完成 polish 所需的最小访谈，返回可执行计划。"""
    sid = section_id
    if sid is None or index.find(sid) is None:
        sid = _ask_section(index, ask)

    section = index.find(sid)
    if section is None:
        raise InterviewError(f"未能定位章节：{sid}")

    goal = ask(
        "希望润色到什么程度？（直接回车 = 保守默认：只删冗余、不改篇幅）\n> "
    ).strip()
    return PolishPlan(section_id=section.id, goal=goal)


def interview_rewrite(
    section_id: str | None,
    index: SectionIndex,
    ask: Ask,
) -> RewritePlan:
    """rewrite 访谈：必问方向；选「补充论证+引用现有工作」时收集检索关键词。"""
    sid = section_id
    if sid is None or index.find(sid) is None:
        sid = _ask_section(index, ask)
    section = index.find(sid)
    if section is None:
        raise InterviewError(f"未能定位章节：{sid}")

    ans = ask(
        "希望改动的方向是什么？\n"
        "  (a) 补充论证 / 加论据\n"
        "  (b) 调整段落顺序 / 结构\n"
        "  (c) 更换叙述角度\n"
        "  (d) 其他（请说明）\n> "
    ).strip()
    letter = ans[:1].lower()

    direction = _DIRECTION_MAP.get(letter, ans)
    details = ""
    want_search = False
    query = ""

    if letter == "a":
        sub = ask(
            "想补什么论据？\n"
            "  (i) 数学分析  (ii) 更多实验对比  (iii) 引用现有工作  (iv) 具体例子\n> "
        ).strip().lower()
        details = sub
        if "iii" in sub or "引用" in sub or "文献" in sub or sub == "3":
            want_search = True
            query = ask("想搜什么主题的文献？（给关键词，可用英文）\n> ").strip()
            if not query:
                want_search = False
    elif letter in ("c", "d"):
        details = ask("请具体说明（举例更好）：\n> ").strip()
        if not details:
            raise InterviewError("rewrite 需要更明确的方向，否则容易改成你不想要的。")

    return RewritePlan(
        section_id=section.id,
        direction=direction,
        details=details,
        want_search=want_search,
        search_query=query,
    )


def _parse_position(answer: str, index: SectionIndex) -> str:
    a = answer.strip()
    low = a.lower()
    if not a or "末尾" in a or low in ("end", "最后"):
        return "end"
    if "开头" in a or low in ("start", "开始"):
        return "start"
    for marker, spec in (("之后", "after"), ("之前", "before"),
                         ("after", "after"), ("before", "before")):
        if marker in low:
            name = re.split(marker, a, flags=re.IGNORECASE)[0].strip(" :：")
            if marker in ("after", "before"):
                name = re.split(marker, a, flags=re.IGNORECASE)[-1].strip(" :：")
            sec = index.find(name)
            if sec is not None:
                return f"{spec}:{sec.id}"
    return "end"


def interview_add_section(
    title_hint: str | None,
    position_hint: str | None,
    index: SectionIndex,
    facts,
    ask: Ask,
) -> AddSectionPlan:
    """add_section 访谈：3 问（目的 / 结构 / 位置）+ 追问 + 结构跨 chat 沿用。"""
    title = (title_hint or "").strip() or ask("要新增的章节标题/名称是？\n> ").strip()
    if not title:
        raise InterviewError("未提供章节名称。")
    if index.find(title) is not None:
        raise InterviewError(f"章节「{title}」已存在，若要修改请用 rewrite。")

    from paper_agent.session.section_index import slugify
    section_type = slugify(title)

    # a1 目的
    purpose = ask(f"「{title}」这一章的类型/目的是？（一句话）\n> ").strip()
    if not purpose:
        purpose = ask("能一句话说清这一章想传达什么吗？\n> ").strip()
        if not purpose:
            raise InterviewError("add_section 需要明确的章节目的，否则容易写成空泛骨架。")

    # a2 结构（先看是否可沿用历史）
    kind, desc, subs = _interview_structure(section_type, index, facts, ask)

    # a3 位置
    if position_hint:
        position = position_hint
    else:
        position = _parse_position(
            ask("插入位置？（开头 / 末尾 / “某章之后” / “某章之前”）\n> "), index
        )

    # 搜文献（引用密集型章节）
    want_search, query = False, ""
    if any(k in section_type or k in title for k in _CITATION_HEAVY):
        ans = ask(
            "这一章通常需要引用文献。要现在搜吗？（给关键词搜 / 回车跳过）\n> "
        ).strip()
        if ans:
            want_search, query = True, ans

    return AddSectionPlan(
        title=title, purpose=purpose, position=position, section_type=section_type,
        structure_kind=kind, structure_desc=desc, subsections=subs,
        want_search=want_search, search_query=query,
    )


def _interview_structure(section_type: str, index: SectionIndex, facts, ask: Ask):
    """返回 (kind, desc, subsections)。优先复用历史结构偏好。"""
    stored = facts.get_section_structure(section_type) if facts else None
    if stored:
        yn = ask(
            f"上次「{section_type}」用的结构是：{stored}\n沿用吗？(y/n)\n> "
        ).strip().lower()
        if yn in ("y", "yes", "是", ""):
            return "desc", stored, []

    ans = ask(
        "这一章内部怎么组织？\n"
        "  (a) 描述式：一句话说骨架\n"
        "  (b) 列表式：给几个小节标题（逗号分隔）\n"
        "  (c) 模仿式：给一篇论文路径 + 章节名\n> "
    ).strip()
    letter = ans[:1].lower()

    if letter == "b":
        raw = ask("请给出小节标题（逗号或换行分隔）：\n> ")
        subs = [s.strip() for s in re.split(r"[,，\n]", raw) if s.strip()]
        return "list", "", subs
    if letter == "c":
        path = ask("参考论文文件路径：\n> ").strip()
        name = ask("参考其中哪个章节的结构？\n> ").strip()
        from paper_agent.session.extractor import extract_structure_from
        subs = extract_structure_from(path, name)
        if subs:
            return "mimic", "", subs
        # 抽不到骨架 → 退回描述式
    desc = ask("用一句话描述这一章的骨架（如：背景 → gap → 我们做了 → 贡献）：\n> ").strip()
    if not desc:
        raise InterviewError("需要更明确的结构，否则容易写成 generic 骨架。")
    return "desc", desc, []


def _ask_section(index: SectionIndex, ask: Ask) -> str:
    sections = index.list()
    if not sections:
        raise InterviewError("当前文件没有可识别的章节。")
    lines = ["润色哪一章？请输入序号或章节名："]
    for i, s in enumerate(sections, start=1):
        indent = "  " * (s.level - 1)
        lines.append(f"  {i}. {indent}{s.title}  [{s.id}]")
    answer = ask("\n".join(lines) + "\n> ").strip()

    # 支持序号选择
    if answer.isdigit():
        idx = int(answer) - 1
        if 0 <= idx < len(sections):
            return sections[idx].id
    # 支持 id / 标题
    found = index.find(answer)
    if found is not None:
        return found.id
    raise InterviewError(f"无法识别的章节选择：{answer!r}")


__all__ = [
    "Ask", "PolishPlan", "ConvertPlan", "RewritePlan", "AddSectionPlan",
    "InterviewError",
    "interview_polish", "interview_convert", "interview_rewrite",
    "interview_add_section",
]
