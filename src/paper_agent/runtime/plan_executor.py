"""PlanExecutor（Task 20-23）：有序串行执行 + 状态聚合 + 取消 + 回滚处置 + 执行期校验。

职责（design Component§8 / D6 / D7 / D11）：

**Task 20 · 执行与聚合**
- `create_baseline` → 维护 `Plan_Execution_State` → **按计划顺序、一次一个**遍历
  Operation（绝不并发改同一 Working_Copy）→ 每 op 一个 `span("operation:<id>")` →
  Handler → `OperationResult`。
- 失败/rejected 即停后续（`skipped_due_to_previous_failure`）；按状态聚合表出
  `plan_status` / `failure_category`。

**Task 21 · 取消与事件**
- `except OperationCancelled: state.mark_cancelled(); publish(PLAN_CANCELLED); raise`
  ——**必须在上抛前发布**，否则 Coordinator 的 finalize 已经落盘、取消进不了 trace。
- 每 op 发 `OPERATION_RESULT`、计划终了发 `PLAN_RESULT`；未执行的 op 记
  `skipped_due_to_cancellation`。

**Task 22 · Baseline 处置**
- 按 D7 处置表 restore/release；`convert` 失败时 best-effort 清理**本轮新产物**
  （先记 `output_existed_before`，只删执行前不存在的文件，绝不删既有用户输出）。
- 取消场景：零写入 → release；部分写入 → 暂存 baseline 立即询问保留/回滚；无法获得
  回答 → 默认保留 working、不删 baseline（不静默丢弃）。

**Task 23 · Pre_Operation_Validator + needs_replan**
- 每 op 执行**前**基于最新 Working_Copy 查章节/格式；失效 → 停 +
  `blocked_needs_replan` + `ExecutionOutcome(needs_replan, remaining_operations)`，
  **不在循环内重澄清**（由 Task 17 的规划循环消费）；后续记 `skipped_due_to_replan`。
"""

from __future__ import annotations

import os
from collections.abc import Callable

from paper_agent.errors import OperationCancelled
from paper_agent.runtime.handlers import HandlerContext, handler_for
from paper_agent.session.section_index import SectionIndex
from paper_agent.tasking.contracts import (
    ExecutionOutcome,
    FailureCategory,
    FieldStatus,
    Operation,
    OperationResult,
    OperationStatus,
    PlanExecutionReport,
    Plan_Execution_State,
    PlanStatus,
    Task_Plan,
)

try:  # 观测事件（可选，None-safe）
    from paper_agent.observability.events import EventKind
except Exception:  # noqa: BLE001
    EventKind = None  # type: ignore

from paper_agent.intents.router import Intent

# 视为「该步没成」的终态：触发停后续
_NOT_OK = (
    OperationStatus.REJECTED,
    OperationStatus.FAILED,
    OperationStatus.BLOCKED_NEEDS_REPLAN,
)
_TEXT_EXT = ("tex", "md", "markdown")
_EDIT_INTENTS = (Intent.POLISH, Intent.REWRITE)

# Baseline 处置动作
KEEP = "keep"
ROLLBACK = "rollback"


# --- 观测 ------------------------------------------------------------------

def _publish(bus, kind_name: str, **payload) -> None:
    if bus is None or EventKind is None:
        return
    kind = getattr(EventKind, kind_name, None)
    if kind is None:
        return
    try:
        bus.publish(kind, **payload)
    except Exception:  # noqa: BLE001 - 观测失败不拖垮执行
        pass


class _NullSpan:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _span(bus, name: str):
    if bus is None:
        return _NullSpan()
    try:
        return bus.span(name)
    except Exception:  # noqa: BLE001
        return _NullSpan()


# --- 结果构造 --------------------------------------------------------------

def _unsupported(op: Operation) -> OperationResult:
    return OperationResult(
        operation_id=op.operation_id,
        status=OperationStatus.REJECTED,
        message=f"没有可用的执行器：{op.intent.value}",
    )


def _marked(op: Operation, status: OperationStatus, reason: str) -> OperationResult:
    return OperationResult(operation_id=op.operation_id, status=status, message=reason)


def aggregate_plan_status(
    results: list[OperationResult],
) -> tuple[PlanStatus, FailureCategory | None]:
    """按 design 状态聚合表算出 (plan_status, failure_category)。"""
    if not results:
        # 没有任何操作 → NO_ACTION，**不是** done（P0：空计划曾被报成执行成功）
        return PlanStatus.NO_ACTION, None

    succeeded = [r for r in results if r.status is OperationStatus.SUCCEEDED]
    cancelled = [r for r in results if r.status is OperationStatus.CANCELLED]
    replan = [r for r in results if r.status is OperationStatus.BLOCKED_NEEDS_REPLAN]
    bad = [r for r in results if r.status in _NOT_OK]

    # 执行期失效 → needs_replan（优先于普通失败：语义是「要重规划」而非「失败」）
    if replan:
        return PlanStatus.NEEDS_REPLAN, None

    if cancelled and not bad:
        return (
            PlanStatus.PARTIALLY_COMPLETED if succeeded else PlanStatus.CANCELLED
        ), None

    if not bad:
        return PlanStatus.DONE, None

    first_bad = bad[0]
    category = (
        FailureCategory.EXECUTION_FAILED
        if first_bad.status is OperationStatus.FAILED
        else FailureCategory.VALIDATION_REJECTED
    )
    if succeeded:
        return PlanStatus.PARTIALLY_COMPLETED, category
    return PlanStatus.FAILED, category


# --- Task 23：Pre_Operation_Validator --------------------------------------

def pre_operation_check(op: Operation, ctx: HandlerContext) -> str | None:
    """基于**最新** Working_Copy 校验该 op 仍可执行；返回失效原因或 None。

    与计划期的投影校验（Task 14）不同：这里查「现在是否真的存在/格式是否匹配」。
    失效不重澄清，只报原因，由 execute_plan 转 needs_replan。
    """
    session = getattr(ctx, "session", None)
    if session is None:
        return None
    ext = str(getattr(session, "ext", "") or "").lower()

    if op.intent in _EDIT_INTENTS:
        target = _slot(op, "section_id")
        if target and ext in _TEXT_EXT:
            try:
                idx = SectionIndex(session.read_working(), ext)
            except Exception:  # noqa: BLE001 - 读不到就不阻塞，交给 Handler 报错
                return None
            if idx.find(str(target)) is None:
                return f"章节「{target}」在当前文档中已不存在"

    elif op.intent is Intent.ADD_SECTION:
        title = _slot(op, "title")
        if title and ext in _TEXT_EXT:
            try:
                idx = SectionIndex(session.read_working(), ext)
            except Exception:  # noqa: BLE001
                return None
            if idx.find(str(title)) is not None:
                return f"章节「{title}」已存在（可能由前序操作创建）"

    elif op.intent is Intent.FORMAT_DOCX:
        if ext and ext != "docx":
            return f"排版要求 docx，当前工作副本为 .{ext}"

    return None


def _slot(op: Operation, name: str):
    sv = op.slots.get(name)
    if sv is None or sv.field_status is not FieldStatus.PRESENT:
        return None
    return sv.value


# --- Task 22：产物快照与清理 -----------------------------------------------

def _snapshot_outputs(plan: Task_Plan, ctx: HandlerContext) -> dict[str, dict]:
    """执行前记录各 convert 目标产物的归属与是否已存在。

    返回 `{operation_id: {path, existed_before}}`——**按操作归属记账**，这样失败清理
    才能只碰「失败操作」的残留，不动「已成功操作」的正式产物（P0）。
    """
    info: dict[str, dict] = {}
    session = getattr(ctx, "session", None)
    source = str(getattr(session, "source_path", "") or "")
    if not source:
        return info
    stem = os.path.splitext(os.path.basename(source))[0]
    for op in plan.operations:
        if op.intent is not Intent.CONVERT_FORMAT:
            continue
        ext = _slot(op, "target_ext")
        if not ext:
            continue
        path = os.path.join(ctx.output_dir, f"{stem}.{ext}")
        info[op.operation_id] = {
            "path": path,
            "existed_before": os.path.exists(path),
        }
    return info


def _cleanup_failed_outputs(
    outputs: dict[str, dict],
    results: list[OperationResult],
) -> list[str]:
    """只清理**失败/未完成操作**留下的、且执行前不存在的产物。

    绝不删：
    - 执行前已存在的用户文件（`existed_before=True`）；
    - **已成功操作**的正式产物——哪怕整个计划最终 partially_completed。
      （历史 bug：转换成功、后续步骤失败 → docx 被删，而报告仍显示第一步成功
      并给出已不存在的路径。）
    """
    succeeded = {
        r.operation_id for r in results if r.status is OperationStatus.SUCCEEDED
    }
    removed: list[str] = []
    for op_id, meta in outputs.items():
        if op_id in succeeded:
            continue                      # 成功操作的产物是正式结果，保留
        if meta.get("existed_before"):
            continue                      # 执行前就在 → 用户文件，不动
        path = meta.get("path") or ""
        try:
            if path and os.path.isfile(path):
                os.remove(path)
                removed.append(path)
        except OSError:
            pass
    return removed


def dispose_baseline(
    state: Plan_Execution_State,
    ctx: HandlerContext,
    plan_status: PlanStatus,
    *,
    ask_disposition: Callable[[list[str]], str] | None = None,
) -> str:
    """按 D7 处置表处置 baseline，返回处置动作。

    | 计划终态 | 处置 |
    |---|---|
    | done | release |
    | partially_completed / cancelled（有写入）| 询问：keep→release，rollback→restore+release |
    | cancelled（零写入）| release |
    | failed（零写入）| release |
    无法获得回答 → 默认保留 working、**不删 baseline**（不静默丢弃）。
    """
    baseline = state.baseline
    session = getattr(ctx, "session", None)
    if baseline is None or session is None:
        return "none"

    has_writes = bool(state.completed_operations)

    if plan_status is PlanStatus.DONE:
        _release(session, baseline)
        state.baseline = None
        return "released"

    if not has_writes:
        # 零写入（含执行前取消、首项即败）→ 无可回滚内容，直接释放
        _release(session, baseline)
        state.baseline = None
        return "released"

    # 部分写入：必须当前 Turn 内询问保留/回滚
    if ask_disposition is None:
        return "pending"  # 无法获得回答 → 保留 working、保留 baseline
    try:
        choice = ask_disposition(list(state.completed_ids))
    except Exception:  # noqa: BLE001
        return "pending"

    if choice == ROLLBACK:
        try:
            session.restore_baseline(baseline)
        except Exception:  # noqa: BLE001
            return "pending"
        _release(session, baseline)
        state.baseline = None
        return "rolled_back"

    _release(session, baseline)
    state.baseline = None
    return "released"


def _release(session, baseline) -> None:
    fn = getattr(session, "release_baseline", None)
    if fn is None:
        return
    try:
        fn(baseline)
    except Exception:  # noqa: BLE001
        pass


# --- 主入口 -----------------------------------------------------------------

def execute_plan(
    plan: Task_Plan,
    state: Plan_Execution_State,
    ctx: HandlerContext,
    *,
    bus=None,
    cancel_token=None,
    ask_disposition: Callable[[list[str]], str] | None = None,
) -> ExecutionOutcome:
    """有序串行执行计划，返回 ExecutionOutcome。

    `state` 由调用方（TurnService）在 coordinator.run **之外**持有，使取消/异常后仍能
    出报告（D7）。取消经 `OperationCancelled` 上抛前先发 `PLAN_CANCELLED`（Task 21）。
    """
    # P0：空计划不得进入执行器（否则「什么都没做」会被聚合成 done）。
    # 不建基线、不发 span，直接返回 NO_ACTION，由上层如实告知用户。
    if not plan.operations:
        report = PlanExecutionReport(plan_status=PlanStatus.NO_ACTION)
        _publish(bus, "PLAN_RESULT", plan_status=PlanStatus.NO_ACTION.value,
                 completed=0, skipped=0)
        return ExecutionOutcome(report=report, plan_status=PlanStatus.NO_ACTION)

    if state.baseline is None:
        state.baseline = _create_baseline(ctx)
    outputs_existed = _snapshot_outputs(plan, ctx)

    results: list[OperationResult] = []
    stop_reason: str | None = None      # None | "failure" | "replan"
    replan_reason = ""

    try:
        for op in plan.operations:
            if stop_reason is not None:
                status = (
                    OperationStatus.SKIPPED_DUE_TO_REPLAN
                    if stop_reason == "replan"
                    else OperationStatus.SKIPPED_DUE_TO_PREVIOUS_FAILURE
                )
                reason = ("需重规划，未执行" if stop_reason == "replan"
                          else "前序操作失败，未执行")
                res = _marked(op, status, reason)
                results.append(res)
                _emit_op_result(bus, res)
                continue

            # Task 23：执行前基于最新 working copy 校验
            invalid = pre_operation_check(op, ctx)
            if invalid:
                res = _marked(op, OperationStatus.BLOCKED_NEEDS_REPLAN, invalid)
                results.append(res)
                _emit_op_result(bus, res)
                stop_reason = "replan"
                replan_reason = invalid
                continue

            state.current_operation_id = op.operation_id
            handler = handler_for(op.intent)
            with _span(bus, f"operation:{op.operation_id}"):
                res = _unsupported(op) if handler is None else handler.execute(
                    op, ctx, bus=bus, cancel_token=cancel_token
                )
            results.append(res)
            _emit_op_result(bus, res)
            if res.status is OperationStatus.SUCCEEDED:
                state.completed_operations.append(res)
            elif res.status in _NOT_OK:
                stop_reason = "failure"

    except OperationCancelled:
        # Task 21：**上抛前**发布取消，否则 Coordinator 已 finalize，取消进不了 trace
        state.mark_cancelled()
        state.current_operation_id = None
        # 未执行的后续 op 记 skipped_due_to_cancellation
        done_ids = {r.operation_id for r in results}
        for op in plan.operations:
            if op.operation_id not in done_ids:
                results.append(_marked(
                    op, OperationStatus.SKIPPED_DUE_TO_CANCELLATION, "已取消，未执行"))
        plan_status = (
            PlanStatus.PARTIALLY_COMPLETED if state.completed_operations
            else PlanStatus.CANCELLED
        )
        report = PlanExecutionReport(
            plan_status=plan_status,
            operations=tuple(results),
            rollback_available=state.rollback_available,
            completed_operation_ids=tuple(state.completed_ids),
        )
        _publish(bus, "PLAN_CANCELLED",
                 plan_status=plan_status.value,
                 completed_operation_ids=list(state.completed_ids),
                 rollback_available=state.rollback_available)
        _publish(bus, "PLAN_RESULT", plan_status=plan_status.value, cancelled=True)
        # Task 22：取消场景的 baseline 处置（零写入 release / 部分写入询问）
        dispose_baseline(state, ctx, plan_status, ask_disposition=ask_disposition)
        raise  # 继续上抛，让 Coordinator 统一 finalize

    state.current_operation_id = None
    plan_status, category = aggregate_plan_status(results)

    # Task 22：只清理**失败操作**的残留产物；已成功操作的正式产物与既有用户文件都保留
    if plan_status in (PlanStatus.FAILED, PlanStatus.PARTIALLY_COMPLETED):
        _cleanup_failed_outputs(outputs_existed, results)

    report = PlanExecutionReport(
        plan_status=plan_status,
        operations=tuple(results),
        failure_category=category,
        rollback_available=state.rollback_available,
        completed_operation_ids=tuple(state.completed_ids),
    )
    _publish(bus, "PLAN_RESULT",
             plan_status=plan_status.value,
             failure_category=category.value if category else None,
             completed=len(state.completed_ids),
             skipped=sum(1 for r in results if r.status.value.startswith("skipped")))

    # Task 22：非 needs_replan 的终态在当前 Turn 内处置 baseline
    # （needs_replan 要保留基线，因为规划循环还会继续执行剩余步骤）
    if plan_status is not PlanStatus.NEEDS_REPLAN:
        dispose_baseline(state, ctx, plan_status, ask_disposition=ask_disposition)

    return ExecutionOutcome(
        report=report,
        plan_status=plan_status,
        remaining_operations=_remaining(plan, results),
        replan_reason=replan_reason,
    )


def _emit_op_result(bus, res: OperationResult) -> None:
    _publish(bus, "OPERATION_RESULT",
             operation_id=res.operation_id,
             status=res.status.value,
             changed_working_copy=res.changed_working_copy,
             generated_outputs=list(res.generated_outputs))


def _remaining(plan: Task_Plan, results: list[OperationResult]) -> tuple[Operation, ...]:
    """未成功执行的 Operation（needs_replan 只重规划这些，绝不重跑已成功）。"""
    done = {r.operation_id for r in results if r.status is OperationStatus.SUCCEEDED}
    return tuple(op for op in plan.operations if op.operation_id not in done)


def _create_baseline(ctx: HandlerContext):
    """建立计划级可回滚基线；session 不支持则返回 None（不阻塞执行）。"""
    session = getattr(ctx, "session", None)
    create = getattr(session, "create_baseline", None)
    if create is None:
        return None
    try:
        return create("plan")
    except Exception:  # noqa: BLE001 - 基线失败不阻断执行，只是无法回滚
        return None


__all__ = [
    "execute_plan", "aggregate_plan_status", "pre_operation_check",
    "dispose_baseline", "KEEP", "ROLLBACK",
]
