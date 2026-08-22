"""Week 2 验收：convert_format 端到端 + 转换后校验 + git undo。"""

from __future__ import annotations

import os
import sys
import zipfile

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.tools.convert import check_pandoc
from paper_agent.tools.doc_stats import (
    DocStats, stats_from_text, validate_conversion,
)
from paper_agent.session.file_session import FileBackedSession
from paper_agent.intents.plans import ConvertPlan
from paper_agent.intents.convert import execute_convert

pandoc_required = pytest.mark.skipif(
    check_pandoc() is None, reason="需要 pandoc"
)

SAMPLE_TEX = r"""\documentclass{article}
\begin{document}
\section{Introduction}
We report 0.87 accuracy \cite{key1}. The model uses $\alpha > 0$.
\section{Results}
Results here.
\begin{table}
\begin{tabular}{cc} a & b \\ 1 & 2 \end{tabular}
\end{table}
\begin{thebibliography}{9}
\bibitem{key1} A ref.
\end{thebibliography}
\end{document}
"""


# ---- doc_stats ----

def test_stats_from_text_tex():
    st = stats_from_text(SAMPLE_TEX, "tex")
    assert st.sections == 2
    assert st.citations == 1
    assert st.formulas == 1
    assert st.tables == 1


def test_validate_critical_pass_with_advisory_diff():
    src = DocStats(sections=2, citations=1, formulas=1, tables=1, figures=0)
    tgt = DocStats(sections=2, citations=0, formulas=1, tables=1, figures=0)
    v = validate_conversion(src, tgt)
    assert v.ok  # 关键指标一致，引用差异仅为参考


def test_validate_critical_fail():
    src = DocStats(sections=2, formulas=1)
    tgt = DocStats(sections=1, formulas=1)
    assert not validate_conversion(src, tgt).ok


# ---- git undo ----

def test_git_undo_reverts_last_write(tmp_path):
    src = tmp_path / "p.tex"
    src.write_text(SAMPLE_TEX, encoding="utf-8")
    s = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))
    s.write_working(SAMPLE_TEX + "\n% edit1\n", message="e1")
    assert "% edit1" in s.read_working()
    assert s.undo() is True
    assert "% edit1" not in s.read_working()


def test_undo_hook_pairs_with_writes_across_bare_commits(tmp_path):
    """hook 载荷必须与「回退一步」严格配对，含 `commit()`（排版）这种裸提交。

    裸提交不占位的话，撤销排版会错误回放上一次 write_working 的载荷。
    """
    src = tmp_path / "p.tex"
    src.write_text(SAMPLE_TEX, encoding="utf-8")
    s = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))
    s.write_working(SAMPLE_TEX + "\n% e1\n", message="e1",
                    undo_hook=lambda: {"mark": "A"})
    if not s.git_enabled:
        pytest.skip("裸 commit 只在 git 模式下产生版本")
    s.commit("format only")                     # 排版提交：无正文之外状态

    assert s.undo() is True
    assert s.take_undo_state() is None          # 撤排版 → 不回放别名
    assert s.undo() is True
    assert s.take_undo_state() == {"mark": "A"}  # 撤那次写入 → 回放它自己的
    assert s.take_undo_state() is None          # 取后清空，不重复回放


# ---- 端到端转换 ----

@pandoc_required
def test_end_to_end_convert_tex_to_docx(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text(SAMPLE_TEX, encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))

    plan = ConvertPlan(target_ext="docx")
    result = execute_convert(session, plan, output_dir=str(tmp_path / "out"))

    assert result.ok, result.message
    assert os.path.isfile(result.output_path)
    assert result.output_path.endswith("paper.docx")
    # 源文件与 working 均未被转换改动
    assert session.read_source() == SAMPLE_TEX


@pandoc_required
def test_two_column_preset_writes_cols(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text(SAMPLE_TEX, encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))

    plan = ConvertPlan(target_ext="docx", presets=["two_column"])
    result = execute_convert(session, plan, output_dir=str(tmp_path / "out"))
    assert result.ok

    with zipfile.ZipFile(result.output_path) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", "ignore")
    assert 'w:num="2"' in xml
