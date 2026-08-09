"""TurnService（Task 16）：intent-planning 三态编排 + Shadow Mode。

设计（design D9 / Component§1 / Property 9）：
- **三个互斥入口**，避免嵌套 Coordinator：off 完全旧路径；shadow 旧路径照跑 + 独立
  shadow run 只记录；active 由 Task 24 接管。
- **执行前只读快照（PlanningSnapshot）**：在 legacy 执行**之前**捕获工作副本 hash /
  文件类型 / 章节 / facts 摘要。shadow 只基于该快照分析，**不重新读取已被 legacy
  改动的 Working_Copy**，保证与旧 Router 当时决策公平比较。
- **不阻塞、不污染、不塌陷**：v1 采「捕获快照 → legacy 先执行拿到结果 → 之后跑
  shadow」，故 shadow 延迟不影响 legacy 的写入与结果；shadow 全程 try/except 兜底，
  任何失败/超预算都只记录、绝不上抛、绝不碰 Working_Copy；记录 `added_latency_ms`。
- **可比差异**：记录 shadow 的操作意图序列、静态校验结果，以及（可选）与 legacy
  Router 意图是否一致，供离线对比。

本模块只依赖 tasking 理解链路与观测脊柱，不改任何 `execute_*`/Gate/写入路径。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from paper_agent.observability.events import EventKind
from paper_agent.session.section_index import SectionIndex, supports_sections
from paper_agent.tasking.contracts import (
    CallStatus,
    PlanningSnapshot,
    SectionSummary,
    Task_Plan,
    ValidationOutcome,
)
from paper_agent.tasking.extractors import build_extractor
from paper_agent.tasking.operation_analyzer import analyze, requirements_for
from paper_agent.tasking.plan_builder import build_plan
from paper_agent.tasking.plan_validator import validate_plan

_TEXT_EXT = ("tex", "md", "markdown")


# --- 只读快照 ---------------------------------------------------------------

def _working_hash(session) -> str:
    try:
        if session.ext in _TEXT_EXT:
            data = session.read_working().encode("utf-8")
        else:
            with open(session.working_path, "rb") as f:
                data = f.read()
    except Exception:  # noqa: BLE001
        data = b""
    return hashlib.sha256(data).hexdigest()


def _snapshot_sections(session) -> tuple[SectionSummary, ...]:
    """快照文档章节。

    判定必须用 `supports_sections`（与 chat.py 列章节同源），**不能**用
    `_TEXT_EXT` —— 后者只有 tex/md，漏了 docx。真实症状：docx 会话里 chat.py
    列出 33 个章节，规划层却收到零章节，于是校验报「计划期找不到要修改的章节」，
    修订提示又说「文档当前没有任何章节」，同一文档两个相反结论。
    docx 的 `read_working()` 返回投影文本，SectionIndex 本就能解析。
    """
    if not supports_sections(session.ext):
        return ()
    try:
        idx = SectionIndex(session.read_working(), session.ext)
        return tuple(
            SectionSummary(section_id=s.id, title=s.title, level=s.level)
            for s in idx.list()
        )
    except Exception:  # noqa: BLE001
        return ()


def _facts_summary(facts) -> dict:
    if facts is None:
        return {}
    try:
        claims = getattr(facts, "claims", {}) or {}
        return {
            "known_refs": len(getattr(facts, "known_refs", []) or []),
            "known_numbers": len(getattr(facts, "known_numbers", []) or []),
            "section_intents": len(claims.get("section_intents", {})),
        }
    except Exception:  # noqa: BLE001
        return {}


def _failed_report(state, run_report):
    """系统异常（非取消）的报告：保留已完成步骤 + 失败类别，不冒充「取消」。"""
    from paper_agent.tasking.contracts import (
        FailureCategory,
        PlanExecutionReport,
        PlanStatus,
    )

    completed = list(state.completed_operations)
    return PlanExecutionReport(
        plan_status=(
            PlanStatus.PARTIALLY_COMPLETED if completed else PlanStatus.FAILED
        ),
        operations=tuple(completed),
        failure_category=FailureCategory.EXECUTION_FAILED,
        rollback_available=state.rollback_available,
        completed_operation_ids=tuple(state.completed_ids),
    )


def capture_snapshot(session, facts=None) -> PlanningSnapshot:
    """在 legacy 执行**之前**捕获只读快照（不改任何状态）。"""
    return PlanningSnapshot(
        working_hash=_working_hash(session),
        file_type=session.ext,
        sections=_snapshot_sections(session),
        facts_summary=_facts_summary(facts),
    )


# --- Shadow 结果 ------------------------------------------------------------

@dataclass
class PlanningShadowResult:
    """一次 shadow 分析的记录（只读、可离线对比，绝不影响 legacy）。"""

    turn_id: str
    ok: bool = False
    analyze_status: str = ""
    operation_intents: tuple[str, ...] = ()
    validation_result: str | None = None
    added_latency_ms: float = 0.0
    error: str | None = None
    snapshot_working_hash: str = ""
    legacy_intent: str | None = None
    matches_legacy: bool | None = None
    plan: Task_Plan | None = None
    validation: ValidationOutcome | None = None


# --- Turn 结果 --------------------------------------------------------------

@dataclass
class TurnResult:
    mode: str
    turn_id: str
    legacy_result: Any = None
    shadow: PlanningShadowResult | None = None
    warnings: list[str] = field(default_factory=list)
    # --- active 专用 ---
    report: Any = None                  # PlanExecutionReport（执行了才有）
    planning_exit: Any = None           # PlanningExit（未进执行时的出口）
    state: Any = None                   # Plan_Execution_State（run 外持有）
    run_report: Any = None              # coordinator.RunReport
    # 新栈**内部**异常的类别（Coordinator 已捕获，未降级到旧栈）。空 = 无异常。
    # 结构化暴露，供 TurnMetrics 归属计数使用——不让调用方去解析 warnings 里的中文。
    run_error_category: str = ""


class TurnService:
    """按 intent_planning_mode 分派：off 旧路径 / shadow 只读旁路 / active 接管。"""

    def __init__(
        self, *, config, llm, shadow_coordinator=None, coordinator=None,
        handler_context=None, interactor=None,
    ) -> None:
        self._config = config
        self._llm = llm
        self._shadow_coordinator = shadow_coordinator
        self._coordinator = coordinator
        self._handler_context = handler_context
        self._interactor = interactor

    @property
    def mode(self) -> str:
        return getattr(self._config, "intent_planning_mode", "off")

    def handle_turn(
        self,
        text: str,
        *,
        session,
        facts=None,
        legacy: Callable[[], Any],
        legacy_intent: str | None = None,
    ) -> TurnResult:
        """处理一个自然语言 Turn。

        `legacy`：零参回调，跑既有旧路径并返回其结果。TurnService 只在其**前后**做
        只读旁路，绝不改变 legacy 的行为与返回值。
        """
        turn_id = uuid4().hex
        mode = self.mode

        if mode == "off":
            return TurnResult(mode="off", turn_id=turn_id, legacy_result=legacy())

        if mode == "shadow":
            # 1) legacy 执行前捕获只读快照
            snapshot = capture_snapshot(session, facts)
            # 2) legacy 照常执行、拿到结果（shadow 尚未运行，不影响其写入/延迟）
            legacy_result = legacy()
            # 3) 之后基于快照跑 shadow：只记录、全兜底、绝不碰 Working_Copy
            shadow = self._run_shadow_safely(text, snapshot, turn_id, legacy_intent)
            return TurnResult(
                mode="shadow", turn_id=turn_id,
                legacy_result=legacy_result, shadow=shadow,
            )

        if mode == "active":
            return self._handle_active(text, session=session, facts=facts,
                                      turn_id=turn_id)

        # 非法模式（config 已降级，理论到不了）→ 退回 off 语义
        return TurnResult(mode="off", turn_id=turn_id, legacy_result=legacy())

    # -- active 通路（Task 24）--

    def _handle_active(self, text: str, *, session, facts, turn_id: str) -> TurnResult:
        """**一个 Turn 一个 coordinator.run**，包住整段旅程（D2）。

        run 内：rule_analyze → 规划循环（理解/校验/澄清/未映射/确认）→ execute_plan。
        `Plan_Execution_State` 在 run **之外**持有，使取消/异常后仍能出报告（D7）。
        needs_replan 由规划循环消费（此处按 D2 回顶重规划未执行步骤）。
        """
        from paper_agent.runtime.plan_executor import execute_plan
        from paper_agent.tasking.contracts import (
            ConfirmedPlan,
            ExecutionReplanContext,
            Plan_Execution_State,
            PlanningExit,
            PlanStatus,
        )
        from paper_agent.tasking.budget import budget_from_config
        from paper_agent.tasking.planning_loop import run_planning_loop

        snapshot = capture_snapshot(session, facts)
        sections = [s.section_id for s in snapshot.sections]
        sections += [s.title for s in snapshot.sections]
        state = Plan_Execution_State()
        ctx = self._handler_context
        result = TurnResult(mode="active", turn_id=turn_id, state=state)
        max_revisions = max(1, getattr(self._config, "planning_max_revisions", 3))
        # **一个 Turn 一份预算**：跨修订累计调用数与 provider 耗时（#9）——
        # 只限"单次"挡不住 3 次修订 × N 个抽取器的总量。
        budget = budget_from_config(self._config)

        def _journey(bus, cancel_token):
            # 重规划上下文：**保留原始请求**，只重规划未执行步骤（Property 11）。
            # 历史 bug：把 replan_reason 当成全新用户请求，丢了 original_request /
            # previous_plan / remaining_operations，导致可能重跑已成功的操作。
            replan: ExecutionReplanContext | None = None
            for _ in range(max_revisions):
                outcome = run_planning_loop(
                    text, llm=self._llm, interactor=self._interactor,
                    current_sections=sections, current_format=snapshot.file_type,
                    turn_id=turn_id, max_plan_revisions=max_revisions,
                    replan=replan, budget=budget, bus=bus, facts=facts,
                )
                if not isinstance(outcome, ConfirmedPlan):
                    return ("exit", outcome)
                exec_outcome = execute_plan(
                    outcome.plan, state, ctx, bus=bus, cancel_token=cancel_token,
                    ask_disposition=getattr(self._interactor, "ask_disposition", None),
                )
                if exec_outcome.plan_status is PlanStatus.NEEDS_REPLAN:
                    replan = ExecutionReplanContext(
                        original_request=text,
                        previous_plan=outcome.plan,
                        remaining_operations=exec_outcome.remaining_operations,
                        completed_operation_ids=tuple(state.completed_ids),
                        replan_reason=exec_outcome.replan_reason,
                    )
                    continue
                return ("report", exec_outcome.report)
            # 修订次数耗尽：明确出口，不返回 None（否则上层看不出发生了什么）
            return ("exit", PlanningExit(
                status=PlanStatus.REVISION_LIMIT_EXCEEDED,
                message=f"重规划超过 {max_revisions} 次仍无法继续",
            ))

        if self._coordinator is None:
            kind, payload = _journey(None, None)
        else:
            run_report = self._coordinator.run(_journey)
            result.run_report = run_report
            if run_report.value is None:
                # P0：Coordinator 已区分 cancelled / failed，此处必须分流——
                # 否则真实的代码异常会被当成「用户取消」报给用户。
                if run_report.run_status == "cancelled":
                    result.report = state.to_cancelled_report()
                else:
                    category = (run_report.error or {}).get("category", "未知")
                    result.report = _failed_report(state, run_report)
                    result.run_error_category = str(category)
                    result.warnings.append(f"执行异常（{category}）")
                return result
            kind, payload = run_report.value

        if kind == "report":
            result.report = payload
        else:
            result.planning_exit = payload
        return result

    # -- shadow 内部 --

    def _run_shadow_safely(
        self, text: str, snapshot: PlanningSnapshot, turn_id: str,
        legacy_intent: str | None,
    ) -> PlanningShadowResult:
        """在独立 run 里跑 shadow；任何异常都被吸收为 error，绝不上抛。"""
        def _fn(bus, _ct):
            return self._compute_shadow(text, snapshot, turn_id, legacy_intent, bus)

        try:
            if self._shadow_coordinator is not None:
                report = self._shadow_coordinator.run(_fn)
                result = report.value
                if isinstance(result, PlanningShadowResult):
                    return result
                # coordinator 捕获了异常（value=None）→ 兜底
                return PlanningShadowResult(
                    turn_id=turn_id, ok=False,
                    error=(report.error or {}).get("category", "shadow_run_failed"),
                    snapshot_working_hash=snapshot.working_hash,
                    legacy_intent=legacy_intent,
                )
            return _fn(None, None)
        except Exception as exc:  # noqa: BLE001 - shadow 绝不影响 legacy
            return PlanningShadowResult(
                turn_id=turn_id, ok=False, error=type(exc).__name__,
                snapshot_working_hash=snapshot.working_hash,
                legacy_intent=legacy_intent,
            )

    def _compute_shadow(
        self, text: str, snapshot: PlanningSnapshot, turn_id: str,
        legacy_intent: str | None, bus,
    ) -> PlanningShadowResult:
        """analyze → pre_extract → build → static_validate（只读，基于快照）。"""
        started = time.monotonic()
        result = PlanningShadowResult(
            turn_id=turn_id, snapshot_working_hash=snapshot.working_hash,
            legacy_intent=legacy_intent,
        )
        try:
            ar = analyze(text, self._llm)
            result.analyze_status = ar.call_status.value
            self._publish(bus, EventKind.OPERATION_ANALYSIS,
                          turn_id=turn_id, call_status=ar.call_status.value,
                          n_operations=len(ar.operations))
            if ar.call_status is not CallStatus.SUCCESS:
                result.ok = False
                result.added_latency_ms = (time.monotonic() - started) * 1000
                return result

            extractions = []
            n_ops = len(ar.operations)
            for op in ar.operations:
                ex = build_extractor(op.intent, self._llm)
                if ex is None:
                    continue
                # 与 active 走同一路由，否则 shadow 的差异统计会失真
                owned, _attr = requirements_for(op, ar.requirements, n_ops)
                er = ex.pre_extract(op, owned)
                extractions.append(er)
                self._publish(bus, EventKind.PRE_EXTRACT,
                              turn_id=turn_id, intent=op.intent.value,
                              call_status=er.call_status.value)

            # extractions 与 operations 对齐（build_extractor 恒非 None：analyze 已滤意图）
            plan = build_plan(
                turn_id, ar.operations, extractions, ar.requirements,
                unmapped_text=ar.unmapped_text,
                source_text=text,
            )
            current_sections = [s.section_id for s in snapshot.sections]
            current_sections += [s.title for s in snapshot.sections]
            validation = validate_plan(
                plan, current_sections=current_sections,
                current_format=snapshot.file_type,
            )
            self._publish(bus, EventKind.PLAN_VALIDATION,
                          turn_id=turn_id, result=validation.result,
                          n_issues=len(validation.issues))

            intents = tuple(op.intent.value for op in plan.operations)
            result.ok = True
            result.operation_intents = intents
            result.validation_result = validation.result
            result.plan = plan
            result.validation = validation
            if legacy_intent is not None:
                result.matches_legacy = (len(intents) == 1 and intents[0] == legacy_intent)
        except Exception as exc:  # noqa: BLE001
            result.ok = False
            result.error = type(exc).__name__
        finally:
            result.added_latency_ms = (time.monotonic() - started) * 1000
        return result

    @staticmethod
    def _publish(bus, kind, **payload) -> None:
        if bus is None:
            return
        try:
            bus.publish(kind, **payload)
        except Exception:  # noqa: BLE001 - 观测失败不拖垮 shadow
            pass


__all__ = ["TurnService", "PlanningShadowResult", "TurnResult", "capture_snapshot"]
