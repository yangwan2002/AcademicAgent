# Implementation Plan — A2b：配额解析（资格过滤 + 计数约束）

## Overview

A2 的第二个子阶段，把用户的文献数量要求收敛为**可验证、可执行的结构化 `QuotaSpec`**：
资格过滤（`EligibilityFilter`）与计数约束（`CountConstraint`）严格分离（Req 6.1），
区分 `QuotaKind`/`QuotaScope`，相对年份一次性固化，矛盾确定性检出。

**纯离线、不碰网络、不运行检索 Loop、不改 Working Copy、不改 facts.json schema。**
解析两段式：LLM 只抽松散的 `RawQuotaSpec`，代码侧 `finalize_quota` 做全部确定性工作
（年份固化、filter/constraint 分流、operator 归一、冲突检测）。

**验收铁律**：既有测试全绿；`finalize_quota` / 过滤 / 计数 / 冲突检测 / 评估器全部为
确定性纯函数、可属性测试；`literature/quota.py` **不 import `tasking`、`config`、
`datetime`**（base_year 与 language policy 注入）。

范围约束（承接 A2a / A2b design）：

- 语言配额只支持中/英：`CountDimension ∈ {total, language.zh, language.en}`；
  语言桶对齐 A2a `quota_bucket`，`other`/`unknown` 可计入 total（无语言硬过滤时），
  **绝不**计入 language.zh/language.en。
- `TOPIC` **不**进入 `EligibilityFilter`——主题相关性由 A2a 的 `RelevanceCriteria` 负责；
  filter field 收缩为 `year | reference_type | language`。
- 裸数量默认 `min`（**多引不拦、少引才拦**）；用户显式“恰好/正好”才 `exact`；
  二者都在确认摘要显式展示。
- **不新增意图、不删 `search_literature`**：Requirements 仍声明该意图，A2b 只是不实现它；
  同时建模 `RETRIEVAL`/`CITATION` 两种 kind 供后续复用。
- **不把 `citation_quota` 槽挂进 Active 的 `REWRITE_SPEC`/`ADD_SECTION_SPEC`**：
  槽位激活与执行消费在 A2d 原子交付（Req 16.4）。A2b 只定义 schema 与槽位常量 + 离线抽取测试。

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2", "4"] },
    { "wave": 3, "tasks": ["3"] },
    { "wave": 4, "tasks": ["5"] }
  ]
}
```

- Task 1 建立全部数据模型，是后续所有任务的地基。
- Task 2（`finalize_quota`，quota.py）与 Task 4（tasking 侧 schema，另一文件）都只依赖
  Task 1、互不冲突，可并行。
- Task 3（评估器 + summary，quota.py）依赖 Task 2 的 `QuotaFinalizeResult`/`QuotaSpec`，
  且与 Task 2 同文件，故排在其后，不与之并行。
- Task 5 是结构验收与全套回归，须等 Task 2/3/4 全部完成。

## Tasks

- [ ] 1. 配额领域模型与枚举（`literature/quota.py` 骨架）
  - 新建 `literature/quota.py`，定义枚举：`FilterField(year|reference_type|language)`、
    `FilterOperator(gte|lte|eq|in|range)`、`CountDimension(total|language.zh|language.en)`、
    `CountOperator(exact|min|max|range)`、`QuotaKind(retrieval|citation)`、
    `QuotaScope(new_only|ledger_total|actually_cited)`、`GapDirection(below|above)`、
    `QuotaIssueSeverity(warning|needs_clarification|error)`。
  - `FilterField` **不含** `TOPIC`（主题走 A2a relevance）。
  - 定义 frozen 数据模型：`EligibilityFilter(field, operator, value, verbatim)`、
    `CountConstraint(dimension, operator, value, verbatim)`、
    `QuotaSpec(kind, scope, eligibility_filters, count_constraints, base_year)`、
    `QuotaGap(dimension, operator, required, achieved, direction, amount)`、
    `QuotaIssue(code, severity, detail, verbatim)`、
    `QuotaFinalizeResult(spec, issues)` 含 `executable` 派生属性（spec 非空且无
    NEEDS_CLARIFICATION/ERROR）。
  - 定义 Raw 模型（clause 列表，可表达同维度多约束/重复/冲突）：
    `RawFilterClause(field, operator, value, verbatim)`、
    `RawCountClause(dimension, operator, value, verbatim)`、
    `RawQuotaSpec(filters, constraints, explicit_scope, notes)`。
  - 固化各 field 的 value 语义（写进 docstring）：YEAR→int 或 `(low,high)` 绝对年份；
    REFERENCE_TYPE→`tuple[str,...]`（取值来自 Req 1.9 的 reference_type 枚举）；
    LANGUAGE→bucket ∈ `{zh,en}`（operator eq/in）。
  - 集合字段一律 tuple；本模块**不** import `tasking`/`config`/`datetime`。
  - 单元测试：模型构造、`QuotaFinalizeResult.executable` 在各 severity 下的取值、
    frozen 不可变。
  - _Requirements: 6.1（结构分离）, 6.4（kind）, 6.5（scope）_
  - _Design: design-a2b.md D1, D2, D3, D9_

- [ ] 2. 确定性 `finalize_quota`（分流 + 年份固化 + 区间求交 + 冲突检测）
  - 实现 `finalize_quota(raw, *, kind, base_year, bare_count_policy="min") ->
    QuotaFinalizeResult`；纯函数，不读时钟（base_year 注入）。
  - **filter/constraint 分流（D3/D2）**：按字段语义重新归类——
    year/reference_type/“只要某语言” → `EligibilityFilter`；
    total/language.zh/language.en 的数量 → `CountConstraint`。LLM 误放的 clause 由此纠正。
  - **相对年份固化（D5，Req 6.7）**：“最近 N 年” → `EligibilityFilter(YEAR, RANGE,
    (base_year-N+1, base_year))`，并把 `QuotaSpec.base_year` 记录下来；不逐轮重算。
  - **operator 归一（Req 6.3）**：按表述解析 exact/min/max/range，不一律当 exact。
    裸数量按 `bare_count_policy`（默认 `min`）：“引用 20 篇” → `TOTAL min 20`；
    显式“恰好/正好 20 篇” → `TOTAL exact 20`。
  - **同维度区间求交（D6）**：每个 CountConstraint 转闭区间
    （exact N→[N,N]、min N→[N,+∞]、max N→[0,N]、range L..H→[L,H]），同 dimension 全部求交；
    交集为空 → `constraint_conflict`；每个 dimension 最多保留一个 canonical constraint；
    `range low>high` 在求交前直接 `constraint_conflict`。
  - **filter 自身与联合可满足性（D7）**：YEAR 的 gte/lte/range 求交空集 → `filter_conflict`；
    `REFERENCE_TYPE IN ()` 或多类型集合交空 → `filter_conflict`；LANGUAGE 支持 eq/in，
    语言值只能 zh/en；LANGUAGE 只允许 zh 却有 `LANGUAGE_EN min>0`（或对称）→
    `filter_constraint_conflict`；LANGUAGE 只允许 zh 且 `TOTAL min>0 + LANGUAGE_ZH max<total下界`
    等必然矛盾 → `filter_constraint_conflict`。
  - **跨维度矛盾（D6，Req 6.6 两条子规则）**：令 total_upper=total 的上界（exact 或 max；
    只有 min 则无上界），lang_lower_sum=zh/en 下界之和；
    total_upper 存在且 lang_lower_sum>total_upper → `constraint_conflict`；
    **WHERE total 为 min（只有下界），分项下界超过它 SHALL NOT 报冲突**；
    分项下界和 < total 下界也**不**冲突（other/unknown 补足）。
  - **不静默丢（D2，Req 6.1 口径）**：未知 field/operator → blocking issue；数字无法解析 →
    `invalid_number`；“约 20 篇/二十来篇”等近似 → `NEEDS_CLARIFICATION/ambiguous_approximation`
    （不私自定义容差）；zh/en 之外语言数量（日/德/…）→ `unsupported_dimension`，不映射成英文。
  - **kind/scope 校验（D1）**：合法组合仅 `CITATION+ACTUALLY_CITED` / `RETRIEVAL+NEW_ONLY` /
    `RETRIEVAL+LEDGER_TOTAL`；kind 由参数传入（调用上下文决定，不由 LLM）；scope 未指明按
    默认（citation→actually_cited、retrieval→new_only）；非法组合 → `invalid_kind_scope`。
  - 属性/单元测试：
    - “引用 20 篇，中文至少 5，最近五年期刊” → filters=[YEAR range, REFERENCE_TYPE in],
      constraints=[TOTAL min 20, LANGUAGE_ZH min 5]，base_year 正确；
    - 显式“恰好 20 篇” → TOTAL exact 20；“20 到 30 篇” → range；“最多 10 篇” → max；
    - exact5+min4 合法、exact5+min6 冲突、多 min/max 收敛正确、range(30,20) 冲突；
    - total max10 + zh min6 + en min6 冲突；**total min10 + zh min8 + en min8 不冲突**；
    - 最近五年在 base_year=2026 → (2022,2026)，同输入确定性；base_year 变 → 区间随之变；
    - “约 20 篇” → ambiguous_approximation（不生成任意范围）；
    - “日文 5 篇” → unsupported_dimension，不进 constraints；
    - 未知 field/operator、非法数字 → 对应 blocking issue，不静默丢。
  - _Requirements: 6.1, 6.3, 6.6, 6.7_
  - _Design: design-a2b.md D2, D3, D4, D5, D6, D7, Property A2b-1/2/3/4/5/6/7_

- [ ] 3. 纯评估器 + QuotaGap + 结构化确认摘要（`literature/quota.py`）
  - `filter_matches(record, filter, *, language_classifier, language_policy) -> bool`：
    YEAR 落区间；REFERENCE_TYPE ∈ 集合；LANGUAGE bucket==value（**复用 A2a**：语言 filter
    同样应用 confidence policy——`high_only` 下 medium/low 不通过 LANGUAGE filter）；
    record 缺 year/reference_type/language 字段 → 保守判**不通过**。
  - `passes_all_filters(record, filters, ...)`：全体 filter 的合取（Req 6.2）。
  - `count_by_dimension(records, spec, *, language_classifier, language_policy) -> dict`：
    **先按 `reference_id` 去重 → 施加全部 filter → 再计数**；total=通过 filter 的去重总数；
    zh/en 计数**委托 A2a `counts_toward(verdict, bucket, policy)`**，不重造语言逻辑；
    `include_low_with_warning` 下记录低置信计数供报告；other/unknown 不进语言维度。
  - `evaluate_constraints(counts, constraints) -> tuple[QuotaGap, ...]`：
    只消费 canonical constraints；min/exact/range 未达下界 → `BELOW`；
    max/exact/range 超上界 → `ABOVE`；`amount` 恒正；满足则不产 gap；不在内部放宽任何要求。
    （因裸数量默认 min，citation 裸配额“多引”天然不产 gap，无需 gate 特判。）
  - `QuotaSummary(kind, scope, base_year, filters, constraints, assumptions, issues)` +
    `build_quota_summary(result) -> QuotaSummary`：filters 与 constraints **分区**呈现，
    回显 base_year/kind/scope；assumptions 必须含“裸数量按至少 N 篇处理（多引不拦、少引才拦）”
    与相对年份的绝对区间；文本渲染留给 Console/UI，本函数只产 DTO（Req 6.9）。
  - 单元测试：
    - filter_matches 各 field/operator 正确；缺字段保守不通过；language filter 应用 policy；
    - count_by_dimension 先去重再过滤；zh/en 经 counts_toward；other/unknown 计入 total 但不计语言维度；
    - evaluate_constraints 的 BELOW/ABOVE/amount 正确，min 多引不产 gap、少引产 BELOW；
    - build_quota_summary filters/constraints 分区、回显 base_year/kind/scope 与 assumptions。
  - _Requirements: 6.2, 6.9（复用 5.6, 5.7）_
  - _Design: design-a2b.md D8, D9, D12, Property A2b-6/7/8/9_

- [ ] 4. 嵌套 quota 的 JSON Schema 与槽位常量（`tasking` 侧，**不激活**）
  - 为 `SlotSpec` 增加可选字段 `json_schema_override: dict | None = None`；
    未提供 override 的既有槽位行为**逐字不变**（默认 None）。
  - 修改 `tasking/extractors/base.py` 的 `_slot_property`：当槽位有 `json_schema_override`
    时**原样使用**该结构 schema（含嵌套 filters/constraints 数组），否则维持现状
    （现有 `name=="operations"` 的硬编码特判保持不变，本任务不重构它）。
  - 定义共享 `RAW_QUOTA_JSON_SCHEMA`：显式声明 `filters`/`constraints` 为对象数组，
    每项含 field/operator/value/verbatim（constraint 用 dimension/operator/value/verbatim），
    以及 `explicit_scope`；rewrite/add_section **共用同一份 schema**，不各自复制。
  - 定义 `CITATION_QUOTA_SLOT_SPEC` 常量（value_type="object" + json_schema_override=
    RAW_QUOTA_JSON_SCHEMA + 抽取 description），**但不**把它加入 `REWRITE_SPEC`/
    `ADD_SECTION_SPEC` 的 slots（D11：激活留 A2d）。
  - 离线抽取测试（stub LLM，复用 A2a 的 `structured_call` 测试模式）：
    - `build_tool_schema` 对带 override 的槽位产出嵌套结构 schema（非空 object）；
    - 无 override 的既有五意图 schema **逐字不变**（快照/结构断言）；
    - LLM 输出后程序侧仍能构造 `RawQuotaSpec` 并拒绝非法枚举/类型（schema 非唯一防线）。
  - _Requirements: 16.2（配额相关槽位的 schema 定义）_
  - _Design: design-a2b.md D10, D11, Property A2b-9_

- [ ] 5. 结构验收与全套回归
  - 全套既有测试绿（以本轮开始收集到的实际数量为基线，新增 A2b 测试后总数应上升）；
    新增 A2b 单元/属性测试全部通过。
  - AST 结构断言：`literature/quota.py` **不** import `tasking`、`config`、`datetime`
    （复用 A2a wiring 测试模式）。
  - 确认 Active 的 `REWRITE_SPEC`/`ADD_SECTION_SPEC` 槽位集合**未变**（citation_quota 未激活），
    既有 planning/extractor/handler 行为逐项不变。
  - 确认 Requirements 与 A2b Design 口径一致：filter field=year/reference_type/language
    （无 topic）、计数维度=total/language.zh/language.en、裸数量默认 min。
    若 Requirements 仍把 topic 列为 filter field，**先同步 Requirements**再宣称 A2b 完成。
  - 输出测试摘要并清理临时文件；不修改 Working Copy 和 facts.json schema。
  - _Requirements: A2b 验收铁律_
  - _Design: design-a2b.md Testing Strategy, Components §1_

## Notes

- 每完成一个 Task，先说明变更与验证结果，再进入下一项。
- 全套测试以本轮开始时实际收集到的既有测试为基线；新增 A2b 测试后总数应上升，验收标准
  是既有测试无回归且新增测试全绿。
- A2b 只保证**解析正确、可离线验证、失败诚实**，不运行检索、不接写作链路。真实检索质量、
  Retrieval_Quota 派生、LiteraturePreflight、CitationQuotaGate 的实际回灌属 A2c/A2d/spec B。
- `finalize_quota` 的 base_year 与 language policy 由**应用组合层**注入；本阶段测试直接传参，
  不新增环境变量、不读系统时钟。
- `citation_quota` 槽位在 A2b **只定义不激活**；把它挂进 Active intent 而执行器尚未消费，
  等于“成功抽取配额却按旧逻辑写作”的静默失效，比明确不支持更危险（D11）。
- git 提交由用户自行完成。

## 已定决策

- filter field = `year | reference_type | language`（**topic 归 A2a relevance**）；
  计数维度 = `total | language.zh | language.en`；语言桶对齐 A2a，只有 zh/en 计数。
- filter 与 constraint 结构分离（Req 6.1）；年份/类型永不作计数 dimension。
- 解析两段式：LLM 抽 `RawQuotaSpec` + 确定性 `finalize_quota`；相对年份固化、operator 归一、
  区间求交、冲突检测全在代码侧；`quota.py` 不 import tasking/config/datetime。
- 裸数量默认 `min`（多引不拦、少引才拦），显式“恰好/正好”才 `exact`；均在确认摘要显式展示。
- 语言 filter 与语言计数**同一严格度**（confidence policy 绑定，high_only 下 medium/low 不通过）。
- QuotaGap 用 `BELOW/ABOVE + 正 amount`，不用正负号猜语义。
- 合法 kind/scope 组合仅三种；kind 由调用上下文决定，不由 LLM。
- `citation_quota` 槽 A2b 只定义 schema/常量，**不激活**；激活与 Handler 消费属 A2d。
- 不删 `search_literature`（Requirements 仍声明），A2b 同时建模 RETRIEVAL/CITATION 两种 kind。
