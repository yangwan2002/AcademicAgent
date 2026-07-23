"""streaming-and-trace Task 10 验收：端到端集成 + 模式矩阵 + 向后兼容。

对应 tasks.md Task 10：
- stream off/on × trace off/on 四种模式
- 端到端事件序列（WORKFLOW_START→span(run)→ROUTE→attempt(llm_call/gate)→
  WORKING_COPY_UPDATE→WORKSPACE_COMMIT→span(run)END→WORKFLOW_END）
- trace.json 结构 + 隐私
- 既有行为不变（不注入 bus / stream=False）
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.coordinator import Coordinator
from paper_agent.observability.events import Event, EventKind
from paper_agent.intents.interview import interview_polish
from paper_agent.intents.polish import execute_polish
from paper_agent.providers.llm.base import CancellationToken
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.section_index import SectionIndex


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

GOOD_BODY = (
    "\nOur approach reaches 0.87 AUROC, outperforming \\cite{vaswani2017} the "
    "0.84 baseline. This gain stems from $\\alpha > 0$.\n"
)


class CaptureRenderer:
    def __init__(self):
        self.events: list[Event] = []

    def emit(self, event):
        self.events.append(event)


def _make_session(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text(SAMPLE_TEX, encoding="utf-8")
    return src, FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))


def _polish_fn(session, llm):
    idx = SectionIndex(session.read_working(), session.ext)
    answers = iter([""])
    plan = interview_polish("introduction", idx, ask=lambda _p: next(answers))

    def fn(bus, ct):
        bus.publish(EventKind.ROUTE_DECISION, intent="polish", section_id="introduction")
        return execute_polish(session, plan, llm, bus=bus, cancel_token=ct,
                              stream=(bus.as_sink_or_none() is not None and _STREAM[0]))
    return fn


_STREAM = [False]  # 供 _polish_fn 读取当前模式（测试内切换）


# --- 模式矩阵：四种组合都能跑通并写回 working ---

def test_mode_off_off(tmp_path):
    _STREAM[0] = False
    src, session = _make_session(tmp_path)
    llm = MockLLMProvider(scripted=[GOOD_BODY])
    coord = Coordinator(enable_trace=False, enable_stream=False, runs_dir=str(tmp_path / "runs"))
    report = coord.run(_polish_fn(session, llm))
    assert report.value.ok
    assert "outperforming" in session.read_working()
    assert not os.path.isdir(str(tmp_path / "runs"))  # trace 关：不建目录
    assert session.read_source() == SAMPLE_TEX          # 源文件不动


def test_mode_off_trace(tmp_path):
    _STREAM[0] = False
    src, session = _make_session(tmp_path)
    llm = MockLLMProvider(scripted=[GOOD_BODY])
    coord = Coordinator(enable_trace=True, enable_stream=False, runs_dir=str(tmp_path / "runs"))
    report = coord.run(_polish_fn(session, llm))
    assert report.value.ok
    path = os.path.join(str(tmp_path / "runs"), report.run_id, "trace.json")
    assert os.path.exists(path)


def test_mode_stream_off(tmp_path):
    _STREAM[0] = True
    src, session = _make_session(tmp_path)
    llm = MockLLMProvider(scripted=[GOOD_BODY])
    renderer = CaptureRenderer()
    coord = Coordinator(enable_trace=False, enable_stream=True, renderer=renderer,
                        runs_dir=str(tmp_path / "runs"))
    report = coord.run(_polish_fn(session, llm))
    assert report.value.ok
    # 流式：renderer 收到 LLM_DELTA
    assert any(e.kind is EventKind.LLM_DELTA for e in renderer.events)


def test_mode_stream_trace(tmp_path):
    _STREAM[0] = True
    src, session = _make_session(tmp_path)
    llm = MockLLMProvider(scripted=[GOOD_BODY])
    renderer = CaptureRenderer()
    coord = Coordinator(enable_trace=True, enable_stream=True, renderer=renderer,
                        runs_dir=str(tmp_path / "runs"))
    report = coord.run(_polish_fn(session, llm))
    assert report.value.ok
    assert os.path.exists(os.path.join(str(tmp_path / "runs"), report.run_id, "trace.json"))
    assert any(e.kind is EventKind.LLM_DELTA for e in renderer.events)


# --- 端到端事件序列 ---

def test_end_to_end_event_sequence(tmp_path):
    _STREAM[0] = False
    src, session = _make_session(tmp_path)
    llm = MockLLMProvider(scripted=[GOOD_BODY])
    renderer = CaptureRenderer()
    coord = Coordinator(enable_trace=False, enable_stream=True, renderer=renderer,
                        runs_dir=str(tmp_path / "runs"))
    coord.run(_polish_fn(session, llm))

    kinds = [e.kind for e in renderer.events]

    def order(k):
        return kinds.index(k)

    # 关键里程碑按序出现
    assert order(EventKind.WORKFLOW_START) < order(EventKind.ROUTE_DECISION)
    assert order(EventKind.ROUTE_DECISION) < order(EventKind.GENERATION_ATTEMPT_START)
    assert order(EventKind.LLM_CALL_START) < order(EventKind.LLM_CALL_END)
    assert order(EventKind.LLM_CALL_END) < order(EventKind.GATE_RESULT)
    assert order(EventKind.GATE_RESULT) < order(EventKind.WORKING_COPY_UPDATE)
    assert order(EventKind.WORKING_COPY_UPDATE) <= order(EventKind.WORKSPACE_COMMIT)
    assert order(EventKind.WORKSPACE_COMMIT) < order(EventKind.WORKFLOW_END)
    # WORKFLOW_END 是最后一个
    assert kinds[-1] is EventKind.WORKFLOW_END
    # 恰一个 WORKFLOW_START / WORKFLOW_END
    assert kinds.count(EventKind.WORKFLOW_START) == 1
    assert kinds.count(EventKind.WORKFLOW_END) == 1


# --- trace.json 结构 + 隐私 ---

def test_trace_json_structure_and_privacy(tmp_path):
    _STREAM[0] = False
    src, session = _make_session(tmp_path)
    llm = MockLLMProvider(scripted=[GOOD_BODY])
    coord = Coordinator(enable_trace=True, enable_stream=False, runs_dir=str(tmp_path / "runs"))
    report = coord.run(_polish_fn(session, llm))

    path = os.path.join(str(tmp_path / "runs"), report.run_id, "trace.json")
    doc = json.loads(open(path, encoding="utf-8").read())

    # 结构：run span 树
    assert doc["run_status"] == "completed"
    assert doc["root"]["name"] == "run"
    # run 下有 generation_attempt，其下有 llm_call 与 gate
    attempt = doc["root"]["children"][0]
    assert attempt["name"] == "generation_attempt"
    child_names = {c["name"] for c in attempt["children"]}
    assert "llm_call" in child_names and "gate" in child_names
    assert doc["attempts"] == 1
    # 隐私：response 以 SafeText(hash+preview+length) 结构存储，而非裸全文字段。
    llm_call = next(c for c in attempt["children"] if c["name"] == "llm_call")
    resp = llm_call["attributes"]["response"]
    assert set(resp.keys()) == {"hash", "preview", "length"}
    assert len(resp["hash"]) == 64
    assert resp["preview"] and len(resp["preview"]) <= 500  # 预览有上限
    # prompt 也是 SafePrompt 结构
    prompt = llm_call["attributes"]["prompt"]
    assert set(prompt.keys()) == {"hash", "preview", "length"}
    # working_copy_status 由 Coordinator 汇总
    assert doc["working_copy_status"] == "updated"
