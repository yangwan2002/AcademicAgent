"""format_docx SOP（卡 5）：只改排版格式，正文一字不动。

流程：解析格式指令 → 提交基线 → 应用 Layer 1 操作 → FormatGate（正文 hash 前后
一致）→ 通过则 commit；不通过则 git 回滚。docx 是二进制，直接操作文件 + git 兜底。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.gates.format_gate import FormatGate, snapshot_docx
from paper_agent.intents.format_parser import parse_format_ops
from paper_agent.session.file_session import FileBackedSession
from paper_agent.tools.docx_format import apply_ops


@dataclass
class FormatResult:
    ok: bool
    message: str = ""
    applied: list[str] = field(default_factory=list)


def execute_format(session: FileBackedSession, message: str) -> FormatResult:
    if session.ext != "docx":
        return FormatResult(
            ok=False,
            message=(
                f"format_docx 目前只作用于 .docx（当前 .{session.ext}）。"
                "若要排版，请先用 convert_format 转成 docx。"
            ),
        )
    if not session.git_enabled:
        return FormatResult(
            ok=False, message="格式操作需要 git 支持（用于失败回滚），当前不可用。",
        )

    ops = parse_format_ops(message)
    if not ops:
        return FormatResult(
            ok=False,
            message="没识别到可执行的格式操作。可试试：字体/字号/行距/对齐/分栏/三线表。",
        )

    session.commit("format baseline")
    before = snapshot_docx(session.working_path)
    try:
        apply_ops(session.working_path, ops)
    except Exception as exc:  # noqa: BLE001
        session.discard_changes()
        return FormatResult(ok=False, message=f"应用格式操作失败，已回滚：{exc}")

    result = FormatGate().check(before, session.working_path)
    if not result.passed:
        session.discard_changes()
        return FormatResult(
            ok=False,
            message="格式操作意外影响了正文/结构，已回滚全部改动：\n"
            + "\n".join(f"  - {v}" for v in result.violations),
        )

    session.commit(f"format: {len(ops)} ops")
    return FormatResult(
        ok=True,
        applied=[op.label for op in ops],
        message=f"已执行 {len(ops)} 项格式操作，正文结构无损校验通过。",
    )


__all__ = ["FormatResult", "execute_format"]
