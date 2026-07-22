"""fix_bibliography（共享设施 C，Week 4）。

常见问题：正文里有 \\cite{key} 或 [id] 引用，但 bibliography 缺对应条目。
这不是 rewrite/add_section 的范畴，是结构性修复。

Week 4 范围（诚实边界）：
- 检测「正文引用了但 bibliography 缺失」的条目。
- 对能在 facts.known_refs 里找到题名的缺失条目，自动补 \\bibitem。
- 找不到元数据的，如实报告，需用户补充或走搜文献。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paper_agent.session.facts import SessionFacts
from paper_agent.session.file_session import FileBackedSession

_CITE = re.compile(r"\\cite[a-zA-Z]*\s*(?:\[[^\]]*\])?\{([^}]*)\}")
_BIBITEM = re.compile(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}")
_BIB_ENV_END = r"\end{thebibliography}"


@dataclass
class FixBibReport:
    fixed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    message: str = ""


def fix_bibliography(session: FileBackedSession, facts: SessionFacts) -> FixBibReport:
    if session.ext != "tex":
        return FixBibReport(
            message="Week 4 的 fix_bibliography 仅支持 .tex；其他格式后续补。"
        )

    text = session.read_working()
    cited: set[str] = set()
    for m in _CITE.finditer(text):
        cited.update(k.strip() for k in m.group(1).split(",") if k.strip())
    existing = set(_BIBITEM.findall(text))
    missing = cited - existing
    if not missing:
        return FixBibReport(message="正文引用与 bibliography 一致，无需修复。")

    titles = {r.bibkey: r.title for r in facts.known_refs if r.bibkey}
    fixed, unresolved, new_items = [], [], []
    for key in sorted(missing):
        title = titles.get(key)
        if title:
            new_items.append(f"\\bibitem{{{key}}} {title}.")
            fixed.append(key)
        else:
            unresolved.append(key)

    if new_items:
        text = _insert_bibitems(text, new_items)
        session.write_working(text, message="fix_bibliography")

    parts = []
    if fixed:
        parts.append(f"已补 {len(fixed)} 条：{', '.join(fixed)}")
    if unresolved:
        parts.append(
            f"无法自动修复 {len(unresolved)} 条（facts 里没有题名）："
            f"{', '.join(unresolved)}。请提供题名/DOI，或用搜文献补齐。"
        )
    return FixBibReport(fixed=fixed, unresolved=unresolved, message="；".join(parts))


def _insert_bibitems(text: str, items: list[str]) -> str:
    block = "\n" + "\n".join(items) + "\n"
    i = text.find(_BIB_ENV_END)
    if i >= 0:
        return text[:i] + block + text[i:]
    # 没有 thebibliography 环境 → 在 \end{document} 前创建一个
    env = "\n\\begin{thebibliography}{9}" + block + "\\end{thebibliography}\n"
    j = text.find(r"\end{document}")
    if j >= 0:
        return text[:j] + env + text[j:]
    return text + env


__all__ = ["FixBibReport", "fix_bibliography"]
