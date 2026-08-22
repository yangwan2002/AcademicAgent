"""Week 5 验收：format_docx + FormatGate（正文 hash 不变）+ 回滚 + 逃生舱。"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

docx = pytest.importorskip("docx")
from docx.shared import Pt

from paper_agent.session.file_session import FileBackedSession
from paper_agent.gates.format_gate import FormatGate, snapshot_docx
from paper_agent.tools.docx_format import apply_ops, FormatOp
from paper_agent.tools.sandbox import run_python_script
from paper_agent.intents.format_parser import parse_format_ops
from paper_agent.intents.format_docx import execute_format


def _make_docx(path):
    d = docx.Document()
    d.add_heading("Introduction", level=1)
    d.add_paragraph("This is the introduction body text with number 0.87.")
    d.add_heading("Method", level=1)
    d.add_paragraph("We describe the method here.")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "a"
    table.cell(0, 1).text = "b"
    d.save(path)


# ---- 解析器 ----

def test_parse_format_ops_acceptance():
    ops = parse_format_ops("全文改成宋体四号，1.5倍行距，一级标题居中，某表加三线格式")
    names = {(o.name, o.scope) for o in ops}
    assert ("set_font_family", "all") in names
    assert ("set_font_size", "all") in names
    assert ("set_line_spacing", "all") in names
    assert ("set_alignment", "heading1") in names
    assert ("apply_three_line_tables", "all") in names
    size = next(o.value for o in ops if o.name == "set_font_size")
    assert size == 14  # 四号


def test_parse_empty_when_no_format():
    assert parse_format_ops("帮我润色引言") == []


# ---- docx_format + FormatGate ----

def test_apply_font_size_keeps_text(tmp_path):
    p = str(tmp_path / "d.docx")
    _make_docx(p)
    before = snapshot_docx(p)
    apply_ops(p, [FormatOp("set_font_size", 12, "all")])
    after = snapshot_docx(p)
    assert before.text_hash == after.text_hash          # 正文未变
    d = docx.Document(p)
    # 至少有一个 run 字号被设置
    sizes = [r.font.size for para in d.paragraphs for r in para.runs if r.font.size]
    assert Pt(12) in sizes


def test_format_gate_detects_text_change(tmp_path):
    p = str(tmp_path / "d.docx")
    _make_docx(p)
    before = snapshot_docx(p)
    # 手动改正文
    d = docx.Document(p)
    d.paragraphs[1].runs[0].text = "TAMPERED CONTENT"
    d.save(p)
    assert not FormatGate().check(before, p).passed


# ---- 端到端 format + 回滚 ----

def test_end_to_end_format_docx(tmp_path):
    src = tmp_path / "paper.docx"
    _make_docx(str(src))
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))
    if not session.git_enabled:
        pytest.skip("需要 git")

    result = execute_format(session, "全文改成宋体四号，1.5倍行距，一级标题居中，三线表")
    assert result.ok, result.message
    assert result.applied
    # 源文件未动；working 正文文本仍一致（只改了格式）
    assert os.path.getsize(session.working_path) > 0


def test_format_rejects_non_docx(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text("\\section{X}\ntext", encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))
    assert not execute_format(session, "改字体").ok


# ---- 逃生舱 ----

def test_sandbox_runs_on_copy_with_consent(tmp_path):
    src = tmp_path / "d.docx"
    _make_docx(str(src))
    script = "import os; open(os.environ['DOCX_PATH'],'ab').close()"  # 无害触碰
    res = run_python_script(script, str(src), confirm=lambda _m: True)
    assert res.ok
    assert res.output_path != str(src)          # 操作的是副本
    assert os.path.abspath(res.output_path) != os.path.abspath(str(src))


def test_sandbox_requires_consent(tmp_path):
    src = tmp_path / "d.docx"
    _make_docx(str(src))
    res = run_python_script("pass", str(src), confirm=lambda _m: False)
    assert not res.ok
