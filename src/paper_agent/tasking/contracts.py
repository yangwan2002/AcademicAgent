"""intent-planning 的数据模型（Task 1）。

只定义结构，不含逻辑。三类可变性（design D3/D7 / tasks Task 1）：
- **值对象 frozen**：AtomicRequirement / SlotValue / RawOperation / Operation /
  Rule_Analysis / PlanRevisionRequest / ExecutionOutcome / SessionBaseline /
  ValidationIssue / ReorderSuggestion / SectionSummary / PlanningSnapshot 等。
- **可变运行态**：Plan_Execution_State（执行中持续更新）。
- **生成后不可变**：PlanExecutionReport / Task_Plan（frozen；改动=产新版本）。

集合字段在 frozen 值对象里用 tuple（真正不可变）；可变运行态用 list。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from paper_agent.intents.router import Intent


# --- 枚举 ---

class CallStatus(str, Enum):
    """一次抽取/分析调用的整体状态（不与字段态折叠）。"""

    SUCCESS = "success"
    PROVIDER_FAILED = "provider_failed"
    PARSE_FAILED = "parse_failed"
    SCHEMA_FAILED = "schema_failed"


class FieldStatus(str, Enum):
    """单个槽位的状态。"""

    PRESENT = "present"
    USER_ABSENT = "user_absent"      # 用户没说
    AMBIGUOUS = "ambiguous"          # 模型不确定
    INVALID = "invalid"              # 值非法
    UNAVAILABLE = "unavailable"      # 因抽取调用失败暂缺（≠user_absent）


class SlotSource(str, Enum):
    """槽位取值来源。"""

    USER_EXPLICIT = "user_explicit"
    NORMALIZED = "normalized"        # 如 "Word"→".docx"
    DEFAULTED = "defaulted"          # 由 IntentSpec 默认值填入（非 LLM）
    USER_CONFIRMED = "user_confirmed"  # 澄清回填


class OperationStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"            # 业务失败（含 Gate 拒）
    FAILED = "failed"               # 系统/工具异常
    CANCELLED = "cancelled"
    BLOCKED_NEEDS_REPLAN = "blocked_needs_replan"
    SKIPPED_DUE_TO_PREVIOUS_FAILURE = "skipped_due_to_previous_failure"
    SKIPPED_DUE_TO_CANCELLATION = "skipped_due_to_cancellation"
    SKIPPED_DUE_TO_REPLAN = "skipped_due_to_replan"


class PlanStatus(str, Enum):
    DRAFT = "draft"
    NEEDS_INPUT = "needs_input"
    # 计划里没有任何可执行操作（如全部要求被忽略/不支持）。**绝不可报成 done**——
    # 「什么都没做」与「都做成了」不是同一件事，折叠二者等于对用户撒谎。
    NO_ACTION = "no_action"
    READY = "ready"
    CONFIRMED = "confirmed"
    RUNNING = "running"
    DONE = "done"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
    NEEDS_REPLAN = "needs_replan"
    REVISION_LIMIT_EXCEEDED = "revision_limit_exceeded"


class FailureCategory(str, Enum):
    EXECUTION_FAILED = "execution_failed"      # 系统/provider/工具失败
    VALIDATION_REJECTED = "validation_rejected"  # Gate 拒绝、质量未达标


class RevisionReason(str, Enum):
    INITIAL = "initial"
    SUPPLEMENT = "supplement"        # unmapped 补充说明
    USER_MODIFY = "user_modify"      # 确认阶段修改
    NEEDS_REPLAN = "needs_replan"    # 执行期失效重规划


# --- 需求与操作（值对象，frozen）---

@dataclass(frozen=True)
class AtomicRequirement:
    """从用户消息识别出的一条原子要求；offset 由程序定位（不让 LLM 算）。"""

    requirement_id: str
    verbatim: str
    source_start: int = -1
    source_end: int = -1
    category: str = ""
    ambiguous_span: bool = False     # 原文定位失败/多匹配


@dataclass(frozen=True)
class SlotValue:
    """一个槽位的取值 + 状态 + 来源 + 它消费了哪些 requirement。"""

    value: Any = None
    field_status: FieldStatus = FieldStatus.USER_ABSENT
    source: SlotSource | None = None
    requirement_ids: tuple[str, ...] = ()
    verbatim: str | None = None


@dataclass(frozen=True)
class RawOperation:
    """Operation_Analyzer 的粗粒度输出：意图 + 原话 + 覆盖的 requirement。"""

    intent: Intent
    verbatim: str
    requirement_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Operation:
    """计划中的一个可执行步骤。

    **`slots` 是唯一真相源**：执行（Handler）、校验、展示全部从它派生。历史上这里还
    有 `target/goal/constraints` 三个反规范化副本，但它们是「生成时算一次、之后不
    更新」的有损快照——澄清回填后就会陈旧（曾导致确认摘要显示「目标=空」），且
    `constraints` 丢掉了 SlotValue 的 source/requirement_ids。故已删除，展示改用
    `plan_builder.operation_target()/operation_goal()/operation_constraints()` 实时派生。
    """

    operation_id: str
    intent: Intent
    slots: dict[str, SlotValue] = field(default_factory=dict)
    requirement_ids: tuple[str, ...] = ()
    status: OperationStatus = OperationStatus.PENDING


# --- 分析/抽取结果（值对象，frozen）---

@dataclass(frozen=True)
class AnalyzeResult:
    """Operation_Analyzer 的返回。"""

    call_status: CallStatus
    operations: tuple[RawOperation, ...] = ()
    requirements: tuple[AtomicRequirement, ...] = ()
    unmapped_text: tuple[str, ...] = ()
    error_category: str | None = None


@dataclass(frozen=True)
class ExtractionResult:
    """单个 Operation 的槽位抽取结果（调用态与字段态分离）。"""

    call_status: CallStatus
    slots: dict[str, SlotValue] = field(default_factory=dict)
    consumed_requirement_ids: tuple[str, ...] = ()
    newly_discovered_requirements: tuple[AtomicRequirement, ...] = ()  # 仅 Fast_Path
    unmapped_requirements: tuple[str, ...] = ()
    error_category: str | None = None


# --- 规则分析（值对象，frozen）---

@dataclass(frozen=True)
class Rule_Analysis:
    """规则层对一条消息的分析，用于 Fast_Path 判定与 Analyzer 失败降级。"""

    candidates: tuple[Intent, ...] = ()
    matched_signals: dict[str, tuple[str, ...]] = field(default_factory=dict)
    has_sequence_signal: bool = False
    uncovered_text: tuple[str, ...] = ()
    unambiguous: bool = False


# --- 校验（值对象，frozen）---

class DispositionStatus(str, Enum):
    """一条 AtomicRequirement 的去向（可验证零静默丢失的账本口径，D4）。"""

    MAPPED = "mapped"          # 已被某个具体 Slot 消费（真的会影响执行）
    UNMAPPED = "unmapped"      # 未被任何 Slot 消费 → 必须由用户处置
    IGNORED = "ignored"        # 用户明确选择忽略
    CLARIFIED = "clarified"    # 经澄清回填后落到 Slot
    REJECTED = "rejected"      # 系统明确拒绝（如不支持的意图）
    UNPARSED = "unparsed"      # 理解整体失败的兜底（原话留痕，不静默丢）


@dataclass(frozen=True)
class RequirementDisposition:
    """requirement → 去向的结构化记录。

    取代原先 `Task_Plan.unmapped_requirements: tuple[str, ...]`——那只有文本，丢了
    requirement_id、丢了「被哪个操作的哪个槽位消费」，无法审计也无法在 trace 里对账。
    """

    requirement_id: str
    status: DispositionStatus
    verbatim: str = ""
    operation_id: str | None = None
    slot_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationIssue:
    operation_id: str
    code: str                        # section_not_found / format_mismatch / ...
    message: str = ""


@dataclass(frozen=True)
class ReorderSuggestion:
    reason: str
    suggested_order: tuple[str, ...] = ()  # operation_id 顺序


@dataclass(frozen=True)
class ValidationOutcome:
    result: str                      # ready | needs_input | rejected
    issues: tuple[ValidationIssue, ...] = ()
    missing_fields: tuple[str, ...] = ()
    reorder_suggestions: tuple[ReorderSuggestion, ...] = ()


@dataclass(frozen=True)
class SectionSummary:
    section_id: str
    title: str
    level: int = 1


@dataclass(frozen=True)
class PlanningSnapshot:
    """Shadow 用的执行前只读快照（不在 legacy 改动后重读文件）。"""

    working_hash: str
    file_type: str
    sections: tuple[SectionSummary, ...] = ()
    facts_summary: dict = field(default_factory=dict)


# --- 计划（生成后不可变，frozen）---

@dataclass(frozen=True)
class Task_Plan:
    plan_id: str
    source_turn_id: str
    operations: tuple[Operation, ...] = ()
    # 每条已识别 requirement 的去向（唯一账本）。`unmapped_requirements` 是它的
    # 派生视图，供 UI/校验按原文使用。
    dispositions: tuple[RequirementDisposition, ...] = ()
    unmapped_requirements: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    reorder_suggestions: tuple[ReorderSuggestion, ...] = ()
    status: PlanStatus = PlanStatus.DRAFT
    schema_version: int = 1
    plan_version: int = 1
    analyzer_version: str = ""
    prompt_version: str = ""


@dataclass(frozen=True)
class PlanRevisionRequest:
    """计划修订：旧计划 + 修订文本 + 原因（不把修订当全新请求丢原计划）。"""

    previous_plan: Task_Plan | None
    revision_text: str
    revision_reason: RevisionReason = RevisionReason.INITIAL


# --- 执行结果（生成后不可变，frozen）---

@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    status: OperationStatus
    changed_working_copy: bool = False
    generated_outputs: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class PlanExecutionReport:
    plan_status: PlanStatus
    operations: tuple[OperationResult, ...] = ()
    failure_category: FailureCategory | None = None
    rollback_scope: str = "working_copy_only"
    rollback_available: bool = False
    completed_operation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionReplanContext:
    """执行期失效后重规划所需的完整上下文（Property 11）。

    只把 `replan_reason` 当成新用户请求是不够的——那会丢掉原始意图、旧计划和「哪些
    步骤已经成功」，导致重建出的计划可能重跑已完成的润色/新增章节。故显式携带：
    """

    original_request: str
    previous_plan: "Task_Plan"
    remaining_operations: tuple["Operation", ...] = ()
    completed_operation_ids: tuple[str, ...] = ()
    replan_reason: str = ""


@dataclass(frozen=True)
class ExecutionOutcome:
    """execute_plan 的返回；needs_replan 时 Planning Loop 据此重规划未执行步骤。"""

    report: PlanExecutionReport
    plan_status: PlanStatus
    remaining_operations: tuple[Operation, ...] = ()
    replan_reason: str = ""


# --- 会话基线（定义在 session 层，此处 re-export，保持依赖方向 tasking→session）---

from paper_agent.session.file_session import SessionBaseline  # noqa: E402,F401


# --- 规划循环出口（值对象，frozen）---

@dataclass(frozen=True)
class ConfirmedPlan:
    """规划循环成功出口：用户已确认、可执行的计划。"""

    plan: Task_Plan


@dataclass(frozen=True)
class PlanningExit:
    """规划循环非执行出口：rejected / revision_limit_exceeded / cancelled 等。"""

    status: PlanStatus
    message: str = ""


# --- 可变运行态（不 frozen）---

@dataclass
class Plan_Execution_State:
    """计划执行中的运行态，持续更新；取消/异常时据此出报告与回滚选项。"""

    completed_operations: list[OperationResult] = field(default_factory=list)
    current_operation_id: str | None = None
    baseline: SessionBaseline | None = None
    cancelled: bool = False

    def mark_cancelled(self) -> None:
        self.cancelled = True

    @property
    def completed_ids(self) -> list[str]:
        return [r.operation_id for r in self.completed_operations]

    @property
    def rollback_available(self) -> bool:
        return self.baseline is not None and bool(self.completed_operations)

    def to_cancelled_report(self) -> PlanExecutionReport:
        """取消后据当前运行态生成报告（部分完成/取消）。"""
        status = (
            PlanStatus.PARTIALLY_COMPLETED
            if self.completed_operations
            else PlanStatus.CANCELLED
        )
        return PlanExecutionReport(
            plan_status=status,
            operations=tuple(self.completed_operations),
            rollback_available=self.rollback_available,
            completed_operation_ids=tuple(self.completed_ids),
        )
