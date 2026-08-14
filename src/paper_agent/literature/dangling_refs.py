r"""载入时的悬空引用诊断（只读，不改任何文件）。

## 为什么需要它

`apply_bibliography` 只在**写入路径**上跑（rewrite / add_section commit 时），
所以系统自己写的引用一定和文献区对齐。但用户拿一篇自己写的稿子进来、里面本来
就有 `\\cite{某个不存在的key}`、而且**没让系统改任何东西** —— 这种情况下没有
任何人会告诉他。LaTeX 编译出来那处引用就是 `[?]`。

这是 `fix_bibliography` 意图（已删，见 backlog B-2）唯一还有价值的部分：它补不了
条目（facts 里没有那篇文献的元数据），但**能报告**。所以这里只做诊断，不做修复 ——
要补齐得走搜文献。

## 为什么不复用 `bibliography_sync._citation_tokens_in_body`

那个函数按系统自己的界定标记（`BIBLIOGRAPHY:BEGIN/END`）切文献区。用户原稿里
没有这些标记，于是整个文献区会被当成正文 —— 文献表自己的 `[1] Author...` 被抽成
引用，再拿去和条目比，结果恒为「无悬空」。诊断必须认**用户的原生形态**：
tex 的 `thebibliography` 环境、md/docx 的 `## 参考文献` 标题。

区域切分因此复用 `tools/doc_stats.py:split_bibliography` —— 它已经在做同一件事
（为「哪章最长」排除文献表），且标题覆盖比手写一版更全（参考资料/参考书目/文献列表/
带编号前缀/`\printbibliography`）。这里只补**条目**那一侧。

## 保守规则：识别不出文献区就不报

误报的代价比漏报高。每次载入一篇正常文档都看到一堆假警告，用户会学会忽略这个
提示，那真有问题时也看不见了。docx 尤其如此 —— 它的文献表可能是 Word 编号列表
或域，投影文本不一定长成 `[1] Title`。所以文献区整个识别不出来时**保持沉默**，
而不是把所有引用都判成悬空。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paper_agent.gates.diff_gate import extract_citation_tokens
from paper_agent.literature.dialect import is_tex, normalize_ext
from paper_agent.tools.doc_stats import split_bibliography

#: tex 条目键：`\bibitem{key}` / `\bibitem[label]{key}`
_TEX_BIBITEM = re.compile(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}")
#: md/docx 条目行：`[1] Title` / `- [1] Title`。docx 走投影文本（一段一行）故同源。
_MD_ENTRY = re.compile(r"(?m)^\s*(?:[-*]\s*)?\[(\w+)\]")


@dataclass
class DanglingReport:
    """`dangling` 非空才值得打印；`region_found=False` 时其余字段一律为空。"""

    region_found: bool = False
    dangling: list[str] = field(default_factory=list)
    uncited: list[str] = field(default_factory=list)
    cited_count: int = 0
    entry_count: int = 0
    #: docx 的文献表形态多变，投影可能漏认条目 —— 打印时据此加一句限定语。
    low_confidence: bool = False

    @property
    def has_findings(self) -> bool:
        return bool(self.dangling)


def diagnose_dangling(text: str, ext: str) -> DanglingReport:
    """比对正文引用与文献区条目，返回只读报告。

    对 tex / md / docx 都跑：引用标记抽取三种格式本来就都支持
    （`extract_citation_tokens`，tex 走 `\\cite{}`，md/docx 走 `[id]`）。
    差别在文献区条目那一侧 —— docx 弱，故有 `low_confidence`。
    """
    ext = normalize_ext(ext)
    tex = is_tex(ext)
    # `split_bibliography` 裸比 `ext == "tex"`，而 `is_tex` 还认 `latex` —— 直接透传
    # 会让 `.latex` 走 md 标题分支切区、却用 `\bibitem` 认条目，两侧口径不一致。
    # 这里统一喂规范值；不改 doc_stats，它有自己的调用方。
    body, region = split_bibliography(text, "tex" if tex else ext)
    #: 两种「判不准」都返回 region_found=False：
    #: 1. `region` 为空 —— 压根没有文献区（可能用外部 BibTeX，也可能还没写）
    #: 2. 有区但一条条目都认不出 —— 多半是 Word 编号列表/域形态
    #: 此时把所有引用判成悬空会让提示变成噪音，宁可沉默。
    entry_re = _TEX_BIBITEM if tex else _MD_ENTRY
    if not region or not entry_re.search(region):
        return DanglingReport(region_found=False)

    cited = list(extract_citation_tokens(body, ext))
    entries = set(entry_re.findall(region))

    # 保序去重后再比，输出稳定（诊断要可复现）。
    seen: set[str] = set()
    dangling = []
    for t in cited:
        if t not in entries and t not in seen:
            seen.add(t)
            dangling.append(t)

    cited_set = set(cited)
    uncited = sorted(e for e in entries if e not in cited_set)
    return DanglingReport(
        region_found=True,
        dangling=dangling,
        uncited=uncited,
        cited_count=len(cited_set),
        entry_count=len(entries),
        low_confidence=(ext == "docx"),
    )


__all__ = ["DanglingReport", "diagnose_dangling"]
