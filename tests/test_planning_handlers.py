"""intent-planning Task 19 验收：五个 IntentHandler。

重点是**验证最终执行效果**，不只验证「Handler 读了字段」：
- polish.constraints 真的进入生成 prompt（从 MockLLM 收到的 messages 里断言）
- format_docx.operations 真的落到文档（真实 docx + git，字号确被改）
- Gate 拒 → rejected；ok → succeeded；系统异常 → failed
- convert 不改 working copy、记录 generated_outputs
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.runtime.handlers import HandlerContext, handler_for
from paper_agent.session.file_session import FileBackedSession
from paper_agent.tasking.contracts import (
    FieldStatus,
    Operation,
    OperationStatus,
    SlotSource,
    SlotValue,
)

_TEX = "\\section{Introduction}\nThe accuracy is 0.87 and we cite \\cite{a}.\n"


def _present(value):
    return SlotValue(value=value, field_status=FieldStatus.PRESENT,
                     source=SlotSource.USER_EXPLICIT)


def _op(intent: Intent, slots: dict, op_id="op1") -> Operation:
    return Operation(operation_id=op_id, intent=intent, slots=slots)


def _session(tmp_path, name="p.tex", text=_TEX) -> FileBackedSession:
    src = tmp_path / name
    src.write_text(text, encoding="utf-8")
    return FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))


def _all_prompt_text(llm: MockLLMProvider) -> str:
    return "\n".join(m.content for call in llm.calls for m in call)


# ============ 注册表 ============

def test_handlers_registered_for_five_intents():
    for intent in (Intent.POLISH, Intent.REWRITE, Intent.ADD_SECTION,
                   Intent.CONVERT_FORMAT, Intent.FORMAT_DOCX):
        h = handler_for(intent)
        assert h is not None and h.intent is intent


def test_unregistered_intent_returns_none():
    assert handler_for(Intent.UNCLEAR) is None


# ============ polish：constraints 真进 prompt（执行效果）============

def test_polish_constraints_reach_generation_prompt(tmp_path):
    session = _session(tmp_path)
    # mock 原样回显原文 → 通过 PolishGate（数字/引用不变）
    body = "\nThe accuracy is 0.87 and we cite \\cite{a}.\n"
    llm = MockLLMProvider(scripted=[body])
    ctx = HandlerContext(session=session, llm=llm)
    op = _op(Intent.POLISH, {
        "section_id": _present("introduction"),
        "goal": _present("保守润色"),
        "constraints": _present(["不要改动任何数字", "保留全部引用"]),
    })
    res = handler_for(Intent.POLISH).execute(op, ctx)
    assert res.status is OperationStatus.SUCCEEDED
    assert res.changed_working_copy is True
    # 关键：约束真的出现在送给模型的 prompt 里
    prompt = _all_prompt_text(llm)
    assert "不要改动任何数字" in prompt
    assert "保留全部引用" in prompt


def test_polish_without_constraints_prompt_has_no_constraint_block(tmp_path):
    session = _session(tmp_path)
    body = "\nThe accuracy is 0.87 and we cite \\cite{a}.\n"
    llm = MockLLMProvider(scripted=[body])
    ctx = HandlerContext(session=session, llm=llm)
    op = _op(Intent.POLISH, {"section_id": _present("introduction")})
    handler_for(Intent.POLISH).execute(op, ctx)
    assert "用户额外要求的硬约束" not in _all_prompt_text(llm)


def test_polish_gate_rejection_maps_to_rejected(tmp_path):
    session = _session(tmp_path)
    # 篡改数字 → PolishGate 必拒（重试耗尽后 ok=False）
    bad = "\nThe accuracy is 0.99 and we cite \\cite{a}.\n"
    llm = MockLLMProvider(scripted=[bad, bad, bad])
    ctx = HandlerContext(session=session, llm=llm, max_retry=2)
    op = _op(Intent.POLISH, {"section_id": _present("introduction")})
    res = handler_for(Intent.POLISH).execute(op, ctx)
    assert res.status is OperationStatus.REJECTED     # 不是 succeeded、不是 failed
    assert res.changed_working_copy is False


def test_polish_missing_section_maps_to_rejected(tmp_path):
    session = _session(tmp_path)
    ctx = HandlerContext(session=session, llm=MockLLMProvider())
    op = _op(Intent.POLISH, {"section_id": _present("nonexistent")})
    res = handler_for(Intent.POLISH).execute(op, ctx)
    assert res.status is OperationStatus.REJECTED


class _FailingLLM(LLMProvider):
    def complete(self, messages, **opts):
        raise RuntimeError("provider exploded")

    def stream(self, messages, *, cancel_token=None, **opts):
        yield from ()


def test_system_exception_maps_to_failed(tmp_path):
    session = _session(tmp_path)
    ctx = HandlerContext(session=session, llm=_FailingLLM())
    op = _op(Intent.POLISH, {"section_id": _present("introduction")})
    res = handler_for(Intent.POLISH).execute(op, ctx)
    assert res.status is OperationStatus.FAILED       # 系统异常 ≠ rejected
    assert "RuntimeError" in res.message


# ============ convert：不改 working copy、记录产物 ============

def test_convert_same_format_rejected_and_no_working_change(tmp_path):
    session = _session(tmp_path)
    ctx = HandlerContext(session=session, output_dir=str(tmp_path / "out"))
    op = _op(Intent.CONVERT_FORMAT, {"target_ext": _present("tex")})
    res = handler_for(Intent.CONVERT_FORMAT).execute(op, ctx)
    assert res.status is OperationStatus.REJECTED     # 同格式 → 业务失败
    assert res.changed_working_copy is False
    assert res.generated_outputs == ()


# ============ format_docx：FormatOp 真落到文档（执行效果）============

def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def test_format_ops_actually_applied_to_document(tmp_path):
    docx = pytest.importorskip("docx")
    if not _git_available():
        pytest.skip("需要 git")
    from paper_agent.intents.format_docx import _format_target
    from paper_agent.tools.docx_format import FormatOp

    path = tmp_path / "d.docx"
    d = docx.Document()
    d.add_heading("Introduction", level=1)
    d.add_paragraph("body text 0.87")
    d.save(str(path))

    session = FileBackedSession(str(path), workspace_root=str(tmp_path / "ws"))
    if not session.git_enabled:
        pytest.skip("需要 git 支持")

    ctx = HandlerContext(session=session)
    ops = [FormatOp("set_font_size", 20, "all")]
    op = _op(Intent.FORMAT_DOCX, {"operations": _present(ops)})
    res = handler_for(Intent.FORMAT_DOCX).execute(op, ctx)

    assert res.status is OperationStatus.SUCCEEDED, res.message
    # 关键：字号真的被改到文档里（读回排版载体验证）。
    # docx 会话的正文走 markdown 投影、排版走二进制副本，二者分工不同；
    # 排版断言必须读那个二进制副本。
    out = docx.Document(_format_target(session))
    sizes = [r.font.size.pt for p in out.paragraphs for r in p.runs if r.font.size]
    assert sizes and all(s == 20 for s in sizes)


def test_format_non_docx_rejected(tmp_path):
    from paper_agent.tools.docx_format import FormatOp
    session = _session(tmp_path)          # .tex
    ctx = HandlerContext(session=session)
    op = _op(Intent.FORMAT_DOCX, {"operations": _present([FormatOp("set_bold", True, "all")])})
    res = handler_for(Intent.FORMAT_DOCX).execute(op, ctx)
    assert res.status is OperationStatus.REJECTED


def _present_vb(value, verbatim):
    return SlotValue(value=value, field_status=FieldStatus.PRESENT,
                     source=SlotSource.USER_EXPLICIT, verbatim=verbatim)


def test_verbatim_fallback_applies_format_when_ops_empty(tmp_path):
    """`SlotValue.verbatim` 是**执行兜底输入**，不只是排障字段。

    `FormatDocxHandler.execute` 在结构化 ops 为空时，把 verbatim 当自然语言喂给
    `parse_format_ops()` 正则重抠一遍。此前无人测这条路 —— 而它会真改文档，
    删掉/规范化这个字段会静默改变产出。

    这条同时钉住 verbatim「必须逐字原话」的约束：正则认的是「四号」这类中文说法，
    喂规范化后的值（比如 `14.0`）反而抠不出东西。
    """
    docx = pytest.importorskip("docx")
    if not _git_available():
        pytest.skip("需要 git")
    from paper_agent.intents.format_docx import _format_target

    path = tmp_path / "d.docx"
    d = docx.Document()
    d.add_paragraph("body text 0.87")
    d.save(str(path))

    session = FileBackedSession(str(path), workspace_root=str(tmp_path / "ws"))
    if not session.git_enabled:
        pytest.skip("需要 git 支持")

    ctx = HandlerContext(session=session)
    # ops 为空 + verbatim 带原话 → 走 parse_format_ops 兜底
    op = _op(Intent.FORMAT_DOCX, {"operations": _present_vb([], "全文四号字")})
    res = handler_for(Intent.FORMAT_DOCX).execute(op, ctx)

    assert res.status is OperationStatus.SUCCEEDED, res.message
    out = docx.Document(_format_target(session))
    sizes = [r.font.size.pt for p in out.paragraphs for r in p.runs if r.font.size]
    assert sizes and all(s == 14 for s in sizes), f"四号=14pt 未落地：{sizes}"


def test_structured_ops_win_over_verbatim_fallback(tmp_path):
    """两者都在时，结构化 ops 优先 —— 兜底不得覆盖模型的结构化结果。

    避免「FormatOp → 中文 → 再解析」的有损往返（见 `execute_format` 文档）。
    """
    docx = pytest.importorskip("docx")
    if not _git_available():
        pytest.skip("需要 git")
    from paper_agent.intents.format_docx import _format_target
    from paper_agent.tools.docx_format import FormatOp

    path = tmp_path / "d.docx"
    d = docx.Document()
    d.add_paragraph("body text")
    d.save(str(path))

    session = FileBackedSession(str(path), workspace_root=str(tmp_path / "ws"))
    if not session.git_enabled:
        pytest.skip("需要 git 支持")

    ctx = HandlerContext(session=session)
    # 结构化说 20pt，verbatim 说四号（14pt）→ 必须听结构化的
    op = _op(Intent.FORMAT_DOCX,
             {"operations": _present_vb([FormatOp("set_font_size", 20, "all")], "全文四号字")})
    res = handler_for(Intent.FORMAT_DOCX).execute(op, ctx)

    assert res.status is OperationStatus.SUCCEEDED, res.message
    out = docx.Document(_format_target(session))
    sizes = [r.font.size.pt for p in out.paragraphs for r in p.runs if r.font.size]
    assert sizes and all(s == 20 for s in sizes), f"结构化 ops 被兜底覆盖：{sizes}"


# ============ 每个 IntentSpec 可执行 Slot 确被 Handler 消费 ============

def test_every_spec_slot_is_consumed_by_handler_source():
    """静态校验：IntentSpec 的每个槽名都出现在对应 Handler 的源码里。

    防「抽取了但 Handler 根本没读」——完整运行时校验在 Task 24。
    """
    import inspect

    from paper_agent.runtime import handlers as H
    from paper_agent.tasking.intent_spec import INTENT_SPECS

    src_by_intent = {
        Intent.POLISH: inspect.getsource(H.PolishHandler),
        Intent.REWRITE: inspect.getsource(H.RewriteHandler),
        Intent.ADD_SECTION: inspect.getsource(H.AddSectionHandler),
        Intent.CONVERT_FORMAT: inspect.getsource(H.ConvertHandler),
        Intent.FORMAT_DOCX: inspect.getsource(H.FormatDocxHandler),
    }
    for intent, spec in INTENT_SPECS.items():
        src = src_by_intent[intent]
        for slot_name in spec.slots:
            if intent is Intent.FORMAT_DOCX and slot_name == "scope":
                continue  # scope 由每个 FormatOp 自带，不单独传
            assert slot_name in src, (intent, slot_name)
