"""文献写作流的 typed 交互 port + Checkpoint 模型（A2d-4 / 任务 2，design-a2d D9/D3）。

`PlanningInteractor.confirm_plan()` 的语义是「确认执行计划」，**不能**拿来承载批次提交与
放宽配额。故定义**窄 Protocol** `LiteratureInteractionPort`；现有
`ConsolePlanningInteractor` 可同时实现两个协议（新增 typed capability，不是新建一套 UI）。

`literature` 领域模块**不** import 本模块（分层：application → literature 单向）。

`PreflightCheckpoint` 只存**工作流状态**：不含 EvidenceBundle 原文、不含摘要/查询原文
（隐私边界，design D11）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from paper_agent.literature.preflight import AcceptedQuotaDeviation
from paper_agent.literature.quota import CountDimension


# ---------------------------------------------------------------- 决定对象

@dataclass(frozen=True)
class BatchDecision:
    action: str            # accept | reject | needs_input
    note: str = ""


@dataclass(frozen=True)
class GapDecision:
    action: str            # accept | cancel | needs_input
    note: str = ""


@dataclass(frozen=True)
class BundleDecision:
    action: str            # accept | reject | needs_input
    note: str = ""


@dataclass(frozen=True)
class FinalGateDecision:
    action: str            # accept | cancel | needs_input
    note: str = ""


@dataclass(frozen=True)
class SearchTopicDecision:
    """用户补充检索主题（A2f）。action=provide 时 text 为补充文本。"""

    action: str            # provide | cancel | needs_input
    text: str = ""


@dataclass(frozen=True)
class SearchIntentDecision:
    """确认/修改合成的检索意图（A2f）。action=modify 时 text 为修改后的主题/说明。"""

    action: str            # accept | modify | cancel | needs_input
    text: str = ""


@runtime_checkable
class LiteratureInteractionPort(Protocol):
    """文献写作流的用户交互（窄 port；与 PlanningInteractor 分离）。"""

    def confirm_retrieval_batch(self, summary) -> BatchDecision:
        ...

    def resolve_quota_gap(self, report) -> GapDecision:
        ...

    def confirm_evidence_bundle(self, summary) -> BundleDecision:
        ...

    def resolve_final_gate(self, report) -> FinalGateDecision:
        ...

    # A2f：检索意图准备的两类交互（typed，不复用 confirm_plan）。
    def request_search_topic(self, summary) -> SearchTopicDecision:
        ...

    def confirm_search_intent(self, summary) -> SearchIntentDecision:
        ...


# ---------------------------------------------------------------- Checkpoint


@dataclass(frozen=True)
class ThroughputPolicySnapshot:
    """Serializable, non-secret policy identity frozen for one operation."""

    batch_enabled: bool = False
    reorder_enabled: bool = False
    concurrency_enabled: bool = False
    batch_size: int = 1
    max_input_chars: int = 0
    max_input_tokens: int | None = None
    max_output_chars: int = 0
    max_output_tokens: int | None = None
    text_policy_version: str = "legacy"
    max_title_chars: int = 0
    max_abstract_chars: int = 0
    judge_provider: str = ""
    judge_model_id: str = ""
    prompt_version: str = "legacy"
    schema_version: str = "legacy"
    retry_limit: int = 0
    per_call_timeout_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "schema_version": "throughput-policy-v1",
            "flags": {
                "batch": self.batch_enabled,
                "reorder": self.reorder_enabled,
                "concurrency": self.concurrency_enabled,
            },
            "batch_size": self.batch_size,
            "input_budget": {
                "max_input_chars": self.max_input_chars,
                "max_input_tokens": self.max_input_tokens,
                "max_output_chars": self.max_output_chars,
                "max_output_tokens": self.max_output_tokens,
            },
            "text_policy": {
                "version": self.text_policy_version,
                "max_title_chars": self.max_title_chars,
                # Privacy scanners reserve the literal raw-field name; this
                # is only a numeric policy limit, never source summary text.
                "max_summary_chars": self.max_abstract_chars,
            },
            "judge_version": {
                "provider": self.judge_provider,
                "model_id": self.judge_model_id,
                "prompt_version": self.prompt_version,
                "schema_version": self.schema_version,
            },
            "retry_limit": self.retry_limit,
            "per_call_timeout_s": self.per_call_timeout_s,
        }

    @property
    def canonical_hash(self) -> str:
        return throughput_policy_hash(self.to_dict())

    @property
    def snapshot_id(self) -> str:
        return f"tp_{self.canonical_hash[:12]}"


def throughput_policy_hash(payload: dict) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def legacy_throughput_policy_payload() -> dict:
    return ThroughputPolicySnapshot().to_dict()

@dataclass(frozen=True)
class PreflightCheckpoint:
    """可暂停的工作流状态（进 facts 单 envelope 的 pending_literature_flows）。

    **不持久化** EvidenceBundle 原文、摘要、查询原文——只有 ID / 哈希 / 计数 / 状态。
    """

    flow_id: str
    operation_id: str
    task_fingerprint: str
    quota_hash: str
    state: str
    initial_query_hash: str = ""            # 只存 hash，不存原文
    batch_id: str = ""
    revision: int = 0
    gaps: tuple[tuple[str, int], ...] = ()  # (dimension, amount) 可序列化形态
    deviations: tuple[AcceptedQuotaDeviation, ...] = ()
    created_at: str = ""
    # A2f：检索意图引用（原文冻结在 pending_search_intents，此处只存 id + 两种哈希）。
    search_intent_id: str = ""
    semantic_fingerprint: str = ""
    strategy_hash: str = ""
    throughput_policy: dict = field(default_factory=legacy_throughput_policy_payload)
    throughput_policy_hash: str = ""
    throughput_policy_snapshot_id: str = ""

    def __post_init__(self) -> None:
        actual_hash = throughput_policy_hash(self.throughput_policy)
        if not self.throughput_policy_hash:
            object.__setattr__(self, "throughput_policy_hash", actual_hash)
        if not self.throughput_policy_snapshot_id:
            object.__setattr__(
                self,
                "throughput_policy_snapshot_id",
                f"tp_{self.throughput_policy_hash[:12]}",
            )


def _deviation_to_dict(d: AcceptedQuotaDeviation) -> dict:
    return {
        "stage": d.stage,
        "dimension": d.dimension.value,
        "original_lower_bound": d.original_lower_bound,
        "achieved_at_decision": d.achieved_at_decision,
        "effective_lower_bound": d.effective_lower_bound,
        "original_gap": d.original_gap,
        "task_fingerprint": d.task_fingerprint,
        "operation_id": d.operation_id,
        "quota_hash": d.quota_hash,
        "accepted_at": d.accepted_at,
    }


def _deviation_from_dict(d: dict) -> AcceptedQuotaDeviation:
    return AcceptedQuotaDeviation(
        stage=d.get("stage", "preflight"),
        dimension=CountDimension(d.get("dimension", "total")),
        original_lower_bound=int(d.get("original_lower_bound", 0)),
        achieved_at_decision=int(d.get("achieved_at_decision", 0)),
        effective_lower_bound=int(d.get("effective_lower_bound", 0)),
        original_gap=int(d.get("original_gap", 0)),
        task_fingerprint=d.get("task_fingerprint", ""),
        operation_id=d.get("operation_id", ""),
        quota_hash=d.get("quota_hash", ""),
        accepted_at=d.get("accepted_at", ""),
    )


def checkpoint_to_dict(cp: PreflightCheckpoint) -> dict:
    return {
        "flow_id": cp.flow_id,
        "operation_id": cp.operation_id,
        "task_fingerprint": cp.task_fingerprint,
        "quota_hash": cp.quota_hash,
        "state": cp.state,
        "initial_query_hash": cp.initial_query_hash,
        "batch_id": cp.batch_id,
        "revision": cp.revision,
        "gaps": [list(g) for g in cp.gaps],
        "deviations": [_deviation_to_dict(d) for d in cp.deviations],
        "created_at": cp.created_at,
        "search_intent_id": cp.search_intent_id,
        "semantic_fingerprint": cp.semantic_fingerprint,
        "strategy_hash": cp.strategy_hash,
        "throughput_policy": cp.throughput_policy,
        "throughput_policy_hash": cp.throughput_policy_hash,
        "throughput_policy_snapshot_id": cp.throughput_policy_snapshot_id,
    }


def checkpoint_from_dict(d: dict) -> PreflightCheckpoint:
    return PreflightCheckpoint(
        flow_id=d.get("flow_id", ""),
        operation_id=d.get("operation_id", ""),
        task_fingerprint=d.get("task_fingerprint", ""),
        quota_hash=d.get("quota_hash", ""),
        state=d.get("state", ""),
        initial_query_hash=d.get("initial_query_hash", ""),
        batch_id=d.get("batch_id", ""),
        revision=int(d.get("revision", 0)),
        gaps=tuple((str(g[0]), int(g[1])) for g in d.get("gaps", []) if len(g) == 2),
        deviations=tuple(_deviation_from_dict(x) for x in d.get("deviations", [])),
        created_at=d.get("created_at", ""),
        search_intent_id=d.get("search_intent_id", ""),
        semantic_fingerprint=d.get("semantic_fingerprint", ""),
        strategy_hash=d.get("strategy_hash", ""),
        throughput_policy=(
            d.get("throughput_policy")
            if isinstance(d.get("throughput_policy"), dict)
            else legacy_throughput_policy_payload()
        ),
        throughput_policy_hash=d.get("throughput_policy_hash", ""),
        throughput_policy_snapshot_id=d.get("throughput_policy_snapshot_id", ""),
    )


__all__ = [
    "BatchDecision", "GapDecision", "BundleDecision", "FinalGateDecision",
    "SearchTopicDecision", "SearchIntentDecision",
    "LiteratureInteractionPort",
    "ThroughputPolicySnapshot", "throughput_policy_hash",
    "legacy_throughput_policy_payload",
    "PreflightCheckpoint", "checkpoint_to_dict", "checkpoint_from_dict",
]
