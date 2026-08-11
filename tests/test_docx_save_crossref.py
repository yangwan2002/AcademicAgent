"""`:save` 把 docx 存成交叉引用域版。

早前 `:save` 只能存纯文本 `[n]`，理由是「带域的 docx 重新载入会让投影层看不见
引用」。`import_crossrefs`（载入即摊平）消除了那个前提，于是限制不再成立 ——
保存产物本身就该是能在 Word 里自动更新编号的成品。

本文件钉住四件事：

1. **保存即成品** —— `:save` 后源文件含 REF 域，可见文字与编辑结果逐字一致。
2. **working 不受影响** —— 升域只作用于写出的那一份，working copy 保持纯文本，
   否则下一轮编辑的投影层又看不见引用了。
3. **可重复往返** —— 存域版 → 重新载入 → 再编辑 → 再存，域与书签不累积、不漂移。
4. **失败不丢正文** —— 升域出错时退回纯文本保存，正文完整。
"""

from __future__ import annotations

import os
import zipfile

import pytest

docx = pytest.importorskip("docx")

from paper_agent.session.file_session import FileBackedSession
from paper_agent.tools.docx_crossref import visible_text_with_fields

BIB_BEGIN = "<!-- BIBLIOGRAPHY:BEGIN (自动同步，勿手改区块内内容) -->"
BIB_END = "<!-- BIBLIOGRAPHY:END -->"


def _counts(path: str) -> tuple[int, int]:
    """(域数, 书签数) —— 判断累积/漂移的直接口径。"""
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    return xml.count("<w:fldSimple"), xml.count("<w:bookmarkStart")


@pytest.fixture
def paper(tmp_path):
    path = str(tmp_path / "论文.docx")
    d = docx.Document()
    d.add_paragraph("引言：跨视角匹配是关键 [1]，位姿桥接见 [2]。")
    d.add_paragraph("参考文献")
    d.add_paragraph(BIB_BEGIN)
    d.add_paragraph("[1] A. Title One. 2020.")
    d.add_paragraph("[2] B. Title Two. 2021.")
    d.add_paragraph(BIB_END)
    d.save(path)
    return path


def _session(paper, tmp_path, **kw):
    return FileBackedSession(
        source_path=paper,
        workspace_root=str(tmp_path / "ws"),
        use_git=False,
        **kw,
    )


def test_save_writes_crossref_fields_to_source(paper, tmp_path):
    """`:save` 后源文件本身就是交叉引用版，不必再敲 `:export`。"""
    s = _session(paper, tmp_path)
    assert _counts(paper) == (0, 0)

    s.write_working(s.read_working().replace("关键", "核心难点"), "edit")
    s.save_to_source(crossrefs=True)

    fields, bookmarks = _counts(paper)
    assert fields == 2
    assert bookmarks == 2
    assert s.last_save_crossref.fields == 2
    assert not s.last_save_crossref.dangling
    # 可见文字（含域缓存）与编辑结果逐字一致
    first = visible_text_with_fields(paper).split("\n")[0]
    assert first == "引言：跨视角匹配是核心难点 [1]，位姿桥接见 [2]。"


def test_working_copy_stays_plain_after_save(paper, tmp_path):
    """升域只作用于写出的那一份 —— working 保持纯文本。

    若 working 也带域，下一轮编辑的投影层就看不见引用了（模型会自己补）。
    """
    s = _session(paper, tmp_path)
    s.save_to_source(crossrefs=True)

    assert _counts(s.working_path)[0] == 0
    assert "[1]" in s.read_working()


def test_save_plain_opt_out(paper, tmp_path):
    """`crossrefs=False`（即 `:save --plain`）仍存纯文本 `[n]`。"""
    s = _session(paper, tmp_path)
    s.save_to_source(crossrefs=False)

    assert _counts(paper) == (0, 0)
    assert "[1]" in "\n".join(p.text for p in docx.Document(paper).paragraphs)


def test_roundtrip_across_sessions_does_not_accumulate(paper, tmp_path):
    """存域版 → 重新载入 → 再编辑 → 再存：域与书签不累积，引用不漂移。

    修复前的症状是域元素堆到段尾（`[1]...[2]` → `[]...[12]`）。
    """
    for i in range(3):
        s = _session(paper, tmp_path / f"r{i}")
        text = s.read_working().split("\n")
        text[0] = f"引言：第{i + 1}轮改写 [1]，位姿桥接见 [2]。"
        s.write_working("\n".join(text), "edit")
        s.save_to_source(crossrefs=True)

        assert _counts(paper) == (2, 2), f"轮 {i + 1} 域/书签累积了"
        first = visible_text_with_fields(paper).split("\n")[0]
        assert first == f"引言：第{i + 1}轮改写 [1]，位姿桥接见 [2]。"
        assert "[12]" not in first


def test_second_load_flattens_saved_fields(paper, tmp_path):
    """存成域版后重新载入：自动摊平，投影层重新看得见 `[n]`。

    这是「`:save` 可以存域版」成立的前提 —— 否则就是把自己的下一次编辑弄坏。
    """
    s = _session(paper, tmp_path / "a")
    s.save_to_source(crossrefs=True)

    s2 = _session(paper, tmp_path / "b")
    assert s2.crossref_import.flattened == 2
    body = s2.read_working().split("参考文献")[0]
    assert "[1]" in body and "[2]" in body


def test_save_degrades_to_plain_when_conversion_fails(paper, tmp_path, monkeypatch):
    """升域抛错时退回纯文本保存 —— 正文不能因为排版增强而丢。"""
    s = _session(paper, tmp_path)
    s.write_working(s.read_working().replace("关键", "核心难点"), "edit")

    import paper_agent.tools.docx_crossref as xr

    def _boom(*a, **k):
        raise RuntimeError("模拟转换失败")

    monkeypatch.setattr(xr, "export_crossrefs", _boom)
    s.save_to_source(crossrefs=True)

    assert s.last_save_crossref_error
    assert "模拟转换失败" in s.last_save_crossref_error
    assert _counts(paper) == (0, 0)                      # 退化成纯文本
    plain = "\n".join(p.text for p in docx.Document(paper).paragraphs)
    assert "核心难点" in plain                            # 编辑结果没丢
    assert "[1]" in plain


def test_no_staging_file_left_behind(paper, tmp_path):
    """升域用的中间文件不留在源文件目录里。"""
    s = _session(paper, tmp_path)
    s.save_to_source(crossrefs=True)

    leftovers = [
        f for f in os.listdir(os.path.dirname(paper))
        if ".tmp." in f or ".xref." in f
    ]
    assert leftovers == []


def test_superscript_applied_on_save(paper, tmp_path):
    """默认存域版时 `[n]` 连方括号一起上标（GB/T 7714 顺序编码制）。"""
    s = _session(paper, tmp_path)
    s.save_to_source(crossrefs=True)

    out = docx.Document(paper)
    brackets = [
        r for r in out.paragraphs[0].runs if r.text.strip() in ("[", "]", ",", ", ")
    ]
    assert brackets
    assert all(r.font.superscript for r in brackets)
    # 文献表条目不上标
    entry = [p for p in out.paragraphs if (p.text or "").startswith("[1] A.")][0]
    assert all(r.font.superscript in (None, False) for r in entry.runs)
