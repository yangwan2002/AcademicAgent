"""EvidenceSpan + quota-aware EvidenceBundle（A2d-2，design-a2d D5/D6）。

**EvidenceSpan 是唯一证据单位**（Req 12.1/12.7），本期实现 Req 12 的最小完整契约：
offset 以 **NFC 规范化后的 Unicode 码位**为单位（Req 12.4），
`verbatim_text == normalized[start:end)`（Req 12.5），content_hash 变则 span stale（Req 12.6）。

**EvidenceBundle 是 quota-aware 的**（D6）：只按相关性截断会把凑语言配额所需的中文文献截掉，
故构造顺序为 筛 → 固定原有合法引用 → 保 zh/en 下限 → 补 total → 稳定排序 → 剩余预算补高相关；
预算装不下 effective 下界时返回 `CONTEXT_BUDGET_INSUFFICIENT`，**不产可用 Bundle**。

不 import `tasking`、不读全局 config（classifier/policy/budget 注入）。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from paper_agent.literature.language import counts_toward
from paper_agent.literature.quota import (
    CountDimension,
    CountOperator,
    passes_all_filters,
)
from paper_agent.literature.relevance import (
    STRICT_ACCEPTANCE,
    relevance_content_hash,
)
from paper_agent.literature.untrusted import clean_external_text
from paper_agent.literature.verification import (
    VerificationPolicy,
    VerificationStatus,
    status_under_policy,
)

_SOURCE_MAX = 8000


class EvidenceCapability(str, Enum):
    """证据可用范围（Req 12 Glossary）。"""

    DISCOVERY_ONLY = "discovery_only"   # 来自 title：只可用于发现/初筛，不可作 Claim 支撑
    CLAIM_SUPPORT = "claim_support"     # 来自 abstract：可作支撑证据


def normalize_source(text: str) -> str:
    """外部文本清洗（A2a）后 NFC 规范化。offset/hash 全部基于本函数输出。"""
    cleaned = clean_external_text(text or "", max_chars=_SOURCE_MAX)
    return unicodedata.normalize("NFC", cleaned)


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def capability_of(source_field: str) -> EvidenceCapability:
    """按 source_field 判 capability（Req 12.2）。"""
    f = (source_field or "").strip().lower()
    if f == "title":
        return EvidenceCapability.DISCOVERY_ONLY
    return EvidenceCapability.CLAIM_SUPPORT


@dataclass(frozen=True)
class EvidenceSpan:
    span_id: str
    reference_id: str
    source_field: str                 # title | abstract | abstract_section:<name>
    capability: EvidenceCapability
    source_version: str
    content_hash: str                 # 该字段**完整** normalized 文本的哈希
    start_offset: int                 # Unicode 码位，[start, end)
    end_offset: int
    verbatim_text: str
    verbatim_hash: str


def make_span(
    reference_id: str,
    source_field: str,
    text: str,
    *,
    start: int | None = None,
    end: int | None = None,
    source_version: str = "",
) -> EvidenceSpan:
    """构造一个 EvidenceSpan。默认整段一个连续 span；给定 start/end 则产确定性切片。

    保证 `verbatim_text == normalized[start:end)`（Req 12.5）。
    """
    normalized = normalize_source(text)
    lo = 0 if start is None else max(0, int(start))
    hi = len(normalized) if end is None else min(len(normalized), int(end))
    if hi < lo:
        hi = lo
    verbatim = normalized[lo:hi]
    vhash = _sha(verbatim)
    chash = _sha(normalized)
    seed = f"{reference_id}|{source_field}|{source_version}|{lo}|{hi}|{vhash}"
    return EvidenceSpan(
        span_id=_sha(seed),
        reference_id=reference_id,
        source_field=source_field,
        capability=capability_of(source_field),
        source_version=source_version,
        content_hash=chash,
        start_offset=lo,
        end_offset=hi,
        verbatim_text=verbatim,
        verbatim_hash=vhash,
    )


def is_stale(span: EvidenceSpan, current_source_text: str) -> bool:
    """字段内容已变（content_hash 不符）→ span stale，不得沿用旧 offset（Req 12.6）。"""
    return _sha(normalize_source(current_source_text)) != span.content_hash


# ---------------------------------------------------------------- Bundle

@dataclass(frozen=True)
class BundleEntry:
    reference_id: str
    bibkey: str = ""
    numeric_alias: str = ""
    title: str = ""
    year: int | None = None
    language_bucket: str = "unknown"
    relevance_verdict: str = ""
    relevance_confidence: str = ""
    spans: tuple[EvidenceSpan, ...] = ()
    evidence_available: bool = False      # 有无 CLAIM_SUPPORT span（Req 12.12）

    def char_cost(self) -> int:
        """送进 prompt 的字符成本（span 文本 + 元数据近似）。

        标题**不重复计入**：`_entry_of` 已为 title 建了一个 span,它已在
        ``sum(span)`` 里。此前额外加 ``len(self.title)`` 使每条虚高约一个标题的长度
        （真机实测 10 条虚高约 800 字符）。40 是渲染时的固定开销近似
        （引用标记、年份、语言桶、换行——见 `context_builder.build_evidence_block`）。
        """
        return sum(len(s.verbatim_text) for s in self.spans) + 40


@dataclass(frozen=True)
class EvidenceBundle:
    bundle_id: str
    task_fingerprint: str
    entries: tuple[BundleEntry, ...] = ()
    effective_constraints: tuple = ()
    truncated_count: int = 0
    budget_chars: int = 0
    policy_version: str = VerificationPolicy.STRICT_FINAL_V1.value
    authority_registry_version: str = ""

    def reference_ids(self) -> tuple[str, ...]:
        return tuple(e.reference_id for e in self.entries)


class BundleStatus(str, Enum):
    OK = "ok"
    CONTEXT_BUDGET_INSUFFICIENT = "context_budget_insufficient"


@dataclass(frozen=True)
class BundleBuildResult:
    status: BundleStatus
    bundle: EvidenceBundle | None = None
    bundle_gaps: tuple = ()               # 预算不足时逐维度缺口
    excluded_count: int = 0


def _lower_bound(constraints, dimension) -> int:
    for c in constraints:
        if c.dimension is not dimension:
            continue
        if c.operator in (CountOperator.MIN, CountOperator.EXACT):
            return int(c.value)
        if c.operator is CountOperator.RANGE:
            return int(c.value[0])
    return 0


_VERDICT_RANK = {"relevant": 0, "weakly_relevant": 1, "unrelated": 2, "": 3}
_CONF_RANK = {"high": 0, "medium": 1, "low": 2, "": 3, None: 3}


def _entry_of(record, verdict, confidence, bucket) -> BundleEntry:
    spans: list[EvidenceSpan] = []
    if record.abstract:
        spans.append(make_span(record.reference_id, "abstract", record.abstract,
                               source_version=record.abstract_content_hash))
    if record.title:
        spans.append(make_span(record.reference_id, "title", record.title))
    has_claim = any(s.capability is EvidenceCapability.CLAIM_SUPPORT for s in spans)
    return BundleEntry(
        reference_id=record.reference_id,
        bibkey=record.bibkey,
        numeric_alias=(record.numeric_aliases[0] if record.numeric_aliases else ""),
        title=record.title,
        year=record.year,
        language_bucket=bucket,
        relevance_verdict=verdict or "",
        relevance_confidence=confidence or "",
        spans=tuple(spans),
        evidence_available=has_claim,
    )


def build_bundle(
    records,
    *,
    criteria,
    quota,
    relevance_store,
    effective_constraints,
    budget_chars: int,
    classifier,
    policy: str = "high_only",
    judge_key: str = "",
    content_hasher=relevance_content_hash,
    preexisting_cited_ids: tuple[str, ...] = (),
    authority_registry_version: str = "",
    acceptance=STRICT_ACCEPTANCE,
) -> BundleBuildResult:
    """构造 quota-aware EvidenceBundle（design D6）。

    只取 STRICT verified + 当前 task_fingerprint relevance 已接受 + 过全部 filters 的记录；
    先固定 `preexisting_cited_ids`，再保 zh/en 下限、补 total，最后用剩余预算补高相关。

    `acceptance`（F-3）：接受语义由**调用方注入**，必须与 `assess_availability` 用
    同一对象——否则会出现「统计说够了但证据集是空的」。此前本函数硬编码
    `("relevant",)`。默认严格 → 不传时行为逐字节不变。
    """
    task_fp = criteria.fingerprint()

    # 1) 筛：STRICT verified + relevance 接受 + filters
    eligible: list[tuple[object, str, str, str]] = []   # (record, verdict, conf, bucket)
    excluded = 0
    for rec in records:
        if status_under_policy(rec.verification_events, VerificationPolicy.STRICT_FINAL_V1) \
                is not VerificationStatus.VERIFIED:
            excluded += 1
            continue
        if not passes_all_filters(rec, quota.eligibility_filters,
                                  classifier=classifier, policy=policy):
            excluded += 1
            continue
        asmt = relevance_store.get(rec.reference_id, task_fp,
                                   content_hasher(rec.title, rec.abstract), judge_key)
        if not acceptance.is_accepted(asmt):
            excluded += 1
            continue
        verdict = classifier(rec)
        bucket = getattr(verdict, "quota_bucket", "unknown")
        eligible.append((rec, asmt.verdict, asmt.confidence or "", bucket))

    # 稳定排序：relevance verdict → confidence → year desc → reference_id
    eligible.sort(key=lambda t: (
        _VERDICT_RANK.get(t[1], 3), _CONF_RANK.get(t[2], 3),
        -(t[0].year or 0), t[0].reference_id,
    ))

    by_id = {t[0].reference_id: t for t in eligible}
    selected: list[str] = []

    def _take(rid: str) -> None:
        if rid not in selected and rid in by_id:
            selected.append(rid)

    # 2) 固定原文已合法引用者（rewrite：可推进「正文实际引用」配额）
    for rid in preexisting_cited_ids:
        _take(rid)

    # 3) 保语言下限
    for dim, bucket in ((CountDimension.LANGUAGE_ZH, "zh"), (CountDimension.LANGUAGE_EN, "en")):
        need = _lower_bound(effective_constraints, dim)
        if need <= 0:
            continue
        have = sum(1 for rid in selected
                   if counts_toward(classifier(by_id[rid][0]), bucket, policy))
        for rec, _v, _c, b in eligible:
            if have >= need:
                break
            if rec.reference_id in selected:
                continue
            if counts_toward(classifier(rec), bucket, policy):
                _take(rec.reference_id)
                have += 1

    # 4) 补 total
    total_need = _lower_bound(effective_constraints, CountDimension.TOTAL)
    for rec, _v, _c, _b in eligible:
        if len(selected) >= total_need:
            break
        _take(rec.reference_id)

    # 预算校验：装不下 effective 下界 → 需决策，不产可用 Bundle
    entries = [_entry_of(*by_id[rid]) for rid in selected]
    cost = sum(e.char_cost() for e in entries)
    if budget_chars > 0 and cost > budget_chars:
        gaps = tuple(
            (d.value, _lower_bound(effective_constraints, d))
            for d in (CountDimension.TOTAL, CountDimension.LANGUAGE_ZH, CountDimension.LANGUAGE_EN)
            if _lower_bound(effective_constraints, d) > 0
        )
        return BundleBuildResult(
            status=BundleStatus.CONTEXT_BUDGET_INSUFFICIENT, bundle=None,
            bundle_gaps=gaps, excluded_count=excluded,
        )

    # 5) 剩余预算补高相关
    truncated = 0
    for rec, _v, _c, _b in eligible:
        if rec.reference_id in selected:
            continue
        entry = _entry_of(*by_id[rec.reference_id])
        if budget_chars > 0 and cost + entry.char_cost() > budget_chars:
            truncated += 1
            continue
        selected.append(rec.reference_id)
        entries.append(entry)
        cost += entry.char_cost()

    payload = {
        "task_fingerprint": task_fp,
        "entries": [
            {"rid": e.reference_id, "spans": [s.span_id for s in e.spans]} for e in entries
        ],
        "effective": [
            (c.dimension.value, c.operator.value, c.value) for c in effective_constraints
        ],
    }
    bundle_id = _sha(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    bundle = EvidenceBundle(
        bundle_id=bundle_id,
        task_fingerprint=task_fp,
        entries=tuple(entries),
        effective_constraints=tuple(effective_constraints),
        truncated_count=truncated,
        budget_chars=budget_chars,
        authority_registry_version=authority_registry_version,
    )
    return BundleBuildResult(status=BundleStatus.OK, bundle=bundle, excluded_count=excluded)


__all__ = [
    "EvidenceCapability",
    "EvidenceSpan",
    "normalize_source",
    "capability_of",
    "make_span",
    "is_stale",
    "BundleEntry",
    "EvidenceBundle",
    "BundleStatus",
    "BundleBuildResult",
    "build_bundle",
]
