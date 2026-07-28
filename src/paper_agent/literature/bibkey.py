"""bibkey 生成（A1 / 任务 5）。

从 ``tools/literature.py`` 迁入，算法与冲突递增策略**逐字保留**，使既有
``search_and_verify`` 产出的 bibkey 与迁移前完全一致。

``entry`` 只需具备 ``authors`` / ``year`` / ``title`` 属性，因此对
``RetrievalCandidate``（= ``ReferenceEntry``）与 ``ReferenceRecord`` 都适用。
"""

from __future__ import annotations

import re


def make_bibkey(entry, taken: set[str]) -> str:
    """按「首作者姓氏 + 年份 + 标题首个长词」生成 bibkey，冲突则递增后缀。"""
    author = ""
    if getattr(entry, "authors", None):
        author = re.sub(r"[^a-zA-Z]", "", entry.authors[0].split()[-1]).lower()
    year = str(entry.year) if getattr(entry, "year", None) else ""
    word = ""
    for tok in re.findall(r"[a-zA-Z]+", getattr(entry, "title", "") or ""):
        if len(tok) > 3:
            word = tok.lower()
            break
    base = (author + year + word) or "ref"
    key = base
    n = 2
    while key in taken:
        key = f"{base}{n}"
        n += 1
    return key


__all__ = ["make_bibkey"]
