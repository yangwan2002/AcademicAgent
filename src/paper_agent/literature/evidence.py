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
from dataclasses import dataclass
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
    # >0 表示摘要被截断，值为截断前的完整长度（0 = 未截断）。**显式标记，不静默降级**：
    # Gate 与 trace 都要能看出证据是截断版。
    abstract_truncated_from: int = 0
    # True 表示该记录**有**摘要但因预算未纳入（退回题录，X-10 A 的分级降级）。
    # 与"记录本身没摘要"必须分开：前者是我们的取舍，后者是数据缺失，
    # 渲染措辞不同（见 `context_builder.build_evidence_block`）——把"我没给"
    # 说成"原文没有"，模型会据此向用户断言该文献无摘要，那是假话。
    abstract_dropped: bool = False

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
    # 分级降级实际落在哪一档（见 `build_bundle`）。full = 未降级。
    degradation: str = "full"
    # 该档使用的摘要上限（0 = 不限）。便于 trace 与 A/B 复现。
    abstract_limit: int = 0

    def reference_ids(self) -> tuple[str, ...]:
        return tuple(e.reference_id for e in self.entries)

    def truncated_entry_count(self) -> int:
        """被截断摘要的条数（供 Gate/呈现层提示降级范围）。"""
        return sum(1 for e in self.entries if e.abstract_truncated_from > 0)

    def dropped_abstract_count(self) -> int:
        """有摘要但被退回题录的条数（X-10 A 的最后一档降级）。

        与 `truncated_entry_count()` 分开报：截断是"给了一部分"，退回题录是
        "一点没给"，对写作质量的影响量级不同，合并成一个数会掩盖后者。
        """
        return sum(1 for e in self.entries if e.abstract_dropped)


class BundleStatus(str, Enum):
    OK = "ok"
    CONTEXT_BUDGET_INSUFFICIENT = "context_budget_insufficient"


@dataclass(frozen=True)
class BundleBuildResult:
    status: BundleStatus
    bundle: EvidenceBundle | None = None
    bundle_gaps: tuple = ()               # 预算不足时逐维度缺口
    excluded_count: int = 0
    # 预算不足时的**量化**信息：光说"装不下"无法判断该提预算还是该减篇数。
    required_chars: int = 0               # 最省一档实际需要多少字符
    budget_chars: int = 0                 # 当时的预算
    min_abstract_limit: int = 0           # 已尝试的最短摘要档

    def shortfall_chars(self) -> int:
        """还差多少字符（<=0 表示不缺）。供呈现层给出可执行建议。"""
        return max(0, self.required_chars - self.budget_chars)


# 摘要上限阶梯（字符）。全摘要装不下时依次下探，取第一个装得下的档。
# 1000 ≈ 摘要前 3–5 句（问题陈述 + 方法 + 主要结论），真机摘要中位 1456 字符；
# 400 是下限，再短就只剩问题陈述、对写作贡献有限，故不再下探而是交上层决策。
_ABSTRACT_LIMIT_LADDER = (1000, 700, 400)



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

# 退回题录的比例阶梯（X-10 A）。截断阶梯用尽后依次尝试：把**排序最靠后**的这个比例
# 的条目退成只给题录，摘要留给靠前的。0.5 → 0.75 → 0.9，最后一档仍给最相关的 10%
# 留摘要。
#
# **为什么按排序位次而不是按 relevance verdict**：默认 `STRICT_ACCEPTANCE` 只接受
# `relevant`，`eligible` 里全是同一个 verdict —— 按 verdict 分级在默认档下**恒等于
# 什么都不做**，正好在 X-10 A 抱怨的那个场景里失效。而 `eligible` 已按
# （verdict → confidence → 年份新 → id）稳定排好序，位次天然是"谁更该留摘要"的
# 现成排名，且 verdict 混合时（`WEAKLY_ACCEPTANCE`）weakly 自然排在后面先被退掉，
# 两种档位下都成立。
#
# **不含 1.0**：全部退成题录等于整个证据集没有可引用的具体内容，那时该让上层决策
# （提预算 / 减篇数），而不是产出一个看着 OK 实则支撑不了写作的 bundle。
_DROP_ABSTRACT_LADDER = (0.5, 0.75, 0.9)

# 摘要截断的句末标记（中英）。截断优先落在句边界，避免把半句送进 prompt。
_SENTENCE_ENDS = "。！？.!?"
# 回退窗口：从硬截点往前最多找这么多字符的句末。找不到就硬截。
# 300 按真机实测定（2026-08-10，25 条 OpenAlex 摘要）：句长中位 149、p90 256、max 484。
# 200 时 1/25 条回退失败被硬截在半词上；300 覆盖 p90，且仍远小于最短档 400。
# 太大会让实际长度显著短于目标档，太小则形同没有回退。
_BOUNDARY_LOOKBACK = 300


def _truncate_abstract_end(text: str, limit: int) -> int:
    """返回摘要应截到的 offset（码位），优先句边界。

    只回答"截到哪"，不切文本——切片由 `make_span(start=, end=)` 完成，这样
    `verbatim_text == normalized[start:end)` 的不变量（Req 12.5）仍然由它保证，
    `content_hash` 记的是**完整**字段哈希，故截断后溯源依然成立。
    """
    normalized = normalize_source(text)
    if limit <= 0 or len(normalized) <= limit:
        return len(normalized)
    lo = max(0, limit - _BOUNDARY_LOOKBACK)
    for i in range(limit - 1, lo - 1, -1):
        if normalized[i] in _SENTENCE_ENDS:
            return i + 1          # 含句末标点
    # 窗口内无句末（超长句，实测 1/25 会走到这里）→ 退到词边界，至少不截在半词上。
    ws = normalized.rfind(" ", lo, limit)
    if ws > lo:
        return ws
    return limit                  # 连空格都没有（如无空格中文长句）→ 硬截


def _entry_of(record, verdict, confidence, bucket, *,
              abstract_limit: int = 0, drop_abstract: bool = False) -> BundleEntry:
    """构造 BundleEntry。

    `abstract_limit>0` → 摘要按该上限截断（分级降级第 2 档，见 `build_bundle`）。
    截断保留摘要**开头**：问题陈述与贡献通常在前几句，实验细节在后半段，对写作
    价值最低。

    `drop_abstract=True` → 完全不建 abstract span，只留题录（最后一档，X-10 A）。
    结果 `evidence_available=False`，于是既有的「不得据标题推导具体结论」约束
    自动生效 —— 这条降级不需要新的 prompt 机制，复用 Req 12.12 那条。
    """
    spans: list[EvidenceSpan] = []
    truncated_from = 0
    dropped = bool(drop_abstract and record.abstract)
    if record.abstract and not drop_abstract:
        end = _truncate_abstract_end(record.abstract, abstract_limit)
        full_len = len(normalize_source(record.abstract))
        if end < full_len:
            truncated_from = full_len
        spans.append(make_span(record.reference_id, "abstract", record.abstract,
                               end=(end if truncated_from else None),
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
        abstract_truncated_from=truncated_from,
        abstract_dropped=dropped,
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
    abstract_ladder: tuple[int, ...] = _ABSTRACT_LIMIT_LADDER,
    drop_ladder: tuple[float, ...] = _DROP_ABSTRACT_LADDER,
) -> BundleBuildResult:
    """构造 quota-aware EvidenceBundle（design D6）。

    只取 STRICT verified + 当前 task_fingerprint relevance 已接受 + 过全部 filters 的记录；
    先固定 `preexisting_cited_ids`，再保 zh/en 下限、补 total，最后用剩余预算补高相关。

    `acceptance`（F-3）：接受语义由**调用方注入**，必须与 `assess_availability` 用
    同一对象——否则会出现「统计说够了但证据集是空的」。此前本函数硬编码
    `("relevant",)`。默认严格 → 不传时行为逐字节不变。

    **分级降级**（三档，依次尝试，取第一个装得下的）：

    1. 全摘要（`degradation="full"`）
    2. 按 `abstract_ladder` 逐档**截短**摘要（`"truncated_abstract"`）
    3. 按 `drop_ladder` 逐档**退回题录**：排序靠后的条目只给题录、摘要留给靠前的
       （`"bibliographic_only"`）；每退一档都重新从全摘要试起，因为腾出的空间
       可能让留下的条目拿回全摘要

    三档都装不下才返回 `CONTEXT_BUDGET_INSUFFICIENT`（附 `required_chars` /
    `shortfall_chars()`）。降级在 `EvidenceBundle.degradation`、
    `BundleEntry.abstract_truncated_from` 与 `.abstract_dropped` 上**显式可见**，不静默。

    第 3 档是 X-10 A 的修复：在它之前，预算不足会**否决用户明确提出的引用数量**——
    而预算是实现约束、不是学术正确性约束。真机实测（2 relevant / 8 weakly）
    「relevant 全摘要 + weakly 只给题录」只需约 4000 字符，即连 8000 都够用；
    撞墙的是"一律全摘要"这个一刀切。分级顺序的依据是 P-3a 三臂 A/B：摘要里的
    量化结果是学术写作的核心价值，所以要优先留给最可能被实质引用的那批。

    `abstract_ladder` / `drop_ladder`：两条阶梯都可注入以做受控实验
    （各传 `()` 即关闭对应档、恢复旧行为，P-3a 三臂 A/B 用；传单元素元组可钉死某一档）。

    动机（真机 2026-08-10，total=20）：25 条合格记录需 28698 字符 > 预算 24000，
    旧实现直接判 INSUFFICIENT，检索白跑。而"20 篇截到 1000 字符"装得下且信息覆盖
    更广——摘要后半段多为实验细节，对写引言价值低于多覆盖 3 篇文献。
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

    # `eligible` 的位次即"谁更该留摘要"的排名（见 `_DROP_ABSTRACT_LADDER`）。
    rank_of = {t[0].reference_id: i for i, t in enumerate(eligible)}

    def _select(abstract_limit: int, drop_ratio: float = 0.0):
        """在给定摘要档下走 2→5 步。返回 (entries, cost, truncated, 是否装下下限)。

        `drop_ratio>0`：排序最靠后的这个比例的条目**只给题录**（不建 abstract span）。
        """
        # 保留摘要的名额：至少 1 条，否则最后一档会退成"全是题录"。
        keep_n = len(eligible) if drop_ratio <= 0 else max(
            1, int(len(eligible) * (1.0 - drop_ratio)))

        def _drops(rid: str) -> bool:
            return drop_ratio > 0 and rank_of.get(rid, 0) >= keep_n

        selected: list[str] = []

        def _take(rid: str) -> None:
            if rid not in selected and rid in by_id:
                selected.append(rid)

        # 2) 固定原文已合法引用者（rewrite：可推进「正文实际引用」配额）
        for rid in preexisting_cited_ids:
            _take(rid)

        # 3) 保语言下限
        for dim, bucket in ((CountDimension.LANGUAGE_ZH, "zh"),
                            (CountDimension.LANGUAGE_EN, "en")):
            need = _lower_bound(effective_constraints, dim)
            if need <= 0:
                continue
            have = sum(1 for rid in selected
                       if counts_toward(classifier(by_id[rid][0]), bucket, policy))
            for rec, _v, _c, _b in eligible:
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

        entries = [_entry_of(*by_id[rid], abstract_limit=abstract_limit,
                             drop_abstract=_drops(rid))
                   for rid in selected]
        cost = sum(e.char_cost() for e in entries)
        if budget_chars > 0 and cost > budget_chars:
            return entries, cost, 0, False

        # 5) 剩余预算补高相关
        truncated = 0
        for rec, _v, _c, _b in eligible:
            if rec.reference_id in selected:
                continue
            entry = _entry_of(*by_id[rec.reference_id], abstract_limit=abstract_limit,
                              drop_abstract=_drops(rec.reference_id))
            if budget_chars > 0 and cost + entry.char_cost() > budget_chars:
                truncated += 1
                continue
            selected.append(rec.reference_id)
            entries.append(entry)
            cost += entry.char_cost()
        return entries, cost, truncated, True

    # 分级降级：全摘要装不下配额下限时，先截摘要再谈放弃篇数（改法 C）。
    # 只调数字（8000→24000）治不了根：预算是常量、配额是用户输入的变量，
    # total 一大就必然重现（真机 total=20 需 28698 > 24000）。
    # 截断保住 verbatim 溯源（见 `_truncate_abstract_end`），故这是无损降级；
    # 而少给篇数会直接违背用户明确要求的引用数量，代价更高——所以先截后减。
    entries, cost, truncated, ok = _select(0)
    degradation, used_limit = "full", 0
    if not ok:
        for limit in abstract_ladder:
            entries, cost, truncated, ok = _select(limit)
            if ok:
                degradation, used_limit = "truncated_abstract", limit
                break

    # X-10 A：截断阶梯用尽仍装不下 → **按相关性退回题录**，而不是否决用户的要求。
    #
    # 为什么这一档必须存在：`build_bundle` 走到 INSUFFICIENT 就是在用预算否决用户
    # 明确提出的引用数量，而预算是我们的实现约束、不是学术正确性约束。真机实测
    # （2 relevant / 8 weakly）「relevant 全摘要 + weakly 只给题录」只需约 4000 字符，
    # 即**连 8000 都够用** —— 撞墙的是"一律全摘要"这个一刀切，不是预算值。
    #
    # 为什么分级退而不是一律退题录：P-3a 三臂 A/B 已证明摘要里的量化结果正是学术
    # 写作的核心价值（全摘要 12 个带出处的百分比 → 截断后 0 个）。所以摘要要优先
    # 留给最可能被实质引用的那批 —— 排名用 `eligible` 已有的稳定排序，理由见
    # `_DROP_ABSTRACT_LADDER`（按 verdict 分在默认 strict 档下恒等于没分）。
    #
    # 复用既有机制、不新建：退回题录后 `evidence_available=False`，Req 12.12 那条
    # 「不得据标题推导具体结论」自动生效，模型不会拿题录编结论。
    if not ok and drop_ladder:
        for ratio in drop_ladder:
            # 每一档退题录后，摘要档仍**从全摘要重试**：退掉靠后条目的摘要腾出的空间，
            # 可能足以让留下的高相关条目拿回全摘要 —— 那正是我们想要的取舍
            # （P-3a：全摘要少篇 > 截摘要多篇）。
            for limit in (0, *abstract_ladder):
                entries, cost, truncated, ok = _select(limit, ratio)
                if ok:
                    degradation = "bibliographic_only"
                    used_limit = limit
                    break
            if ok:
                break

    if not ok:
        # 三档全试过仍装不下 → 真无解，交上层决策。附**量化**缺口：
        # 光说"装不下"无法判断是预算设小了还是摘要偏长。
        #
        # `cost` 取自**最后一次** `_select`，即最深的退题录档 + 最短摘要档 ——
        # 阶梯每深一档都更省，故它就是我们能做到的**最省配置**的开销。
        # 于是 `shortfall_chars()` 是"无论怎么降级都还差多少"，正是上层要的那个数
        # （比按全摘要报缺口更有指导性：后者会让人以为提一点预算就够）。
        gaps = tuple(
            (d.value, _lower_bound(effective_constraints, d))
            for d in (CountDimension.TOTAL, CountDimension.LANGUAGE_ZH, CountDimension.LANGUAGE_EN)
            if _lower_bound(effective_constraints, d) > 0
        )
        return BundleBuildResult(
            status=BundleStatus.CONTEXT_BUDGET_INSUFFICIENT, bundle=None,
            bundle_gaps=gaps, excluded_count=excluded,
            required_chars=cost, budget_chars=budget_chars,
            min_abstract_limit=(abstract_ladder[-1] if abstract_ladder else 0),
        )

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
        degradation=degradation,
        abstract_limit=used_limit,
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
