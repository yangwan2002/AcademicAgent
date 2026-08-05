"""配额模型：资格过滤 + 计数约束（A2b / 任务 1，Req 6）。

把用户的文献数量要求收敛为**可验证、可执行**的结构化 `QuotaSpec`。关键纪律
（design-a2b.md）：

- **filter 与 constraint 严格分离**（D1，Req 6.1）:硬过滤门槛（年份/类型/限定语言）
  归 `EligibilityFilter`;数量要求（总数/中文数/英文数）归 `CountConstraint`。
  年份/类型**永不**作计数 dimension。
- **计数维度只有中/英**（D1）:`CountDimension ∈ {total, language.zh, language.en}`,
  语言桶对齐 A2a `quota_bucket`;`other`/`unknown` 可计入 total（无语言硬过滤时）,
  **绝不**计入 language.zh/language.en。
- **topic 不进 filter**（D1）:主题相关性由 A2a 的 `RelevanceCriteria` 负责,
  filter field 收缩为 `year | reference_type | language`。
- **Raw 用 clause 列表**（D2）:能表达「中文至少 5、最多 8」这类同维度多约束,
  从而可靠检测冲突;扁平 `raw_zh/raw_en` 做不到。
- **结果含成功/警告/澄清/冲突**（D3）:`QuotaFinalizeResult(spec, issues)` 而非
  `QuotaSpec | QuotaConflict` 二选一,可同时返回部分结果与多条 issue。
- **缺口方向明确**（D9）:`QuotaGap` 用 `BELOW/ABOVE + 正 amount`,不用正负号猜语义。

本模块**不** import `tasking`、`config`、`datetime`（base_year 与 language policy
由应用组合层注入）,保持纯函数、可重复测试。

本文件为 Task 1：只定义枚举与数据模型。`finalize_quota`（Task 2）、评估器与
`build_quota_summary`（Task 3）后续补入本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# --- 枚举 -------------------------------------------------------------------

class FilterField(str, Enum):
    """Eligibility_Filter 的字段。**不含 topic**（主题走 A2a relevance）。"""

    YEAR = "year"
    REFERENCE_TYPE = "reference_type"
    LANGUAGE = "language"


class FilterOperator(str, Enum):
    GTE = "gte"
    LTE = "lte"
    EQ = "eq"
    IN = "in"
    RANGE = "range"


class CountDimension(str, Enum):
    """计数维度。语言桶对齐 A2a `quota_bucket`,只有中/英。"""

    TOTAL = "total"
    LANGUAGE_ZH = "language.zh"
    LANGUAGE_EN = "language.en"


class CountOperator(str, Enum):
    EXACT = "exact"
    MIN = "min"
    MAX = "max"
    RANGE = "range"


class QuotaKind(str, Enum):
    RETRIEVAL = "retrieval"          # 要求「选入候选集」多少篇
    CITATION = "citation"            # 要求「正文实际引用」多少篇


class QuotaScope(str, Enum):
    NEW_ONLY = "new_only"            # 仅本批新增
    LEDGER_TOTAL = "ledger_total"    # 可复用库中已有
    ACTUALLY_CITED = "actually_cited"  # 正文实际引用的不同文献数


class GapDirection(str, Enum):
    """配额缺口的方向（D9）:不足 / 超出。"""

    BELOW = "below"                  # 未达下界（min/exact/range 的 low）
    ABOVE = "above"                  # 超过上界（max/exact/range 的 high）


class QuotaIssueSeverity(str, Enum):
    """解析问题的严重级别（D3）。"""

    WARNING = "warning"                      # 可执行,但需在报告中提示
    NEEDS_CLARIFICATION = "needs_clarification"  # 阻断:须用户澄清
    ERROR = "error"                          # 阻断:无法生成可执行 spec


# 合法的 (kind, scope) 组合（D1）。其余视为解析/程序错误。
_ALLOWED_KIND_SCOPE: frozenset[tuple[QuotaKind, QuotaScope]] = frozenset({
    (QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED),
    (QuotaKind.RETRIEVAL, QuotaScope.NEW_ONLY),
    (QuotaKind.RETRIEVAL, QuotaScope.LEDGER_TOTAL),
})


def is_allowed_kind_scope(kind: QuotaKind, scope: QuotaScope) -> bool:
    """(kind, scope) 是否为允许组合（D1）。"""
    return (kind, scope) in _ALLOWED_KIND_SCOPE


def default_scope_for(kind: QuotaKind) -> QuotaScope:
    """用户未指明 scope 时的文档化默认（Req 6.5）。"""
    return QuotaScope.ACTUALLY_CITED if kind is QuotaKind.CITATION else QuotaScope.NEW_ONLY


# --- Final 数据模型（frozen 值对象）-----------------------------------------

@dataclass(frozen=True)
class EligibilityFilter:
    """硬过滤条件。value 语义按 field 固定:

    - ``YEAR``:``GTE``/``LTE`` → int 年份;``RANGE`` → ``(low, high)`` 闭区间
      （**已是绝对年份**,相对年份在 finalize 时固化,见 D5）。
    - ``REFERENCE_TYPE``:``IN`` → ``tuple[str, ...]``,取值来自 Req 1.9 的
      reference_type 枚举（journal_article / conference_paper / ...）。
    - ``LANGUAGE``:``EQ`` → 单一 bucket;``IN`` → bucket 元组。bucket ∈ ``{zh, en}``
      （「只要中文」/「只要中英文」）。
    """

    field: FilterField
    operator: FilterOperator
    value: int | str | tuple[int, int] | tuple[str, ...]
    verbatim: str = ""


@dataclass(frozen=True)
class CountConstraint:
    """计数约束。``value``:``RANGE`` → ``(low, high)``;其余 → int。"""

    dimension: CountDimension
    operator: CountOperator
    value: int | tuple[int, int]
    verbatim: str = ""


@dataclass(frozen=True)
class QuotaSpec:
    """可执行的结构化配额（filters 与 constraints 分开呈现,Req 6.1/6.9）。

    `base_year`:相对年份（「最近五年」）求值所依据的基准年,一次性固化并记录,
    使检索 Loop 各轮不重算（Req 6.7）。无相对年份时可为 None。
    """

    kind: QuotaKind
    scope: QuotaScope
    eligibility_filters: tuple[EligibilityFilter, ...] = ()
    count_constraints: tuple[CountConstraint, ...] = ()
    base_year: int | None = None


@dataclass(frozen=True)
class QuotaGap:
    """逐维度未满足的差额（D9）。

    `direction`+`amount`（恒正）唯一表达不足/超出,不用正负号猜语义。
    `required` 对 RANGE 为 ``(low, high)``,其余为 int。
    """

    dimension: CountDimension
    operator: CountOperator
    required: int | tuple[int, int]
    achieved: int
    direction: GapDirection
    amount: int


@dataclass(frozen=True)
class QuotaIssue:
    """解析问题（D3）。`code` 为稳定枚举字符串,便于报告与测试对账。

    典型 code:`invalid_number` / `unsupported_dimension` /
    `ambiguous_approximation` / `constraint_conflict` / `filter_conflict` /
    `filter_constraint_conflict` / `invalid_kind_scope` / `unknown_field` /
    `unknown_operator`。
    """

    code: str
    severity: QuotaIssueSeverity
    detail: str = ""
    verbatim: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuotaFinalizeResult:
    """finalize_quota 的返回:成品 spec + 全部 issue（D3）。

    `executable`:spec 非空且不含任何 NEEDS_CLARIFICATION/ERROR issue。
    WARNING 不阻断执行（只需在报告中提示）。
    """

    spec: QuotaSpec | None = None
    issues: tuple[QuotaIssue, ...] = ()

    @property
    def executable(self) -> bool:
        if self.spec is None:
            return False
        blocking = (QuotaIssueSeverity.NEEDS_CLARIFICATION, QuotaIssueSeverity.ERROR)
        return not any(iss.severity in blocking for iss in self.issues)


# --- Raw 模型（LLM 抽取产物,clause 列表,D2）--------------------------------

@dataclass(frozen=True)
class RawFilterClause:
    """LLM 抽出的松散过滤条款(未归一、可能误分类,由 finalize 纠正)。"""

    field: str
    operator: str
    value: object = None
    verbatim: str = ""


@dataclass(frozen=True)
class RawCountClause:
    """LLM 抽出的松散计数条款。"""

    dimension: str
    operator: str
    value: object = None
    verbatim: str = ""


@dataclass(frozen=True)
class RawQuotaSpec:
    """LLM 抽取的原始配额意图。可保留同维度多约束/重复/冲突,交 finalize 处理。

    `explicit_scope`:用户显式表达的 scope（罕见）;通常留空由 finalize 填默认。
    `notes`:LLM 的补充说明或无法归类的片段,留痕不静默丢。
    """

    filters: tuple[RawFilterClause, ...] = ()
    constraints: tuple[RawCountClause, ...] = ()
    explicit_scope: str = ""
    notes: tuple[str, ...] = ()


__all__ = [
    # 枚举
    "FilterField", "FilterOperator", "CountDimension", "CountOperator",
    "QuotaKind", "QuotaScope", "GapDirection", "QuotaIssueSeverity",
    # kind/scope 助手
    "is_allowed_kind_scope", "default_scope_for",
    # Final 模型
    "EligibilityFilter", "CountConstraint", "QuotaSpec", "QuotaGap",
    "QuotaIssue", "QuotaFinalizeResult",
    # Raw 模型
    "RawFilterClause", "RawCountClause", "RawQuotaSpec",
]


# ===========================================================================
# Task 2：确定性 finalize_quota（分流 + 年份固化 + 区间求交 + 冲突检测）
# ===========================================================================
#
# 解析两段式（design D4）：LLM 抽松散的 RawQuotaSpec，本函数做**全部**确定性工作。
# 关键边界：LLM 只报「用户表达过什么」（含相对年份的 N、数量的数字），
# finalize 负责「据此执行什么」（相对年份 → 绝对区间、operator 归一、区间求交、
# filter/constraint 分流与矛盾检测）。本模块不读时钟（base_year 注入）。

import re

# Req 1.9 的 reference_type 白名单。
_KNOWN_REFERENCE_TYPES: frozenset[str] = frozenset({
    "journal_article", "conference_paper", "book", "book_chapter", "thesis",
    "preprint", "report", "standard", "web_page",
})

# 语言桶 → CountDimension（只有中/英）。
_LANG_DIM = {"zh": CountDimension.LANGUAGE_ZH, "en": CountDimension.LANGUAGE_EN}

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(s: str) -> int | None:
    """把常见中文数字（0–99）转成 int；无法解析返回 None。"""
    s = s.strip()
    if not s:
        return None
    if "十" not in s:
        # 纯个位或多位连写（如「二〇」少见，按位拼）
        if len(s) == 1 and s in _CN_DIGITS:
            return _CN_DIGITS[s]
        return None
    # 处理「十/一十/二十/二十五/十五」
    parts = s.split("十")
    left, right = parts[0], parts[1] if len(parts) > 1 else ""
    tens = _CN_DIGITS.get(left, 1) if left else 1
    ones = _CN_DIGITS.get(right, 0) if right else 0
    return tens * 10 + ones


def _to_int(value: object) -> int | None:
    """把 raw 数量值收敛为 int。支持 int/float、数字串、常见中文数字。失败 → None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    if isinstance(value, str):
        t = value.strip()
        m = re.search(r"-?\d+", t)
        if m:
            return int(m.group())
        return _cn_to_int(t)
    return None


def _to_pair(value: object) -> tuple[int, int] | None:
    """把 range 值收敛为 (low, high)。接受二元序列或形如「20-30」「20~30」的串。"""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        lo, hi = _to_int(value[0]), _to_int(value[1])
        if lo is not None and hi is not None:
            return (lo, hi)
        return None
    if isinstance(value, str):
        nums = re.findall(r"\d+", value)
        if len(nums) >= 2:
            return (int(nums[0]), int(nums[1]))
    return None


# ---- filter 归一 ----

def _finalize_year(clause_op: str, value: object, verbatim: str, base_year: int,
                   issues: list, used_base_year: list) -> EligibilityFilter | None:
    op = clause_op.lower()
    if op == "recent_years":
        n = _to_int(value)
        if n is None or n <= 0:
            issues.append(QuotaIssue("invalid_number", QuotaIssueSeverity.ERROR,
                                     f"无法解析相对年份数量：{value!r}", (verbatim,)))
            return None
        used_base_year.append(base_year)
        return EligibilityFilter(FilterField.YEAR, FilterOperator.RANGE,
                                 (base_year - n + 1, base_year), verbatim)
    if op in ("gte", "since", "after"):
        y = _to_int(value)
        if y is None:
            issues.append(QuotaIssue("invalid_number", QuotaIssueSeverity.ERROR,
                                     f"无法解析年份：{value!r}", (verbatim,)))
            return None
        return EligibilityFilter(FilterField.YEAR, FilterOperator.GTE, y, verbatim)
    if op in ("lte", "before", "until"):
        y = _to_int(value)
        if y is None:
            issues.append(QuotaIssue("invalid_number", QuotaIssueSeverity.ERROR,
                                     f"无法解析年份：{value!r}", (verbatim,)))
            return None
        return EligibilityFilter(FilterField.YEAR, FilterOperator.LTE, y, verbatim)
    if op == "range":
        pair = _to_pair(value)
        if pair is None:
            issues.append(QuotaIssue("invalid_number", QuotaIssueSeverity.ERROR,
                                     f"无法解析年份区间：{value!r}", (verbatim,)))
            return None
        if pair[0] > pair[1]:
            issues.append(QuotaIssue("filter_conflict", QuotaIssueSeverity.ERROR,
                                     f"年份区间下界大于上界：{pair}", (verbatim,)))
            return None
        return EligibilityFilter(FilterField.YEAR, FilterOperator.RANGE, pair, verbatim)
    issues.append(QuotaIssue("unknown_operator", QuotaIssueSeverity.ERROR,
                             f"年份不支持的 operator：{clause_op!r}", (verbatim,)))
    return None


def _finalize_reference_type(value: object, verbatim: str, issues: list) -> EligibilityFilter | None:
    if isinstance(value, str):
        raw_types = [value]
    elif isinstance(value, (list, tuple)):
        raw_types = [str(x) for x in value]
    else:
        issues.append(QuotaIssue("invalid_value", QuotaIssueSeverity.ERROR,
                                 f"无法解析文献类型：{value!r}", (verbatim,)))
        return None
    recognized = tuple(t for t in raw_types if t in _KNOWN_REFERENCE_TYPES)
    unknown = [t for t in raw_types if t not in _KNOWN_REFERENCE_TYPES]
    if unknown:
        issues.append(QuotaIssue("unknown_reference_type", QuotaIssueSeverity.WARNING,
                                 f"忽略无法识别的文献类型：{unknown}", (verbatim,)))
    if not recognized:
        issues.append(QuotaIssue("filter_conflict", QuotaIssueSeverity.ERROR,
                                 "文献类型过滤为空集", (verbatim,)))
        return None
    return EligibilityFilter(FilterField.REFERENCE_TYPE, FilterOperator.IN, recognized, verbatim)


def _finalize_language_filter(value: object, verbatim: str, issues: list) -> EligibilityFilter | None:
    if isinstance(value, str):
        buckets = [value.strip().lower()]
    elif isinstance(value, (list, tuple)):
        buckets = [str(x).strip().lower() for x in value]
    else:
        issues.append(QuotaIssue("invalid_value", QuotaIssueSeverity.ERROR,
                                 f"无法解析语言过滤：{value!r}", (verbatim,)))
        return None
    bad = [b for b in buckets if b not in ("zh", "en")]
    if bad:
        issues.append(QuotaIssue("unsupported_dimension", QuotaIssueSeverity.ERROR,
                                 f"语言过滤只支持中/英，无法处理：{bad}", (verbatim,)))
        return None
    uniq = tuple(dict.fromkeys(buckets))
    if len(uniq) == 1:
        return EligibilityFilter(FilterField.LANGUAGE, FilterOperator.EQ, uniq[0], verbatim)
    return EligibilityFilter(FilterField.LANGUAGE, FilterOperator.IN, uniq, verbatim)


# ---- constraint 归一（→ 区间）----

def _count_op_to_interval(op: str, value: object, verbatim: str, bare_count_policy: str,
                          issues: list) -> tuple[int, int | None] | None:
    """把一条计数条款转成闭区间 (lo, hi)（hi=None 表示 +∞）。失败/近似 → None + issue。"""
    op = op.lower()
    if op == "approx":
        issues.append(QuotaIssue("ambiguous_approximation", QuotaIssueSeverity.NEEDS_CLARIFICATION,
                                 f"近似数量需澄清（不自定义容差）：{verbatim!r}", (verbatim,)))
        return None
    if op == "range":
        pair = _to_pair(value)
        if pair is None:
            issues.append(QuotaIssue("invalid_number", QuotaIssueSeverity.ERROR,
                                     f"无法解析数量区间：{value!r}", (verbatim,)))
            return None
        if pair[0] > pair[1]:
            issues.append(QuotaIssue("constraint_conflict", QuotaIssueSeverity.ERROR,
                                     f"数量区间下界大于上界：{pair}", (verbatim,)))
            return None
        return (pair[0], pair[1])
    n = _to_int(value)
    if n is None:
        issues.append(QuotaIssue("invalid_number", QuotaIssueSeverity.ERROR,
                                 f"无法解析数量：{value!r}", (verbatim,)))
        return None
    if op == "bare":
        op = bare_count_policy  # 默认 min（多引不拦、少引才拦）
    if op == "exact":
        return (n, n)
    if op == "min":
        return (n, None)
    if op == "max":
        return (0, n)
    issues.append(QuotaIssue("unknown_operator", QuotaIssueSeverity.ERROR,
                             f"数量不支持的 operator：{op!r}", (verbatim,)))
    return None


def _interval_to_constraint(dim: CountDimension, lo: int, hi: int | None,
                            verbatim: str) -> CountConstraint | None:
    """把归并后的区间映射回 canonical CountConstraint。(0,+∞) → 无约束（None）。"""
    if hi is not None and lo == hi:
        return CountConstraint(dim, CountOperator.EXACT, lo, verbatim)
    if hi is None:
        if lo <= 0:
            return None  # (0,+∞) 无实际约束
        return CountConstraint(dim, CountOperator.MIN, lo, verbatim)
    if lo <= 0:
        return CountConstraint(dim, CountOperator.MAX, hi, verbatim)
    return CountConstraint(dim, CountOperator.RANGE, (lo, hi), verbatim)


def _c_lower(c: CountConstraint) -> int:
    if c.operator is CountOperator.EXACT:
        return int(c.value)  # type: ignore[arg-type]
    if c.operator is CountOperator.MIN:
        return int(c.value)  # type: ignore[arg-type]
    if c.operator is CountOperator.RANGE:
        return int(c.value[0])  # type: ignore[index]
    return 0  # max


def _c_upper(c: CountConstraint) -> int | None:
    if c.operator is CountOperator.EXACT:
        return int(c.value)  # type: ignore[arg-type]
    if c.operator is CountOperator.MAX:
        return int(c.value)  # type: ignore[arg-type]
    if c.operator is CountOperator.RANGE:
        return int(c.value[1])  # type: ignore[index]
    return None  # min → +∞


# ---- 主入口 ----

def finalize_quota(
    raw: RawQuotaSpec,
    *,
    kind: QuotaKind,
    base_year: int,
    bare_count_policy: str = "min",
) -> QuotaFinalizeResult:
    """把 RawQuotaSpec 收敛为可执行 QuotaSpec（纯确定性，design D2–D7）。

    - kind 由调用上下文决定（不由 LLM）；scope 取 raw.explicit_scope 或默认。
    - 相对年份按注入的 base_year 一次性固化（Req 6.7）。
    - filter/constraint 按字段语义分流；同维度区间求交；filter 自身与联合可满足性校验。
    - 任何未知/非法/近似都产出 QuotaIssue，不静默丢（Req 6.1 口径）。
    """
    issues: list[QuotaIssue] = []
    used_base_year: list[int] = []

    # 1) 分流并归一每条 clause（按 token 语义重新归类，纠正 LLM 误放）。
    filters: list[EligibilityFilter] = []
    # dimension → 累积区间与 verbatim
    dim_intervals: dict[CountDimension, tuple[int, int | None]] = {}
    dim_verbatims: dict[CountDimension, list[str]] = {}

    def _route_filter_token(token: str, op: str, value: object, verbatim: str) -> None:
        tok = token.strip().lower()
        if tok == "year":
            f = _finalize_year(op, value, verbatim, base_year, issues, used_base_year)
            if f is not None:
                filters.append(f)
        elif tok == "reference_type":
            f = _finalize_reference_type(value, verbatim, issues)
            if f is not None:
                filters.append(f)
        elif tok == "language":
            f = _finalize_language_filter(value, verbatim, issues)
            if f is not None:
                filters.append(f)
        else:
            issues.append(QuotaIssue("unknown_field", QuotaIssueSeverity.ERROR,
                                     f"未知过滤字段：{token!r}", (verbatim,)))

    def _route_count_token(token: str, op: str, value: object, verbatim: str) -> None:
        tok = token.strip().lower()
        dim_map = {
            "total": CountDimension.TOTAL,
            "language.zh": CountDimension.LANGUAGE_ZH,
            "language.en": CountDimension.LANGUAGE_EN,
        }
        dim = dim_map.get(tok)
        if dim is None:
            # zh/en 之外的语言数量维度（如 language.ja）→ 不支持，不静默丢
            if tok.startswith("language."):
                issues.append(QuotaIssue("unsupported_dimension", QuotaIssueSeverity.ERROR,
                                         f"语言数量只支持中/英，无法处理维度：{token!r}", (verbatim,)))
            else:
                issues.append(QuotaIssue("unknown_field", QuotaIssueSeverity.ERROR,
                                         f"未知计数维度：{token!r}", (verbatim,)))
            return
        iv = _count_op_to_interval(op, value, verbatim, bare_count_policy, issues)
        if iv is None:
            return
        lo, hi = iv
        if dim in dim_intervals:
            plo, phi = dim_intervals[dim]
            nlo = max(plo, lo)
            nhi = hi if phi is None else (phi if hi is None else min(phi, hi))
            dim_intervals[dim] = (nlo, nhi)
        else:
            dim_intervals[dim] = (lo, hi)
        dim_verbatims.setdefault(dim, []).append(verbatim)

    # RawFilterClause 与 RawCountClause 都按 token 语义路由（LLM 误放可被纠正）。
    for fc in raw.filters:
        _dispatch_clause(fc.field, fc.operator, fc.value, fc.verbatim,
                         _route_filter_token, _route_count_token)
    for cc in raw.constraints:
        _dispatch_clause(cc.dimension, cc.operator, cc.value, cc.verbatim,
                         _route_filter_token, _route_count_token)

    # 2) 同维度区间 → canonical constraint（空集 → 冲突）。
    constraints: list[CountConstraint] = []
    for dim, (lo, hi) in dim_intervals.items():
        if hi is not None and lo > hi:
            issues.append(QuotaIssue("constraint_conflict", QuotaIssueSeverity.ERROR,
                                     f"{dim.value} 的约束区间为空：[{lo}, {hi}]",
                                     tuple(dim_verbatims.get(dim, ()))))
            continue
        c = _interval_to_constraint(dim, lo, hi, " / ".join(dim_verbatims.get(dim, ())))
        if c is not None:
            constraints.append(c)

    # 3) 跨维度矛盾（Req 6.6）：分项下界之和 > total 上界（total 有上界时才校验）。
    by_dim = {c.dimension: c for c in constraints}
    total_c = by_dim.get(CountDimension.TOTAL)
    total_upper = _c_upper(total_c) if total_c is not None else None
    lang_lower_sum = sum(
        _c_lower(by_dim[d]) for d in (CountDimension.LANGUAGE_ZH, CountDimension.LANGUAGE_EN)
        if d in by_dim
    )
    if total_upper is not None and lang_lower_sum > total_upper:
        issues.append(QuotaIssue("constraint_conflict", QuotaIssueSeverity.ERROR,
                                 f"中英文下界之和 {lang_lower_sum} 超过 total 上界 {total_upper}"))

    # 4) filter 与 constraint 联合可满足性（D7）。
    _check_filter_constraint(filters, by_dim, issues)

    # 5) kind/scope。
    scope = _resolve_scope(raw.explicit_scope, kind, issues)

    base = used_base_year[0] if used_base_year else None
    spec = QuotaSpec(
        kind=kind,
        scope=scope,
        eligibility_filters=tuple(filters),
        count_constraints=tuple(constraints),
        base_year=base,
    )
    return QuotaFinalizeResult(spec=spec, issues=tuple(issues))


def _dispatch_clause(token, op, value, verbatim, route_filter, route_count) -> None:
    """按 token 语义把一条 raw clause 路由到 filter 或 count 处理（纠正 LLM 误放）。"""
    tok = str(token).strip().lower()
    if tok in ("year", "reference_type", "language"):
        route_filter(tok, str(op), value, str(verbatim or ""))
    elif tok == "total" or tok.startswith("language."):
        route_count(tok, str(op), value, str(verbatim or ""))
    else:
        # 无法归类：交给 filter 路由报 unknown_field（统一出口）
        route_filter(tok, str(op), value, str(verbatim or ""))


def _resolve_scope(explicit: str, kind: QuotaKind, issues: list) -> QuotaScope:
    scope: QuotaScope | None = None
    if explicit:
        try:
            scope = QuotaScope(explicit.strip().lower())
        except ValueError:
            issues.append(QuotaIssue("invalid_kind_scope", QuotaIssueSeverity.WARNING,
                                     f"无法识别的 scope：{explicit!r}，改用默认"))
            scope = None
    if scope is None:
        scope = default_scope_for(kind)
    if not is_allowed_kind_scope(kind, scope):
        issues.append(QuotaIssue("invalid_kind_scope", QuotaIssueSeverity.ERROR,
                                 f"不允许的 kind/scope 组合：{kind.value}/{scope.value}"))
    return scope


def _check_filter_constraint(filters, by_dim, issues) -> None:
    """LANGUAGE 硬过滤与语言/total 约束的联合可满足性（D7）。"""
    lang_filters = [f for f in filters if f.field is FilterField.LANGUAGE]
    if not lang_filters:
        return
    # 多个语言 filter 取交集
    allowed: set[str] | None = None
    for f in lang_filters:
        s = {f.value} if isinstance(f.value, str) else set(f.value)  # type: ignore[arg-type]
        allowed = s if allowed is None else (allowed & s)
    if not allowed:
        issues.append(QuotaIssue("filter_conflict", QuotaIssueSeverity.ERROR,
                                 "语言过滤取交集为空集"))
        return

    # 被排除的语言维度却有下界 > 0 → 冲突
    for bucket, dim in _LANG_DIM.items():
        if bucket not in allowed and dim in by_dim and _c_lower(by_dim[dim]) > 0:
            issues.append(QuotaIssue("filter_constraint_conflict", QuotaIssueSeverity.ERROR,
                                     f"只允许语言 {sorted(allowed)}，但 {dim.value} 下界 > 0"))

    # 只允许 zh/en 子集时，total 下界不得超过被允许语言上界之和（均有限时）。
    total_c = by_dim.get(CountDimension.TOTAL)
    if total_c is None:
        return
    total_lower = _c_lower(total_c)
    if total_lower <= 0:
        return
    uppers: list[int] = []
    for bucket in allowed:
        dim = _LANG_DIM.get(bucket)
        if dim is None:
            return  # 允许集合含非 zh/en（不应发生），保守不判
        u = _c_upper(by_dim[dim]) if dim in by_dim else None
        if u is None:
            return  # 该语言无上界 → total 可达，不冲突
        uppers.append(u)
    if uppers and total_lower > sum(uppers):
        issues.append(QuotaIssue("filter_constraint_conflict", QuotaIssueSeverity.ERROR,
                                 f"只允许语言 {sorted(allowed)}，但 total 下界 {total_lower} "
                                 f"超过其上界之和 {sum(uppers)}"))


__all__ += [
    "finalize_quota",
]


# ===========================================================================
# Task 3：纯评估器 + QuotaGap 产出 + 结构化确认摘要
# ===========================================================================
#
# 供 A2c 检索 Loop、A2d LiteraturePreflight / CitationQuotaGate 复用同一套判定
# （复用不重写）。语言计数**委托 A2a** `counts_toward`；filter/count 全为纯函数，
# 给定 record 列表 + QuotaSpec 即可离线确定性求值。

from dataclasses import dataclass as _dataclass  # 避免与上方 import 混淆命名

from paper_agent.literature.language import (
    DEFAULT_THRESHOLDS,
    classify_record_language,
    counts_toward,
)


def _default_classifier(record):
    return classify_record_language(record, DEFAULT_THRESHOLDS)


@_dataclass(frozen=True)
class DimensionCounts:
    """按维度的计数结果。`low_confidence`：计入 zh/en 的条目中非 high 置信的条数
    （`include_low_with_warning` 下供报告，Req 5.7/5.8）。"""

    total: int = 0
    language_zh: int = 0
    language_en: int = 0
    low_confidence: int = 0

    @property
    def by_dimension(self) -> dict:
        return {
            CountDimension.TOTAL: self.total,
            CountDimension.LANGUAGE_ZH: self.language_zh,
            CountDimension.LANGUAGE_EN: self.language_en,
        }


# ---- filter 匹配（纯函数）----

def filter_matches(record, filt: EligibilityFilter, *, classifier=None, policy: str = "high_only") -> bool:
    """单条 filter 是否匹配 record。缺字段保守判**不通过**（不为凑数纳入，Req 10.2）。"""
    if filt.field is FilterField.YEAR:
        year = getattr(record, "year", None)
        if year is None:
            return False
        if filt.operator is FilterOperator.GTE:
            return year >= filt.value
        if filt.operator is FilterOperator.LTE:
            return year <= filt.value
        if filt.operator is FilterOperator.RANGE:
            lo, hi = filt.value  # type: ignore[misc]
            return lo <= year <= hi
        return False
    if filt.field is FilterField.REFERENCE_TYPE:
        rt = getattr(record, "reference_type", None)
        if not rt:
            return False
        allowed = filt.value if isinstance(filt.value, (tuple, list)) else (filt.value,)
        return rt in allowed
    if filt.field is FilterField.LANGUAGE:
        cls = classifier or _default_classifier
        verdict = cls(record)
        buckets = {filt.value} if isinstance(filt.value, str) else set(filt.value)  # type: ignore[arg-type]
        # 语言 filter 同样应用 A2a confidence policy（D8）：high_only 下 medium/low 不通过。
        return any(counts_toward(verdict, b, policy) for b in buckets)
    return False


def passes_all_filters(record, filters, *, classifier=None, policy: str = "high_only") -> bool:
    """全体 filter 的合取（Req 6.2）。"""
    return all(filter_matches(record, f, classifier=classifier, policy=policy) for f in filters)


# ---- 计数（纯函数，语言委托 A2a）----

def count_by_dimension(records, spec: QuotaSpec, *, classifier=None, policy: str = "high_only") -> DimensionCounts:
    """先按 reference_id 去重 → 施加全部 filter → 计数（Req 6.2 / D8 / Property A2b-7）。

    total = 通过 filter 的去重总数；zh/en 经 A2a `counts_toward`；
    other/unknown 计入 total（若通过 filter），**绝不**计入 zh/en。
    """
    cls = classifier or _default_classifier
    # 去重（保序保留首个）
    seen: set = set()
    deduped = []
    for r in records:
        rid = getattr(r, "reference_id", None)
        if rid is not None:
            if rid in seen:
                continue
            seen.add(rid)
        deduped.append(r)

    total = zh = en = low_conf = 0
    for r in deduped:
        if not passes_all_filters(r, spec.eligibility_filters, classifier=cls, policy=policy):
            continue
        total += 1
        verdict = cls(r)
        if counts_toward(verdict, "zh", policy):
            zh += 1
            if verdict.confidence != "high":
                low_conf += 1
        elif counts_toward(verdict, "en", policy):
            en += 1
            if verdict.confidence != "high":
                low_conf += 1
    return DimensionCounts(total=total, language_zh=zh, language_en=en, low_confidence=low_conf)


# ---- 缺口评估（纯函数）----

def evaluate_constraints(counts, constraints) -> tuple[QuotaGap, ...]:
    """逐 canonical constraint 比对，产出未满足维度的 QuotaGap（D9）。

    `counts`：`DimensionCounts` 或 `dict[CountDimension, int]`。
    min/exact/range 未达下界 → BELOW；max/exact/range 超上界 → ABOVE；amount 恒正；
    满足则不产 gap。不在内部放宽任何要求。
    """
    cmap = counts.by_dimension if isinstance(counts, DimensionCounts) else dict(counts)
    gaps: list[QuotaGap] = []
    for c in constraints:
        achieved = int(cmap.get(c.dimension, 0))
        lower = _c_lower(c)
        upper = _c_upper(c)
        required = c.value
        if lower > 0 and achieved < lower:
            gaps.append(QuotaGap(c.dimension, c.operator, required, achieved,
                                 GapDirection.BELOW, lower - achieved))
        if upper is not None and achieved > upper:
            gaps.append(QuotaGap(c.dimension, c.operator, required, achieved,
                                 GapDirection.ABOVE, achieved - upper))
    return tuple(gaps)


# ---- 结构化确认摘要（D12，Req 6.9）----

@_dataclass(frozen=True)
class QuotaSummary:
    kind: QuotaKind
    scope: QuotaScope
    base_year: int | None
    filters: tuple[str, ...]
    constraints: tuple[str, ...]
    assumptions: tuple[str, ...]
    issues: tuple[str, ...]


_OP_WORD = {
    CountOperator.EXACT: "恰好",
    CountOperator.MIN: "至少",
    CountOperator.MAX: "最多",
}
_DIM_LABEL = {
    CountDimension.TOTAL: "总数",
    CountDimension.LANGUAGE_ZH: "中文",
    CountDimension.LANGUAGE_EN: "英文",
}


def _render_constraint(c: CountConstraint) -> str:
    label = _DIM_LABEL[c.dimension]
    if c.operator is CountOperator.RANGE:
        lo, hi = c.value  # type: ignore[misc]
        return f"{label}：{lo}–{hi} 篇"
    return f"{label}：{_OP_WORD[c.operator]} {c.value} 篇"


def _render_filter(f: EligibilityFilter) -> str:
    if f.field is FilterField.YEAR:
        if f.operator is FilterOperator.RANGE:
            lo, hi = f.value  # type: ignore[misc]
            return f"年份：{lo}–{hi}"
        if f.operator is FilterOperator.GTE:
            return f"年份：≥ {f.value}"
        return f"年份：≤ {f.value}"
    if f.field is FilterField.REFERENCE_TYPE:
        vals = f.value if isinstance(f.value, (tuple, list)) else (f.value,)
        return "文献类型：" + " / ".join(str(v) for v in vals)
    if f.field is FilterField.LANGUAGE:
        vals = {f.value} if isinstance(f.value, str) else set(f.value)  # type: ignore[arg-type]
        return "限定语言：" + " / ".join(sorted(vals))
    return f"{f.field.value}：{f.value}"


def build_quota_summary(result: QuotaFinalizeResult) -> QuotaSummary:
    """把 finalize 结果渲染为结构化摘要 DTO（filters/constraints 分区，D12）。

    文本渲染供 Console/UI 使用；本函数只产 DTO。
    """
    spec = result.spec
    if spec is None:
        return QuotaSummary(
            kind=QuotaKind.CITATION, scope=QuotaScope.ACTUALLY_CITED, base_year=None,
            filters=(), constraints=(), assumptions=(),
            issues=tuple(f"{i.code}: {i.detail}" for i in result.issues),
        )

    filters = tuple(_render_filter(f) for f in spec.eligibility_filters)
    constraints = tuple(_render_constraint(c) for c in spec.count_constraints)

    assumptions: list[str] = []
    # 相对年份的绝对区间（可从 base_year + YEAR RANGE filter 回显）。
    if spec.base_year is not None:
        for f in spec.eligibility_filters:
            if f.field is FilterField.YEAR and f.operator is FilterOperator.RANGE:
                lo, hi = f.value  # type: ignore[misc]
                assumptions.append(f"相对年份按基准年 {spec.base_year} 固化为 {lo}–{hi}")
                break
    # 数量按「至少」理解的口径（多引不拦、少引才拦）。
    if any(c.operator is CountOperator.MIN for c in spec.count_constraints):
        assumptions.append("未显式说明的数量按『至少』处理（多引不拦、少引才拦）；如需精确请说『恰好』")

    return QuotaSummary(
        kind=spec.kind,
        scope=spec.scope,
        base_year=spec.base_year,
        filters=filters,
        constraints=constraints,
        assumptions=tuple(assumptions),
        issues=tuple(f"{i.code}: {i.detail}" for i in result.issues),
    )


__all__ += [
    "DimensionCounts",
    "filter_matches",
    "passes_all_filters",
    "count_by_dimension",
    "evaluate_constraints",
    "QuotaSummary",
    "build_quota_summary",
]
