"""Week 4 验收：add_section + AddSectionGate + 跨 chat 复用。

（原含 fix_bibliography 用例；该意图与 tools/bibliography.py 已删除，见 backlog B-2。
文献区同步现由 literature/bibliography_sync.py 的 apply_bibliography 在 commit 时承担，
覆盖它的测试在 tests/test_bibliography_sync*.py。）
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.session.facts import (
    SessionFacts, KnownRef, KnownNumber, project_facts_path,
)
from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.extractor import extract_structure_from
from paper_agent.gates.diff_gate import AddSectionGate
from paper_agent.providers.llm.base import ToolCall
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.intents.plans import AddSectionPlan
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


# ---- 端到端 add_section ----

def test_end_to_end_add_section(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text(SAMPLE_TEX, encoding="utf-8")
    session = FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))
    facts = _facts(str(tmp_path / "f.json"))

    plan = AddSectionPlan(
        title="Introduction",
        purpose="介绍问题与贡献",
        position="before:method",
        section_type="introduction",
        structure_kind="desc",
        structure_desc="背景 → gap → 贡献",
    )

    body = (
        "The current manuscript organizes its Method section into Setup and Model. "
        "The Results section records the existing result together with citation "
        "\\cite{key1}. This introduction therefore limits itself to framing those "
        "two documented components and the result already present in the manuscript. "
        "It does not add a new dataset, evaluation protocol, comparison, or causal "
        "claim beyond the material supplied by the current working copy."
    )
    grounded = ToolCall(
        id="ground-1", name="verify_add_section_grounding",
        arguments={
            "supported": True,
            "reason_code": "supported",
            "unsupported_claims": [],
        },
    )
    result = execute_add_section(
        session, plan, facts, MockLLMProvider(scripted=[body, grounded]),
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
