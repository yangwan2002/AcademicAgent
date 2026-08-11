"""SearchIntent：区分「检索什么」（语义任务）与「怎么搜」（查询策略）（A2f-1，design-a2f D1/D2）。

dogfood 暴露:带配额写作在用户没给显式 search_query 时,系统拿「引言/Introduction」当检索词。
A2f 把「检索什么」建模为**冻结的 SearchIntent**:
- `semantic_topic + section_purpose` 决定「什么文献算切题」→ 进 RelevanceCriteria / TaskFingerprint;
- `seeds` 决定「到 provider 里怎么搜」→ 只影响召回,**不进** semantic fingerprint。

两种哈希严格分离(D1)：`semantic_fingerprint()` 只含语义字段(seed 变不影响相关性缓存);
`strategy_hash()` = semantic_fingerprint + 规范化 seeds(供 checkpoint/恢复/审计)。

纯值对象 + 验证器,**不 import** tasking / session / config。构造只经验证工厂/`__post_init__`,
保证 topic 非空、seed 1–6 个且均已规范化;失败用 outcome 状态表达,**绝不**用「空 SearchIntent」
表示失败(那会重新引入拿章节名硬搜的旧问题)。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from enum import Enum

from paper_agent.literature.untrusted import clean_external_text

_TOPIC_MIN, _TOPIC_MAX = 3, 500
_SEED_MIN, _SEED_MAX = 2, 300
_MAX_SEEDS = 6
_SCHEMA_VERSION = "search-intent-v1"

#: 纯通用章节名——**不得**单独作为检索主题或 seed（会稀释真实主题，dogfood 教训）。
#: 判定按**规范化后完全等于**这些词（子串包含会误伤「引言中的深度学习」这类含实义标题）。
_GENERIC_TITLES: frozenset[str] = frozenset({
    "引言", "前言", "背景", "研究背景", "相关工作", "相关研究", "文献综述", "综述",
    "方法", "研究方法", "实验", "结果", "讨论", "结论", "总结", "摘要", "致谢",
    "introduction", "background", "related work", "relatedwork", "literature review",
    "review", "method", "methods", "methodology", "experiment", "experiments",
    "result", "results", "discussion", "conclusion", "conclusions", "abstract",
})


class SearchIntentOrigin(str, Enum):
    """SearchIntent / seed 的来源（可观测，不塞任意字符串）。"""

    USER_EXPLICIT = "user_explicit"          # 用户显式给了 search_query
    OPERATION_DERIVED = "operation_derived"  # 由 operation 语义（purpose/details/…）投影
    PROJECT_DERIVED = "project_derived"      # 由 facts.claims（gap/novelty/contributions）
    MIXED = "mixed"


class SeedTarget(str, Enum):
    """seed 的调度提示（**不**证明返回文献语言，最终仍以 A2a LanguageVerdict 计数）。"""

    ANY = "any"
    LANGUAGE_ZH = "language.zh"
    LANGUAGE_EN = "language.en"


class SearchIntentPreparationStatus(str, Enum):
    READY = "ready"
    NEEDS_INPUT = "needs_input"
    ERROR = "error"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------- 规范化

def normalize_text(text: str, *, max_chars: int) -> str:
    """清洗（A2a untrusted）+ NFC + 空白归一 + 截断。用于 topic/seed 的**内容**规范化。"""
    cleaned = clean_external_text(text or "", max_chars=max_chars)
    cleaned = unicodedata.normalize("NFC", cleaned)
    return " ".join(cleaned.split()).strip()


def _dedup_key(text: str) -> str:
    """去重键：NFC + casefold + 空白归一（大小写/空白差异视为同一 seed）。"""
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def is_generic_title(text: str) -> bool:
    """规范化后**完全等于**通用章节名 → True（子串不算，避免误伤含实义标题）。"""
    return _dedup_key(text) in _GENERIC_TITLES


def is_informative_title(text: str) -> bool:
    """标题是否有信息量（非空、非纯通用章节名）→ 可作 operation 主题信号。"""
    norm = normalize_text(text, max_chars=_TOPIC_MAX)
    return bool(norm) and not is_generic_title(norm)


# ---------------------------------------------------------------- 值对象

class SearchIntentError(ValueError):
    """SearchIntent 契约违背（构造期拒绝，不静默接受）。"""


@dataclass(frozen=True)
class SearchSeed:
    text: str
    target: SeedTarget = SeedTarget.ANY
    origin: SearchIntentOrigin = SearchIntentOrigin.OPERATION_DERIVED

    def __post_init__(self) -> None:
        norm = normalize_text(self.text, max_chars=_SEED_MAX)
        if not (_SEED_MIN <= len(norm) <= _SEED_MAX):
            raise SearchIntentError(f"seed 长度非法（{len(norm)}）：{self.text!r}")
        if is_generic_title(norm):
            raise SearchIntentError(f"seed 不得为纯通用章节名：{self.text!r}")
        object.__setattr__(self, "text", norm)


@dataclass(frozen=True)
class SearchIntent:
    semantic_topic: str
    section_purpose: str = ""
    seeds: tuple[SearchSeed, ...] = ()
    origin: SearchIntentOrigin = SearchIntentOrigin.OPERATION_DERIVED
    schema_version: str = _SCHEMA_VERSION
    # 用户原始表述，**只用于构造查询词**（不进任何 hash，见 `semantic_fingerprint`）。
    # 为什么需要独立字段：`semantic_topic` 由合成器归一，真机实测中文课题会被译成英文
    # （"跨视角图像地理定位" → "cross-view geo-localization"）。补 language.zh 缺口时
    # Planner 手里没有任何中文词，只能拼出检索不到中文文献的查询。原先靠
    # `section_purpose` 兜（dogfood 里它恰好等于课题名），但该字段语义上是「章节目的」，
    # 生产路径填 add_section 的 purpose 槽、也可能是规范化章节类型，不保证含原文。
    raw_topic: str = ""

    def __post_init__(self) -> None:
        topic = normalize_text(self.semantic_topic, max_chars=_TOPIC_MAX)
        if not (_TOPIC_MIN <= len(topic) <= _TOPIC_MAX):
            raise SearchIntentError(f"semantic_topic 长度非法（{len(topic)}）")
        if is_generic_title(topic):
            raise SearchIntentError(f"semantic_topic 不得为纯通用章节名：{self.semantic_topic!r}")
        object.__setattr__(self, "semantic_topic", topic)
        object.__setattr__(self, "section_purpose",
                           normalize_text(self.section_purpose, max_chars=_TOPIC_MAX))
        # 只归一化、**不校验**：raw_topic 是纯召回辅助信号，不参与判定也不进 hash，
        # 非法值（通用章节名/空）退化为「没有原文可用」，不该让整个请求失败。
        object.__setattr__(self, "raw_topic",
                           normalize_text(self.raw_topic, max_chars=_TOPIC_MAX))
        deduped = _dedup_seeds(self.seeds)
        if not (1 <= len(deduped) <= _MAX_SEEDS):
            raise SearchIntentError(f"seed 数量非法（{len(deduped)}），须 1–{_MAX_SEEDS}")
        object.__setattr__(self, "seeds", deduped)

    def semantic_fingerprint(self) -> str:
        """仅语义字段（schema_version + topic + section_purpose，**不含 seeds**）。

        进 RelevanceCriteria / TaskFingerprint：seed 增删/排序/planner 改写不改变它。

        **`raw_topic` 故意不在内**：它只影响召回（查询词构造），不影响「什么算切题」。
        放进来会让同一课题因原始表述措辞不同而产生不同指纹，白丢相关性缓存。
        同理不进 `strategy_hash`。
        """
        payload = {
            "schema_version": self.schema_version,
            "semantic_topic": self.semantic_topic,
            "section_purpose": self.section_purpose,
        }
        return _canonical_hash(payload)

    def strategy_hash(self) -> str:
        """semantic_fingerprint + 按确认顺序规范化的 seeds（供 checkpoint/恢复/审计）。"""
        payload = {
            "semantic": self.semantic_fingerprint(),
            "seeds": [(s.text, s.target.value) for s in self.seeds],
        }
        return _canonical_hash(payload)


def _dedup_seeds(seeds) -> tuple[SearchSeed, ...]:
    """按 (dedup_key(text), target) 去重、保序。"""
    out: list[SearchSeed] = []
    seen: set[tuple[str, str]] = set()
    for s in seeds:
        key = (_dedup_key(s.text), s.target.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return tuple(out)


def _canonical_hash(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 只读输入 + 准备结果

@dataclass(frozen=True)
class SearchContext:
    """合成器的只读输入（受控字段，不含可修改 quota；design D2）。

    **论文上下文字段（`paper_*`）的由来（2026-08-01）**：此前本结构只有 operation
    槽位 + `facts.claims`，**没有任何文档内容**。真机后果：用户对已有引言骨架说
    「完善我的引言部分」，合成器收到的 user 消息只有两行——

    ==========  ================
    写作操作     rewrite
    改写方向     完善引言部分
    ==========  ================

    （`target_title="引言"` 被 `is_informative_title` 判为通用章节名而丢弃，
    `purpose` 是 add_section 的槽、rewrite 没有，`details` 用户没说，
    `project_*` 来自 `facts.claims`、首次运行为空。）

    于是 LLM 只能产出「引言这一章通常引什么」的通用模板
    （`research gap identification` / `limitations of existing approaches`…）
    ——**那是它在该输入下能给出的最好结果，模型没错，输入是空的**。

    而需要的信息全在手边：目标章节现有正文里写着「UAV与UGV感知范围互补」
    「跨平台视觉关联」「Relay中间视角」「主共视支撑域」，其他章节标题里写着
    「跨视角图像匹配研究」「图像共视区域估计研究」。系统持有却没送进来。

    这与「让用户在输入里重复课题名」是同一件事的两种处置：后者把系统的活推给用户。
    """

    explicit_query: str = ""
    operation_kind: str = ""                     # rewrite | add_section
    target_title: str = ""
    purpose: str = ""
    direction: str = ""
    details: str = ""
    structure_terms: tuple[str, ...] = ()
    project_gap: str = ""
    project_novelty: str = ""
    project_contributions: tuple[str, ...] = ()
    requested_dimensions: tuple[str, ...] = ()   # 只读 zh/en/total 信号，不含可改 quota
    # --- 文档上下文（只读投影，全部由 SectionIndex / 工作副本派生）---
    paper_title: str = ""                        # 论文题目（文档里有才填）
    paper_section_titles: tuple[str, ...] = ()   # 全文章节标题清单（含编号，原样）
    target_section_body: str = ""                # 目标章节现有正文（rewrite 时信息最密）


@dataclass(frozen=True)
class SearchIntentPreparationOutcome:
    """准备结果包装。READY 必带合法 intent；其余 intent 必空并带稳定 issue_code。"""

    status: SearchIntentPreparationStatus
    intent: SearchIntent | None = None
    issue_code: str = ""

    def __post_init__(self) -> None:
        if self.status is SearchIntentPreparationStatus.READY:
            if self.intent is None:
                raise SearchIntentError("READY 必须携带 intent")
        elif self.intent is not None:
            raise SearchIntentError(f"{self.status.value} 不得携带 intent")


def to_relevance_criteria(intent: SearchIntent, *, query_intent: str = ""):
    """SearchIntent → RelevanceCriteria：**只取语义字段**（design D5）。

    `query_intent` 传 operation 级语义目标(稳定)，**绝不**用 `" ".join(seeds)`——否则 seed
    增删/排序会改 TaskFingerprint、令相关性缓存失效。seed 只影响召回,不进 criteria。
    """
    from paper_agent.literature.relevance import RelevanceCriteria

    return RelevanceCriteria(
        topic=intent.semantic_topic,
        section_purpose=intent.section_purpose,
        query_intent=normalize_text(query_intent, max_chars=500),
    )


def search_intent_to_dict(intent: SearchIntent) -> dict:
    """canonical payload（进 session-scoped pending_search_intents；不进 trace）。"""
    return {
        "schema_version": intent.schema_version,
        "semantic_topic": intent.semantic_topic,
        "section_purpose": intent.section_purpose,
        "raw_topic": intent.raw_topic,
        "origin": intent.origin.value,
        "seeds": [{"text": s.text, "target": s.target.value, "origin": s.origin.value}
                  for s in intent.seeds],
    }


def search_intent_from_dict(d: dict) -> SearchIntent:
    """从 payload 重建冻结 SearchIntent（恢复用；非法 → SearchIntentError）。"""
    seeds = tuple(
        SearchSeed(text=str(s.get("text", "")),
                   target=SeedTarget(s.get("target", "any")),
                   origin=SearchIntentOrigin(s.get("origin", "operation_derived")))
        for s in d.get("seeds", [])
    )
    return SearchIntent(
        semantic_topic=str(d.get("semantic_topic", "")),
        section_purpose=str(d.get("section_purpose", "")),
        # 缺 key → ""：旧 payload（无 raw_topic）照常重建，退化为「没有原文可用」。
        raw_topic=str(d.get("raw_topic", "")),
        seeds=seeds,
        origin=SearchIntentOrigin(d.get("origin", "operation_derived")),
        schema_version=str(d.get("schema_version", _SCHEMA_VERSION)),
    )


def build_seeds(specs) -> tuple[SearchSeed, ...]:
    """从 (text, target, origin) 三元组/dict 构造并去重 seeds；非法项跳过（不抛）。

    合成器/planner 用它把 LLM 输出收敛为合法 seeds；全被剔 → 空元组（调用方据此判 NEEDS_INPUT）。
    """
    out: list[SearchSeed] = []
    for spec in specs or ():
        try:
            if isinstance(spec, SearchSeed):
                out.append(spec)
            elif isinstance(spec, dict):
                out.append(SearchSeed(
                    text=str(spec.get("text", "")),
                    target=SeedTarget(spec.get("target", "any")),
                    origin=SearchIntentOrigin(spec.get("origin", "operation_derived")),
                ))
            elif isinstance(spec, (tuple, list)) and spec:
                out.append(SearchSeed(text=str(spec[0])))
        except (SearchIntentError, ValueError):
            continue                              # 非法 seed 剔除，不静默恢复章节名
    return _dedup_seeds(out)


__all__ = [
    "SearchIntentOrigin",
    "SeedTarget",
    "SearchIntentPreparationStatus",
    "SearchSeed",
    "SearchIntent",
    "SearchContext",
    "SearchIntentPreparationOutcome",
    "SearchIntentError",
    "normalize_text",
    "is_generic_title",
    "is_informative_title",
    "build_seeds",
    "to_relevance_criteria",
    "search_intent_to_dict",
    "search_intent_from_dict",
]
