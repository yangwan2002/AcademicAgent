"""Week 3 验收：rewrite + 搜文献子 SOP + RewriteGate。"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.session.extractor import extract_numbers, extract_refs
from paper_agent.session.facts import SessionFacts, KnownRef, KnownNumber
from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.section_index import SectionIndex
from paper_agent.gates.diff_gate import RewriteGate
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.literature import search_and_verify
from paper_agent.intents.router import Router, Intent
from paper_agent.intents.interview import interview_rewrite, InterviewError
from paper_agent.intents.rewrite import execute_rewrite


SAMPLE_TEX = r"""\documentclass{article}
\begin{document}
\section{Introduction}
We report 0.87 accuracy \cite{key1}. It beats the baseline.
\section{Results}
\begin{tabular}{cc} method & acc \\ ours & 0.87 \\ base & 0.84 \end{tabular}
\begin{thebibliography}{9}
\bibitem{key1} A ref.
\end{thebibliography}
\end{document}
"""


def _facts(tmp="facts.json"):
    return SessionFacts(
        path=tmp,
        known_refs=[KnownRef(id="1", bibkey="key1", verified=True)],
        known_numbers=[KnownNumber(value="0.87", confirmed=True)],
    )


# ---- extractor ----

def test_extract_numbers_from_tex_table():
    nums = {n.value for n in extract_numbers(SAMPLE_TEX, "tex")}
    assert "0.87" in nums and "0.84" in nums


# ---- facts ----

def test_facts_add_ref_dedupe_and_next_id():
    f = SessionFacts(path="x", known_refs=[KnownRef(id="1", bibkey="a")])
    assert f.next_ref_id() == "2"
    assert f.add_ref(KnownRef(id="2", bibkey="a")) is False  # bibkey 重复
    assert f.add_ref(KnownRef(id="2", bibkey="b")) is True


# ---- 搜文献子 SOP ----

def test_search_and_verify_adds_verified_refs():
    f = SessionFacts(path="x")
    provider = MockRetrievalProvider()
    verifier = CitationVerifier(provider)
    out = search_and_verify("transformers", 5, provider, verifier, f)
    assert out.verified == 2 and len(out.added) == 2
    assert all(r.verified for r in f.known_refs)
    assert len(f.bibkeys()) == 2


# ---- RewriteGate ----

def _gate(f):
    return RewriteGate("tex", f, target_level=1)


ORIG = r"We report 0.87 accuracy \cite{key1}. It beats the baseline."


def test_gate_accepts_valid_rewrite():
    cand = r"Our method reaches 0.87 accuracy \cite{key1}, clearly beating the baseline."
    assert _gate(_facts()).check(ORIG, cand).passed


def test_gate_rejects_unknown_citation():
    cand = r"Our method reaches 0.87 \cite{ghost}, beating the baseline."
    assert not _gate(_facts()).check(ORIG, cand).passed


def test_gate_rejects_fabricated_number():
    cand = r"Our method reaches 0.95 accuracy \cite{key1}, beating the baseline."
    assert not _gate(_facts()).check(ORIG, cand).passed


def test_gate_allows_confirmed_number():
    cand = r"Our method reaches 0.95 accuracy \cite{key1}, beating the baseline."
    assert _gate(_facts()).check(ORIG, cand, confirmed_numbers={"0.95"}).passed


def test_gate_rejects_new_same_level_heading():
    cand = ORIG + "\n\\section{Sneaky}\nextra"
    assert not _gate(_facts()).check(ORIG, cand).passed


def test_gate_rejects_paragraph_explosion():
    cand = "\n\n".join([f"para {i} \\cite{{key1}}" for i in range(10)])
    assert not _gate(_facts()).check(ORIG, cand).passed


# ---- Router ----

def _router():
    return Router(SectionIndex(SAMPLE_TEX, "tex"))


def test_router_detects_rewrite():
    r = _router().route("帮我改写 Introduction，补充论证")
    assert r.intent is Intent.REWRITE and r.section_id == "introduction"


def test_router_polish_takes_precedence():
    # 同时命中改写与润色 → 保守走 polish
    r = _router().route("改写 Introduction 让它更通顺")
    assert r.intent is Intent.POLISH


# ---- Interview ----

def test_interview_rewrite_triggers_search():
    idx = SectionIndex(SAMPLE_TEX, "tex")
    answers = iter(["a", "iii", "graph neural network"])
    plan = interview_rewrite("introduction", idx, ask=lambda _p: next(answers))
    assert plan.want_search and plan.search_query == "graph neural network"


def test_interview_rewrite_vague_direction_fails():
    idx = SectionIndex(SAMPLE_TEX, "tex")
    answers = iter(["c", ""])  # 换叙述角度但不说明
    with pytest.raises(InterviewError):
        interview_rewrite("introduction", idx, ask=lambda _p: next(answers))


# ---- 端到端 ----

def test_end_to_end_rewrite(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text(SAMPLE_TEX, encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))

    facts = SessionFacts(path=str(tmp_path / "facts.json"))
    facts.known_refs = extract_refs(SAMPLE_TEX, "tex")
    facts.known_numbers = extract_numbers(SAMPLE_TEX, "tex")

    idx = SectionIndex(session.read_working(), session.ext)
    route = Router(idx).route("改写 Introduction 补充论证")
    assert route.intent is Intent.REWRITE

    answers = iter(["a", "ii"])  # 补充论证 / 更多实验对比（不搜文献）
    plan = interview_rewrite(route.section_id, idx, ask=lambda _p: next(answers))

    good = (
        "\nOur method attains 0.87 accuracy \\cite{key1}, consistently "
        "surpassing the 0.84 baseline across settings.\n"
    )
    provider = MockRetrievalProvider()
    result = execute_rewrite(
        session, plan, facts, MockLLMProvider(scripted=[good]),
        retrieval_provider=provider, verifier=CitationVerifier(provider),
        confirm=lambda _v, _c: False, max_retry=2,
    )
    assert result.ok, result.message
    assert session.read_source() == SAMPLE_TEX          # 源文件未动
    working = session.read_working()
    assert "surpassing" in working
    assert "\\section{Results}" in working              # 其他章节保留
    assert "0.84" in working
