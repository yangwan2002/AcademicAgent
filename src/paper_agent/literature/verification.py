"""核验事件与状态折叠（A1 / 任务 3）。

核验状态**不存字段**，而是由一条**只追加**的事件序列折叠派生。这样做消掉了两个
矛盾与一个漏洞：

- frozen 记录带可变 ``resolved`` / ``quarantined`` 布尔是自相矛盾的（改不了）；
  改为追加 ``CONFLICT_RESOLVED`` / ``RELEASED`` 事件。
- ``provider_error`` 只是「这次没问到」，绝不是文献的永久状态；它不出现在
  :func:`aggregate_status` 的任何返回分支里。
- 隔离/解除若「按时序后者胜」，跨源合并时相同或乱序的时间戳会让一条来源不明的
  RELEASED 悄悄解除隔离。改为**因果引用**：RELEASED 必须指向它解除的那次
  QUARANTINED，折叠结果与时间戳无关。

``authority``（final | corroboration，见 :mod:`.sources`）决定一条核验事件能否把状态
推到 VERIFIED：只有 ``final`` 事件算数，``corroboration`` 事件仅作佐证。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum


class EventKind(str, Enum):
    """核验事件类型。前四种是核验尝试，后三种是状态变迁。"""

    VERIFIED = "verified"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    PROVIDER_ERROR = "provider_error"
    CONFLICT_RESOLVED = "conflict_resolved"   # target 指向被消解的 CONFLICT
    QUARANTINED = "quarantined"
    RELEASED = "released"                      # target 指向被解除的 QUARANTINED


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    CONFLICT = "conflict"
    QUARANTINED = "quarantined"


#: 核验尝试类事件（相对于状态变迁事件）。
_ATTEMPT_KINDS = frozenset(
    {
        EventKind.VERIFIED,
        EventKind.CONFLICT,
        EventKind.NOT_FOUND,
        EventKind.PROVIDER_ERROR,
    }
)


@dataclass(frozen=True)
class VerificationEvent:
    """一次核验尝试或一次状态变迁的不可变记录。

    - ``authority``：``final`` 表示该事件由对该标识符类型有最终核验权的源产生，
      可推动 VERIFIED / CONFLICT；``corroboration`` 只作佐证，不改状态。
    - ``target_event_id``：仅变迁事件使用，指向被作用的核验事件的 ``event_id``。
    """

    event_id: str
    kind: EventKind
    source: str = ""
    provenance: str = "aggregator"
    authority: str = "corroboration"   # final | corroboration
    identifier_type: str = ""
    identifier: str = ""
    checked_at: str = ""               # ISO8601
    target_event_id: str = ""
    details: str = ""


def make_event_id(
    kind: EventKind,
    source: str,
    identifier: str,
    checked_at: str,
    seq: int = 0,
) -> str:
    raw = f"{kind.value}|{source}|{identifier}|{checked_at}|{seq}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def make_event(
    kind: EventKind,
    *,
    source: str = "",
    provenance: str = "aggregator",
    authority: str = "corroboration",
    identifier_type: str = "",
    identifier: str = "",
    checked_at: str = "",
    target_event_id: str = "",
    details: str = "",
    seq: int = 0,
) -> VerificationEvent:
    """构造一个 ``VerificationEvent``，自动计算 ``event_id``。"""
    return VerificationEvent(
        event_id=make_event_id(kind, source, identifier, checked_at, seq),
        kind=kind if isinstance(kind, EventKind) else EventKind(kind),
        source=source,
        provenance=provenance,
        authority=authority,
        identifier_type=identifier_type,
        identifier=identifier,
        checked_at=checked_at,
        target_event_id=target_event_id,
        details=details,
    )


@dataclass
class FoldResult:
    """折叠结果：聚合状态 + 被判无效的变迁事件（供审计报告）。"""

    status: VerificationStatus
    invalid_transitions: tuple[str, ...] = field(default_factory=tuple)


def fold_events(events) -> FoldResult:
    """按**因果引用**折叠事件序列，得到聚合状态与无效变迁列表。

    折叠规则（与时间戳先后无关）：

    1. 校验变迁事件的 target：``CONFLICT_RESOLVED`` 必须指向存在的 ``CONFLICT``、
       ``RELEASED`` 必须指向存在的 ``QUARANTINED``；否则该变迁无效。
    2. QUARANTINED  若存在未被有效 RELEASED 指向的 QUARANTINED
       CONFLICT      否则若存在未被消解的 **final** CONFLICT
       VERIFIED      否则若存在 **final** VERIFIED
       UNVERIFIED    否则
    """
    events = list(events)
    by_id: dict[str, VerificationEvent] = {e.event_id: e for e in events}

    resolved_conflict_ids: set[str] = set()
    released_quarantine_ids: set[str] = set()
    invalid: list[str] = []

    for e in events:
        if e.kind == EventKind.CONFLICT_RESOLVED:
            target = by_id.get(e.target_event_id)
            if target is not None and target.kind == EventKind.CONFLICT:
                resolved_conflict_ids.add(e.target_event_id)
            else:
                invalid.append(e.event_id)
        elif e.kind == EventKind.RELEASED:
            target = by_id.get(e.target_event_id)
            if target is not None and target.kind == EventKind.QUARANTINED:
                released_quarantine_ids.add(e.target_event_id)
            else:
                invalid.append(e.event_id)

    has_open_quarantine = any(
        e.kind == EventKind.QUARANTINED and e.event_id not in released_quarantine_ids
        for e in events
    )
    if has_open_quarantine:
        return FoldResult(VerificationStatus.QUARANTINED, tuple(invalid))

    has_open_conflict = any(
        e.kind == EventKind.CONFLICT
        and e.authority == "final"
        and e.event_id not in resolved_conflict_ids
        for e in events
    )
    if has_open_conflict:
        return FoldResult(VerificationStatus.CONFLICT, tuple(invalid))

    has_final_verified = any(
        e.kind == EventKind.VERIFIED and e.authority == "final" for e in events
    )
    if has_final_verified:
        return FoldResult(VerificationStatus.VERIFIED, tuple(invalid))

    return FoldResult(VerificationStatus.UNVERIFIED, tuple(invalid))


def aggregate_status(events) -> VerificationStatus:
    """折叠事件序列得到聚合状态（丢弃无效变迁明细，只要状态）。"""
    return fold_events(events).status


# ===========================================================================
# A2c-1 / 任务 2：严格核验策略（读时投影，不改历史事件）
# ===========================================================================
#
# 问题（design-a2c D2）：A1 兼容路径在 ``require_final=False`` 下把 aggregator 对 DOI 的
# 成功回查固化成 ``authority=final``。这些事件已写进历史，事后翻配置**不会**让旧记录降级。
# 若原地改写事件，又会制造第二真相源、破坏「事件只记发生了什么」的不变量。
#
# 解法：把「严格与否」做成**读时投影**——原始事件不动，读取时按一张**版本化**的
# ``(source, identifier_type) -> final|corroboration|none`` 权限快照重算有效 authority，
# 再走与 :func:`fold_events` 相同的因果折叠。版本号绑定策略，写进 Assessment/Batch/trace，
# 使同一批事件在任何时间得到同一严格状态（不随后续 config 漂移）。


class VerificationPolicy(str, Enum):
    """核验状态的解释策略。"""

    COMPAT_A1 = "compat_a1"            # 读事件里存的 authority（等价 aggregate_status，现状）
    STRICT_FINAL_V1 = "strict_final_v1"  # 忽略存储 authority，按版本化 registry 重算


#: STRICT_FINAL_V1 绑定的 authority registry 版本号。**权限口径变更必须 bump 此版本**，
#: 使「这批事件按哪套权限算的」可追溯、可复现。
STRICT_FINAL_V1_VERSION = "authority-registry-v1"


@dataclass(frozen=True)
class AuthorityRegistry:
    """冻结的 ``(source, identifier_type) -> final|corroboration|none`` 权限快照。

    从 A1 :func:`paper_agent.literature.sources.verification_authority` 的口径**快照**而来
    （同一套判定），但一旦绑定某 ``version`` 便不随运行期 config 变化——严格投影只认与
    ``STRICT_FINAL_V1`` 绑定的这份版本化权限，不读「当前配置」。
    """

    version: str

    def authority_of(self, source: str, identifier_type: str) -> str:
        """该 (source, id_type) 的有效核验权限：``final`` | ``corroboration`` | ``none``。"""
        # 惰性 import 避免顶层环依赖（sources 不 import verification，此处单向）。
        from paper_agent.literature.sources import verification_authority

        return verification_authority(source, identifier_type)


#: STRICT_FINAL_V1 使用的版本化权限快照单例。
STRICT_FINAL_V1_REGISTRY = AuthorityRegistry(version=STRICT_FINAL_V1_VERSION)


def _effective_authority(
    event: VerificationEvent,
    policy: VerificationPolicy,
    registry: AuthorityRegistry,
) -> str:
    """一条事件在给定策略下的**有效** authority。

    - COMPAT_A1：用事件里存的 ``authority``（现状）。
    - STRICT_FINAL_V1：**忽略**存储值，按版本化 registry 依 ``(source, identifier_type)`` 重算。
      只有核验尝试类事件（VERIFIED/CONFLICT）才需要 authority；变迁事件原样返回其存储值
      （折叠只看它们的 target/kind，不看 authority）。
    """
    if policy is VerificationPolicy.COMPAT_A1:
        return event.authority
    if event.kind in (EventKind.VERIFIED, EventKind.CONFLICT):
        return registry.authority_of(event.source, event.identifier_type)
    return event.authority


def status_under_policy(
    events,
    policy: VerificationPolicy = VerificationPolicy.STRICT_FINAL_V1,
    registry: AuthorityRegistry = STRICT_FINAL_V1_REGISTRY,
) -> VerificationStatus:
    """在给定策略下折叠事件得到核验状态（读时投影，不改原事件）。

    STRICT_FINAL_V1 下，OpenAlex/S2 的 DOI 事件即使历史写了 ``final``，也只算
    ``corroboration``；Crossref 的 DOI、arXiv 的原生 id 等才算 ``final``。折叠规则
    （因果引用、QUARANTINED > final CONFLICT > final VERIFIED > UNVERIFIED）与
    :func:`fold_events` **完全一致**，只是 authority 的取值改由 ``_effective_authority`` 决定。
    """
    events = list(events)
    if policy is VerificationPolicy.COMPAT_A1:
        return fold_events(events).status

    by_id = {e.event_id: e for e in events}
    resolved_conflict_ids: set[str] = set()
    released_quarantine_ids: set[str] = set()

    for e in events:
        if e.kind == EventKind.CONFLICT_RESOLVED:
            target = by_id.get(e.target_event_id)
            if target is not None and target.kind == EventKind.CONFLICT:
                resolved_conflict_ids.add(e.target_event_id)
        elif e.kind == EventKind.RELEASED:
            target = by_id.get(e.target_event_id)
            if target is not None and target.kind == EventKind.QUARANTINED:
                released_quarantine_ids.add(e.target_event_id)

    has_open_quarantine = any(
        e.kind == EventKind.QUARANTINED and e.event_id not in released_quarantine_ids
        for e in events
    )
    if has_open_quarantine:
        return VerificationStatus.QUARANTINED

    has_open_conflict = any(
        e.kind == EventKind.CONFLICT
        and _effective_authority(e, policy, registry) == "final"
        and e.event_id not in resolved_conflict_ids
        for e in events
    )
    if has_open_conflict:
        return VerificationStatus.CONFLICT

    has_final_verified = any(
        e.kind == EventKind.VERIFIED
        and _effective_authority(e, policy, registry) == "final"
        for e in events
    )
    if has_final_verified:
        return VerificationStatus.VERIFIED

    return VerificationStatus.UNVERIFIED


__all__ = [
    "EventKind",
    "VerificationStatus",
    "VerificationEvent",
    "FoldResult",
    "make_event",
    "make_event_id",
    "fold_events",
    "aggregate_status",
    # A2c-1：严格核验策略（读时投影）
    "VerificationPolicy",
    "AuthorityRegistry",
    "STRICT_FINAL_V1_VERSION",
    "STRICT_FINAL_V1_REGISTRY",
    "status_under_policy",
]
