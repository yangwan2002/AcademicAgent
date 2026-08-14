"""Week 4 验收：add_section + AddSectionGate + 跨 chat 复用。

（原含 fix_bibliography 用例；该意图与 tools/bibliography.py 已删除，见 backlog B-2。
文献区同步现由 literature/bibliography_sync.py 的 apply_bibliography 在 commit 时承担，
覆盖它的测试在 tests/test_bibliography_sync*.py。）
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.session.facts import (
    SessionFacts, KnownRef, KnownNumber, project_facts_path,
)
from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.section_index import SectionIndex
from paper_agent.session.extractor import extract_structure_from
from paper_agent.gates.diff_gate import AddSectionGate
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.intents.router import Router, Intent
from paper_agent.intents.interview import interview_add_section, InterviewError
from paper_agent.intents.add_section import execute_add_section


SAMPLE_TEX = r"""\documentclass{article}
\begin{document}
\section{Method}
\subsection{Setup}
Setup text.
\subsection{Model}
Model text.
\section{Results}
Results here \cite{key1}.
\begin{thebibliography}{9}
\bibitem{key1} A ref.
\end{thebibliography}
\end{document}
"""


def _facts(tmp="f.json"):
    return SessionFacts(
        path=tmp,
        known_refs=[KnownRef(id="1", bibkey="key1", verified=True)],
        known_numbers=[KnownNumber(value="0.87", confirmed=True)],
    )


# ---- extract_structure_from ----

def test_extract_structure_from(tmp_path):
    p = tmp_path / "ref.tex"
    p.write_text(SAMPLE_TEX, encoding="utf-8")
    subs = extract_structure_from(str(p), "Method")
    assert subs == ["Setup", "Model"]


# ---- Router ----

def _router():
    return Router(SectionIndex(SAMPLE_TEX, "tex"))


def test_router_detects_add_section():
    r = _router().route("帮我补写一章 Introduction")
    assert r.intent is Intent.ADD_SECTION and r.title == "Introduction"


def test_router_add_section_batch():
    assert _router().route("帮我看看还缺哪些章节").intent is Intent.ADD_SECTION_BATCH


def test_router_existing_section_rejected():
    # Method 已存在 → 不应作为 add_section
    r = _router().route("新增 Method 章节")
    assert r.intent is Intent.UNCLEAR


# ---- AddSectionGate ----

def _gate(f):
    return AddSectionGate("tex", f, min_chars=20, max_chars=5000)


def test_add_gate_accepts_valid_block():
    block = "\\section{Introduction}\nThis introduces the work \\cite{key1}, motivating 0.87."
    assert _gate(_facts()).check(block, title="Introduction").passed


def test_add_gate_rejects_missing_title():
    block = "\\section{Intro}\nsome text \\cite{key1}"
    assert not _gate(_facts()).check(block, title="Introduction").passed


def test_add_gate_rejects_unknown_citation():
    block = "\\section{Introduction}\ntext \\cite{ghost} here, plenty of words."
    assert not _gate(_facts()).check(block, title="Introduction").passed


def test_add_gate_requires_subsections():
    block = "\\section{Introduction}\nno subsections here at all, just prose text."
    res = _gate(_facts()).check(
        block, title="Introduction", expected_subsections=["Background", "Goal"]
    )
    assert not res.passed


# ---- Interview（含跨 chat 结构复用）----

def test_interview_add_section_reuses_stored_structure():
    idx = SectionIndex(SAMPLE_TEX, "tex")
    f = _facts()
    f.set_section_structure("introduction", "背景 → gap → 贡献")
    # a1 目的；a2 沿用(y)；a3 位置末尾；引用密集型会问搜文献 → 回车跳过
    answers = iter(["讲清问题重要性和贡献", "y", "末尾", ""])
    plan = interview_add_section("Introduction", None, idx, f, ask=lambda _p: next(answers))
    assert plan.structure_desc == "背景 → gap → 贡献"
    assert plan.section_type == "introduction"


def test_interview_add_section_rejects_existing():
    idx = SectionIndex(SAMPLE_TEX, "tex")
    with pytest.raises(InterviewError):
        interview_add_section("Method", None, idx, _facts(), ask=lambda _p: "x")


# ---- 端到端 add_section ----

def test_end_to_end_add_section(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text(SAMPLE_TEX, encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))
    facts = _facts(str(tmp_path / "f.json"))

    idx = SectionIndex(session.read_working(), session.ext)
    # 顺序：a1 目的 → a2 结构(选 a 描述式 → 给描述) → a3 位置 → 搜文献(跳过)
    answers = iter(["介绍问题与贡献", "a", "背景 → gap → 贡献", "Method 之前", ""])
    plan = interview_add_section("Introduction", None, idx, facts, ask=lambda _p: next(answers))

    body = (
        "Air-ground collaborative perception has become increasingly important "
        "for large-scale mapping tasks. Prior systems struggle with wide viewpoint "
        "changes and drift. In this work we present a method that addresses these "
        "issues, building on established techniques \\cite{key1}. Our approach "
        "achieves 0.87 accuracy on the target benchmark, and we discuss why the "
        "design choices matter for robustness across diverse environments."
    )
    result = execute_add_section(
        session, plan, facts, MockLLMProvider(scripted=[body]),
        retrieval_provider=MockRetrievalProvider(),
        verifier=CitationVerifier(MockRetrievalProvider()),
        confirm=lambda _v, _c: False, max_retry=2,
    )
    assert result.ok, result.message

    working = session.read_working()
    assert "\\section{Introduction}" in working
    assert "\\section{Method}" in working          # 原有章节保留
    assert session.read_source() == SAMPLE_TEX      # 源文件未动
    # Introduction 插在 Method 之前
    assert working.index("\\section{Introduction}") < working.index("\\section{Method}")
    # claims 已记住
    assert facts.claims["section_intents"]["introduction"] == "介绍问题与贡献"


def test_cross_chat_claims_persist(tmp_path):
    """两次会话共享同一 project facts：第二次能读到第一次写入的 claims。"""
    ws = str(tmp_path / "ws")
    src = tmp_path / "paper.tex"
    src.write_text(SAMPLE_TEX, encoding="utf-8")

    p1 = project_facts_path(ws, str(src))
    f1 = SessionFacts.load(p1)
    f1.set_section_structure("introduction", "背景 → gap → 贡献")
    f1.save()

    # 第二次会话：同一源文件 → 同一 project facts 路径
    p2 = project_facts_path(ws, str(src))
    f2 = SessionFacts.load(p2)
    assert f2.get_section_structure("introduction") == "背景 → gap → 贡献"
