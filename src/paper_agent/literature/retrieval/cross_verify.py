"""多标识符交叉核验器 + 缓存（A2c-1 / 任务 4，design-a2c D4）。

把 A1 的单次回查升级为：对候选的**每个**作品级标识符，选一个对该类型有 final 权限的
VerifyProvider 回查（doi→Crossref、arxiv→arXiv、openalex_work→OpenAlex、
semantic_scholar_paper→S2），产出**多条** VerificationEvent，再用 A2c-1 的严格投影
:func:`status_under_policy` 折叠出严格核验状态。

关键纪律（design D4 + Req 4.1）：

- **权限按类型选源**：DOI 必须由 registry（Crossref）final 才算 VERIFIED；aggregator
  对 DOI 只作 corroboration（`status_under_policy` 结构性保证）。
- **标题相似度**（Req 4.1）：回查命中 **且** 标题相似度达阈值才记 VERIFIED；命中但标题
  实质不符 → 记 CONFLICT（该标识符指向了另一篇作品）。
- **预算**：所有 verify 调用计入注入的 :class:`VerifyBudget`（调用次数 + provider 时间），
  触顶即停、如实标注，不无界遍历标识符。
- **缓存**：``(provider, id_type, normalized_id)`` 级；``PROVIDER_ERROR`` 用短 TTL、
  ``FOUND``/``NOT_FOUND_AT_SOURCE`` 在本次 Loop 内固定复用。
- **门槛**：至少一条 strict final VERIFIED 且无 open final conflict → 达严格门槛；
  canonical identifier 在其 final 源 ``NOT_FOUND_AT_SOURCE`` → 不可接受；
  次要 identifier not-found → 记 issue、不静默删除。

本模块**不 import `tasking`、不读全局 config、不读全局时钟**（阈值/预算/clock 注入），
**不改**旧 ``ReferenceVerifier`` / ``search_and_verify``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from paper_agent.observability.current import ambient_publish
from difflib import SequenceMatcher
from typing import Callable, Sequence

from paper_agent.literature import dedup as _dedup
from paper_agent.literature import identifiers as _ids
from paper_agent.literature.sources import provenance_of
from paper_agent.literature.verification import (
    STRICT_FINAL_V1_REGISTRY,
    AuthorityRegistry,
    EventKind,
    VerificationEvent,
    VerificationPolicy,
    VerificationStatus,
    make_event,
    status_under_policy,
)
from paper_agent.providers.retrieval.capabilities import FetchStatus, VerifyProvider


# ---------------------------------------------------------------- 预算

@dataclass
class VerifyBudget:
    """核验调用预算（A2c-1 版；A2c-4 的 BudgetLedger 可包裹/替换）。"""

    max_verify_calls: int = 30
    provider_time_budget_s: float = 60.0
    calls_made: int = 0
    time_spent_s: float = 0.0

    @property
    def exhausted(self) -> bool:
        return (
            self.calls_made >= self.max_verify_calls
            or self.time_spent_s >= self.provider_time_budget_s
        )

    def charge(self, *, elapsed_s: float = 0.0) -> None:
        self.calls_made += 1
        self.time_spent_s += elapsed_s


# ---------------------------------------------------------------- 缓存

@dataclass
class _CacheEntry:
    result: object
    is_error: bool
    stored_at: float


class VerificationCache:
    """``(provider, id_type, normalized_id)`` → FetchResult 缓存。

    ``PROVIDER_ERROR`` 用短 TTL（便于后续 Attempt 重试）；``FOUND`` /
    ``NOT_FOUND_AT_SOURCE`` 视为稳定结论、本 Loop 内固定复用。
    """

    def __init__(self, *, error_ttl_s: float = 300.0) -> None:
        self._d: dict[tuple[str, str, str], _CacheEntry] = {}
        self._error_ttl = error_ttl_s

    def get(self, key: tuple[str, str, str], *, now: float):
        entry = self._d.get(key)
        if entry is None:
            return None
        if entry.is_error and (now - entry.stored_at) > self._error_ttl:
            return None            # 错误结果过期 → 允许重试
        return entry.result

    def put(self, key: tuple[str, str, str], result, *, now: float) -> None:
        is_error = getattr(result, "status", None) is FetchStatus.PROVIDER_ERROR
        self._d[key] = _CacheEntry(result=result, is_error=is_error, stored_at=now)


# ---------------------------------------------------------------- 结果

@dataclass(frozen=True)
class VerificationAssessment:
    """一次交叉核验的结果（策略/版本随附，供 Batch/trace 记录）。"""

    events: tuple[VerificationEvent, ...]
    strict_status: VerificationStatus
    policy: VerificationPolicy
    authority_registry_version: str
    meets_strict_threshold: bool
    canonical_verified: bool
    issues: tuple[str, ...] = field(default_factory=tuple)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _title_conflict(a_title: str, b_title: str, threshold: float) -> bool:
    """两标题是否实质不符（回查命中却指向另一篇）。任一为空则无法比较，判不冲突。"""
    na, nb = _dedup.normalize_title(a_title), _dedup.normalize_title(b_title)
    if not na or not nb:
        return False
    return SequenceMatcher(None, na, nb).ratio() < threshold


class CrossVerifier:
    """按标识符类型选权威源、多次回查、产多条事件、折叠严格状态。"""

    def __init__(
        self,
        verify_providers: Sequence[tuple[str, VerifyProvider]],
        *,
        registry: AuthorityRegistry = STRICT_FINAL_V1_REGISTRY,
        title_similarity_threshold: float = 0.85,
        clock: Callable[[], float] = time.monotonic,
        now_iso: Callable[[], str] = _now_iso,
    ) -> None:
        self._providers = list(verify_providers)
        self._registry = registry
        self._sim_threshold = title_similarity_threshold
        self._clock = clock
        self._now_iso = now_iso

    def _pick_provider(self, id_type: str):
        """为某标识符类型选源：优先 final，否则 corroboration；都无则 None。"""
        final_pick = None
        corr_pick = None
        for name, provider in self._providers:
            authority = self._registry.authority_of(name, id_type)
            if authority == "final" and final_pick is None:
                final_pick = (name, provider, "final")
            elif authority == "corroboration" and corr_pick is None:
                corr_pick = (name, provider, "corroboration")
        return final_pick or corr_pick

    def assess(
        self,
        candidate,
        *,
        budget: VerifyBudget,
        cache: VerificationCache,
    ) -> VerificationAssessment:
        keys = sorted(_ids.work_keys(candidate.work_identifiers))
        canonical = _ids.pick_by_priority(candidate.work_identifiers)
        events: list[VerificationEvent] = []
        issues: list[str] = []
        canonical_not_found = False

        for seq, (id_type, norm_id) in enumerate(keys):
            pick = self._pick_provider(id_type)
            if pick is None:
                issues.append(f"no_verifier:{id_type}")
                continue
            name, provider, authority = pick
            if budget.exhausted:
                issues.append("budget_exhausted")
                break

            cache_key = (name, id_type, norm_id)
            result = cache.get(cache_key, now=self._clock())
            if result is None:
                t0 = self._clock()
                result = provider.fetch_metadata(id_type, norm_id)
                _elapsed = self._clock() - t0
                budget.charge(elapsed_s=_elapsed)
                cache.put(cache_key, result, now=self._clock())
                # 归因用：verify 是网络往返，与 relevance LLM 争同一个时间预算。
                # 不出耗时就无法判断 B2（先判相关性、只核验 survivor）到底能省多少。
                ambient_publish(
                    "VERIFY_FETCH",
                    provider=name,
                    id_type=id_type,
                    status=getattr(result.status, "value", str(result.status)),
                    elapsed_ms=round(_elapsed * 1000, 1),
                    cached=False,
                )
            else:
                ambient_publish("VERIFY_FETCH", provider=name, id_type=id_type,
                                status=getattr(result.status, "value", str(result.status)),
                                elapsed_ms=0.0, cached=True)

            events.append(self._to_event(result, name, authority, id_type, norm_id, candidate, seq))

            is_canonical = canonical is not None and (id_type, norm_id) == canonical
            if result.status is FetchStatus.NOT_FOUND_AT_SOURCE:
                if is_canonical and authority == "final":
                    canonical_not_found = True
                elif not is_canonical:
                    issues.append(f"secondary_not_found:{id_type}")  # 记 issue，不静默删

        strict_status = status_under_policy(
            events, VerificationPolicy.STRICT_FINAL_V1, self._registry
        )
        canonical_verified = (
            canonical is not None
            and strict_status is VerificationStatus.VERIFIED
            and not canonical_not_found
        )
        meets = strict_status is VerificationStatus.VERIFIED and not canonical_not_found

        return VerificationAssessment(
            events=tuple(events),
            strict_status=strict_status,
            policy=VerificationPolicy.STRICT_FINAL_V1,
            authority_registry_version=self._registry.version,
            meets_strict_threshold=meets,
            canonical_verified=canonical_verified,
            issues=tuple(issues),
        )

    def _to_event(self, result, name, authority, id_type, norm_id, candidate, seq) -> VerificationEvent:
        if result.status is FetchStatus.FOUND:
            fetched_title = getattr(result.candidate, "title", "") or ""
            if _title_conflict(getattr(candidate, "title", "") or "", fetched_title, self._sim_threshold):
                kind = EventKind.CONFLICT      # 命中却标题不符 → 指向另一篇
            else:
                kind = EventKind.VERIFIED
        elif result.status is FetchStatus.NOT_FOUND_AT_SOURCE:
            kind = EventKind.NOT_FOUND
        else:
            kind = EventKind.PROVIDER_ERROR

        return make_event(
            kind,
            source=name,
            provenance=provenance_of(name),
            authority=authority,          # 存策略版权限；严格投影另按 registry 重算
            identifier_type=id_type,
            identifier=norm_id,
            checked_at=self._now_iso(),
            details=getattr(result, "detail", ""),
            seq=seq,
        )


__all__ = [
    "VerifyBudget",
    "VerificationCache",
    "VerificationAssessment",
    "CrossVerifier",
]
