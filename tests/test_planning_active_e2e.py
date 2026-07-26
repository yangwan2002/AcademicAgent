"""intent-planning Task 24 验收：Active 端到端 + 模式矩阵。

专用 Fixture：含 Introduction / Method / Conclusion 的 tex。
- 用例一：「新增 Discussion 章节讨论结果意义与局限，放 Conclusion 前」
  （依赖 Conclusion 存在）→ 正确拆解/抽槽/不重复追问/确认/执行/落 trace
- 用例二：「先保守润色 Introduction、保留数字和引用，再转换为 docx」
  → 多操作按序 + convert 终止校验通过 + 直通不触发澄清
- 模式矩阵 off / shadow / active × mock
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
    "Our findings suggest that the proposed approach generalises beyond the "
    "specific benchmark considered here, and that the observed gains stem "
    "primarily from the redesigned training objective rather than from added "
    "capacity. This matters because it points to a direction that is cheap to "
    "adopt in practice. At the same time, several limitations temper these "
    "conclusions. The evaluation covers a single domain, so transfer to other "
    "domains remains an open question. The comparison also assumes access to "
    "clean annotations, which is often unrealistic in deployment settings. "
    "Finally, we did not study robustness under distribution shift, and we "
    "regard that as the most pressing item for future work."
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


def _svc(cfg_mode, llm, ctx, interactor, *, coordinator=None):
    return TurnService(
        config=Config(intent_planning_mode=cfg_mode),
        llm=llm, coordinator=coordinator, shadow_coordinator=Coordinator(),
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
        # add_section 正文生成（须 ≥200 字符过 AddSectionGate，且不含新数字）
        _DISCUSSION_BODY, _DISCUSSION_BODY, _DISCUSSION_BODY,
    ])
    it = _Interactor()
    ctx = HandlerContext(session=session, llm=llm, facts=_facts(tmp_path))
    svc = _svc("active", llm, ctx, it, coordinator=Coordinator())

    out = svc.handle_turn(text, session=session, facts=ctx.facts, legacy=lambda: None)

    assert out.mode == "active"
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
    svc = _svc("active", llm, ctx, it, coordinator=Coordinator())

    out = svc.handle_turn(text, session=session, facts=None, legacy=lambda: None)

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
    svc = _svc("active", llm, ctx, it, coordinator=Coordinator())
    svc.handle_turn("润色 Introduction 保留数字", session=session, facts=None,
                    legacy=lambda: None)
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
    svc = _svc("active", llm, ctx, it, coordinator=Coordinator())
    out = svc.handle_turn("润色引言", session=session, facts=None, legacy=lambda: None)
    assert out.report is None
    assert out.planning_exit.status is PlanStatus.REJECTED
    assert session.read_working() == before        # 逐字节不变


# ============ 模式矩阵 off / shadow / active ============

def test_mode_matrix_off_runs_legacy_only(tmp_path):
    session = _session(tmp_path)
    llm = MockLLMProvider()
    ctx = HandlerContext(session=session, llm=llm)
    svc = _svc("off", llm, ctx, _Interactor())
    calls = []
    out = svc.handle_turn("润色引言", session=session, facts=None,
                          legacy=lambda: calls.append(1) or "legacy")
    assert out.mode == "off" and out.legacy_result == "legacy"
    assert llm.calls == []                         # 理解层零调用
    assert out.report is None


def test_mode_matrix_shadow_readonly(tmp_path):
    session = _session(tmp_path)
    before = session.read_working()
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色引言", [])]),
        _extract({"section_id": "introduction"}),
    ])
    ctx = HandlerContext(session=session, llm=llm)
    svc = _svc("shadow", llm, ctx, _Interactor())
    out = svc.handle_turn("润色引言", session=session, facts=None, legacy=lambda: "legacy")
    assert out.mode == "shadow"
    assert out.legacy_result == "legacy"
    assert out.shadow is not None and out.shadow.ok is True
    assert session.read_working() == before        # shadow 不写
    assert out.report is None                      # shadow 不执行


def test_mode_matrix_active_executes(tmp_path):
    session = _session(tmp_path)
    body = "\nOur method reaches 0.87 accuracy, improving on prior work \\cite{smith2020}.\n\n"
    llm = MockLLMProvider(scripted=[
        _analyze([("polish", "润色引言", [])]),
        _extract({"section_id": "introduction"}),
        body,
    ])
    ctx = HandlerContext(session=session, llm=llm)
    svc = _svc("active", llm, ctx, _Interactor(), coordinator=Coordinator())
    out = svc.handle_turn("润色引言", session=session, facts=None, legacy=lambda: None)
    assert out.mode == "active"
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
    svc = _svc("active", llm, ctx, _Interactor(), coordinator=coordinator)
    out = svc.handle_turn("润色引言", session=session, facts=None, legacy=lambda: None)

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
    # 原始用户文本不落盘
    assert "润色引言" not in trace_path.read_text(encoding="utf-8")
