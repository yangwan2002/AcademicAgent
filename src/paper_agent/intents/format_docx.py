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


def _format_target(session) -> str:
    """返回可排版的 docx 路径；不可排版返回空串。

    docx 会话的 working copy 就是 docx 副本本身，排版直接改它 —— 正文改动走段落
    就地写回，两者作用于同一个文件、互不覆盖。
    """
    if session.ext == "docx":
        return session.working_path
    return ""


def execute_format(session: FileBackedSession, message: str, *, ops=None) -> FormatResult:
    """执行排版。

    ``ops``（可选）：已结构化的 `list[FormatOp]`。intent-planning 的
    `format_docx.operations` 槽经此**直接**落到文档（Task 19 绑定
    `execute_format.ops`），避免「FormatOp → 中文 → 再解析」的有损往返。
    不传时行为与旧调用完全一致（用 `parse_format_ops(message)` 规则解析）。
    """
    target = _format_target(session)
    if not target:
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

    ops = list(ops) if ops else parse_format_ops(message)
    if not ops:
        return FormatResult(
            ok=False,
            message="没识别到可执行的格式操作。可试试：字体/字号/行距/对齐/分栏/三线表。",
        )

    session.commit("format baseline")
    before = snapshot_docx(target)
    try:
        apply_ops(target, ops)
    except Exception as exc:  # noqa: BLE001
        session.discard_changes()
        return FormatResult(ok=False, message=f"应用格式操作失败，已回滚：{exc}")

    result = FormatGate().check(before, target)
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
