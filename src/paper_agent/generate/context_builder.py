"""ContextBuilder（B5）。纯函数：按意图把材料拼成 messages。

Week 1 只有 polish。保真硬约束写进 system message；违规回灌拼进 user message。
"""

from __future__ import annotations

from paper_agent.providers.llm.base import Message

_POLISH_SYSTEM = """你是学术论文的语言润色助手。你的唯一任务是改善给定章节的**语言表达**：
让句子更通顺、简洁、符合学术书面语。

严格约束（必须全部遵守，否则输出会被程序拒绝）：
1. 不得改动任何数字。
2. 不得新增、删除或改动任何引用（\\cite{...} 或 [id]）。
3. 不得改动任何公式（$...$、$$...$$、\\begin{equation} 等），逐字保留。
4. 不得增删章节、不得改变标题层级。
5. 不得改变论述的事实含义、因果强度或比较对象。
6. 不得留下任何占位符（如 TODO / 待补充 / xxx）。
7. 篇幅应与原文相近，只调整语言。

只输出润色后的章节正文本身，不要加解释、不要加代码块标记。"""


def build_polish_messages(
    original: str,
    goal: str,
    violations: list[str] | None = None,
    constraints: list[str] | None = None,
) -> list[Message]:
    """构造 polish 的 messages。

    - ``original``：目标章节原文。
    - ``goal``：用户对润色程度/方向的说明（可为空）。
    - ``violations``：上一轮 gate 的违规条目；非空表示这是重试。
    - ``constraints``：用户显式提出的额外硬约束（如「不改数字」「保留引用」）。
      intent-planning 的 `polish.constraints` 槽由此**真正影响生成**（Task 19 绑定
      `ContextBuilder.constraints`），而非只存在计划里。为空/None 时消息逐字节不变。
    """
    user_parts: list[str] = []
    if goal:
        user_parts.append(f"润色目标/偏好：{goal}")
    if constraints:
        user_parts.append(
            "用户额外要求的硬约束（必须全部遵守）：\n"
            + "\n".join(f"- {c}" for c in constraints)
        )
    user_parts.append("以下是需要润色的章节正文：\n\n" + original)

    if violations:
        user_parts.append(
            "\n你上一次的输出违反了以下硬约束，请重新润色并修正这些问题："
            + "\n".join(f"- {x}" for x in violations)
        )

    return [
        Message(role="system", content=_POLISH_SYSTEM),
        Message(role="user", content="\n\n".join(user_parts)),
    ]


_REWRITE_SYSTEM = """你是学术论文的改写助手。你可以调整给定章节的内容组织、补充论证、
更换叙述角度，但必须严格遵守以下约束（否则输出会被程序拒绝）：

1. 只改写给定的这一章，不要输出其他章节，也不要新增与本章同级或更高级的标题。
2. 只能引用「可用文献列表」中给出的文献；不得编造或引用列表外的文献。
   - .tex 用 \\cite{bibkey}，键必须来自列表；.md 用 [id]，id 必须来自列表。
3. 不得编造数字。只能使用原文已有的数字，或「已知数字」列表中的数字。
4. 不得改变已有事实的含义、因果强度或比较对象。
5. 不得留下任何占位符（TODO / 待补充 / xxx 等）。
6. 篇幅可适度增减，但不要过度膨胀。

只输出改写后的章节正文本身，不要加解释、不要加代码块标记。"""


def _fmt_refs(refs, ext: str) -> str:
    if not refs:
        return "（无可用文献）"
    lines = []
    for r in refs:
        token = r.bibkey if ext == "tex" else r.id
        lines.append(f"- {token}: {r.title}")
    return "\n".join(lines)


def build_rewrite_messages(
    original: str,
    direction: str,
    details: str,
    refs,
    known_numbers: list[str],
    ext: str,
    violations: list[str] | None = None,
) -> list[Message]:
    """构造 rewrite 的 messages。``refs`` 为可用 KnownRef 列表。"""
    parts = [f"改写方向：{direction}"]
    if details:
        parts.append(f"补充说明：{details}")
    parts.append("可用文献列表（只能引用这些）：\n" + _fmt_refs(refs, ext))
    if known_numbers:
        parts.append("已知数字（可安全使用）：" + ", ".join(known_numbers))
    parts.append("以下是需要改写的章节正文：\n\n" + original)
    if violations:
        parts.append(
            "\n你上一次的输出违反了以下硬约束，请修正后重写：\n"
            + "\n".join(f"- {x}" for x in violations)
        )
    return [
        Message(role="system", content=_REWRITE_SYSTEM),
        Message(role="user", content="\n\n".join(parts)),
    ]


_ADD_SECTION_SYSTEM = """你是学术论文写作助手，负责为论文新增一个章节的正文。
严格遵守以下约束（否则输出会被程序拒绝）：

1. 只写这一章的正文，按给定的内部结构组织；不要重复输出章节大标题。
2. 若给了小节骨架，请为每个小节生成对应内容（.tex 用 \\subsection{...}，.md 用 ## 小节标题）。
3. 只能引用「可用文献列表」中的文献；不得编造或引用列表外的文献。
4. 不得编造数字；只能使用「已知数字」列表中的数字。
5. 不得留下任何占位符（TODO / 待补充 / xxx 等）。
6. 内容要具体、切题，不要写空泛的模板骨架。

只输出该章节正文本身（含小节标题），不要加解释、不要加代码块标记。"""


def _fmt_claims(claims: dict) -> str:
    parts = []
    for key in ("novelty", "gap"):
        if claims.get(key):
            parts.append(f"{key}: {claims[key]}")
    if claims.get("contributions"):
        parts.append("contributions: " + "; ".join(claims["contributions"]))
    return "\n".join(parts)


def build_add_section_messages(
    title: str,
    purpose: str,
    structure_desc: str,
    subsections: list[str],
    refs,
    known_numbers: list[str],
    ext: str,
    claims: dict | None = None,
    violations: list[str] | None = None,
) -> list[Message]:
    parts = [f"新章节标题：{title}", f"这一章的目的：{purpose}"]
    if subsections:
        parts.append("内部小节骨架（请逐个生成）：\n" + "\n".join(f"- {s}" for s in subsections))
    elif structure_desc:
        parts.append(f"内部结构说明：{structure_desc}")
    if claims and _fmt_claims(claims):
        parts.append("论文已确认的关键信息（用于 grounding）：\n" + _fmt_claims(claims))
    parts.append("可用文献列表（只能引用这些）：\n" + _fmt_refs(refs, ext))
    if known_numbers:
        parts.append("已知数字（可安全使用）：" + ", ".join(known_numbers))
    if violations:
        parts.append(
            "\n你上一次的输出违反了以下硬约束，请修正后重写：\n"
            + "\n".join(f"- {x}" for x in violations)
        )
    return [
        Message(role="system", content=_ADD_SECTION_SYSTEM),
        Message(role="user", content="\n\n".join(parts)),
    ]


__all__ = [
    "build_polish_messages", "build_rewrite_messages", "build_add_section_messages",
]
