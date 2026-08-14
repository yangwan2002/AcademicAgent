"""载入时悬空引用诊断（B-2 的只读那一半）。

`fix_bibliography` 意图删除后，用户原稿里的悬空引用没人负责报告了。本文件钉住：

1. tex / md / docx 三种格式都能诊断（引用标记抽取本来就三种都支持）
2. **识别不出文献区就不报** —— 误报的代价比漏报高（用户会学会忽略提示）
3. 只读：诊断不改文本
4. 系统自己写的引用不该触发它（`apply_bibliography` 已对齐）
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.literature.dangling_refs import diagnose_dangling


TEX_OK = (
    "\\section{Intro}\nWe cite \\cite{key1} and \\cite{key2}.\n"
    "\\begin{thebibliography}{9}\n"
    "\\bibitem{key1} Ref one.\n\\bibitem{key2} Ref two.\n"
    "\\end{thebibliography}\n"
)
TEX_DANGLING = (
    "\\section{Intro}\nWe cite \\cite{key1} and \\cite{missing}.\n"
    "\\begin{thebibliography}{9}\n\\bibitem{key1} Ref one.\n"
    "\\end{thebibliography}\n"
)

MD_OK = (
    "# 1 引言\n\n观点见 [1] 与 [2]。\n\n## 参考文献\n\n[1] Ref one.\n[2] Ref two.\n"
)
MD_DANGLING = "# 1 引言\n\n观点见 [1] 与 [7]。\n\n## 参考文献\n\n[1] Ref one.\n"


def test_tex_all_resolved_no_findings():
    rep = diagnose_dangling(TEX_OK, "tex")
    assert rep.region_found is True
    assert rep.dangling == []
    assert rep.has_findings is False
    assert rep.cited_count == 2 and rep.entry_count == 2


def test_tex_dangling_detected():
    rep = diagnose_dangling(TEX_DANGLING, "tex")
    assert rep.dangling == ["missing"]
    assert rep.has_findings is True


def test_md_dangling_detected():
    rep = diagnose_dangling(MD_DANGLING, "md")
    assert rep.dangling == ["7"]
    assert rep.low_confidence is False


def test_md_all_resolved():
    assert diagnose_dangling(MD_OK, "md").has_findings is False


def test_docx_projection_works_but_flagged_low_confidence():
    """docx 走投影文本（一段一行、标题带 `#`），与 md 同路，但标记为低置信。"""
    rep = diagnose_dangling(MD_DANGLING, "docx")
    assert rep.dangling == ["7"]
    assert rep.low_confidence is True


# ---- 保守规则：识别不出文献区就不报 ----

def test_no_bibliography_region_stays_silent():
    """正文有引用但整篇没有文献区 —— 不报。

    这不是"没问题"，而是**判不准**：可能文献表在另一个文件（`\\bibliography{refs}`
    + BibTeX），也可能用户还没写。此时把每个引用都判悬空会让提示变成噪音。
    """
    for ext, text in (
        ("tex", "\\section{Intro}\nWe cite \\cite{key1}.\n"),
        ("md", "# 1 引言\n\n观点见 [1]。\n"),
    ):
        rep = diagnose_dangling(text, ext)
        assert rep.region_found is False, ext
        assert rep.has_findings is False, ext
        assert rep.dangling == [], ext


def test_md_heading_without_parsable_entries_stays_silent():
    """有「## 参考文献」标题但条目认不出（docx 编号列表形态）—— 不报。"""
    text = "# 1 引言\n\n观点见 [1] 与 [7]。\n\n## 参考文献\n\nAuthor A. Title one. 2020.\n"
    rep = diagnose_dangling(text, "docx")
    assert rep.region_found is False
    assert rep.has_findings is False


def test_bibtex_external_file_not_misreported():
    """`\\bibliography{refs}` 外部 BibTeX：条目在 .bib 里，认不出 `\\bibitem` ⇒ 沉默。

    这是最该沉默的一种 —— 文档本身完全正常，条目只是不在这个文件里。
    """
    text = "\\section{Intro}\nWe cite \\cite{key1}.\n\\bibliography{refs}\n"
    assert diagnose_dangling(text, "tex").region_found is False


def test_heading_variants_recognized():
    """区域切分复用 `doc_stats.split_bibliography`，标题变体一并生效。

    手写一版正则只会认「参考文献 / references」；复用后「参考资料」「文献列表」
    「带编号前缀」都认得。这条钉住复用带来的覆盖面，防止有人回头改成手写正则。
    """
    for heading in ("## 参考文献", "## 参考资料", "## 文献列表", "## 引用文献",
                    "## References", "## 5 参考文献", "### BIBLIOGRAPHY"):
        text = f"# 1 引言\n\n见 [1] 与 [7]。\n\n{heading}\n\n[1] Ref one.\n"
        rep = diagnose_dangling(text, "md")
        assert rep.dangling == ["7"], f"{heading} -> {rep}"


def test_tex_printbibliography_region():
    """biblatex 的 `\\printbibliography`：切得出区，但没有 `\\bibitem` ⇒ 沉默。"""
    text = "\\section{A}\n\\cite{key1}\n\\printbibliography\n"
    assert diagnose_dangling(text, "tex").region_found is False


# ---- 口径细节 ----

def test_bibliography_entries_not_counted_as_body_citations():
    """文献区自己的 `[1] ...` 不能被当成正文引用。

    否则每条条目都"自己引用自己"，`uncited` 恒为空、悬空判定失真。
    """
    rep = diagnose_dangling(MD_OK, "md")
    assert rep.cited_count == 2      # 正文的 [1][2]，不是文献区的
    assert rep.uncited == []


def test_uncited_entries_reported_separately():
    """文献表有、正文没引用 —— 记录但不算 findings（不影响编译，不该打扰用户）。"""
    text = "# 1 引言\n\n观点见 [1]。\n\n## 参考文献\n\n[1] Ref one.\n[2] Ref two.\n"
    rep = diagnose_dangling(text, "md")
    assert rep.uncited == ["2"]
    assert rep.dangling == []
    assert rep.has_findings is False


def test_multiple_tokens_in_one_bracket_split():
    """`[1, 7]` 是两条引用 —— 与 extract_citation_tokens 同口径。"""
    text = "# 1 引言\n\n见 [1, 7]。\n\n## 参考文献\n\n[1] Ref one.\n"
    assert diagnose_dangling(text, "md").dangling == ["7"]


def test_dangling_deduped_and_order_stable():
    text = (
        "\\section{A}\n\\cite{missing} \\cite{missing} \\cite{other}\n"
        "\\begin{thebibliography}{9}\n\\bibitem{key1} One.\n"
        "\\end{thebibliography}\n"
    )
    assert diagnose_dangling(text, "tex").dangling == ["missing", "other"]


def test_diagnosis_is_read_only():
    """诊断不得改动入参文本（它拿到的是 working copy 的内容）。"""
    before = TEX_DANGLING
    diagnose_dangling(before, "tex")
    assert before == TEX_DANGLING


def test_ext_normalized():
    """`.tex` / `TEX` 都要落 tex 分支 —— 裸比 `ext == "tex"` 曾导致引用一个都抽不到。"""
    for ext in (".tex", "TEX", "latex"):
        assert diagnose_dangling(TEX_DANGLING, ext).dangling == ["missing"], ext


# ---- 与写入路径的关系 ----

def test_system_written_output_passes_diagnosis():
    """`apply_bibliography` 的产物必须诊断干净 —— 否则每次写完都自己报一遍警告。

    这两条路径互补：`apply_bibliography` 管系统写的引用（commit 时自动对齐），
    诊断管用户原稿的悬空引用。诊断认原生形态（`thebibliography` / `## 参考文献`），
    而同步产物**恰好带这两种形态**（外面再套 `BIBLIOGRAPHY:BEGIN/END` 标记），
    所以能被认出来。这个测试钉住那个"恰好"，防止哪天改了区块渲染形态而失配。
    """
    from paper_agent.literature.bibliography_sync import apply_bibliography
    from paper_agent.literature.ledger import ReferenceLedger
    from paper_agent.literature.migration import record_from_dict

    def _rec(n: int):
        return record_from_dict({
            "reference_id": f"r{n:012x}", "bibkey": f"key{n}", "title": f"Title {n}",
            "authors": [f"Author {n}A"], "year": 2020 + (n % 5),
            "numeric_aliases": [str(n)], "legacy_verified": True,
        })

    ledger = ReferenceLedger([_rec(1), _rec(2), _rec(7)])
    for ext, body in (
        ("tex", "\\section{A}\nWe cite \\cite{key1} and \\cite{key7}.\n"),
        ("md", "# 1 引言\n\n见 [1] 与 [7]。\n"),
    ):
        out, _ = apply_bibliography(body, ext, ledger)
        rep = diagnose_dangling(out, ext)
        assert rep.region_found is True, ext
        assert rep.dangling == [], f"{ext}: {rep.dangling}"
        assert rep.has_findings is False, ext
