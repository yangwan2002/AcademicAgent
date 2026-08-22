"""自然语言 Turn 的唯一编排入口：Planning → AskLoop / PlanExecutor。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from paper_agent.session.section_index import SectionIndex, supports_sections
from paper_agent.tasking.contracts import (
    PlanningSnapshot,
    SectionSummary,
)

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
    """在 Planning 执行前捕获只读快照（不改任何状态）。"""
    return PlanningSnapshot(
        working_hash=_working_hash(session),
        file_type=session.ext,
        sections=_snapshot_sections(session),
        facts_summary=_facts_summary(facts),
    )


# --- Turn 结果 --------------------------------------------------------------

@dataclass
class TurnResult:
    turn_id: str
    warnings: list[str] = field(default_factory=list)
    report: Any = None                  # PlanExecutionReport（执行了才有）
    planning_exit: Any = None           # PlanningExit（未进执行时的出口）
    readonly_result: Any = None         # AskResult（零写作操作时，同 run 内只读回答）
    state: Any = None                   # Plan_Execution_State（run 外持有）
    run_report: Any = None              # coordinator.RunReport
    # 内部异常类别（Coordinator 已捕获）。空 = 无异常。
    run_error_category: str = ""


class TurnService:
    """所有自然语言请求统一进入 Planning。"""

    def __init__(
        self, *, config, llm, coordinator=None,
        handler_context=None, interactor=None,
    ) -> None:
        self._config = config
        self._llm = llm
        self._coordinator = coordinator
        self._handler_context = handler_context
        self._interactor = interactor

    def handle_turn(
        self,
        text: str,
        *,
        session,
        facts=None,
    ) -> TurnResult:
        """分析、确认并执行一个自然语言 Turn。"""
        turn_id = uuid4().hex
        return self._handle_planning(text, session=session, facts=facts, turn_id=turn_id)

    def _handle_planning(self, text: str, *, session, facts, turn_id: str) -> TurnResult:
        """**一个 Turn 一个 coordinator.run**，包住整段旅程（D2）。

        run 内：理解/校验/澄清/未映射/确认 → execute_plan，或零写作操作时
        → AskLoop。两个出口共享同一 bus 与 cancel token。
        `Plan_Execution_State` 在 run **之外**持有，使取消/异常后仍能出报告（D7）。
        needs_replan 由规划循环消费（此处按 D2 回顶重规划未执行步骤）。
        """
        from paper_agent.runtime.plan_executor import execute_plan
        from paper_agent.application.ask_context import capture as capture_ask_context
        from paper_agent.application.ask_loop import run_ask
        from paper_agent.coordinator import RunOutcome
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
        result = TurnResult(turn_id=turn_id, state=state)
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
                    if getattr(outcome, "route_to_readonly", False):
                        # 自动只读交接仍属于这一条用户消息：复用当前 run 的 bus 与
                        # cancel_token。此前 chat.py 在 TurnService 返回后另开 run，
                        # 导致一条消息两份无法关联的 trace。
                        ask_result = run_ask(
                            text,
                            capture_ask_context(session, facts),
                            self._llm,
                            max_iterations=max(
                                1,
                                getattr(
                                    self._config,
                                    "agent_loop_max_iterations",
                                    10,
                                ),
                            ),
                            max_llm_calls=max(
                                1,
                                getattr(
                                    self._config,
                                    "agent_loop_max_llm_calls",
                                    12,
                                ),
                            ),
                            cancel_token=cancel_token,
                            bus=bus,
                        )
                        return ("readonly", (outcome, ask_result))
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
            def _probe_working_copy_status() -> str:
                return (
                    "updated"
                    if _working_hash(session) != snapshot.working_hash
                    else "unchanged"
                )

            def _coordinated_journey(bus, cancel_token):
                value = _journey(bus, cancel_token)
                # 由拥有 session 的应用层显式声明最终状态。比较最终工作副本与
                # Turn 起点快照，避免把 AskResult.ok / 业务 Result.ok 误当写入证据。
                return RunOutcome(
                    value=value,
                    working_copy_status=_probe_working_copy_status(),
                )

            run_report = self._coordinator.run(
                _coordinated_journey,
                # 正常返回由 RunOutcome 显式声明；取消/异常没有返回值时，finally
                # 仍能只读比较起止 hash，保留已发生的部分写入事实。
                working_copy_probe=_probe_working_copy_status,
            )
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
        elif kind == "readonly":
            result.planning_exit, result.readonly_result = payload
        else:
            result.planning_exit = payload
        return result

__all__ = ["TurnService", "TurnResult", "capture_snapshot"]
