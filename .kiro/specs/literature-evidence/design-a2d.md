# Design Document — A2d：写作链路接入（LiteraturePreflight + EvidenceBundle + CitationQuotaGate）

> 本文件沿用 A2a/A2b/A2c 后的内部编号 **A2d**；它在 `requirements.md` Requirement 19.7
> 的高层交付阶段中对应 **A3（写作接入）**，不是 Requirements 所说的 A2。实现任务、trace
> 与 feature flag 使用能力名 `literature_writing_integration`，避免把内部 A2d 与高层 A3 混用。
>
> A2d 只收尾“带引用配额的写作链路”。多引文格式完整渲染、`fix_bibliography`、spec B
> Evidence Gate 与真实 Web Discovery 后端仍不在本期，因此 A2d 不是整个 Spec A 的最终收尾。

## Overview

A2d 解决如下完整请求：

> “改写引言，正文至少引用 20 篇文献，其中中文至少 5 篇，限定最近五年。”

```text
用户请求
  → RawQuotaSpec（LLM 只抽原始 clauses）
  → QuotaMaterializer（确定性 finalize；Turn 级 base_year）
  → Operation.slots["citation_quota"] = executable QuotaSpec(CITATION/ACTUALLY_CITED)
  → WritingLiteratureOrchestrator
       1. 冻结 RelevanceCriteria + TaskFingerprint
       2. LiteraturePreflight：统计 → AcquisitionGap → 必要时 RetrievalLoop
       3. STAGED batch 持久化 → 用户确认 → 单 envelope commit → 重新统计
       4. 构造 quota-aware EvidenceBundle；必要时取得证据清单确认
       5. Writer Loop：generate → Structural Gate + CitationQuotaGate → 修订
       6. 全部通过，或用户明确接受最终偏差后，才写 Working Copy
```

### A2d 做

- 激活 `rewrite/add_section.citation_quota`，并在同一原子交付中完成 finalize、Plan、Handler 消费；
- LiteraturePreflight 领域计算与可恢复编排；
- 满足 Requirement 12 全字段契约的 `EvidenceSpan/EvidenceBundle`；
- CitationQuotaGate、公开引用 token 解析器及 Writer Loop 接线；
- 带配额路径严格核验、批次确认、缺口决定、trace 与回归保护。

### A2d 不做

- 不实现 spec B 的 Claim–Evidence 语义支撑判定；
- 不实现真实 Web Discovery provider；
- 不实现 `fix_bibliography` 或多格式参考文献列表完整渲染；
- 不在本期合并 Shadow/Active、删除兼容字段或拆分 `quota.py`；这些另开去冗余任务；
- 不删除独立 `search_literature` 能力。Requirement 16.1 仍要求该意图存在。

### 验收红线

- 没有 CitationQuota 的 rewrite/add_section 继续走旧路径，输出与现状逐字节一致；
- 用户明确给出配额但功能不可用时必须明确 `NEEDS_INPUT/UNSUPPORTED`，不得降级成“忽略配额继续写”；
- 写作循环内绝不检索；检索只发生在 LiteraturePreflight；
- 未经用户确认不得放宽配额、提交 STAGED batch 或接受最终未达标正文；
- 检索已提交的文献是独立项目资产，后续写作失败或 Working Copy 回滚不得回滚 Ledger；
- 默认测试全部离线，既有测试全绿。

## 前置门槛与 Requirements 同步

1. **A2c 必须先真正收尾。** `tasks-a2c-5.md` Task 3 已把 Loop–Discovery 接线定义为 A2c-5
   职责；当前 `retrieval/loop.py` 仍声明“不接 Discovery”。A2d 不接管该遗留任务：要么先完成
   A2c-5 Task 3，要么保持 Discovery 关闭后进入 A2d。A2d 不修改 RetrievalLoop 的核心状态机。
2. **真实相关性评测门槛。** 未达到 A2c-5 冻结的真实评测阈值前，只允许 supervised 模式：
   用户必须确认最终 EvidenceBundle 清单；不得把 relevance 自动结论作为完全无人监督的写作门槛。
3. **Requirement 13.11/14.6 需要收紧措辞。** “已知缺口维度不判败”不能解释成整个维度
   无条件跳过，否则用户接受“最多只能找到 18/20”后，正文只引用 2 篇也可能通过。实现前同步为：
   原始差额不再判为新失败，但 Gate 仍必须执行用户接受时冻结的 `effective_lower_bound=18`。
4. Requirement 12.1–12.7 已明确 EvidenceSpan 是唯一证据单位，因此 A2d 不再把 offset/hash
   推迟给 spec B；本期以“标题/摘要中的真实连续切片”实现最小完整契约。

## 关键设计决策

### D1. citation_quota 激活必须与 materialize、Plan、Handler 原子交付

LLM 只产 `RawQuotaSpec`。组合层在抽取后、`build_plan` 前执行：

```python
materialize_citation_quota(
    raw,
    *,
    base_year: int,
) -> QuotaMaterializationResult
```

- `kind=QuotaKind.CITATION` 与默认 `scope=QuotaScope.ACTUALLY_CITED` 由代码注入，不让 LLM 决定；
- `base_year` 由 Turn 级 Clock 固定一次；Loop、Gate 与重试不得重新求值；
- 裸数量按 A2b 的 `min` 语义；显式 exact/range/max 保留原意；
- 成功时，`Operation.slots["citation_quota"].value` 只能是 executable `QuotaSpec`，不得把
  `RawQuotaSpec` 或 `QuotaFinalizeResult` 传给 Handler；
- `RewritePlan/AddSectionPlan` 增加 `citation_quota: QuotaSpec | None`，Handler 在同一提交中
  真实读取并传入写作编排器；只挂槽、不消费视为失败；
- Handler 入口再次断言 `kind=CITATION/scope=ACTUALLY_CITED`，错误类型明确拒绝。

`citation_quota` 虽是“可选槽”，但用户一旦显式提供，INVALID/AMBIGUOUS 就是 blocking gap。
现有 Clarifier 只追问 required/must_ask 槽，不能直接满足这一点。A2d 增加条件式规则：

- `USER_ABSENT + optional`：不追问；
- 用户显式值为 INVALID/AMBIGUOUS：必须追问；
- 澄清答案再次走同一 RawQuota schema + `materialize_citation_quota`，不能把自然语言字符串
  直接塞入 `QuotaSpec`；
- 冲突项、相对年份落点、裸数量按 min 的解释进入计划确认摘要。

### D2. 写作任务只生成一个 RelevanceCriteria 与 TaskFingerprint

写作编排开始时冻结：

```python
WritingEvidenceRequest(
    criteria: RelevanceCriteria,
    task_fingerprint: str,       # 必须等于 criteria.fingerprint()
    citation_quota: QuotaSpec,
    initial_query: str,
)
```

- rewrite：topic/section_purpose 来自目标章节标题、原文摘要或内容哈希、direction/details；
- add_section：来自 title、purpose、structure_desc/subsections；
- `search_query` 非空时作为 `initial_query`，否则由上述结构确定性生成；
- RetrievalLoop、RelevanceStore、EvidenceBundle、Gate 与 trace 共用同一 fingerprint；
- `RetrievalRequest.task_fingerprint` 不得接受与 `criteria.fingerprint()` 不同的值。建议 A2d
  接线前把该字段改为派生值或在构造时断言，消除当前两套指纹入口。

### D3. LiteraturePreflight 分成领域服务与可恢复编排

`literature/preflight.py` 不接收 interactor、不 import `tasking/application`。确定性计算保持纯函数，
缺失 relevance 的补判则通过注入 Judge/Budget 的领域服务完成：

```python
ensure_relevance(criteria, records, *, judge, store, budget) -> RelevancePreparation
assess_availability(quota, criteria, ledger, relevance_store, policy) -> Availability
derive_retrieval_quota(acquisition_gaps, citation_quota, redundancy) -> QuotaSpec
effective_constraints(citation_quota, accepted_deviations) -> tuple[CountConstraint, ...]
```

应用层 `WritingLiteratureOrchestrator` 负责外部调用与状态推进：

```text
ASSESSING
  ├─ gap=0 ───────────────────────────────→ BUILDING_BUNDLE
  └─ gap>0 → RETRIEVING → NEEDS_BATCH_CONFIRMATION
                 ├─ reject ───────────────→ CANCELLED
                 └─ accept → CONFIRMED → COMMITTED → REASSESSING
                                                ├─ gap=0 → BUILDING_BUNDLE
                                                └─ gap>0 → NEEDS_GAP_DECISION
                                                              ├─ cancel → CANCELLED
                                                              └─ accept → BUILDING_BUNDLE
BUILDING_BUNDLE
  ├─ supervised → NEEDS_BUNDLE_CONFIRMATION → READY_TO_WRITE / CANCELLED
  └─ evaluated active mode ─────────────────→ READY_TO_WRITE
```

可暂停状态以 `PreflightCheckpoint` 保存 canonical WritingEvidenceRequest、batch_id、operation_id、
quota hash、task fingerprint、当前 gaps、revision 与已接受偏差；不持久化 EvidenceBundle 原文。
Checkpoint 进入 facts 单 envelope 的 `pending_literature_flows`，旧 schema 缺字段时按空处理。
控制台适配器可同步等待，API/多轮会话可返回 `NEEDS_*` 并在下一轮恢复。Checkpoint 是工作流
状态，不是第二套文献实体。

#### 可用文献统计

只统计同时满足以下条件的不同 `reference_id`：

- `status_under_policy(..., STRICT_FINAL_V1) == VERIFIED`；
- 通过全部 EligibilityFilter；
- 当前 TaskFingerprint 的 RelevanceAssessment 为 SUCCESS 且符合明确
  `RelevanceAcceptancePolicy`；
- relevance 缺失时在预检预算内判定；ERROR 不伪装 unrelated，也不计可用。

AcquisitionGap 只针对 lower bound（min/exact/range 的下界）触发检索；纯 max/above 不触发检索，
但仍由最终 CitationQuotaGate 执行。

#### 内部 RetrievalQuota 派生

- `kind=RETRIEVAL`、`scope=NEW_ONLY`；完整复制 EligibilityFilter；
- 每个缺口目标为 `ceil(BELOW_gap * redundancy_factor)`，factor 必须 `>=1` 且有上限；
- total/zh/en 仍是同一候选集上的重叠约束，不能把三个数相加成总检索量；
- 预算上限优先于冗余目标；报告分别显示“用户要求、当前可用、缺口、内部冗余目标”；
- 原始 CitationQuota 全程 frozen，绝不被内部 RetrievalQuota 覆盖。

Loop 结束后先把 STAGED batch 写入 pending 区，再向用户展示；接受后持久化 CONFIRMED 状态并
调用 `commit_batch`。拒绝则标记 REJECTED，Ledger 不变。commit 后必须重新统计，不能直接相信
Loop 的预览计数。

### D4. 已知缺口使用“有数量的偏差记录”，不是维度级布尔豁免

```python
@dataclass(frozen=True)
class AcceptedQuotaDeviation:
    stage: str                    # preflight | context_budget | final_gate
    dimension: CountDimension
    original_lower_bound: int
    achieved_at_decision: int
    effective_lower_bound: int
    original_gap: int
    task_fingerprint: str
    operation_id: str
    quota_hash: str
    accepted_at: str
```

例如用户要求 min 20，但严格可用文献只有 18，并明确接受继续：原始要求仍是 20，报告仍显示
缺 2；Gate 的阻塞下界变成 18，而不是跳过 total 维度。exact/range 的原始上界保持不变。

- 每次接受只作用于当前 operation + task fingerprint + quota hash；不得跨任务复用；
- 非交互/Shadow 路径不得默认接受偏差，只能返回 NEEDS_INPUT 或保守取消；
- Writer 重试耗尽后若用户再次接受正文实际缺口，新增 `stage=final_gate` 记录；不得改写旧记录；
- 偏差决定进入项目工作流状态与 trace，但不修改 CitationQuota 本体。

### D5. EvidenceSpan 本期实现完整最小契约

```python
@dataclass(frozen=True)
class EvidenceSpan:
    span_id: str
    reference_id: str
    source_field: str             # title | abstract | abstract_section:<name>
    capability: EvidenceCapability
    source_version: str
    content_hash: str             # NFC 后完整 source_field 哈希
    start_offset: int             # Unicode code point；[start, end)
    end_offset: int
    verbatim_text: str
    verbatim_hash: str
```

- 文本先走 A2a 的外部文本清洗，再 NFC；offset/hash 全基于同一规范化文本；
- A2d 最小实现可为每个 abstract 创建一个完整连续 span；超预算时可创建确定性连续切片，
  但必须保证 `verbatim_text == normalized_source[start:end]`；
- title span 一律 `DISCOVERY_ONLY`；abstract/abstract_section span 为 `CLAIM_SUPPORT`；
- source 内容哈希变化后旧 span 为 stale，不得复用；
- `span_id` 由 reference_id/source_field/source_version/offset/verbatim_hash 稳定派生；
- EvidenceSummary 若存在，只能引用 span_ids，不能成为独立证据。

### D6. EvidenceBundle 必须 quota-aware，不能只按相关性截断

EvidenceBundle 是一次 writing run 的冻结值对象；同一 run 内的多次生成重试共用同一实例。
Bundle 内容变化必须产生新 bundle_id 与新 writing run。

构造顺序：

1. 只取 STRICT verified、当前 task relevant、通过 filters 的 Reference；
2. rewrite 中原文已经引用、且满足 STRICT/relevance/filter 的 Reference 优先固定进 Bundle，
   使合法的原有引用可以推进“最终正文实际引用”配额；
3. 再按 effective constraints 选择 zh/en 最低数量，补足 total，按 reference_id 去重；
4. 各桶内按 relevance verdict/confidence、证据能力、年份、reference_id 稳定排序；
5. 满足最低配额后，才用剩余上下文预算补充高相关候选；
6. ContextBuilder 的系统提示、原章节与用户要求预算必须先预留，不能把全部窗口给摘要；
7. 若预算不足以容纳 effective lower bounds，返回 `CONTEXT_BUDGET_INSUFFICIENT` 与逐维度
   bundle gap，在用户接受新的 context_budget 偏差或调整预算前不得进入 Writer Loop。

Bundle 携带 `bundle_id`、task_fingerprint、policy/authority version、reference entries、
EvidenceSpans、截断统计与 effective constraints。bundle_id 由上述内容的 canonical hash 产生。
Bundle 只存在于 writing run，不写普通 trace；trace 只记 ID、哈希、数量和预算。

ContextBuilder 使用明确的“外部引用数据块”包裹标题/摘要 span，声明其中指令不可执行；无
CLAIM_SUPPORT span 的 Reference 可用于说明书目存在，但 prompt 明确禁止据其标题推导具体结论。

真实评测未过门槛时，Bundle 构造后必须展示清单并取得用户确认，即使 Preflight 没有触发新检索。

### D7. CitationQuotaGate 同时负责配额与新增引用边界

不要跨模块复用私有 `_cite_tokens`。将其提升为公开纯函数，例如：

```python
extract_citation_tokens(text: str, ext: str) -> tuple[str, ...]
```

解析 `.tex` 的 `\cite{}` 与 `.md/.docx` 的 `[id]`，并通过 `ledger.resolve_token(token, ext)`
保留 `RESOLVED/AMBIGUOUS/NOT_FOUND` 三态，不把歧义当未找到或任意选择一条。

安全与计数口径：

- add_section：所有引用都必须解析到当前 Bundle；
- rewrite：原文已经存在的引用可按兼容规则原样保留；候选正文**新增**的引用必须来自当前 Bundle；
- 新增 unresolved、ambiguous、new_out_of_bundle 任一出现都阻塞写入；原文中原样保留的 legacy/
  unresolved token 单独报告但不推进配额，也不因本次 rewrite 被自动删除；
- 只有 `Bundle ∩ candidate resolved references` 进入 CitationQuota 计数；原文保留但不在 Bundle
  或不满足 STRICT/relevance/filter 的引用不推进新配额；
- 以不同 reference_id 去重，应用 EligibilityFilter 与 effective constraints；同时保留按原始
  CitationQuota 计算的 gap 供诚实报告；
- `used_reference_ids = Bundle ∩ candidate`，`unused_reference_ids = Bundle - used`，二者不交且
  并集等于 Bundle reference IDs。

```python
@dataclass(frozen=True)
class CitationQuotaGateResult:
    passed: bool
    effective_gaps: tuple[QuotaGap, ...]
    original_gaps: tuple[QuotaGap, ...]
    used_reference_ids: tuple[str, ...]
    unused_reference_ids: tuple[str, ...]
    new_unresolved_tokens: tuple[str, ...]
    new_ambiguous_tokens: tuple[str, ...]
    new_out_of_bundle_ids: tuple[str, ...]
    preserved_legacy_tokens: tuple[str, ...]
    violations: tuple[str, ...]
```

Writer 每轮对同一 candidate 同时运行现有 RewriteGate/AddSectionGate 与 CitationQuotaGate，合并
违规后一次回灌。回灌只给逐维度 gap 与 Bundle 中满足该维度但未使用的文献，不重新检索、不更换
Bundle。两类 Gate 共用同一个 writer retry budget。

预算耗尽仍未满足时，只能：返回失败；或展示候选与 gap，由用户明确接受 final_gate 偏差。
Structural Gate、未知引用、歧义引用与 Bundle 外新增引用不可被 quota 偏差豁免。

### D8. 带配额路径替代旧搜文献 SOP，避免双重检索

- 有 CitationQuota：Preflight 是唯一检索入口；`want_search/search_query` 只为 Preflight 提供
  initial_query，禁止再调用旧 `search_and_verify`；
- 无 CitationQuota：完整保留当前 `want_search → search_and_verify(require_final=False)` 路径；
- 带配额路径只使用 `STRICT_FINAL_V1`，全局 `literature_require_final_verification` 默认继续
  False，不在 A2d 翻全局开关；
- CitationQuota feature 被关闭但用户明确提出配额时，返回“该能力未启用”，不得走旧 SOP 后
  假装已经满足；只有没有新配额请求时，全部新 feature 关闭才退化为旧行为。

### D9. 用户交互使用独立 typed port，但复用同一个 UI 实现

`PlanningInteractor.confirm_plan()` 的语义是“确认执行计划”，不能拿来承载批次提交和放宽配额。
A2d 在 application 层定义窄协议：

```python
class LiteratureInteractionPort(Protocol):
    def confirm_retrieval_batch(summary: BatchSummary) -> BatchDecision: ...
    def resolve_quota_gap(report: QuotaGapReport) -> GapDecision: ...
    def confirm_evidence_bundle(summary: EvidenceBundleSummary) -> BundleDecision: ...
    def resolve_final_gate(report: FinalGateReport) -> FinalGateDecision: ...
```

现有 `ConsolePlanningInteractor` 可以同时实现 PlanningInteractor 与 LiteratureInteractionPort，
所以这是新增 typed capability，不是新建一套 UI/交互层。literature 领域模块不 import 该 port。

### D10. 提交、写作与回滚边界

- STAGED batch 未确认不进 Ledger；CONFIRMED batch 经 `commit_batch` 单 envelope 原子提交；
- commit 成功后，即使 Bundle 构建、LLM、Gate 或 Working Copy 写入失败，Ledger 仍保留该批文献；
- PlanExecutor 的 Working Copy baseline rollback 不得恢复或覆盖已经提交的 facts envelope；
- 候选正文只在所有不可豁免 Gate 通过，且 quota 已满足或有匹配的显式偏差记录后 splice；
- 任意检索/核验失败不得写入部分正文；任意写作失败不得产生半截 Working Copy。

### D11. 分层与可观测性

- `literature/`：Quota、availability、EvidenceSpan/Bundle 构造等纯领域能力；不 import tasking；
- `gates/`：公开 citation parser 与 CitationQuotaGate；
- `application/` 或 `runtime/`：Preflight 状态编排、typed interaction、feature flags、依赖注入；
- `intents/`：只负责具体 rewrite/add_section 生成与 splice，不直接管理 RetrievalLoop 状态；
- `generate/`：消费冻结 Bundle，按不可信数据规则组 prompt。

trace 至少记录：operation_id、task_fingerprint、quota hash、preflight 状态、availability/gap 计数、
batch_id/decision、bundle_id/引用数/截断数、Gate original/effective gaps、retry budget、用户偏差决定。
不得记录查询原文、摘要、EvidenceSpan.verbatim_text 或用户未公开的研究文本。

## Architecture

```text
Operation(citation_quota=QuotaSpec | None)
  ├─ None → existing rewrite/add_section path（逐字节不变）
  └─ QuotaSpec
       → WritingEvidenceRequest(criteria/fingerprint/query)
       → WritingLiteratureOrchestrator / Preflight state machine
            → availability
            → optional RetrievalLoop → pending batch → user decision → commit
            → re-assess → optional AcceptedQuotaDeviation
            → quota-aware EvidenceBundle → optional supervised confirmation
       → Writer Loop（同一 Bundle）
            generate
              → RewriteGate/AddSectionGate
              → CitationQuotaGate
              → retry feedback（不检索、不换 Bundle）
       → pass / explicit final quota deviation
       → splice Working Copy
```

## Components

- `tasking/intent_spec.py`：激活 citation_quota；
- `tasking/quota_materializer.py`（或组合层等价模块）：Raw → executable + 条件式澄清；
- `intents/interview.py`：RewritePlan/AddSectionPlan 增 `citation_quota`；
- `runtime/handlers.py`：真实读取配额并传给写作编排；
- `literature/preflight.py`：availability、AcquisitionGap、RetrievalQuota 派生、effective constraints；
- `literature/evidence.py`：EvidenceCapability、EvidenceSpan、EvidenceBundle、quota-aware builder；
- `gates/citation_quota_gate.py`：公开 token parser、解析与配额 Gate；
- `application/literature_flow.py`：状态机、checkpoint、typed interaction 与恢复；
- `generate/context_builder.py`：冻结 Bundle + untrusted data blocks；
- `intents/rewrite.py` / `intents/add_section.py`：接入组合 Gate 与最终写回；
- `session/facts.py`：增加 `pending_literature_flows` checkpoint/偏差记录，schema 向前升级且旧文件
  缺字段按空；仍用单 envelope。

`literature/retrieval/loop.py` 的 Discovery 接线属于 A2c-5，不列入 A2d Components。

## Correctness Properties

1. **无配额零影响**：没有 CitationQuota 时旧写作路径逐字节不变。
2. **显式约束零静默丢失**：用户给了 quota 但 materialize/feature 失败时不进入写作。
3. **单一任务指纹**：criteria、Loop、Store、Bundle、Gate 的 fingerprint 必须相同。
4. **预检先于生成**：未到 READY_TO_WRITE 不调用 writer LLM。
5. **无人替用户放宽**：所有 accepted deviation 都有数量、作用域和显式用户决定。
6. **派生配额不篡改原配额**：RetrievalQuota frozen-copy filters，只以 gap 推导下界。
7. **证据定位完整**：每个 span 的文本等于 NFC source 的 `[start:end)`，hash 可复算。
8. **Bundle 可实现配额**：进入 Writer Loop 前，Bundle 对 effective constraints 可行；否则先决策。
9. **新增引用受 Bundle 限制**：未知、歧义或 Bundle 外新增引用永不写入。
10. **计数确定**：只按正文解析出的不同 Bundle reference_id 计数，不信模型自报。
11. **循环有界且不嵌套检索**：Writer retry budget 有界，retry 中不调用 RetrievalLoop。
12. **资产提交不回滚**：检索 commit 与 Working Copy 成败解耦。
13. **隐私边界**：trace 不含查询、摘要或 span 原文。

## Error Handling

| 场景 | 处理 |
|---|---|
| quota raw 冲突/含糊 | 条件式澄清；未解决不生成 |
| relevance ERROR | 不计可用，不映射 unrelated；报告并按 supervised 路径处理 |
| RetrievalLoop 未满足 | 持久化成果，展示 gap，等待用户决定 |
| STAGED batch 被拒绝 | batch→REJECTED，Ledger 不变 |
| commit revision conflict/落盘失败 | 保持 CONFIRMED，可重试；Ledger/内存/磁盘不变 |
| 非交互模式需要确认 | 返回 NEEDS_INPUT，不默认 accept |
| Bundle 上下文容量不足 | 返回 bundle gap；用户决定前不调用 writer |
| 引用 token 歧义/未知 | Gate 硬失败，不允许 quota waiver 绕过 |
| Writer budget 耗尽 | 返回候选与 gap，等待明确 accept/cancel；不静默写回 |
| Working Copy 写失败 | Ledger 保留，Working Copy 保持写前状态 |

## Testing Strategy

1. quota slot 未提供时旧 prompt、旧搜索调用与写回字节完全一致；
2. quota 激活、Raw 构造、base_year 固化、blocking issue 条件式澄清、Handler 真消费；
3. criteria/fingerprint 任一不一致立即拒绝；
4. availability 对 strict/relevance/filter/language 的组合与 ERROR 分支；
5. RetrievalQuota 按 gap+冗余派生，不修改 CitationQuota，total/zh/en 不错误相加；
6. Preflight 全状态转移、checkpoint 恢复、STAGED/CONFIRMED/REJECTED/COMMITTED；
7. 非交互路径绝不默认确认 batch、Bundle 或 quota deviation；
8. EvidenceSpan NFC、offset、content/verbatim hash、stale 判定属性测试；
9. quota-aware Bundle 的语言/总数覆盖、稳定排序、预算不足与 bundle_id 变化；
10. prompt 中外部摘要被数据块包裹，trace 不含原文；
11. citation parser 的 tex/md/docx、多 token、别名、歧义、未知与去重；
12. rewrite 允许原引用原样保留，但阻止新增 Bundle 外引用；add_section 全部引用须在 Bundle；
13. 原 quota 与 effective quota 双重报告；18/20 waiver 后正文引用 2 篇仍失败；
14. Structural Gate 与 CitationQuotaGate 合并反馈、共享预算、不触发检索；
15. commit 后 Writer/Gate/写盘失败，Ledger 仍保留，Working Copy 不产生部分内容；
16. supervised/active feature gate 与真实评测门槛；
17. 全套既有测试与新增端到端 stub 测试通过，默认不发网络。

## 实施顺序

1. **A2d-0：前置同步**
   - 明确 A2d=Requirements A3；同步 Requirement 13.11/14.6/Property 25 的 effective lower bound；
   - A2c-5 Discovery Task 3 完成或明确关闭；冻结 supervised 默认。
2. **A2d-1：Quota 原子激活**
   - materializer、条件式澄清、IntentSpec、Plan、Handler 同一批交付。
3. **A2d-2：Evidence 契约与 Bundle**
   - EvidenceSpan 全字段、quota-aware builder、ContextBuilder 数据块与容量门槛。
4. **A2d-3：CitationQuotaGate**
   - 公共 parser、新增引用边界、original/effective 双评估、Writer Loop 接线。
5. **A2d-4：LiteraturePreflight 编排**
   - availability、gap→RetrievalQuota、pending batch、typed interaction、commit/re-assess/deviation。
6. **A2d-5：端到端与迁移回归**
   - feature flags、trace、恢复、资产/Working Copy 回滚边界、全套离线测试。

## 已定决策

- 保留文件名 A2d，但明确它对应 Requirements 高层 A3；
- EvidenceSpan 最小完整契约本期实现，不推迟给 spec B；
- `require_final` 全局默认仍 False；只在带 CitationQuota 的新路径显式使用 STRICT；
- Discovery Loop 接线归 A2c-5，A2d 不修改 RetrievalLoop 核心；
- 已知缺口用有数量的 AcceptedQuotaDeviation，不做维度级无条件豁免；
- 交互新增 typed port，复用现有 Console/UI 实现，不滥用 `confirm_plan()`；
- 真实 relevance 评测达标前默认 supervised，非交互模式不自动放宽；
- A2d 只接 rewrite/add_section；polish 不新增引用，不接 Preflight/Gate；
- `search_literature` 不删除；Shadow/Active 去冗余与 quota.py 拆分独立实施。
