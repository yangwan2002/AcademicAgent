# Design Document — A2b：配额解析（资格过滤 + 计数约束）

> A2 的第二个子阶段。A2b 只把用户的文献数量要求收敛为可验证、可执行的结构化配额，
> 不访问网络、不运行检索 Loop、不修改 Working Copy，也不把尚未接通执行器的槽位提前
> 暴露给 Active 路径。

## Overview

A2b 回答：

> 用户说“引言引用 20 篇，其中中文至少 5 篇、英文至少 10 篇，且都是最近五年的期刊论文”
> 时，系统究竟应执行哪些硬过滤和数量约束？

核心拆成三层：

1. `EligibilityFilter`：哪些文献有资格被计数，例如年份、文献类型、限定语言；
2. `CountConstraint`：需要多少篇，例如总数、中文数、英文数；
3. `QuotaKind + QuotaScope`：统计“检索得到”还是“正文实际引用”，以及统计新增还是已有集合。

A2b 消费 A2a 的语言口径：

```text
language_code ∈ zh | en | ja | ko | other | unknown
quota_bucket  ∈ zh | en | other | unknown
```

只有 zh/en 可进入相应语言数量约束；other/unknown 可以在没有语言硬过滤时进入 total，
但绝不能冒充中文或英文。

本阶段交付：

- 配额领域模型、Raw Schema、确定性 finalizer；
- 相对年份固化、约束归一与完整冲突检测；
- 过滤、计数、缺口评估与结构化确认摘要；
- 供 rewrite/add_section/search_literature 后续接线复用的槽位 Schema 定义。

本阶段不做：

- 不运行检索、不派生 AcquisitionGap；
- 不执行 LiteraturePreflight、CitationQuotaGate 或 Writer Loop；
- 不把 `citation_quota` 挂到当前 Active IntentSpec 后任由 Handler 忽略；
- 不决定是否永久取消独立 `search_literature`。当前 Requirements 仍声明该意图，A2b 仅不实现它；
  若产品后续决定删除，必须先同步 Requirements，不能只在 Design 中宣布删除。

> `CitationQuotaGate` 属 literature-evidence / Spec A 的写作编排边界，Spec B 的
> Evidence Gate 只消费引用结果并判断支撑关系，不承担配额计算。

验收要求：既有测试无回归；所有配额归一、过滤、冲突检测和评估函数为确定性纯函数；
`literature/quota.py` 不 import `tasking`、`config`、`datetime`。

## 关键设计决策

### D1. Final 模型严格分离 filter、constraint、kind 和 scope

```python
class FilterField(str, Enum):
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
    TOTAL = "total"
    LANGUAGE_ZH = "language.zh"
    LANGUAGE_EN = "language.en"

class CountOperator(str, Enum):
    EXACT = "exact"
    MIN = "min"
    MAX = "max"
    RANGE = "range"

class QuotaKind(str, Enum):
    RETRIEVAL = "retrieval"
    CITATION = "citation"

class QuotaScope(str, Enum):
    NEW_ONLY = "new_only"
    LEDGER_TOTAL = "ledger_total"
    ACTUALLY_CITED = "actually_cited"

@dataclass(frozen=True)
class EligibilityFilter:
    field: FilterField
    operator: FilterOperator
    value: int | str | tuple[int, int] | tuple[str, ...]
    verbatim: str = ""

@dataclass(frozen=True)
class CountConstraint:
    dimension: CountDimension
    operator: CountOperator
    value: int | tuple[int, int]
    verbatim: str = ""

@dataclass(frozen=True)
class QuotaSpec:
    kind: QuotaKind
    scope: QuotaScope
    eligibility_filters: tuple[EligibilityFilter, ...] = ()
    count_constraints: tuple[CountConstraint, ...] = ()
    base_year: int | None = None
```

允许组合固定为：

- `CITATION + ACTUALLY_CITED`；
- `RETRIEVAL + NEW_ONLY`；
- `RETRIEVAL + LEDGER_TOTAL`。

其他 kind/scope 组合视为程序或解析错误，不生成可执行 QuotaSpec。

`TOPIC` 不进入 EligibilityFilter。主题相关性已经由 A2a 的 `RelevanceCriteria` 和
`RelevanceAssessment` 负责，而且是语义判断；把 topic 再塞进确定性硬过滤会形成双重职责。
Requirements 中若仍把 topic 列为 filter field，应同步删除。

### D2. Raw 模型必须能表达重复、冲突和未知条款

不能用 `raw_total/raw_zh/raw_en` 各一个字段：这种结构无法表达“中文至少 5 篇、最多 8 篇”
或“恰好 5 篇但又至少 6 篇”，自然也无法可靠检测冲突。

```python
@dataclass(frozen=True)
class RawFilterClause:
    field: str
    operator: str
    value: object
    verbatim: str = ""

@dataclass(frozen=True)
class RawCountClause:
    dimension: str
    operator: str
    value: object
    verbatim: str = ""

@dataclass(frozen=True)
class RawQuotaSpec:
    filters: tuple[RawFilterClause, ...] = ()
    constraints: tuple[RawCountClause, ...] = ()
    explicit_scope: str = ""
    notes: tuple[str, ...] = ()
```

LLM 可以误放条款，但 finalizer 必须按字段语义重新分流：YEAR/REFERENCE_TYPE/“只允许某语言”
只能形成 filter；TOTAL/LANGUAGE_ZH/LANGUAGE_EN 的数量只能形成 constraint。

未知字段、未知 operator、无法解析的数量不能静默丢弃，必须形成 `QuotaIssue`。

### D3. Finalize 结果同时表达成功、警告、澄清和冲突

单一 `QuotaSpec | QuotaConflict` 无法同时返回部分可用结果和多个问题，因此改成：

```python
class QuotaIssueSeverity(str, Enum):
    WARNING = "warning"
    NEEDS_CLARIFICATION = "needs_clarification"
    ERROR = "error"

@dataclass(frozen=True)
class QuotaIssue:
    code: str
    severity: QuotaIssueSeverity
    detail: str
    verbatim: tuple[str, ...] = ()

@dataclass(frozen=True)
class QuotaFinalizeResult:
    spec: QuotaSpec | None
    issues: tuple[QuotaIssue, ...] = ()

    @property
    def executable(self) -> bool:
        return self.spec is not None and not any(
            x.severity in {NEEDS_CLARIFICATION, ERROR} for x in self.issues
        )
```

典型 issue：`invalid_number`、`unsupported_dimension`、`ambiguous_approximation`、
`constraint_conflict`、`filter_conflict`、`invalid_kind_scope`。

“二十来篇”“约 20 篇”等近似说法不能由系统私自定义容差；本阶段返回
`NEEDS_CLARIFICATION/ambiguous_approximation`。如果未来产品定义统一容差，再确定性映射为 RANGE。

### D4. 两段式解析：LLM 抽 Raw，代码确定性 finalize

```text
用户文本
  └─ tasking function calling
       ▼
  RawQuotaSpec
       └─ finalize_quota(raw, kind, base_year, bare_count_policy)
            ▼
  QuotaFinalizeResult(spec, issues)
```

LLM 只抽取用户表达过的条款和 verbatim，不负责：

- 计算相对年份；
- 决定当前 Operation 的 QuotaKind；
- 填系统默认 scope；
- 判定约束是否可满足；
- 私自把近似数量变成精确范围。

`QuotaKind` 由调用上下文决定，而不是由 LLM 猜：rewrite/add_section 中的配额是 CITATION；
若 A2c 实现 search_literature，该意图中的配额是 RETRIEVAL。

### D5. 相对年份只求值一次

```python
finalize_quota(raw, *, kind, base_year, bare_count_policy="min")
```

- `base_year` 由应用 Clock 在一次 Turn 开始时取得并注入；quota 模块不读系统时钟；
- “最近 N 年”固化为闭区间 `(base_year - N + 1, base_year)`；
- QuotaSpec 保存 base_year；检索 Loop 各轮不得重新求值；
- 测试使用 Fake Clock 或直接传 base_year，不需要新增环境变量。

裸数量采用文档化默认 `min`（**多引不拦、少引才拦**）：用户说“引用 20 篇”解释为
`TOTAL min 20`——正文引用 22 篇不产生 gap（`min` 天然只在 BELOW 报缺口，见 D9），
引用 18 篇才判未达标。这样把裸数量的 gate 宽严语义**在解析层一次表达清楚**，
CitationQuotaGate 无需为 kind 单独特判 ABOVE/BELOW。
用户**显式**说“恰好 20 篇 / 正好 20 篇”时才解析为 `exact`（此时多引也算越界，
因为用户明确要求精确数量）。裸数量默认按 min 处理这一点必须在确认摘要中显式展示，
供用户纠正为 exact。检索阶段的冗余超采样是 A2d 的派生目标，不改变本 citation 配额的
`min` 语义。

### D6. 约束统一转换成区间后求交，不逐条特判

每个 CountConstraint 转成闭区间：

```text
exact N     → [N, N]
min N       → [N, +∞]
max N       → [0, N]
range L..H  → [L, H]
```

同一 dimension 的全部区间求交：交集为空才是冲突。这样：

- `exact 5 + min 4` 合法；
- `exact 5 + min 6` 冲突；
- 多个 min/max 自动收敛为最严格边界；
- `range low > high` 在进入求交前直接报错。

归一完成后，每个 dimension 最多保留一个 canonical constraint，避免后续评估器重复解释。

跨维度校验：

- `zh.lower + en.lower > total.upper` → 冲突；
- total 只有下界时，语言下界之和超过 total.lower 不构成冲突；
- 语言分项下界之和小于 total.lower 不构成冲突，剩余可由 other/unknown 补足。

### D7. Filter 自身及 filter/constraint 组合也必须校验

Finalizer 同时规范化并求交 EligibilityFilter：

- YEAR 的 GTE/LTE/RANGE 求交，空集 → `filter_conflict`；
- `REFERENCE_TYPE IN ()` → 冲突；多个类型集合取交集，空集 → 冲突；
- LANGUAGE 支持 `EQ` 与 `IN`，用于表达“只要中文”及“只要中英文”；
- 多个语言集合取交集，空集 → 冲突；
- 语言值只能是 zh/en，other/unknown 不能作为本阶段用户目标语言。

还必须检查 filter 与 constraint 的联合可满足性：

- LANGUAGE 只允许 zh，但存在 `LANGUAGE_EN min > 0` → 冲突；
- LANGUAGE 只允许 en，但存在 `LANGUAGE_ZH min > 0` → 冲突；
- LANGUAGE 只允许 zh，`TOTAL min 20 + LANGUAGE_ZH max 10` → 冲突；
- LANGUAGE 只允许 `{zh,en}` 时，total 下界不能超过 zh/en 上界之和（两者均有有限上界时）。

### D8. 语言过滤和计数必须复用 A2a policy

```python
def filter_matches(record, filter, *, language_classifier, language_policy) -> bool
def passes_all_filters(record, filters, *, language_classifier, language_policy) -> bool
def count_by_dimension(records, spec, *, language_classifier, language_policy) -> Counts
```

- 语言 filter 不能只看 bucket 字符串，还必须应用 A2a 的 confidence policy；
- `high_only` 下 medium/low 不能通过 LANGUAGE filter 或进入语言维度；
- `include_low_with_warning` 可接受低置信结果，但评估结果必须记录低置信计数；
- YEAR/reference_type 缺失时保守判 filter 不通过；
- 输入记录先按 `reference_id` 去重，再过滤和计数；
- other/unknown 若没有 LANGUAGE 硬过滤，可以计入 TOTAL，但永不计入 LANGUAGE_ZH/EN。

### D9. QuotaGap 不使用含义不明的正负 gap

```python
class GapDirection(str, Enum):
    BELOW = "below"
    ABOVE = "above"

@dataclass(frozen=True)
class QuotaGap:
    dimension: CountDimension
    operator: CountOperator
    required: int | tuple[int, int]
    achieved: int
    direction: GapDirection
    amount: int
```

- min/exact/range 未达到下界 → BELOW；
- max/exact/range 超过上界 → ABOVE；
- amount 始终为正数；满足约束时不产生 QuotaGap。

`evaluate_constraints` 只消费 canonical constraints，返回全部未满足维度，不在内部放宽任何要求。

### D10. 嵌套 quota object 必须有明确 JSON Schema

现有 SlotSpec 的 `value_type="object"` 只会生成空 object Schema，不能约束 RawQuotaSpec。
A2b 必须提供共享 `RAW_QUOTA_JSON_SCHEMA`，显式声明 filters/constraints 数组、字段、operator、
value 和 verbatim。

实现方式优先为 SlotSpec 增加可选 `json_schema_override`：

- 未提供 override 的既有槽位行为逐字不变；
- `build_tool_schema` 对 quota 槽使用 override；
- rewrite/add_section 共用同一份 Schema，不各自复制；
- LLM 输出后仍由程序构造 RawQuotaSpec 并校验，JSON Schema 不是唯一防线。

### D11. 槽位激活必须与执行消费原子交付

A2b 可以实现：

- `RAW_QUOTA_JSON_SCHEMA`；
- `CITATION_QUOTA_SLOT_SPEC` 常量；
- Raw DTO 构造与 finalizer 测试。

但在 A2d 完成下面全部接线前，不把该槽位挂入 Active 的 `REWRITE_SPEC/ADD_SECTION_SPEC`：

1. 抽取后的 raw quota 被 finalize；
2. blocking issue 会进入澄清/拒绝，而不是被忽略；
3. Plan 中只保存 executable QuotaSpec；
4. Handler/写作编排器真实消费 QuotaSpec；
5. 计划确认摘要展示最终 filters/constraints。

否则系统会“成功抽取用户配额，却继续按旧逻辑写作”，这是比明确不支持更危险的静默失效。
如果必须在 A2b 提前挂槽，只能受默认关闭的 feature flag 保护，并且关闭时不得宣称已支持配额。

### D12. 结构化确认摘要先产 DTO，再做文本渲染

```python
@dataclass(frozen=True)
class QuotaSummary:
    kind: QuotaKind
    scope: QuotaScope
    base_year: int | None
    filters: tuple[str, ...]
    constraints: tuple[str, ...]
    assumptions: tuple[str, ...]
    issues: tuple[str, ...]
```

`build_quota_summary(result) -> QuotaSummary` 为稳定接口；Console/UI 再负责渲染文本。
摘要必须明确展示：裸数量按 min 处理（多引不拦、少引才拦）、相对年份的绝对区间、
语言桶、kind/scope 和所有警告。

## Architecture

```text
用户文本
  └─ tasking PreExtractor + RAW_QUOTA_JSON_SCHEMA
       ▼
  RawQuotaSpec（多 clause，可保留重复/冲突/verbatim）
       │
       └─ finalize_quota(raw, kind, base_year, bare_count_policy)
            ├─ 类型与 operator 规范化
            ├─ 相对年份固化
            ├─ filter / constraint 分流
            ├─ 同维度区间求交
            ├─ filter 与 constraint 联合可满足性检查
            ▼
  QuotaFinalizeResult
       ├─ executable → QuotaSpec
       └─ issues → warning / clarification / error

QuotaSpec
  ├─ build_quota_summary → A2d 计划确认
  └─ count_by_dimension + evaluate_constraints
       ├─ A2c Retrieval Loop
       └─ A2d LiteraturePreflight / CitationQuotaGate
```

## Components and Interfaces

### 1. `literature/quota.py`

包含：

- 枚举和 frozen 数据模型；
- Raw DTO 与 `QuotaFinalizeResult`；
- `finalize_quota`；
- filter 规范化、constraint 区间求交、联合冲突检测；
- `filter_matches` / `passes_all_filters` / `count_by_dimension`；
- `evaluate_constraints`；
- `build_quota_summary`。

禁止 import `tasking`、`config`、`datetime`。

### 2. `tasking` Schema 扩展

- `SlotSpec` 增加可选 `json_schema_override`；
- `build_tool_schema` 在存在 override 时原样使用结构 Schema；
- 定义共享 `RAW_QUOTA_JSON_SCHEMA` 与 `CITATION_QUOTA_SLOT_SPEC`；
- A2b 默认不把槽位接入 Active intent，接线与消费在 A2d 原子交付。

### 3. 应用组合层（A2d 接线）

- Turn 开始时从 Clock 取得一次 base_year；
- 根据 Operation intent 决定 QuotaKind；
- finalize raw quota；
- blocking issue 进入 Clarifier/明确拒绝；
- 只把 executable QuotaSpec 交给 Plan 和 Handler。

## Correctness Properties

### Property A2b-1：filter / constraint 结构分离
YEAR、REFERENCE_TYPE、限定语言只出现在 filters；TOTAL、LANGUAGE_ZH、LANGUAGE_EN 的数量
只出现在 constraints；TOPIC 不进入配额模型。

### Property A2b-2：解析不静默丢失
每个非空 Raw clause 要么进入 canonical spec，要么产生可见 QuotaIssue；不存在未知维度、
未知 operator 或非法数字被静默忽略的路径。

### Property A2b-3：同维度约束等价于区间交集
Canonical constraint 等于同维度全部约束区间的交集；交集为空才冲突；每个维度最多一个结果。

### Property A2b-4：过滤器及联合约束可满足
YEAR/type/language filters 自身交集非空，且 filter 与语言/total constraints 不构成必然矛盾。

### Property A2b-5：相对年份稳定
同一 raw + base_year 输入得到相同绝对年份区间；模块不读时钟，Loop 不重算。

### Property A2b-6：语言策略单一来源
语言 filter 和语言计数均使用 A2a classifier + policy；other/unknown 永不进入 zh/en 维度。

### Property A2b-7：计数顺序确定
按 reference_id 去重 → 应用全部 filters → 计算 total/zh/en → 评估 canonical constraints。

### Property A2b-8：缺口方向明确
QuotaGap 的 direction/amount 唯一表达不足或超出，不使用正负号猜语义。

### Property A2b-9：Active 路径无静默槽位
在 Handler 和写作编排器真实消费 QuotaSpec 前，Active IntentSpec 不暴露 quota 槽位。

## Error Handling

| 失败点 | 处理 |
|---|---|
| 未知 field/operator | blocking QuotaIssue，不生成 executable spec |
| 数字无法解析 | `invalid_number`，不得猜测 |
| “约 N 篇/二十来篇” | `ambiguous_approximation`，请求澄清 |
| relative year 缺 base_year | 编程错误；组合层必须注入 |
| 同维度区间交集为空 | `constraint_conflict` |
| filter 交集为空 | `filter_conflict` |
| filter 与 constraint 必然矛盾 | `filter_constraint_conflict` |
| zh/en 之外的语言数量要求 | `unsupported_dimension`，不映射为英文 |
| kind/scope 不合法 | `invalid_kind_scope` |
| record 缺过滤字段 | 保守不通过该 filter |
| 无配额 | 不生成 QuotaSpec，不追问，维持既有行为 |

## Testing Strategy

1. 裸“引用 20 篇”→ TOTAL min 20（引 22 篇不产 gap、引 18 篇产 BELOW gap），summary 展示
   “按至少 20 篇处理”的 assumption；显式“恰好 20 篇”→ exact 20（多引也产 ABOVE gap）；
   “至少/最多/区间”正确。
2. 同维度区间求交：exact5+min4 合法，exact5+min6 冲突，多 min/max 收敛正确。
3. 年份/type/language filter 正常求交及冲突。
4. LANGUAGE=zh + EN min1、LANGUAGE=zh + TOTAL min20 + ZH max10 均冲突。
5. total max10 + zh min6 + en min6 冲突；total min10 + zh min8 + en min8 不冲突。
6. “最近五年”在 base_year=2026 时为 2022–2026；同输入确定性。
7. “约20篇”返回 clarification，不自动生成任意范围。
8. “只要中英文”→ LANGUAGE IN (zh,en)；“中文至少5篇”→ LANGUAGE_ZH min5。
9. count_by_dimension 先按 reference_id 去重，再过滤；语言 confidence policy 生效。
10. other/unknown 可进入 total，但不进入 zh/en；zh/en 外语言数量产生 unsupported issue。
11. QuotaGap 对低于下界和超过上界分别产 BELOW/ABOVE，amount 恒正。
12. RAW_QUOTA_JSON_SCHEMA 能约束嵌套数组和值；程序侧仍拒绝非法枚举和类型。
13. `literature/quota.py` AST 不 import tasking/config/datetime。
14. 未启用 A2d 接线时，当前 Active intent 行为及既有测试逐项不变。

## 实施顺序建议

1. `literature/quota.py` 的枚举、Raw/Final 数据模型与 Issue 模型；
2. filter/constraint 规范化、区间求交、冲突检测与属性测试；
3. 纯评估器、QuotaGap 和 summary DTO；
4. `RAW_QUOTA_JSON_SCHEMA` 与 SlotSpec `json_schema_override`，仅做离线抽取测试；
5. 结构依赖检查和全套回归；
6. A2d 再原子接入 Active IntentSpec、finalize、Clarifier、Plan、Handler 和确认摘要。

## 已定决策

- 语言配额只支持中文/英文；other/unknown 不进入语言分项。
- Topic 由 A2a relevance 负责，不进入 EligibilityFilter。
- 裸数量默认 min（多引不拦、少引才拦），显式“恰好/正好”才 exact；均在确认摘要显式展示。
- 相对年份由 Turn 级 Clock 注入并一次性固化，不新增 quota 模块时钟依赖。
- Plan 只保存 executable QuotaSpec，不保存待 finalize 的 RawQuotaSpec。
- A2b 不提前激活一个尚未被执行器消费的 quota 槽位。
- A2b 同时建模 Retrieval/Citation；是否向用户暴露独立 search_literature 由 A2c/产品决策，
  不能在 Requirements 尚未同步时由本设计单方面删除。
