"""polish SOP（卡 1）：把访谈计划落地成对 working copy 的一次受控润色。

流程：读取目标章节原文 → Generator（生成+gate+重试）→ 通过则原地 splice 写回
working copy → 返回 diff 供展示。源文件不动。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from paper_agent.gates.diff_gate import PolishGate
from paper_agent.generate.generator import run_polish
from paper_agent.intents.plans import PolishPlan
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.section_index import SectionIndex


@dataclass
class PolishResult:
    ok: bool
    section_id: str
    attempts: int
    diff: str = ""
    message: str = ""


def unified_diff(before: str, after: str, label: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{label} (原)",
        tofile=f"{label} (润色后)",
    )
    return "".join(lines)


def execute_polish(
    session: FileBackedSession,
    plan: PolishPlan,
    llm: LLMProvider,
    *,
    max_char_change_ratio: float = 0.30,
    max_retry: int = 2,
    bus=None,
    cancel_token=None,
    stream: bool = False,
) -> PolishResult:
    # 每次基于最新 working 重建索引，保证偏移正确
    index = SectionIndex(session.read_working(), session.ext)
    section = index.find(plan.section_id)
    if section is None:
        return PolishResult(
            ok=False, section_id=plan.section_id, attempts=0,
            message=f"章节不存在：{plan.section_id}",
        )

    original_body = index.body_of(section)
    gate = PolishGate(session.ext, max_char_change_ratio=max_char_change_ratio)
    outcome = run_polish(
        original_body, plan.goal, llm, gate,
        max_retry=max_retry, bus=bus, cancel_token=cancel_token, stream=stream,
        constraints=list(getattr(plan, "constraints", ()) or ()) or None,
        source_text=getattr(plan, "source_text", "") or "",
    )

    if not outcome.ok:
        return PolishResult(
            ok=False,
            section_id=section.id,
            attempts=outcome.attempts,
            message=(
                f"我尝试了 {outcome.attempts} 次都没能只改语言而不动数字/引用/公式。\n"
                "最后一次违反：\n"
                + "\n".join(f"  - {v}" for v in outcome.last_violations)
                + "\n\n这段可能实际需要 rewrite 模式（Week 3 提供）。"
            ),
        )

    new_full = index.splice(section, outcome.text or "")
    # 带上章节 id：不传 message 时 git 只留默认的 "edit"，事后看不出润色了哪一章
    # （add_section / rewrite 一直是带的，这里漏了）。
    session.write_working(new_full, message=f"polish {section.id}", bus=bus)
    diff = unified_diff(original_body, outcome.text or "", section.title)
    return PolishResult(
        ok=True,
        section_id=section.id,
        attempts=outcome.attempts,
        diff=diff,
        message=f"已润色章节「{section.title}」（{outcome.attempts} 次生成通过校验）。",
    )


__all__ = ["PolishResult", "execute_polish", "unified_diff"]
