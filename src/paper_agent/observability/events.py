"""结构化事件与事件接收器（sink）。

系统各处通过 sink.emit(event) 发出结构化事件，订阅方（控制台渲染器、
未来的前端、trace 记录器等）自行决定如何呈现。这样业务逻辑与展示完全解耦。

事件是**不可变值对象**（`frozen=True` + 深度冻结的 `data`）：一旦创建，任何
下游 sink 都无法（也不应）修改它，从而保证多路分发（FanOutSink）时各下游看到
的是同一份权威快照，杜绝一个 sink 改动 payload 影响另一个 sink 的隐蔽 bug。

信封字段（envelope）——`event_id/sequence/schema_version/generation_id/attempt/
llm_call_id/source_event_id`——默认空，构造点可不填；权威值由 EventBus 在发布时
统一「盖章」（stamp）。由于 Event 不可变，盖章通过 `dataclasses.replace` 产生新
实例，而非原地修改（见 Task 3 的 EventBus）。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


class EventKind(str, Enum):
    """v1（P0 薄脊柱）事件种类。

    仅保留 streaming-and-trace v1 实际需要的种类；V1 遗留但 V2 未使用的
    种类（PHASE/AGENT_*/ITERATION/REVIEW_SCORES/LLM_REQUEST/LLM_RESPONSE/
    DEGRADATION/EXPORT_ASSET/旧 SPAN）已删除。需要时再按需增补。
    """

    # --- 运行边界 ---
    WORKFLOW_START = "workflow_start"
    WORKFLOW_END = "workflow_end"
    RUN_CANCELLED = "run_cancelled"

    # --- Span 生命周期 ---
    SPAN_START = "span_start"
    SPAN_END = "span_end"

    # --- 路由 / 访谈 / 检索 ---
    ROUTE_DECISION = "route_decision"
    INTERVIEW_QA = "interview_qa"
    RETRIEVAL_START = "retrieval_start"
    RETRIEVAL_END = "retrieval_end"

    # --- 生成尝试 ---
    GENERATION_ATTEMPT_START = "generation_attempt_start"
    GENERATION_ATTEMPT_END = "generation_attempt_end"

    # --- Gate 校验 ---
    GATE_START = "gate_start"
    GATE_RESULT = "gate_result"
    GATE_RETRY = "gate_retry"

    # --- LLM 调用闭环 ---
    LLM_CALL_START = "llm_call_start"
    LLM_CALL_END = "llm_call_end"
    LLM_DELTA = "llm_delta"        # 流式增量（逐块输出）；sensitive_ephemeral
    LLM_THINKING = "llm_thinking"  # 模型思考内容（reasoning_content），敏感、永不展示原文
    LLM_USAGE = "llm_usage"        # 单次调用 token 用量
    LLM_RETRY = "llm_retry"        # 调用失败后重试（含重试序号、计划休眠时长、异常类别）

    # --- 持久化三态 ---
    WORKING_COPY_UPDATE = "working_copy_update"   # 内存/工作副本更新
    WORKSPACE_COMMIT = "workspace_commit"         # 工作区落盘/快照
    SOURCE_SAVE = "source_save"                   # 用户 :save 写回源文件

    # --- intent-planning 理解层业务事件（复用 WORKFLOW_START/END=Turn、
    #     span("operation:...")=Operation，不新增生命周期事件）---
    OPERATION_ANALYSIS = "operation_analysis"     # 多操作拆分结果
    PRE_EXTRACT = "pre_extract"                   # 单操作槽位抽取结果
    PLAN_VALIDATION = "plan_validation"           # 计划静态校验结果
    CLARIFICATION_QA = "clarification_qa"         # 澄清问答（脱敏）
    UNMAPPED_RESOLUTION = "unmapped_resolution"   # 未映射要求的用户处置
    PROJECT_FACT_RECORDED = "project_fact_recorded"  # 项目级陈述写入 claims（X-11）
    PLAN_CONFIRMED = "plan_confirmed"             # 用户确认计划
    PLAN_REJECTED = "plan_rejected"               # 用户否决计划
    PLAN_CANCELLED = "plan_cancelled"             # 计划执行中取消（finalize 前发布）
    OPERATION_RESULT = "operation_result"         # 单操作结果（succeeded/rejected/...）
    PLAN_RESULT = "plan_result"                   # 计划最终结果（done/partial/...）

    # --- 文献账本（literature-evidence / A1）---
    # 单条文献 upsert 的脱敏摘要（action / dedup 依据与置信 / 冲突数 / 核验状态 /
    # legacy 标记）。A1 登记事件种类；实际发布待 A2 检索 Loop 接入 bus。
    REFERENCE_LEDGER_UPSERT = "reference_ledger_upsert"


# 敏感的临时原文事件：允许 ConsoleRenderer 实时显示、TraceRecorder 计算
# hash/preview，但**禁止**写入完整 trace.json、普通日志、错误报告或第三方
# exporter。新增下游 sink 时应默认不转发此类事件的原文。
SENSITIVE_EPHEMERAL_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.LLM_DELTA,
        EventKind.LLM_THINKING,
    }
)


def deep_freeze(value: Any) -> Any:
    """递归地把可变容器转为不可变视图。

    - dict / Mapping → MappingProxyType（键值递归冻结）
    - list / tuple / 非 str 的 Sequence → tuple（元素递归冻结）
    - set / frozenset → frozenset（元素递归冻结）
    - 其余（str/int/float/bool/None 等）原样返回

    幂等：对已冻结结构再次调用得到等价结构。
    """
    if isinstance(value, (str, bytes)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(v) for v in value)
    if isinstance(value, Set):
        return frozenset(deep_freeze(v) for v in value)
    if isinstance(value, Sequence):
        # 兜底：其它序列（非 str/bytes）也转 tuple。
        return tuple(deep_freeze(v) for v in value)
    return value


def _unfreeze(value: Any) -> Any:
    """serialize_event 的逆冻结：把不可变视图转回 json.dumps 可处理的普通结构。

    MappingProxyType → dict、tuple → list、frozenset → list、Enum → .value。
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, bytes)):
        return value.decode() if isinstance(value, bytes) else value
    if isinstance(value, (Mapping, MappingProxyType)):
        return {k: _unfreeze(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_unfreeze(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_unfreeze(v) for v in value]
    return value


@dataclass(frozen=True)
class Event:
    kind: EventKind
    message: str = ""
    data: Mapping = field(default_factory=dict)
    # --- 追踪字段（可选，默认空；未追踪时保持空）---
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    ts: float = 0.0                     # epoch 秒；0.0 表示未追踪/未补全
    duration_ms: float | None = None    # 仅 span 收尾事件带
    # --- 权威信封（envelope）：默认空，由 EventBus 发布时统一盖章 ---
    event_id: str = ""
    sequence: int = -1                  # -1 表示未分配序号
    schema_version: int = 1
    generation_id: str = ""             # 每次 run_polish 调用一个（跨重试共享）
    attempt: int | None = None          # 生成尝试序号（从 0 开始），非生成事件为 None
    llm_call_id: str = ""               # 单次 LLM 调用唯一 id
    source_event_id: str = ""           # provider 侧原始 event_id（盖章覆盖后留存）

    def __post_init__(self) -> None:
        # frozen=True 下不能直接赋值，用 object.__setattr__ 深度冻结 data，
        # 确保没有任何下游能修改（含嵌套）payload。
        object.__setattr__(self, "data", deep_freeze(self.data))


def serialize_event(event: Event) -> dict:
    """把 Event 转为可被 ``json.dumps`` 序列化的普通 dict。

    枚举转字符串值、不可变视图转普通 dict/list。用于 trace 落盘、日志、
    调试。注意：**调用方**负责在序列化敏感事件（SENSITIVE_EPHEMERAL_KINDS）
    前先经过 Sanitizer——本函数不做脱敏，只做结构转换。
    """
    out: dict = {}
    for f in dataclasses.fields(event):
        out[f.name] = _unfreeze(getattr(event, f.name))
    return out


@runtime_checkable
class EventSink(Protocol):
    def emit(self, event: Event) -> None:
        ...


class NullSink:
    """默认空实现：丢弃所有事件（无可观测开销）。"""

    def emit(self, event: Event) -> None:  # noqa: D401
        return None


class CallbackSink:
    """把事件转发给一个回调函数，便于自定义订阅。"""

    def __init__(self, callback) -> None:
        self._cb = callback

    def emit(self, event: Event) -> None:
        self._cb(event)
