"""IntentHandlers（Task 19）：把 Operation 的槽位喂进现有 `execute_*`。

铁律（design D8 / Property 10）：
- **薄适配、不重写 SOP**：每个 Handler 只做「Slot → 现有 *Plan / execute_* 参数」的
  翻译，执行逻辑仍在 `intents/*.py`。
- **业务失败翻译**：现有 `execute_*` 用 `Result(ok=False)` 表达业务失败（含 Gate 拒），
  不抛异常。故 `ok=True → succeeded`、否则 `rejected`；**不看 run_status**（否则 Gate
  失败会被记成 run completed / 计划成功）。系统异常 → `failed`。
- **抽取的槽位必须真正产生执行效果**（防「提取了但没执行」）：
  - `polish.constraints` → `PolishPlan.constraints` → `run_polish(constraints=...)`
    → `build_polish_messages` 的硬约束块（真影响生成）。
  - `format_docx.operations` → `execute_format(ops=...)`（可选参数，直接落文档，
    不做「FormatOp → 中文 → 再解析」的有损往返）。
- 记录 `changed_working_copy` / `generated_outputs`，供报告与失败清理定位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from paper_agent.errors import OperationCancelled
from paper_agent.intents.add_section import execute_add_section
from paper_agent.intents.convert import execute_convert
from paper_agent.intents.format_docx import execute_format
from paper_agent.intents.interview import (
    AddSectionPlan,
    ConvertPlan,
    PolishPlan,
    RewritePlan,
)
from paper_agent.intents.polish import execute_polish
from paper_agent.intents.rewrite import execute_rewrite
from paper_agent.intents.router import Intent
from paper_agent.session.section_index import slugify
from paper_agent.tasking.contracts import (
    FieldStatus,
    Operation,
    OperationResult,
    OperationStatus,
)


@dataclass
class HandlerContext:
    """Handler 执行所需的外部依赖（由 PlanExecutor 装配）。"""

    session: Any
    llm: Any = None
    facts: Any = None
    retrieval_provider: Any = None
    verifier: Any = None
    # (value, ctx) -> bool。**默认保守拒绝**：未经用户确认的新数字视为编造，交由 Gate
    # 拦下（绝不因为「没人回答」就默认放行编造数字）。
    confirm_number: Any = None
    max_retry: int = 2
    max_char_change_ratio: float = 0.30
    stream: bool = False
    output_dir: str = "output"
    extra: dict = field(default_factory=dict)


class IntentHandler(Protocol):
    intent: Intent

    def execute(self, op: Operation, ctx: HandlerContext, bus=None, cancel_token=None) -> OperationResult:
        ...


# --- 槽位读取助手 -----------------------------------------------------------

def _val(op: Operation, name: str, default=None):
    """取槽位值：只认 present（其余状态无可信值，见 base._VALUED_STATUSES 语义）。"""
    sv = op.slots.get(name)
    if sv is None or sv.field_status is not FieldStatus.PRESENT:
        return default
    return default if sv.value is None else sv.value


def _list(op: Operation, name: str) -> list:
    v = _val(op, name)
    if isinstance(v, (list, tuple)):
        return list(v)
    return []


def _ok_result(op: Operation, res, *, changed: bool, outputs=()) -> OperationResult:
    """按 D8 翻译业务结果：ok → succeeded，否则 rejected（含 Gate 拒）。"""
    ok = bool(getattr(res, "ok", False))
    return OperationResult(
        operation_id=op.operation_id,
        status=OperationStatus.SUCCEEDED if ok else OperationStatus.REJECTED,
        changed_working_copy=changed and ok,
        generated_outputs=tuple(outputs) if ok else (),
        message=str(getattr(res, "message", "")),
    )


def _failed(op: Operation, exc: Exception) -> OperationResult:
    return OperationResult(
        operation_id=op.operation_id,
        status=OperationStatus.FAILED,
        message=f"{type(exc).__name__}: {exc}",
    )


def _reject_unconfirmed(_value: str, _ctx: str) -> bool:
    """未配置数字确认回调时的保守默认：视为编造（Gate 会拦），不放行。"""
    return False


def _confirm_of(ctx: HandlerContext):
    return ctx.confirm_number if callable(ctx.confirm_number) else _reject_unconfirmed


def _guard(fn):
    """系统异常 → failed；取消一路上抛（由 PlanExecutor/Coordinator 处理）。"""
    def wrapper(self, op, ctx, bus=None, cancel_token=None):
        try:
            return fn(self, op, ctx, bus, cancel_token)
        except OperationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            return _failed(op, exc)
    return wrapper


# --- 五个 Handler -----------------------------------------------------------

class PolishHandler:
    intent = Intent.POLISH

    @_guard
    def execute(self, op, ctx, bus=None, cancel_token=None) -> OperationResult:
        plan = PolishPlan(
            section_id=str(_val(op, "section_id", "")),
            goal=str(_val(op, "goal", "") or ""),
            # 真执行绑定：constraints → ContextBuilder 硬约束块
            constraints=[str(c) for c in _list(op, "constraints")],
        )
        res = execute_polish(
            ctx.session, plan, ctx.llm,
            max_char_change_ratio=ctx.max_char_change_ratio,
            max_retry=ctx.max_retry, bus=bus, cancel_token=cancel_token,
            stream=ctx.stream,
        )
        return _ok_result(op, res, changed=True)


class RewriteHandler:
    intent = Intent.REWRITE

    @_guard
    def execute(self, op, ctx, bus=None, cancel_token=None) -> OperationResult:
        plan = RewritePlan(
            section_id=str(_val(op, "section_id", "")),
            direction=str(_val(op, "direction", "") or ""),
            details=str(_val(op, "details", "") or ""),
            want_search=bool(_val(op, "want_search", False)),
            search_query=str(_val(op, "search_query", "") or ""),
            search_limit=int(_val(op, "search_limit", 5) or 5),
        )
        res = execute_rewrite(
            ctx.session, plan, ctx.facts, ctx.llm,
            retrieval_provider=ctx.retrieval_provider, verifier=ctx.verifier,
            confirm=_confirm_of(ctx), max_retry=ctx.max_retry, bus=bus,
        )
        return _ok_result(op, res, changed=True)


class AddSectionHandler:
    intent = Intent.ADD_SECTION

    @_guard
    def execute(self, op, ctx, bus=None, cancel_token=None) -> OperationResult:
        title = str(_val(op, "title", "") or "")
        plan = AddSectionPlan(
            title=title,
            purpose=str(_val(op, "purpose", "") or ""),
            position=str(_val(op, "position", "end") or "end"),
            section_type=slugify(title),
            structure_kind=str(_val(op, "structure_kind", "desc") or "desc"),
            structure_desc=str(_val(op, "structure_desc", "") or ""),
            subsections=[str(s) for s in _list(op, "subsections")],
            want_search=bool(_val(op, "want_search", False)),
            search_query=str(_val(op, "search_query", "") or ""),
            search_limit=int(_val(op, "search_limit", 5) or 5),
        )
        res = execute_add_section(
            ctx.session, plan, ctx.facts, ctx.llm,
            retrieval_provider=ctx.retrieval_provider, verifier=ctx.verifier,
            confirm=_confirm_of(ctx), max_retry=ctx.max_retry, bus=bus,
        )
        return _ok_result(op, res, changed=True)


class ConvertHandler:
    intent = Intent.CONVERT_FORMAT

    @_guard
    def execute(self, op, ctx, bus=None, cancel_token=None) -> OperationResult:
        plan = ConvertPlan(
            target_ext=str(_val(op, "target_ext", "") or ""),
            presets=[str(p) for p in _list(op, "presets")],
        )
        res = execute_convert(ctx.session, plan, output_dir=ctx.output_dir)
        outputs = (res.output_path,) if getattr(res, "output_path", "") else ()
        # convert 只产新文件、不改 working copy（D7 回滚边界）
        return _ok_result(op, res, changed=False, outputs=outputs)


class FormatDocxHandler:
    intent = Intent.FORMAT_DOCX

    @_guard
    def execute(self, op, ctx, bus=None, cancel_token=None) -> OperationResult:
        # 真执行绑定：结构化 FormatOp 列表 → execute_format 的可选 ops 参数
        ops = _list(op, "operations")
        message = op.slots.get("operations")
        fallback_text = (message.verbatim if message is not None and message.verbatim else "") or ""
        res = execute_format(ctx.session, fallback_text, ops=ops or None)
        return _ok_result(op, res, changed=True)


_HANDLERS: dict[Intent, IntentHandler] = {
    Intent.POLISH: PolishHandler(),
    Intent.REWRITE: RewriteHandler(),
    Intent.ADD_SECTION: AddSectionHandler(),
    Intent.CONVERT_FORMAT: ConvertHandler(),
    Intent.FORMAT_DOCX: FormatDocxHandler(),
}


def handler_for(intent: Intent) -> IntentHandler | None:
    """按意图取 Handler；未注册返回 None（调用方标 rejected/unsupported）。"""
    return _HANDLERS.get(intent)


__all__ = [
    "HandlerContext", "IntentHandler", "handler_for",
    "PolishHandler", "RewriteHandler", "AddSectionHandler",
    "ConvertHandler", "FormatDocxHandler",
]
