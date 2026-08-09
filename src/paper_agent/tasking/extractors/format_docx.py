"""Format_Docx 抽取器（Task 12）。

format_docx 槽位（design 附录）：
- `operations`（必填，缺则追问）：**受限** `list[FormatOp]`——只允许现有
  `docx_format.REGISTRY` 支持的操作类型（字体/字号/加粗/斜体/对齐/行距/首行缩进/
  分栏/页边距/页面方向/三线表）。含任何非法类型 → 整槽标 invalid（不静默丢弃）。
- `scope`（可选）：文档级作用域（默认 document，PlanBuilder 填）。

排版是确定性操作，现有 `parse_format_ops()` 用规则解析。抽取器的价值只在于：把
用户自然语言里的排版诉求归一成**结构化、类型受限**的 op 列表，让下游 Handler
（Task 19）可直接喂给 `apply_ops`，而不是把非法/幻觉操作带进执行。

`REGISTRY` 惰性导入：避免整个 planning 导入链在不做排版时也吃上 python-docx。
"""

from __future__ import annotations

from typing import Any

from paper_agent.intents.router import Intent
from paper_agent.tasking.contracts import FieldStatus, SlotSource, SlotValue
from paper_agent.tasking.extractors.base import PreExtractor, _merge_ids
from paper_agent.tasking.intent_spec import FORMAT_DOCX_SPEC, IntentSpec, SlotSpec

_LEGAL_SCOPES = ("all", "body", "headings", "heading1")


def _legal_op_names() -> set[str]:
    """合法 op 类型 = 现有 docx_format REGISTRY 的 keys（单一真相源，惰性导入）。"""
    from paper_agent.tools.docx_format import REGISTRY

    return set(REGISTRY.keys())


def _to_format_ops(raw_value: Any) -> list:
    """把 LLM 给的 op dict 列表转成受限 list[FormatOp]；非法类型抛 ValueError。"""
    from paper_agent.tools.docx_format import FormatOp

    if not isinstance(raw_value, list):
        raise ValueError("operations 必须是列表")
    legal = _legal_op_names()
    ops = []
    for item in raw_value:
        if not isinstance(item, dict):
            raise ValueError("每个 operation 必须是对象")
        name = str(item.get("name", ""))
        if name not in legal:
            raise ValueError(f"不支持的格式操作类型：{name!r}")
        scope = str(item.get("scope", "all"))
        if scope not in _LEGAL_SCOPES:
            scope = "all"
        vb = item.get("verbatim")
        ops.append(FormatOp(
            name=name, value=item.get("value"), scope=scope,
            verbatim=str(vb).strip() if isinstance(vb, str) else "",
        ))
    return ops


class FormatDocxExtractor(PreExtractor):
    @property
    def intent(self) -> Intent:
        return Intent.FORMAT_DOCX

    def _spec(self) -> IntentSpec:
        return FORMAT_DOCX_SPEC

    def _coerce_slot(
        self,
        name: str,
        slot_spec: SlotSpec,
        raw: Any,
        reported_verbatim: str | None = None,
        declared_ids: tuple[str, ...] = (),
    ) -> SlotValue:
        # 只有 operations 槽走受限 FormatOp 转换；其余槽走通用逻辑。
        if name != "operations":
            return super()._coerce_slot(
                name, slot_spec, raw, reported_verbatim, declared_ids=declared_ids,
            )

        if not isinstance(raw, dict):
            raw = {"value": raw}

        status_str = str(raw.get("status", "present"))
        try:
            status = FieldStatus(status_str)
        except ValueError:
            status = FieldStatus.AMBIGUOUS
        if status not in (
            FieldStatus.PRESENT, FieldStatus.USER_ABSENT,
            FieldStatus.AMBIGUOUS, FieldStatus.INVALID,
        ):
            status = FieldStatus.AMBIGUOUS

        req_ids = _merge_ids(raw.get("requirement_ids"), declared_ids)
        raw_value = raw.get("value")
        verbatim = raw.get("verbatim") or reported_verbatim

        if status is not FieldStatus.PRESENT:
            keep = raw_value if status in (FieldStatus.AMBIGUOUS, FieldStatus.INVALID) else None
            return SlotValue(
                value=keep, field_status=status, source=None,
                requirement_ids=req_ids, verbatim=verbatim,
            )

        # present：尝试转受限 FormatOp 列表；含非法类型 → invalid（保留原始供澄清）
        try:
            ops = _to_format_ops(raw_value)
        except ValueError:
            return SlotValue(
                value=raw_value, field_status=FieldStatus.INVALID, source=None,
                requirement_ids=req_ids, verbatim=verbatim,
            )
        return SlotValue(
            value=ops, field_status=FieldStatus.PRESENT, source=SlotSource.USER_EXPLICIT,
            requirement_ids=req_ids, verbatim=verbatim,
        )


__all__ = ["FormatDocxExtractor"]
