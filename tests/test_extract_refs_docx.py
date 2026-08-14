"""docx 初稿的参考文献抽取，以及「未核验」的如实告知。

背景：`chat.py` 的 `_TEXT_EXT`（= `SECTIONABLE_EXTS`）一直包含 docx，所以载入 docx
时 `extract_refs` 确实被调用；但 `extractor.py` 里只有 tex 和 md 两个分支，docx
两个都不匹配 ⇒ 静默返回 0 条。用户看到「抽取到 0 条文献引用」，会以为自己文献表
写得不对，实际是格式没被支持。
"""

from __future__ import annotations

import io

import pytest

from paper_agent.session.extractor import extract_refs
from paper_agent.session.facts import KnownRef


def _docx_bytes(paragraphs: list[tuple[str, int]]) -> bytes:
    """按 (文本, heading级别) 造一份 docx；级别 0 表示正文段落。"""
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    for text, level in paragraphs:
        if level:
            doc.add_heading(text, level=level)
        else:
            doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _session(tmp_path, paragraphs):
    from paper_agent.session.file_session import FileBackedSession

    p = tmp_path / "draft.docx"
    p.write_bytes(_docx_bytes(paragraphs))
    return FileBackedSession(str(p), workspace_root=str(tmp_path / "ws"))


DRAFT = [
    ("1 引言", 1),
    ("协同SLAM近年受到关注 [1]。多机器人系统亦有进展 [2]。", 0),
    ("参考文献", 1),
    ("[1] Zhang X. Visual SLAM: current trends. 2021.", 0),
    ("[2] Li Y. Multi-Robot Collaborative SLAM. 2022.", 0),
]


def test_docx_refs_extracted(tmp_path):
    """docx 投影文本里的 `[1] Title` 行应被抽成条目（以前恒为 0 条）。"""
    session = _session(tmp_path, DRAFT)
    refs = extract_refs(session.read_source(), session.ext)
    assert [r.id for r in refs] == ["1", "2"]
    assert refs[0].title.startswith("Zhang X.")


# ---- Word 自动编号列表 ----

def _autonumbered_docx(tmp_path, entries, body="协同SLAM受关注 [1]，另有 [2]。"):
    """文献表用 `List Number` 样式：编号由 numPr 渲染，**不在文本流里**。"""
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_heading("1 引言", level=1)
    doc.add_paragraph(body)
    doc.add_heading("参考文献", level=1)
    for text in entries:
        doc.add_paragraph(text, style="List Number")
    p = tmp_path / "auto.docx"
    doc.save(str(p))
    from paper_agent.session.docx_document import project_text
    return project_text(str(p))


def test_autonumbered_list_gets_positional_ids(tmp_path):
    """自动编号列表以前抽到 0 条：投影里没有 `[n]`，正则要求行首编号。

    回退按段落顺序补 1..n —— Word 自动编号默认也是 1 起顺排，因此与用户看到的一致。
    """
    text = _autonumbered_docx(tmp_path, [
        "Zhang X. Visual SLAM Current Trends. 2021.",
        "Li Y. Multi-Robot Collaborative SLAM Survey. 2022.",
    ])
    refs = extract_refs(text, "docx")
    assert [r.id for r in refs] == ["1", "2"]
    assert refs[0].title.startswith("Zhang X.")
    assert refs[1].title.startswith("Li Y.")


def test_positional_fallback_skips_headings(tmp_path):
    text = _autonumbered_docx(tmp_path, ["Zhang X. Visual SLAM Trends. 2021."])
    refs = extract_refs(text, "docx")
    assert all(not r.title.startswith("#") for r in refs)
    assert "参考文献" not in [r.title for r in refs]


def test_no_bibliography_section_means_no_positional_refs(tmp_path):
    """切不出文献区时不得走位置回退 —— 否则正文段落被当成文献。

    `_bib_region` 切不出区会退回**全文**以保持显式 `[n]` 的旧行为；位置回退若也
    吃这个 region，无文献表的稿子会抽出正文句子（实测 3 条，含「位姿图优化是核心
    环节[6]，误差 0.87 m。」）。回归自 test_docx_inplace 的启动期抽取用例。
    """
    session = _session(tmp_path, [
        ("1 引言", 1),
        ("位姿图优化是核心环节[6]，误差 0.87 m。", 0),
        ("多机器人协同仍有挑战[2]。", 0),
    ])
    assert extract_refs(session.read_source(), session.ext) == []


def test_explicit_numbering_wins_over_positional(tmp_path):
    """有显式 `[n]` 时不走回退：编号来源必须整篇一致，否则正文 [n] 对不上。"""
    session = _session(tmp_path, DRAFT)
    refs = extract_refs(session.read_source(), session.ext)
    assert [r.id for r in refs] == ["1", "2"]
    assert not refs[0].title.startswith("[")


def test_docx_body_citation_not_taken_as_entry(tmp_path):
    """正文段落以 `[3]` 开头时不能被当成文献条目。

    docx 投影是「一段一行」，`_MD_REF_LINE` 的行首约束在这里挡不住正文段落 ——
    所以抽取必须限定在参考文献区内。
    """
    paragraphs = [
        ("1 引言", 1),
        ("见 [1]。", 0),
        ("[3] 这一段是正文，只是恰好以引用标记开头。", 0),
        ("参考文献", 1),
        ("[1] Zhang X. Visual SLAM. 2021.", 0),
    ]
    session = _session(tmp_path, paragraphs)
    refs = extract_refs(session.read_source(), session.ext)
    assert [r.id for r in refs] == ["1"], f"误抽了正文段落：{[r.id for r in refs]}"


def test_docx_refs_are_unverified(tmp_path):
    """抽取产出的条目一律 `verified=False` —— 文本扫描不等于核验。"""
    session = _session(tmp_path, DRAFT)
    refs = extract_refs(session.read_source(), session.ext)
    assert refs and all(r.verified is False for r in refs)


def test_md_without_heading_still_extracts():
    """切不出文献区就退回全文，不能因为限定区域反而少抽（保持旧行为）。"""
    refs = extract_refs("[1] Ref one.\n[2] Ref two.\n", "md")
    assert [r.id for r in refs] == ["1", "2"]


def test_tex_unchanged():
    """tex 分支不受影响。"""
    tex = (
        "\\section{A}\n\\cite{a}\n"
        "\\begin{thebibliography}{9}\n"
        "\\bibitem{a} Ref A title.\n"
        "\\bibitem{b} Ref B title.\n"
        "\\end{thebibliography}\n"
    )
    refs = extract_refs(tex, "tex")
    assert [r.bibkey for r in refs] == ["a", "b"]


# ---- 载入时的初稿文献核验 ----
#
# 原先这里测的是 `_note_refs_unverified`（只打印「未经核验」免责声明）。它已被
# `_verify_draft_refs` 取代 —— 系统现在真去 Crossref 反查，而不是声明自己没查。
# 核验本身的行为（三态、措辞、缓存）在 tests/test_draft_ref_verify.py。


def _load_chat_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("chatmod", "scripts/chat.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_verify_hook_exists_and_skips_when_no_refs():
    """无文献时不该发任何请求、不该打印任何东西。"""
    from contextlib import redirect_stdout

    mod = _load_chat_module()

    class F:
        known_refs: list = []

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod._verify_draft_refs(F(), "nonexistent/facts.json")
    assert buf.getvalue().strip() == ""


def test_interrupt_keeps_partial_cache(tmp_path, monkeypatch):
    """Ctrl-C 跳过核验时，已验部分必须落盘 —— 否则中断等于白等。

    实测单条约 2s，36 条初稿要 70s 以上，用户中途放弃是常态。
    """
    from contextlib import redirect_stdout

    import paper_agent.providers.retrieval.crossref as cr
    from paper_agent.literature.draft_refs import cache_path_for, load_cache
    from paper_agent.workspace.models import RetrievalCandidate

    class _Stub:
        def __init__(self):
            self.n = 0

        def search_bibliographic(self, q, *, limit=5):
            self.n += 1
            if self.n > 2:
                raise KeyboardInterrupt
            return [RetrievalCandidate(
                id="d", title=q.title, authors=["A"], year=2020,
                source="crossref", source_id="10.1/x",
                work_identifiers={"doi": "10.1/x"})]

    monkeypatch.setattr(cr, "CrossrefVerifyProvider", lambda *a, **k: _Stub())
    mod = _load_chat_module()
    facts_path = str(tmp_path / "facts.json")

    class F:
        known_refs = [
            KnownRef(id=str(i), title=f"Author A. Paper Number {i} With Long Title. 2020.")
            for i in range(1, 6)
        ]

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod._verify_draft_refs(F(), facts_path)
    assert "已跳过" in buf.getvalue()
    assert len(load_cache(cache_path_for(facts_path))) == 2


def test_verify_skips_already_verified_refs():
    """已核验的条目不重复反查。"""
    from contextlib import redirect_stdout

    mod = _load_chat_module()

    class F:
        known_refs = [KnownRef(id="1", title="A B C. Some Title Here. 2020.",
                               verified=True)]

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod._verify_draft_refs(F(), "nonexistent/facts.json")
    assert buf.getvalue().strip() == ""
