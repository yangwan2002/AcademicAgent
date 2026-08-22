"""convert_format SOP（卡 4）。

把 working copy 转成目标格式，产出**新文件**（不改 working copy），
并做转换后校验（章节/公式为关键指标，引用/表格/图为参考指标）。

转换是确定性的：pandoc + python-docx，无 LLM、无重试。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from paper_agent.intents.plans import ConvertPlan
from paper_agent.session.file_session import FileBackedSession
from paper_agent.tools.convert import ConvertError, convert_document
from paper_agent.tools.doc_stats import stats_for, validate_conversion


@dataclass
class ConvertResult:
    ok: bool
    output_path: str = ""
    message: str = ""
    report_lines: list[str] = field(default_factory=list)


def execute_convert(
    session: FileBackedSession,
    plan: ConvertPlan,
    *,
    output_dir: str = "output",
) -> ConvertResult:
    source_ext = session.ext
    if plan.target_ext == source_ext and not plan.presets:
        return ConvertResult(
            ok=False,
            message=f"源已是 .{source_ext}，无需转换。",
        )

    # 转换用 working copy 作为源（含用户已接受的润色）
    source_path = session.working_path
    try:
        source_stats = stats_for(source_path, source_ext)
        output_path = convert_document(
            source_path, plan.target_ext, plan.presets, output_dir=output_dir
        )
        target_stats = stats_for(output_path, plan.target_ext)
    except ConvertError as exc:
        return ConvertResult(ok=False, message=str(exc))
    except Exception as exc:  # noqa: BLE001
        return ConvertResult(ok=False, message=f"转换后校验读取失败：{exc}")

    validation = validate_conversion(source_stats, target_stats)
    preset_note = (
        f"，已应用预设：{', '.join(plan.presets)}" if plan.presets else ""
    )
    # 用干净文件名重命名到源文件同名 stem
    final_path = _final_output_path(session.source_path, plan.target_ext, output_dir)
    if final_path != output_path:
        os.replace(output_path, final_path)
        output_path = final_path

    if validation.ok:
        msg = f"已生成 {output_path}{preset_note}。关键指标（章节/公式）校验通过。"
    else:
        msg = (
            f"已生成 {output_path}{preset_note}，但**关键指标不一致**——"
            "pandoc 转换可能有信息损失，请核对。"
        )
    return ConvertResult(
        ok=validation.ok,
        output_path=output_path,
        message=msg,
        report_lines=validation.lines,
    )


def _final_output_path(source_path: str, target_ext: str, output_dir: str) -> str:
    stem = os.path.splitext(os.path.basename(source_path))[0]
    return os.path.join(output_dir, f"{stem}.{target_ext}")


__all__ = ["ConvertResult", "execute_convert"]
