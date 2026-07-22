"""Week 1 验收：polish 端到端 + Diff Gate 安全规则。

验收对应卡 1 的核心承诺：只改语言，不动数字/引用/公式/结构。
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.gates.diff_gate import PolishGate
from paper_agent.generate.generator import run_polish
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.section_index import SectionIndex, slugify
from paper_agent.session.extractor import extract_refs
from paper_agent.intents.router import Router, Intent
from paper_agent.intents.interview import interview_polish
from paper_agent.intents.polish import execute_polish


SAMPLE_TEX = r"""\documentclass{article}
\begin{document}
\section{Introduction}
Our method achieves 0.87 AUROC which is better \cite{vaswani2017} than the
baseline of 0.84. The improvement, as shown, is due to $\alpha > 0$.
\section{Method}
We use a graph neural network.
\begin{thebibliography}{9}
\bibitem{vaswani2017} Attention Is All You Need.
\end{thebibliography}
\end{document}
"""


# ---- SectionIndex ----

def test_section_index_parses_and_splices():
    idx = SectionIndex(SAMPLE_TEX, "tex")
    ids = [s.id for s in idx.list()]
    assert "introduction" in ids and "method" in ids
    intro = idx.find("Introduction")
    assert intro is not None
    body = idx.body_of(intro)
    assert "0.87 AUROC" in body
    spliced = idx.splice(intro, "\nREPLACED\n")
    assert "REPLACED" in spliced
    # method 章节与 bibliography 未被触碰
    assert "graph neural network" in spliced
    assert "vaswani2017" in spliced


def test_slugify():
    assert slugify("Related Work") == "related_work"
    assert slugify("  Results & Discussion ") == "results_discussion"


# ---- Extractor ----

def test_extract_refs_from_tex():
    refs = extract_refs(SAMPLE_TEX, "tex")
    assert len(refs) == 1
    assert refs[0].bibkey == "vaswani2017"


# ---- PolishGate ----

def _gate():
    return PolishGate("tex", max_char_change_ratio=0.30)


ORIG = r"We got 0.87 AUROC \cite{a} versus 0.84, with $x^2$ terms."


def test_gate_accepts_language_only_change():
    cand = r"We achieved 0.87 AUROC \cite{a} compared to 0.84, using $x^2$ terms."
    assert _gate().check(ORIG, cand).passed


def test_gate_rejects_number_change():
    cand = r"We achieved 0.99 AUROC \cite{a} compared to 0.84, using $x^2$ terms."
    assert not _gate().check(ORIG, cand).passed


def test_gate_rejects_citation_change():
    cand = r"We achieved 0.87 AUROC \cite{b} compared to 0.84, using $x^2$ terms."
    assert not _gate().check(ORIG, cand).passed


def test_gate_rejects_formula_change():
    cand = r"We achieved 0.87 AUROC \cite{a} compared to 0.84, using $x^3$ terms."
    assert not _gate().check(ORIG, cand).passed


def test_gate_rejects_placeholder():
    cand = ORIG + " TODO: finish this."
    assert not _gate().check(ORIG, cand).passed


def test_gate_rejects_oversize_change():
    cand = ORIG + " " + ("padding words " * 50)
    assert not _gate().check(ORIG, cand).passed


# ---- Generator（生成+gate+重试）----

def test_run_polish_passes_with_good_output():
    good = r"We achieved 0.87 AUROC \cite{a} compared to 0.84, using $x^2$ terms."
    llm = MockLLMProvider(scripted=[good])
    out = run_polish(ORIG, "", llm, _gate(), max_retry=2)
    assert out.ok and out.attempts == 1


def test_run_polish_retries_then_fails_on_bad_output():
    bad = r"We achieved 0.99 AUROC \cite{a} compared to 0.84, using $x^2$ terms."
    llm = MockLLMProvider(scripted=[bad, bad, bad])
    out = run_polish(ORIG, "", llm, _gate(), max_retry=2)
    assert not out.ok and out.attempts == 3


# ---- Router + Interview + 端到端 ----

def test_router_detects_polish_and_section():
    idx = SectionIndex(SAMPLE_TEX, "tex")
    r = Router(idx).route("帮我润色一下 Introduction")
    assert r.intent is Intent.POLISH and r.section_id == "introduction"


def test_router_unclear_for_unrelated_message():
    idx = SectionIndex(SAMPLE_TEX, "tex")
    assert Router(idx).route("今天天气怎么样").intent is Intent.UNCLEAR


def test_end_to_end_polish(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text(SAMPLE_TEX, encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))

    idx = SectionIndex(session.read_working(), session.ext)
    route = Router(idx).route("润色 Introduction")
    assert route.intent is Intent.POLISH

    # 脚本化访谈答案：目标留空
    answers = iter([""])
    plan = interview_polish(route.section_id, idx, ask=lambda _p: next(answers))

    # mock LLM 返回一个合法润色（保留数字/引用/公式）
    good_body = (
        "\nOur approach reaches 0.87 AUROC, outperforming \\cite{vaswani2017} the "
        "0.84 baseline. This gain stems from $\\alpha > 0$.\n"
    )
    llm = MockLLMProvider(scripted=[good_body])
    result = execute_polish(session, plan, llm)
    assert result.ok, result.message

    # 源文件未被修改；working 已更新；其他章节不变
    assert session.read_source() == SAMPLE_TEX
    working = session.read_working()
    assert "outperforming" in working
    assert "graph neural network" in working   # method 章节未动
    assert "\\bibitem{vaswani2017}" in working  # bibliography 未动

    # save 后源文件才更新
    session.save_to_source()
    assert "outperforming" in session.read_source()
