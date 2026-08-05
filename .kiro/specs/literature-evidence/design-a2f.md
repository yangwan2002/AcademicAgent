# Design Document — A2f：检索意图准备与缺口驱动查询规划

> A2f 是 `literature-evidence` 的查询质量加固阶段，不是新的顶层 Agent，也不在意图识别前增加
> “整句改写”。它解决 dogfood 暴露的真实问题：带配额写作在用户没有显式 `search_query` 时，
> 当前实现把“引言/Introduction”等章节名当检索词；同时组合层虽已有 `LLMQueryPlanner`，却始终
> 注入 `DeterministicQueryPlanner`。
>
> 本期把“检索什么”建模为冻结的 SearchIntent，把“怎样继续搜”留给 QueryPlanner。前者决定
> RelevanceCriteria 的语义主题，后者只改变检索策略，二者不得混成同一字符串。

## 现状与问题

- `runtime/handlers._prepare_citation_quota()` 使用
  `initial_query = plan.search_query or criteria_topic`；add_section 的 criteria_topic 是章节标题，
  rewrite 是 section_id，因此可能用“引言”检索。
- 用户原话中未必有独立 search_query，但 `purpose/details/structure`、项目 claims 可能已经包含
  足够主题；当前没有利用这些受控信号。
- 当前 `RelevanceCriteria(topic=章节标题, query_intent=initial_query)` 会让错误主题同时污染
  检索与相关性判断。
- `LLMQueryPlanner` 已实现，组合层却固定注入 `DeterministicQueryPlanner`。
- 当前 Loop 首轮只运行一个 initial_query；直接改成“一次跑完六个 seed”又会浪费预算，并在
  第一个查询已满足配额时继续发无意义请求。

## 目标

1. 从显式搜索要求、写作操作语义和受控项目上下文生成一个语义主题与 1–6 个候选 seed。
2. 语义主题用于 RelevanceCriteria；seed 只用于召回策略，不参与 TaskFingerprint。
3. 多 seed 按缺口和预算自适应执行，每个 seed 后立即重新评估，满足后立刻停止。
4. 续搜可配置 deterministic / llm / hybrid，并始终保留确定性 fallback。
5. 缺少可用主题时明确澄清，不再拿通用章节名硬搜。
6. 不带 CitationQuota 的旧 rewrite/add_section 完全不进入本能力，行为逐字节不变。

## 非目标

- 不在 OperationAnalyzer 前重写整条用户消息；
- 不修改用户 CitationQuota、EligibilityFilter、TaskFingerprint 或预算上限；
- 不增加中文数据库或真实 Web Discovery provider；
- 不在本期实现按 provider 方言下推 language/year/type filter；该能力可另做 A2g；
- 不重新设计相关性 Judge、核验、Bundle 或 CitationQuotaGate。

## Requirements 对齐前置

A2f 是 Requirement 8.2“调整检索策略”、13.5“Preflight 调 RetrievalLoop”、17“不可信输入”与
18.3“trace 隐私”的细化。冻结 Tasks 前在 Requirements 增加以下验收口径，避免 Design 独自
创造用户行为：

- 禁止仅以通用章节名（引言、相关工作、Discussion 等）作为检索主题；
- 无显式 query 且上下文不足时必须澄清；
- SearchIntent 的主题变化会改变 TaskFingerprint，seed/planner 查询变化不会；
- 多 seed 与查询规划调用必须计入现有检索预算；
- 持久化 trace 不记录 topic/query 原文，只记录 hash、长度、数量和来源类型。
- 派生主题需要确认时必须可跨 turn 恢复同一个冻结 SearchIntent，不得在恢复时重新采样；
- 恢复到批次确认状态时必须复用原 pending batch，不得重复发起检索。

## 关键设计决策

### D1. SearchIntent 区分语义任务与查询策略

```python
class SearchIntentOrigin(str, Enum):
    USER_EXPLICIT = "user_explicit"
    OPERATION_DERIVED = "operation_derived"
    PROJECT_DERIVED = "project_derived"
    MIXED = "mixed"

class SeedTarget(str, Enum):
    ANY = "any"
    LANGUAGE_ZH = "language.zh"
    LANGUAGE_EN = "language.en"

@dataclass(frozen=True)
class SearchSeed:
    text: str
    target: SeedTarget = SeedTarget.ANY
    origin: SearchIntentOrigin = SearchIntentOrigin.OPERATION_DERIVED

@dataclass(frozen=True)
class SearchIntent:
    semantic_topic: str
    section_purpose: str = ""
    seeds: tuple[SearchSeed, ...] = ()
    origin: SearchIntentOrigin = SearchIntentOrigin.OPERATION_DERIVED
    schema_version: str = "search-intent-v1"

    def semantic_fingerprint(self) -> str: ...
    # canonical semantic fields；不含 seeds，用于 RelevanceCriteria / TaskFingerprint

    def strategy_hash(self) -> str: ...
    # semantic_fingerprint + 按确认顺序规范化后的 seeds，用于 checkpoint / 恢复 / 审计
```

准备过程的状态不塞进 `SearchIntent`，由 application 层的结果包装承载：

```python
class SearchIntentPreparationStatus(str, Enum):
    READY = "ready"
    NEEDS_INPUT = "needs_input"
    ERROR = "error"
    CANCELLED = "cancelled"

@dataclass(frozen=True)
class SearchIntentPreparationOutcome:
    status: SearchIntentPreparationStatus
    intent: SearchIntent | None = None
    issue_code: str = ""
```

关键不变量：

- `semantic_topic + section_purpose` 表达“什么文献算切题”，进入 RelevanceCriteria；
- `seeds` 表达“到 provider 里怎么搜”，只影响召回，不进入 RelevanceCriteria semantic fingerprint；
- QueryPlanner 后续产生的新 query 更不能改变 semantic_topic、quota 或 semantic fingerprint；
- `semantic_fingerprint()` 使用 schema_version、清洗后的 semantic_topic 和 section_purpose；
  `strategy_hash()` 再加入按确认顺序排列的规范化 seed text/target。两者都基于 canonical JSON 计算；
- 两种哈希不得混用；Loop 运行中新增的 Planner query 由 checkpoint 的 tried-query 状态单独记录；
- READY 必须携带合法 intent；NEEDS_INPUT/ERROR/CANCELLED 的 intent 必须为空并携带稳定 issue_code，
  不用“空 SearchIntent”表示失败；
- SearchIntent 只能通过验证工厂/`__post_init__` 构造，保证 topic 非空、seed 为 1–6 个且均已规范化；
- 删除原方案未被消费的 `source_hint`，避免先造字段、后猜用途；
- `origin` 使用 Enum，不用任意字符串；不提供 `title_fallback` 这种会重新引入旧问题的来源。

`SearchIntent` / `SearchSeed` 放在 `literature/retrieval/search_intent.py`，是纯值对象和验证器，
不 import tasking、session 或 config。

### D2. 主题信号优先级必须明确且有边界

应用层先构造只读 `SearchContext`，再交给 synthesizer：

```python
@dataclass(frozen=True)
class SearchContext:
    explicit_query: str = ""
    operation_kind: str = ""          # rewrite | add_section
    target_title: str = ""
    purpose: str = ""
    direction: str = ""
    details: str = ""
    structure_terms: tuple[str, ...] = ()
    project_gap: str = ""
    project_novelty: str = ""
    project_contributions: tuple[str, ...] = ()
    requested_dimensions: tuple[str, ...] = ()  # 只读 zh/en/total 信号，不含可修改 quota
```

信号优先级：

1. 用户显式 `search_query`；
2. 当前 operation 的 purpose/details/direction/有信息量的 title/structure；
3. `facts.claims` 中已确认的 gap/novelty/contributions；
4. 仍不足则 NEEDS_INPUT。

“现有章节标题列表”不能作为高优先级主题来源；它通常只有 Introduction/Method 等结构词，会
稀释真正主题。目标标题仅在通过 `is_informative_title()` 后使用；纯“引言/背景/相关工作/
Introduction/Background/Related Work/Discussion”等通用标题必须剔除。

不把整篇正文直接送入 synthesizer。本期只用上述有界字段，每字段清洗、NFC、限制长度；未来如
需使用正文，必须先有独立、可审计的论文主题摘要，而不是把长正文塞进 prompt。

### D3. 显式 query 优先走确定性路径，必要时才调用 LLM

定义协议与两种实现：

```python
class SearchIntentSynthesizer(Protocol):
    def synthesize(context: SearchContext) -> SearchIntent: ...

class DeterministicSearchIntentSynthesizer: ...
class LLMSearchIntentSynthesizer: ...
```

执行规则：

1. `explicit_query` 有效：不调用 LLM，直接构造 USER_EXPLICIT topic/seed；
2. operation/claims 已有明确主题：LLM 可生成同义词、学术术语与中英 seed，但 semantic_topic
   必须能追溯到输入字段；
3. LLM provider/parse/schema 失败：若确定性信号足够，退回 deterministic intent；否则 ERROR；
4. 信号本身不足：NEEDS_INPUT，不把系统失败伪装成“用户没说明”；
5. LLM 不能输出 quota 数量、filter、provider 凭据或 TaskFingerprint；Schema 中不存在这些字段。

输出统一经过代码侧验证：

- semantic_topic 清洗后长度 3–500；seed 清洗后长度 2–300；
- seed 数量 1–6、NFC + casefold + 空白归一后去重并保序；
- 删除空 query、纯通用章节名、仅标点/数字和与 topic 完全无关的 provider 指令；
- `target` 只能是 any/language.zh/language.en，且只生成用户配额实际需要的语言维度；
- 全部 seed 被删后返回 NEEDS_INPUT/ERROR，不偷偷恢复章节标题。

中文/英文 seed 可以由同一 semantic_topic 派生，但 `target` 只是调度提示，不证明返回文献语言；
最终仍以 A2a LanguageVerdict 计数。

### D4. 不把 LLM 合成逻辑塞进 Handler

原方案“Handler 内直接调用 synthesizer”不采用，原因是：

- 它发生在计划确认后，属于隐藏的第二次请求理解；
- 难以进入统一预算、trace、暂停恢复和用户修改；
- Handler 会同时承担槽读取、LLM 调用、澄清与执行编排，职责过重。

A2f 在 application 层增加 `SearchIntentPreparationService`：

```python
prepare_search_intent(
    operation_view,
    project_context,
    quota_dimensions,
    *, synthesizer, interaction_port, budget,
) -> SearchIntentPreparationOutcome
```

Handler 只把 operation 字段交给该服务，并消费 READY 结果；不 import LLM 实现。

- USER_EXPLICIT intent 已在计划摘要出现，可直接使用；
- 由**已确认 operation 字段**确定性投影出的 semantic_topic 可直接使用，避免重复确认；
- LLM 改写/合成了 semantic_topic，或主题主要来自 PROJECT_DERIVED/MIXED claims 时，必须在检索前
  通过 LiteratureInteractionPort 展示 semantic_topic 与 seed 摘要，用户可 accept/modify/cancel；
- 若 LLM 只生成同义词/中英 seed、没有改变确定性 semantic_topic，不强制增加一次确认；seed 仍可在
  当前用户界面临时展示，因为它不改变相关性标准或 TaskFingerprint；
- NEEDS_INPUT 调 `request_search_topic()`，用户补充文本后最多重新合成 N 次；不得无限追问；
- 非交互模式仅在确实需要澄清/确认时返回
  `NEEDS_SEARCH_TOPIC/NEEDS_SEARCH_INTENT_CONFIRMATION`，不默认接受；
- `strategy_hash()` 进入 PreflightCheckpoint；恢复时调用方必须提供相同策略哈希的冻结 intent，
  不允许重新采样 LLM 后悄悄换主题。

仅保存 hash 不能重建 LLM 已生成的 topic/seed，因此暂停时还必须保存**可恢复的冻结值**：

- `PreflightCheckpoint` 增 `search_intent_id`、`semantic_fingerprint`、`strategy_hash`；
- 完整 SearchIntent canonical payload 进入 session-scoped `pending_search_intents`，以
  `search_intent_id` 引用；它属于用户会话状态，不进入 trace/普通日志；
- 恢复时读取原 payload、重新计算两种哈希并比对。缺失或不一致返回 `STALE_SEARCH_INTENT`，
  不重新调用 LLM；用户 modify 时写入新版本并递增 revision；
- accept/cancel/flow 完成后清理 pending intent；落盘保护、保留期和删除策略沿用论文正文的 session 数据策略。

恢复必须按 checkpoint.state 继续，而不是无条件从 `run()` 开头重放：

- `NEEDS_SEARCH_TOPIC/NEEDS_SEARCH_INTENT_CONFIRMATION`：加载 pending intent 或继续澄清；
- `NEEDS_BATCH_CONFIRMATION`：按 checkpoint.batch_id 加载已有 pending batch，禁止重跑 RetrievalLoop；
- 其余已有状态沿用 A2d 的状态恢复规则。这样 UUID batch_id 不会在恢复时生成第二个批次。

因此 LiteratureInteractionPort 增加两个 typed 方法，而不是复用 `confirm_plan()`：

```python
def request_search_topic(summary) -> SearchTopicDecision: ...
def confirm_search_intent(summary) -> SearchIntentDecision: ...
```

### D5. RelevanceCriteria 只消费语义字段

```python
criteria = RelevanceCriteria(
    topic=search_intent.semantic_topic,
    section_purpose=search_intent.section_purpose,
    query_intent=operation_semantic_goal,
)
```

禁止使用 `" ".join(seed_queries)` 作为 `query_intent`。seed 的增删、排序或 Planner 改写只是检索
策略变化，不应使已有 RelevanceAssessment 缓存全部失效。只有用户/确认上下文改变
semantic_topic 或 section_purpose 时，才形成新的 TaskFingerprint 与新的 writing flow。

### D6. Request 模型只有一个查询真相源

`WritingEvidenceRequest` 增 `search_intent: SearchIntent | None = None`；
直接给 `RetrievalRequest` 增加 `seed_queries` 不是最佳长期模型，因为它会与 `initial_query` 形成双真相源。
采用兼容迁移：

```python
@dataclass(frozen=True)
class RetrievalRequest:
    quota: QuotaSpec
    criteria: RelevanceCriteria
    search_intent: SearchIntent | None = None
    initial_query: str = ""       # legacy only；search_intent 非空时必须为空或等于首 seed

    def effective_seeds(self) -> tuple[SearchSeed, ...]: ...
```

- 新 A2f 路径只传 SearchIntent；
- 旧测试/调用点可继续传 initial_query；
- 两者同时出现且内容冲突时构造失败，不允许一个字段被静默忽略；
- `batch_id` 使用 Loop 启动时生成并持久化的 UUID；恢复时复用原 UUID。`operation_id`、
  `semantic_fingerprint`、`strategy_hash` 作为独立 trace/checkpoint 字段，不再用 query hash 充当批次身份。

### D7. 多 seed 自适应调度，不在首轮盲跑全部查询

Loop 增加 seed queue，但保持现有 quota、核验、去重、停止语义：

```text
initial_gaps = evaluate_gaps(current_batch_or_empty, retrieval_quota)
若 quota satisfied → 不执行 seed queue
prioritized_seed_queue = prioritize(seeds, initial_gaps)

for seed in prioritized_seed_queue:
    检查预算/取消/query_fingerprint 去重
    若 seed.target 对应维度已经满足 → skip
    run_attempt(seed)
    立即重新计算 gaps/progress
    若 quota satisfied → 立即停止，不再跑剩余 seed

若仍有 gap：QueryPlanner.propose(gaps, tried_queries, ...)
    → 校验/去重 → run_attempt → 每次后重新评估
```

- seed 优先级：当前非零 gap 对应 target > any > 已满足维度 target；同级保持用户确认顺序；
- `query_fingerprint` 使用规范化 query + source + filters，跨 seed/planner 共用去重；
- 每个 seed 是一次 search call，所有候选、核验、LLM 与时间继续记入同一 BudgetLedger；
- `max_iterations` 表示策略轮次，`max_search_calls` 才是 seed 调用硬上限；文档和 trace 分开显示；
- 任一预算触顶保留已有 Batch，stop_reason/触顶维度不变；
- 这属于首轮调度的最小修改，因此“Loop 完全不改”不再作为非目标；预算上限和状态机不改。

### D8. QueryPlanner 采用可配置策略，Reviewer 不负责生成查询

配置：

```text
PAPER_LIT_QUERY_PLANNER=deterministic | llm | hybrid
```

- `deterministic`：当前规则，默认测试/回滚模式；
- `llm`：使用 LLMQueryPlanner，失败/非法输出退 DeterministicQueryPlanner；
- `hybrid`：先用确定性语言扩展；无进展后再调用 LLM，仍以 deterministic 为最终 fallback；
- 未知配置值降级 deterministic 并告警。

LLMQueryPlanner 默认注入 writer LLM，或专用低成本 planner LLM；**不使用 relevance reviewer**。
Reviewer 的职责是独立判断结果是否相关，让它同时制定查询再评判自己的结果会削弱角色隔离。

Planner 输出必须二次校验：source_name 必须在 ProviderRegistry allowlist 中；query 走与 seed 相同
的清洗/长度/去重；未知 provider/filter 被拒绝而非透传。Planner 不能修改 SearchIntent 的语义字段。

上线策略：代码被“接入”不等于立即全局默认 LLM。代码默认 deterministic，dogfood 显式开启
hybrid；真实命中率、成本和错误主题率达阈值后再考虑把 hybrid 作为产品默认。

### D9. 合成调用计入同一个文献预算

SearchIntent LLM 调用不能游离在 RetrievalLoop 的 `max_llm_calls` 之外。由 application 层创建
共享 BudgetLedger：

```python
budget = BudgetLedger(limits)
prepare_search_intent(..., budget=budget)  # 最多一次/受澄清轮数限制，charge_llm
run_retrieval_loop(..., budget_ledger=budget)
```

`run_retrieval_loop` 新增可选 `budget_ledger=None`：未传时沿用当前内部创建行为，保证旧调用兼容；
传入时复用同一 ledger。QueryPlanner、RelevanceJudge、search/verify 全部继续汇入它，避免双重记账。

澄清造成的重新合成也必须计费，并受 `max_search_intent_revisions` 限制。
LLM 预算在**真正发出模型调用之前**扣减：deterministic synthesizer/planner 不消耗 `max_llm_calls`；
hybrid 仅在进入 LLM 分支时扣减；失败或 schema 非法的实际调用仍计费。不得像当前 Loop 一样在不知道
planner 实现类型时预先无条件 `charge_llm()`。计费点由 LLM 实现或统一 budget-aware wrapper 承担，
并用测试保证一次真实调用只记一次。

### D10. 可观测与隐私

在 Requirement 18.2 的既有字段（provider、provider_role、filters、task_fingerprint、候选/核验/
相关性/配额进度等）基础上，A2f 新增或调整：

- semantic_fingerprint、strategy_hash、semantic_topic_length、seed_count、各 target 计数、origin；
- 每个 query_hash/query_length/target/strategy_type；
- synthesizer/planner model + prompt/schema version、调用状态、预算快照；
- 用户 accept/modify/cancel 决定，不记录用户补充原文。

不记录 semantic_topic、seed/query 原文、claims 或正文。交互 UI 可向当前用户临时展示查询词；
dogfood debug 输出必须显式开关且不得进入普通 trace/日志文件，默认关闭。
所有 digest 通过 observability 的统一 digest-only helper 计算，不在 Loop/Handler 内散落新的 sha1；
该 helper 不生成 preview。业务内部去重键与持久化 trace digest 使用不同字段，避免语义混用。

## Architecture

```text
CitationQuota Operation
  → SearchContext（显式 query + operation 语义 + 受控 project claims）
  → SearchIntentPreparationService
       explicit → deterministic READY
       derived  → LLM/deterministic synthesis → typed user confirmation
       missing  → typed clarification / NEEDS_INPUT
  → frozen SearchIntent{semantic_topic, section_purpose, targeted seeds}
       ├─ semantic fields → RelevanceCriteria → TaskFingerprint
       └─ seeds → RetrievalRequest.effective_seeds()
  → RetrievalLoop
       adaptive seed queue → gap evaluation after every attempt
       → deterministic/llm/hybrid QueryPlanner continuation
  → existing verify/relevance/batch/preflight/bundle/writer flow
```

## Components

- `literature/retrieval/search_intent.py`：值对象、Enum、规范化/验证、generic-title 规则；
- `literature/retrieval/query_synthesizer.py`：Protocol、deterministic/LLM 实现；
- `application/search_intent_preparation.py`：SearchContext、typed interaction、预算与冻结；
- `application/literature_ports.py`：search topic/intent 两类 decision；
- `application/literature_composition.py`：synthesizer 与 planner mode 装配；
- `application/literature_flow.py`：消费 frozen SearchIntent，并按 checkpoint.state 恢复，禁止重放已完成检索；
- `literature/retrieval/loop.py`：兼容 SearchIntent、adaptive seed queue、共享 BudgetLedger；
- `session/facts.py`：版本化持久化 pending_search_intents，旧 envelope 缺字段时默认为空；
- `observability/sanitize.py`：增加不含 preview 的统一 digest-only helper；
- `config.py`：planner mode、synthesis feature flag、seed/revision 上限；
- `runtime/handlers.py`：只调用 preparation service、消费结果，不直接调用 LLM。

不修改 `search_query` 旧槽类型；它继续服务无配额旧路径，并作为 A2f 的最高优先级显式信号。
无需为了多 seed 把旧槽改成 array，也不在 Handler 内另造隐藏字段。

## Correctness Properties

1. **无配额零影响**：无 CitationQuota 时不构造 SearchIntent、不调用 synthesizer。
2. **无通用标题检索**：READY intent 的 topic/seeds 不可能只是“引言/Introduction”等通用标题。
3. **语义与策略隔离**：seed/planner query 变化不改变 TaskFingerprint；topic 变化必改变；
   已确认 seed 变化会改变 strategy_hash，Loop 内 Planner query 只进入运行态 query_fingerprint 去重集合。
4. **显式 query 零额外 LLM**：有效 search_query 直接构造 intent，不调用 synthesizer。
5. **失败语义诚实**：系统 ERROR 与用户 NEEDS_INPUT 分离；两者都不回退章节标题。
6. **确认不过度**：已确认 operation 的确定性投影不重复确认；LLM 语义合成或 PROJECT/MIXED 派生
   主题在非交互路径不默认接受。
7. **预算统一**：synthesis、planner、relevance、search、verify 共用同一 BudgetLedger。
8. **多 seed 有界**：规范去重、最多 N 个；每次 attempt 后重算 gap，满足即停。
9. **Planner 不越权**：不能修改 topic/quota/semantic fingerprint/预算，未知 provider 被拒绝。
10. **隐私安全**：普通持久化 trace 不含 topic/query/claims/正文原文。
11. **精确恢复**：暂停后复用原 frozen intent 与 pending batch，不重新采样、不重复检索。

## Error Handling

| 场景 | 处理 |
|---|---|
| 显式 query 为空/仅通用章节名 | 继续检查 operation/claims；仍不足则 NEEDS_INPUT |
| Synthesizer provider/parse/schema 失败 | 有足够确定性信号则 fallback；否则 ERROR，不硬搜 |
| 用户修改 synthesized intent | 在修订上限内重建 intent；语义变化更新 semantic fingerprint，seed 变化更新 strategy hash；旧 checkpoint 不复用 |
| 用户取消/无交互 port | CANCELLED/NEEDS_INPUT，不发检索调用 |
| seed 重复或非法 | 代码侧删除；全部被删则 NEEDS_INPUT/ERROR |
| 某 target 维度已满足 | 跳过其专用 seed，不浪费 search call |
| Planner 返回未知源/空 query | 拒绝非法项，走 deterministic fallback |
| 合成/多 seed 触顶预算 | 保留已有 Batch，报告具体 exhausted dimension |

## Testing Strategy

1. explicit query → READY、零 synthesizer LLM 调用；
2. purpose/details/claims → stub LLM 生成 topic 与2–6个 seed，origin/provenance 正确；
3. 通用中英章节名、空串、重复/超长/控制字符 seed 被过滤；
4. provider ERROR 与上下文不足 NEEDS_INPUT 不混淆；
5. 确定性 confirmed-operation intent 不重复确认；LLM semantic synthesis 与 PROJECT/MIXED intent
   覆盖 accept/modify/cancel、修订上限与非交互暂停；
6. SearchIntent semantic fingerprint 不含 seeds；topic 改变则不同；strategy hash 包含规范化 seeds；
7. criteria fingerprint 不因 seed 排序、Planner 改写而变化；seed 排序改变 strategy hash；
8. zh/en target seed 只按当前非零 gap 优先，LanguageVerdict 仍是最终计数依据；
9. 首个 seed 满足 quota 后不调用剩余 seed；无进展时才续搜；
10. seed/planner 跨来源使用 query_fingerprint 去重，预算和 stop reason 正确；
11. deterministic/llm/hybrid 装配、LLM 失败 fallback、Reviewer 未被用作 planner；
12. shared BudgetLedger 不重复/遗漏计费，旧调用未传 ledger 时行为不变；
13. deterministic planner 不记 LLM 调用，hybrid/LLM 每次真实调用（含失败）恰好记一次；
14. pending intent 序列化/迁移/清理；恢复时 hash 不一致返回 STALE_SEARCH_INTENT 且不调用 LLM；
15. NEEDS_BATCH_CONFIRMATION 恢复复用原 batch_id，search provider 调用次数不增加；
16. trace 保留 Requirement 18.2 字段且只含 hash/长度/计数/version，debug query 输出默认关闭；
17. 无配额 rewrite/add_section prompt、检索调用和输出逐字节不变；
18. 带配额端到端 stub：SearchIntent → 多 seed Loop → strict verify → Bundle → CitationQuotaGate；
19. 全套测试以实施开始时实际收集值为基线，不在设计中硬编码“864”。

## 实施顺序

1. **A2f-1：契约与 Requirements 同步**
   - SearchIntent/SearchSeed/SearchContext、验证器、generic title 集；补 Requirements 口径。
2. **A2f-2：Synthesizer 与交互准备**
   - deterministic/LLM、typed port、确认/修改/澄清、pending intent 持久化与统一预算计费。
3. **A2f-3：语义主题接入**
   - RelevanceCriteria 只消费 semantic fields；flow/checkpoint 分别使用 semantic fingerprint 与 strategy hash。
4. **A2f-4：Adaptive multi-seed Loop**
   - RetrievalRequest 兼容迁移、UUID batch、state-aware resume、seed queue、逐 attempt gap、共享 BudgetLedger。
5. **A2f-5：Planner 装配与验收**
   - deterministic/llm/hybrid、组合层配置、trace、dogfood 与全套回归。

## 已定决策

- 保留旧 `search_query: str`，不改为 array；多 seed 使用独立 SearchIntent；
- Synthesizer 不放 Handler，放 application 准备服务；Handler 只消费结果；
- 主题来源优先级为：显式 query > 当前 operation 语义 > 已确认 project claims > 澄清；
- 现有章节标题列表不作为主题来源；有信息量的目标标题仅作 operation 信号；
- semantic topic 进入 TaskFingerprint，seed/planner queries 不进入；
- QueryPlanner 不使用 relevance reviewer；默认 deterministic，dogfood 开 hybrid，达标后再评估默认值；
- 命名沿用 A2f，归属 `literature-evidence` 查询质量加固；
- A2f 不接中文源，也不声称仅靠 query 优化即可保证中文配额。
