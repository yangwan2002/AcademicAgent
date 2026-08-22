"""Planning 自然语言管线端到端验收。

专用 Fixture：含 Introduction / Method / Conclusion 的 tex。
- 用例一：「新增 Discussion 章节讨论结果意义与局限，放 Conclusion 前」
  （依赖 Conclusion 存在）→ 正确拆解/抽槽/不重复追问/确认/执行/落 trace
- 用例二：「先保守润色 Introduction、保留数字和引用，再转换为 docx」
  → 多操作按序 + convert 终止校验通过 + 直通不触发澄清
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.application.turn_service import TurnService
from paper_agent.config import Config
from paper_agent.coordinator import Coordinator
from paper_agent.providers.llm.base import ToolCall
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.runtime.handlers import HandlerContext
from paper_agent.session.file_session import FileBackedSession
from paper_agent.tasking.clarifier import ConfirmDecision, UnmappedDisposition
from paper_agent.tasking.contracts import OperationStatus, PlanStatus

# --- 专用 Fixture ---

FIXTURE_TEX = """\\section{Introduction}
Our method reaches 0.87 accuracy, improving on prior work \\cite{smith2020}.

\\section{Method}
We describe the architecture and training procedure in detail.

\\section{Conclusion}
We presented a method that achieves strong results.
"""


def _session(tmp_path) -> FileBackedSession:
    src = tmp_path / "paper.tex"
    src.write_text(FIXTURE_TEX, encoding="utf-8")
    return FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))


class _Interactor:
    """记录型交互：默认直接确认；字段追问计数用于「不重复追问」断言。"""

    def __init__(self, *, fields=None, confirm=None, unmapped=None):
        self._fields = list(fields or [])
        self._confirm = list(confirm or [])
        self._unmapped = list(unmapped or [])
        self.field_asks: list[str] = []
        self.confirm_calls = 0

    def ask_field(self, prompt):
        self.field_asks.append(prompt)
        return self._fields.pop(0) if self._fields else ""

    def resolve_unmapped(self, item):
        return self._unmapped.pop(0) if self._unmapped else UnmappedDisposition("ignore")

    def confirm_plan(self, summary):
        self.confirm_calls += 1
        return self._confirm.pop(0) if self._confirm else ConfirmDecision("accept")


def _analyze(ops, reqs=(), unmapped=()):
    return json.dumps({
        "operations": [
            {"intent": i, "verbatim": v, "requirement_ids": list(r)}
            for i, v, r in ops
        ],
        "requirements": [
            {"requirement_id": rid, "verbatim": v, "category": c} for rid, v, c in reqs
        ],
        "unmapped_text": list(unmapped),
    }, ensure_ascii=False)


def _extract(slots):
    return json.dumps({
        "slots": {
            k: ({"value": v, "status": "present"} if not isinstance(v, dict) else v)
            for k, v in slots.items()
        }
    }, ensure_ascii=False)


_DISCUSSION_BODY = (
    "The Introduction reports that the method reaches 0.87 accuracy. The Method "
    "section states that the manuscript describes the architecture and training "
    "procedure, while the Conclusion records that the presented method achieves "
    "strong results. On the basis of those statements, this discussion treats the "
    "reported accuracy as the manuscript's current result and does not infer a new "
    "evaluation setting, data source, mechanism, comparison, or causal explanation. "
    "Any stronger interpretation requires additional evidence from the author."
)

_GROUNDED = ToolCall(
    id="ground-1", name="verify_add_section_grounding",
    arguments={
        "supported": True,
        "reason_code": "supported",
        "unsupported_claims": [],
    },
)


def _collect_kinds(node) -> set[str]:
    """递归收集 trace 里所有事件 kind（事件嵌在各 span 的 attributes.events 下）。"""
    found: set[str] = set()
    if isinstance(node, dict):
        if "kind" in node and isinstance(node["kind"], str):
            found.add(node["kind"])
        for v in node.values():
            found |= _collect_kinds(v)
    elif isinstance(node, list):
        for v in node:
            found |= _collect_kinds(v)
    return found


def _svc(llm, ctx, interactor, *, coordinator=None):
    return TurnService(
        config=Config(),
        llm=llm, coordinator=coordinator,
        handler_context=ctx, interactor=interactor,
    )


# ============ 用例一：新增 Discussion 放 Conclusion 前 ============

def test_case1_add_discussion_before_conclusion(tmp_path):
    session = _session(tmp_path)
    text = "新增 Discussion 章节讨论结果意义与局限，放 Conclusion 前"
    llm = MockLLMProvider(scripted=[
        _analyze(
            [("add_section", text, ["r1", "r2"])],
            reqs=[("r1", "新增 Discussion 章节", "target"),
                  ("r2", "讨论结果意义与局限", "goal")],
        ),
        _extract({
            "title": "Discussion",
            "purpose": "讨论结果的意义与局限",
            "structure_kind": "desc",
            "structure_desc": "意义 → 局限",
            "position": "before:conclusion",
        }),
        # add_section 正文生成 + 独立事实依据 Gate
        _DISCUSSION_BODY, _GROUNDED,
    ])
    it = _Interactor()
    ctx = HandlerContext(session=session, llm=llm, facts=_facts(tmp_path))
    svc = _svc(llm, ctx, it, coordinator=Coordinator())

    out = svc.handle_turn(text, session=session, facts=ctx.facts)

    assert out.report is not None, out.planning_exit
    assert out.report.plan_status is PlanStatus.DONE, out.report.operations[0].message
    assert out.report.operations[0].status is OperationStatus.SUCCEEDED
    # 抽槽齐全 → 不触发澄清追问（确认不算追问）
    assert it.field_asks == []
    assert it.confirm_calls == 1
    # 真的写进了 working copy，且插在 Conclusion 之前
    working = session.read_working()
    assert "Discussion" in working
    assert working.index("Discussion") < working.index("\\section{Conclusion}")


def _facts(tmp_path):
    from paper_agent.session.facts import SessionFacts

    return SessionFacts.load(str(tmp_path / "facts.json"))


# ============ 用例二：先润色 Introduction 再转 docx ============

def test_case2_polish_then_convert_ordered_and_no_clarification(tmp_path):
    session = _session(tmp_path)
    text = "先保守润色 Introduction、保留数字和引用，再转换为 docx"
    body = "\nOur method reaches 0.87 accuracy, improving on prior work \\cite{smith2020}.\n\n"
    llm = MockLLMProvider(scripted=[
        _analyze(
            [("polish", "先保守润色 Introduction、保留数字和引用", ["r1", "r2"]),
             ("convert_format", "再转换为 docx", ["r3"])],
            reqs=[("r1", "保守润色 Introduction", "target"),
                  ("r2", "保留数字和引用", "constraint"),
                  ("r3", "转换为 docx", "target")],
        ),
        _extract({"section_id": "introduction", "goal": "保守润色",
                  "constraints": ["保留数字和引用"]}),
        _extract({"target_ext": "docx"}),
        body,   # polish 生成（原样回显 → 过 Gate）
    ])
    it = _Interactor()
    ctx = HandlerContext(session=session, llm=llm, output_dir=str(tmp_path / "out"))
    svc = _svc(llm, ctx, it, coordinator=Coordinator())

    out = svc.handle_turn(text, session=session, facts=None)

    assert out.report is not None, out.planning_exit
    ops = out.report.operations
    # 多操作按序：polish 先、convert 后
    assert ops[0].status is OperationStatus.SUCCEEDED     # polish 成功
    assert len(ops) == 2
    # 直通不触发澄清
    assert it.field_asks == []


def test_case2_constraints_reach_polish_prompt(tmp_path):
    """约束真的进入生成 prompt（端到端验证执行绑定）。"""
    session = _session(tmp_path)
    body = "\nOur method reaches 0.87 accuracy, improving on prior work \\cite{smith2020}.\n\n"
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色 Introduction 保留数字", ["r1"])],
                 reqs=[("r1", "保留数字和引用", "constraint")]),
        _extract({"section_id": "introduction", "constraints": ["保留数字和引用"]}),
        body,
    ])
    it = _Interactor()
    ctx = HandlerContext(session=session, llm=llm)
    svc = _svc(llm, ctx, it, coordinator=Coordinator())
    svc.handle_turn("润色 Introduction 保留数字", session=session, facts=None)
    prompt = "\n".join(m.content for call in llm.calls for m in call)
    assert "保留数字和引用" in prompt


# ============ 计划被否决 → 不写 ============

def test_rejected_plan_does_not_write(tmp_path):
    session = _session(tmp_path)
    before = session.read_working()
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色引言", [])]),
        _extract({"section_id": "introduction"}),
    ])
    it = _Interactor(confirm=[ConfirmDecision("reject")])
    ctx = HandlerContext(session=session, llm=llm)
    svc = _svc(llm, ctx, it, coordinator=Coordinator())
    out = svc.handle_turn("润色引言", session=session, facts=None)
    assert out.report is None
    assert out.planning_exit.status is PlanStatus.REJECTED
    assert session.read_working() == before        # 逐字节不变


def test_planning_executes(tmp_path):
    session = _session(tmp_path)
    body = "\nOur method reaches 0.87 accuracy, improving on prior work \\cite{smith2020}.\n\n"
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色引言", [])]),
        _extract({"section_id": "introduction"}),
        body,
    ])
    ctx = HandlerContext(session=session, llm=llm)
    svc = _svc(llm, ctx, _Interactor(), coordinator=Coordinator())
    out = svc.handle_turn("润色引言", session=session, facts=None)
    assert out.report is not None and out.report.plan_status is PlanStatus.DONE


# ============ Trace 完整性（active 全旅程在一个 run 内）============

def test_active_trace_covers_whole_turn(tmp_path):
    session = _session(tmp_path)
    body = "\nOur method reaches 0.87 accuracy, improving on prior work \\cite{smith2020}.\n\n"
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色引言", [])]),
        _extract({"section_id": "introduction"}),
        body,
    ])
    ctx = HandlerContext(session=session, llm=llm)
    runs_dir = tmp_path / "runs"
    coordinator = Coordinator(enable_trace=True, runs_dir=str(runs_dir))
    svc = _svc(llm, ctx, _Interactor(), coordinator=coordinator)
    out = svc.handle_turn("润色引言", session=session, facts=None)

    assert out.run_report is not None
    trace_path = runs_dir / out.run_report.run_id / "trace.json"
    assert trace_path.is_file()
    data = json.loads(trace_path.read_text(encoding="utf-8"))
    kinds = _collect_kinds(data)
    # 一个 run 覆盖整段旅程：确认（规划期）+ 执行结果（执行期）同在一份 trace
    assert "plan_confirmed" in kinds
    assert "operation_result" in kinds
    assert "plan_result" in kinds
    assert out.run_report.run_status == "completed"
    assert out.run_report.working_copy_status == "updated"
    assert data["working_copy_status"] == "updated"
    # 原始用户文本不落盘
    assert "润色引言" not in trace_path.read_text(encoding="utf-8")


def test_readonly_fallback_stays_in_the_planning_run(tmp_path):
    """一次自然语言消息只有一个 run，Planning 与 Ask 事件在同一棵 trace。"""
    session = _session(tmp_path)
    before = session.read_working()
    question = "这篇论文有哪些章节？"
    llm = MockLLMProvider(scripted=[
        _analyze([], unmapped=[question]),
        "这篇论文包含 Introduction、Method 和 Conclusion 三个章节。",
    ])
    ctx = HandlerContext(session=session, llm=llm)
    runs_dir = tmp_path / "runs"
    coordinator = Coordinator(enable_trace=True, runs_dir=str(runs_dir))
    svc = _svc(llm, ctx, _Interactor(), coordinator=coordinator)

    out = svc.handle_turn(question, session=session, facts=None)

    assert out.planning_exit is not None
    assert out.planning_exit.status is PlanStatus.NO_ACTION
    assert out.readonly_result is not None and out.readonly_result.ok
    assert "Introduction" in out.readonly_result.answer
    assert session.read_working() == before
    assert out.run_report.working_copy_status == "unchanged"

    traces = list(runs_dir.glob("*/trace.json"))
    assert len(traces) == 1, "一条自然语言消息不应拆成 Planning/Ask 两份 trace"
    data = json.loads(traces[0].read_text(encoding="utf-8"))
    kinds = _collect_kinds(data)
    assert {"operation_analysis", "ask_turn_start", "ask_turn_end"} <= kinds
    assert data["working_copy_status"] == "unchanged"
    # 原始问句只出 hash/length，不进入 trace。
    assert question not in traces[0].read_text(encoding="utf-8")


# ============ P0：系统异常不得报成「用户取消」 ============

class _BoomInteractor(_Interactor):
    def confirm_plan(self, summary):
        raise RuntimeError("interactor exploded")


def test_system_exception_reported_as_failed_not_cancelled(tmp_path):
    """Coordinator 区分 cancelled/failed，TurnService 必须分流。

    历史 bug：只要 run_report.value is None 就调 to_cancelled_report()，
    真实代码异常会被用户看成「已取消」。
    """
    session = _session(tmp_path)
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色引言", [])]),
        _extract({"section_id": "introduction"}),
    ])
    ctx = HandlerContext(session=session, llm=llm)
    svc = _svc(llm, ctx, _BoomInteractor(), coordinator=Coordinator())

    out = svc.handle_turn("润色引言", session=session, facts=None)

    assert out.run_report.run_status == "failed"
    assert out.report is not None
    assert out.report.plan_status is PlanStatus.FAILED          # 不是 cancelled
    assert out.report.failure_category is not None              # 保留失败类别
    assert any("执行异常" in w for w in out.warnings)


def test_cancel_still_reported_as_cancelled(tmp_path):
    from paper_agent.errors import OperationCancelled

    class _CancelInteractor(_Interactor):
        def confirm_plan(self, summary):
            raise OperationCancelled()

    session = _session(tmp_path)
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色引言", [])]),
        _extract({"section_id": "introduction"}),
    ])
    ctx = HandlerContext(session=session, llm=llm)
    svc = _svc(llm, ctx, _CancelInteractor(), coordinator=Coordinator())
    out = svc.handle_turn("润色引言", session=session, facts=None)

    assert out.run_report.run_status == "cancelled"
    assert out.report.plan_status is PlanStatus.CANCELLED
    assert out.warnings == []


def _run_turn_that_writes_then_raises(tmp_path, monkeypatch, error):
    """用真实 TurnService + Coordinator 覆盖执行器异常边界。"""
    from paper_agent.runtime import plan_executor as plan_executor_module

    session = _session(tmp_path)
    before = session.read_working()
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色引言", [])]),
        _extract({"section_id": "introduction"}),
    ])
    ctx = HandlerContext(session=session, llm=llm)
    runs_dir = tmp_path / "runs"

    def write_then_raise(plan, state, handler_context, **kwargs):
        handler_context.session.write_working(
            handler_context.session.read_working() + "\n% partial write\n",
            bus=kwargs.get("bus"),
        )
        raise error

    monkeypatch.setattr(plan_executor_module, "execute_plan", write_then_raise)
    svc = _svc(
        llm,
        ctx,
        _Interactor(),
        coordinator=Coordinator(enable_trace=True, runs_dir=str(runs_dir)),
    )

    out = svc.handle_turn("润色引言", session=session, facts=None)
    trace_path = runs_dir / out.run_report.run_id / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert session.read_working() != before
    return out, trace


def test_write_then_cancel_keeps_updated_working_copy_status(tmp_path, monkeypatch):
    from paper_agent.errors import OperationCancelled

    out, trace = _run_turn_that_writes_then_raises(
        tmp_path,
        monkeypatch,
        OperationCancelled(),
    )

    assert out.run_report.run_status == "cancelled"
    assert out.run_report.working_copy_status == "updated"
    assert trace["working_copy_status"] == "updated"


def test_write_then_exception_keeps_updated_working_copy_status(tmp_path, monkeypatch):
    out, trace = _run_turn_that_writes_then_raises(
        tmp_path,
        monkeypatch,
        RuntimeError("failed after write"),
    )

    assert out.run_report.run_status == "failed"
    assert out.run_report.working_copy_status == "updated"
    assert trace["working_copy_status"] == "updated"


# ============ P0：空计划端到端不报 done ============

def test_unsupported_request_reports_no_action(tmp_path):
    session = _session(tmp_path)
    before = session.read_working()
    llm = MockLLMProvider(scripted=[
        _analyze([], unmapped=["帮我查一下这篇论文的重复率"]),
        "当前只读工具无法计算重复率。",
    ])
    ctx = HandlerContext(session=session, llm=llm)
    svc = _svc(llm, ctx, _Interactor(), coordinator=Coordinator())
    out = svc.handle_turn("帮我查一下这篇论文的重复率", session=session, facts=None)

    assert out.report is None
    assert out.planning_exit.status is PlanStatus.NO_ACTION
    assert out.planning_exit.route_to_readonly is True
    assert out.readonly_result is not None
    assert session.read_working() == before        # 逐字节未变
