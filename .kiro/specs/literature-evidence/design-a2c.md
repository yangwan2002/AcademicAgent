# Design Document — A2c：配额驱动的有界检索 Loop + 真实核验源 + Discovery 契约

> A2c 是第一个访问真实网络、也是 A2 中体量最大的阶段。它把 A1 的 ReferenceLedger、
> A2a 的语言/相关性和 A2b 的 RetrievalQuota 串成一条可审计、有预算、可暂存、可原子提交
> 的检索链路。A2c 不接入写作链路；rewrite/add_section 的切换留给 A2d。

## Overview

```text
RetrievalQuota + RelevanceCriteria + 初始查询
  └─ RetrievalLoop
       └─ RetrievalAttempt
            SEARCH → 清洗 → 去重 → 资格预筛 → 严格核验 → 相关性判定
            → Staged BatchItem
       └─ A2b 统一评估 quota gaps
       └─ 未满足且预算允许：调整查询/切换源/继续
       └─ 权威源穷尽后，可选 Discovery 契约兜底
  └─ LoopResult(batch=STAGED, gaps, stop_reason, diagnostics)
  └─ A2d 呈现并取得用户确认
  └─ CONFIRMED batch 原子提交 Ledger
```

A2c 交付：

- Crossref DOI 精确核验与书目候选查询；
- 能力感知的 ProviderRegistry 和严格 VerificationPolicy；
- 新增严格核验组件，但不替换既有写作路径的 `search_and_verify/ReferenceVerifier`；
- 不可变 CandidateSnapshot、RetrievalBatch 状态机和原子提交；
- RetrievalAttempt、配额驱动 RetrievalLoop、完整预算和停止原因；
- DiscoveryProvider 接口、IdentifierResolution 和隔离规则，不接真实 Web 后端；
- staged batch 与 RelevanceStore 持久化；
- trace、离线评测骨架和 env-gated 真实源冒烟。

A2c 不做：

- 不激活 rewrite/add_section 的 quota 槽；
- 不执行 LiteraturePreflight、CitationQuotaGate、EvidenceBundle 或 Writer Loop；
- 不在本阶段把旧 `search_and_verify` 切成严格核验；
- 不实现真实 Web 搜索；
- 不用单元测试宣称真实 Reviewer 已达到语义准确率。

`CitationQuotaGate` 属 Spec A/A2d 写作编排边界；Spec B EvidenceGate 只判断引用是否支撑
Claim，不承担配额计算。

## Requirements 同步前置

本设计以现有 `requirements.md` 为上位约束，但实现前必须先消除一处已有冲突：

- Requirement 11.13 仍把“权威源模糊反查无匹配”定义为 `not_found/疑似不存在`；本设计 D3、
  D13 将其定义为 `UNRESOLVED_NO_MATCH`。后者更准确，因为模糊查询无结果不能证明作品不存在。
  **冻结 A2c-5 Tasks 前必须同步修改 Requirement 11.13、11.16 的对应统计项与 Property 31**，否则实现
  无法同时满足 Requirements 和 Design。

Glossary 中“只有入 Ledger 后才成为 `ReferenceRecord`”继续成立。暂存 Batch 持久化的是
`CandidateSnapshot` 与 `ProposedRecordSnapshot` 工作流快照，不是第二套可写文献实体；真正的
`ReferenceRecord` 只在 commit 时构造并写入 Ledger。

## 分期决策

A2c 必须拆成五期，每期独立出 Tasks、独立回归。原方案中 Attempt 先于 Batch，但 Attempt
的输出类型正是 BatchItem，会形成反向依赖，因此顺序调整为：

1. **A2c-1 核验地基**：Crossref、provider 能力协议、CrossVerifier、Strict VerificationPolicy、
   兼容事件严格投影；
2. **A2c-2 Registry + Batch 地基**：ProviderRegistry、CandidateSnapshot、BatchItem、状态机、
   原子提交骨架；
3. **A2c-3 Attempt 流水线**：清洗、两阶段去重、过滤、相关性和 disposition；
4. **A2c-4 RetrievalLoop**：QueryPlanner、预算、无进展检测、QuotaGap、trace、取消；
5. **A2c-5 Discovery 契约 + 持久化**：IdentifierResolution、UnresolvedLead、staged batch 和
   RelevanceStore 持久化、受控实测入口。

任何一期失败都不影响既有 rewrite/add_section 的 `search_and_verify`。

## 关键设计决策

### D1. Provider 按能力拆协议，不强迫所有源实现同一接口

Crossref 主要是核验/书目反查源，不应为了满足一个过宽的 RetrievalProvider Protocol 而伪造
主题检索能力。定义能力协议：

```python
class SearchProvider(Protocol):
    def search(query: str, *, limit: int, filters=()) -> list[RetrievalCandidate]: ...

class VerifyProvider(Protocol):
    def fetch_metadata(identifier_type: str, identifier: str) -> FetchResult: ...
    def search_bibliographic(query: BibliographicQuery, *, limit: int) -> list[RetrievalCandidate]: ...

class DiscoveryProvider(Protocol):
    def discover(query: str, *, limit: int) -> list[DiscoveryLead]: ...
```

`FetchResult` 明确区分 `FOUND | NOT_FOUND | PROVIDER_ERROR`；网络错误不得伪装成空结果。

```python
@dataclass(frozen=True)
class NamedProvider:
    name: str
    provider: object
    profile: SourceProfile

class ProviderRegistry:
    search_sources: tuple[NamedProvider, ...]
    verify_sources: tuple[NamedProvider, ...]
    discovery_sources: tuple[NamedProvider, ...] = ()
```

Registry 构造时校验声明角色与实际能力一致。未知源没有默认 VERIFY 权限。

### D2. `require_final` 只对新 Loop 生效，不切全局兼容路径

接受“只在 A2c 新 Loop 打开严格核验”的方案：

- 既有 rewrite/add_section 继续调用 `search_and_verify(..., require_final=False)`；
- 新 Loop 显式注入 `VerificationPolicy.STRICT_FINAL_V1`；
- A2d 完成写作链路迁移后，再统一切换旧路径或删除旧 SOP；
- `config.literature_require_final_verification` 的全局默认在 A2c 不改成 True。

不能只依赖 `ReferenceRecord.verification_status` 判断严格可用性。A1 兼容路径曾把 OpenAlex 对
DOI 的成功回查写成 `authority=final`，该 authority 已固化在历史事件里；后来切配置不会自动
让旧记录降级。

A2c-1 增加：

```python
class VerificationPolicy(str, Enum):
    COMPAT_A1 = "compat_a1"
    STRICT_FINAL_V1 = "strict_final_v1"

def status_under_policy(events, policy, authority_registry) -> VerificationStatus
```

STRICT 策略按事件的 `source + identifier_type` 重新核对当前
`verification_authority()`：OpenAlex/S2 的 DOI 事件即使历史上写了 final，在 Strict View
中也只算 corroboration；Crossref DOI、arXiv 原生 ID 等才能算 final。

`authority_registry` 必须有不可变版本号；Batch 与 trace 同时记录该版本。严格投影只能使用
与 `STRICT_FINAL_V1` 绑定的版本化权限表，不能读取随后可能变化的“当前配置”，否则相同 Batch
在不同时间会得到不同核验状态。

`VerificationEvent` 继续只记录“发生了什么”，不把 `policy_version` 写进原始事件；否则同一
provider observation 会同时承担事实与解释策略，且策略升级容易制造第二真相源。策略版本写在
`VerificationAssessment`、`RetrievalBatch.verification_policy` 和 trace 中。旧事件不改写，
由 `status_under_policy()` 在读取时形成 compat/strict 两种投影；若将来需要迁移事件 JSON，
新增的是纯序列化用途的 `event_schema_version`，不得用它决定 authority。

### D3. Crossref 是 DOI final 源，但“Crossref 没找到”不等于论文不存在

实现 `CrossrefVerifyProvider`：

- DOI 精确回查；
- 书目文本查询，返回候选供 IdentifierResolution 比对；
- 支持 polite pool 的 `mailto`/User-Agent、超时、退避和速率限制；
- HTTP 404 只表示该 DOI 未在 Crossref 命中；连接/超时/限流是 PROVIDER_ERROR。

Crossref 不是所有 DOI 的唯一注册库，DataCite DOI 可能不在 Crossref。因此：

- exact DOI 在 Crossref 未命中 → `NOT_FOUND_AT_SOURCE`，候选保持 UNVERIFIED；
- 不向用户宣称“论文不存在”；
- 未来可插入 DataCite，无需修改 CrossVerifier；
- 模糊书目查询无高相似结果只表示 `UNRESOLVED_NO_MATCH`，绝不产生“疑似不存在”。

### D4. CrossVerifier 有总预算、有缓存，不无界遍历标识符

```python
class CrossVerifier:
    def assess(candidate, *, policy, budget, cache) -> VerificationAssessment: ...
```

- 对每个规范化 work identifier 选择具备对应 final/corroboration 权限的 VerifyProvider；
- DOI→Crossref，arXiv→arXiv，OpenAlex ID→OpenAlex，S2 ID→S2；没有 final 源则只记 corroboration；
- 所有 verify 调用计入统一 `max_verify_calls/max_provider_calls/provider_time_budget`；
- `(provider, identifier_type, normalized_id)` 级缓存跨 Attempt 复用；错误结果使用短 TTL，成功
  结果可在本次 Loop 固定复用；
- 任一 final CONFLICT 阻塞候选；canonical identifier 在 final 源 NOT_FOUND 时不可接受；
- 次要 identifier NOT_FOUND 不静默删除，记 issue，候选默认不计配额，等待后续消解；
- 至少一个 Strict final VERIFIED、无开放 final conflict，才满足严格核验门槛。

### D5. BatchItem 保存不可变快照和拟提交命令，不提前创建持久化 ReferenceRecord

`ReferenceEntry/RetrievalCandidate` 含可变 dict/list，不能直接作为“冻结快照”放入 Batch。

```python
@dataclass(frozen=True)
class CandidateSnapshot:
    title: str
    authors: tuple[str, ...]
    year: int | None
    abstract: str
    work_identifiers: tuple[tuple[str, str], ...]
    container_identifiers: tuple[tuple[str, str], ...]
    source: str
    content_hash: str

class BatchDisposition(str, Enum):
    ACCEPT_NEW = "accept_new"
    REUSE_EXISTING = "reuse_existing"
    MERGE_DUPLICATE = "merge_duplicate"
    REJECT = "reject"

@dataclass(frozen=True)
class ProposedRecordSnapshot:
    target_reference_id: str
    expected_base_hash: str | None   # ACCEPT_NEW 为 None；MERGE 用于乐观并发校验
    canonical_record_payload: str   # 规范 JSON；commit 时校验并构造 ReferenceRecord

@dataclass(frozen=True)
class BatchItem:
    item_id: str
    candidate_snapshot: CandidateSnapshot
    proposed_record_snapshot: ProposedRecordSnapshot | None
    disposition: BatchDisposition
    target_reference_id: str | None
    language_verdict: LanguageVerdict | None
    relevance: RelevanceAssessment | None
    verification_events: tuple[VerificationEvent, ...]
    rejection_reasons: tuple[str, ...] = ()
```

- ACCEPT_NEW 在 staged 阶段已经预留稳定 `target_reference_id`，但尚未成为 Ledger entity；
- ACCEPT_NEW/MERGE_DUPLICATE 必须有 `proposed_record_snapshot`；REUSE_EXISTING/REJECT 必须没有；
- ACCEPT_NEW/REUSE_EXISTING/MERGE_DUPLICATE 的 `target_reference_id` 必须非空，REJECT 可为空；
- MERGE_DUPLICATE 保存“合并后的拟提交快照”及 `expected_base_hash`，配额预览与 commit 使用
  同一快照；若确认期间 Ledger 目标已变化，commit 返回 revision conflict，禁止覆盖新数据；
- strict verification status 从 snapshot/events + policy 派生，不单独持久化
  `verification_status`，避免双真相源；
- 一个候选可能有多个拒绝原因，不能压成单一字符串。

### D6. RetrievalBatch 使用显式、幂等状态机

```text
RUNNING → STAGED → CONFIRMED → COMMITTED
                └→ REJECTED
```

非法跳转必须拒绝；`commit(COMMITTED)` 幂等返回既有结果；RUNNING/STAGED 不允许 commit。
RUNNING 只存在于本次 Loop 内存中；取消、预算触顶、无进展等正常停止都把已有成果收束为
STAGED，并通过 `stop_reason` 表达原因。若在形成合法 STAGED 快照前发生不可恢复错误，则返回
显式失败且不持久化 Batch，不再额外引入与 Requirements 不一致的 ABORTED 状态。

```python
@dataclass(frozen=True)
class RetrievalBatch:
    batch_id: str
    status: BatchStatus
    task_fingerprint: str
    quota: QuotaSpec
    verification_policy: VerificationPolicy
    authority_registry_version: str
    items: tuple[BatchItem, ...]
    created_at: str
    revision: int
```

items 是唯一事实源；accepted/reused/merge/reject/count/ledger_delta 均为派生视图。

### D7. 原子提交覆盖 Ledger、RelevanceStore、Batch 状态和磁盘

仅在内存 Ledger 副本试算后交换引用不够：若随后 facts 原子写失败，会出现内存已提交、磁盘
未提交。`commit_batch` 必须作为 project facts 事务：

1. 检查 batch=CONFIRMED、revision、`expected_base_hash` 和 batch_id 幂等键；
2. 复制完整 `ProjectFactsEnvelope`（Ledger、RelevanceStore、pending batches、幂等记录）；
3. 在副本中应用 ACCEPT_NEW/MERGE，并在**同一副本**把 batch 置 COMMITTED、写入
   committed_batch_id；
4. 序列化这个完整新 envelope，通过 A1 的 tmp + fsync + os.replace 一次原子落盘；
5. 磁盘成功后才一次性替换内存 envelope；
6. 任一步失败，内存和磁盘的 Ledger、RelevanceStore 与 Batch 状态均保持提交前状态。

这里的原子性以“一个物理 ProjectFactsEnvelope 文件”为前提。若实现选择把 pending batch、
RelevanceStore 和 Ledger 拆成多个物理文件，就必须增加 write-ahead journal + crash recovery；
不能用多个独立 `os.replace` 宣称跨文件原子事务。A2c 默认选择单 envelope，控制实现复杂度。

已 COMMITTED 的 batch 重试不得二次增加记录。REJECTED batch 不修改 Ledger。

### D8. staged Batch 必须持久化，但不能提前进入项目 Ledger

A2d 的用户确认可能跨消息甚至跨进程，纯内存 Batch 不足以承载。A2c-5 在
`ProjectFactsEnvelope` 内增加逻辑独立的 pending batch 区：

- staged/confirmed batch 存在 session/project 的 pending 区，不写 ReferenceLedger；
- 保存 CandidateSnapshot、ProposedRecordSnapshot、评估和 quota，但不把候选暴露为
  ReferenceRecord 或可引用文献；
- 按 batch_id/revision 乐观并发控制；
- committed/rejected 后按保留策略归档或清理；
- 加载损坏 batch 时隔离并报告，不修改 Ledger。

### D9. Attempt 顺序以“先便宜、后昂贵”为原则，并保持保守

```text
SEARCH
→ 清洗/规范化 CandidateSnapshot
→ 批内 + Ledger 精确标识符去重
→ 可确定的 YEAR/reference_type/language 预筛
→ Strict CrossVerifier
→ 规范元数据后的保守模糊聚类/合并建议
→ 当前 task fingerprint 的 RelevanceJudge/Store
→ 生成 proposed_record_snapshot、disposition、rejection reasons
```

- 核验前只允许基于规范化 work identifier 或完全相同稳定键的确定性去重，避免重复核验；
- 标题/作者/年份相似度只能在核验与元数据规范化后形成“合并建议”，不得在昂贵调用前
  不可逆删除候选；低置信匹配保持独立 BatchItem；
- 硬过滤预筛只用于明确缺失/不满足的候选，不能依据不可信字段作不可逆删除；被预筛拒绝的
  候选仍保留 BatchItem 和原因；
- 只有 strict verified 后才调用相关性 Reviewer，避免为虚假候选浪费 LLM；
- title_only 的弱相关是否接受由明确 RelevanceAcceptancePolicy 决定，不能在 Attempt 内猜；
- REUSE/MERGE 在当前 TaskFingerprint 下仍需相关性结论，不能复用其他任务结论。

### D10. Loop 使用独立 QueryPlanner 接口，默认 writer

接受“组合层注入、默认 writer”的方案。查询改写是生成/规划任务，不是证据审查；默认不占用
独立 relevance reviewer，避免 Reviewer 同时制定检索策略又评判自己的结果。

```python
class QueryPlanner(Protocol):
    def propose(request, gaps, history, diagnostics) -> SearchPlan: ...

@dataclass(frozen=True)
class SearchPlan:
    queries: tuple[str, ...]
    source_names: tuple[str, ...]
    provider_filters: tuple[tuple[str, object], ...]
    strategy_reason: str
```

- 默认由组合层复用 writer 的模型客户端、模型配置与 structured-call 基础设施来构造
  `LLMQueryPlanner`，**不复用 writer 的写作 prompt、对话历史或可变上下文**；可配置独立低成本 planner；
- Planner 走 structured_call，输出 Schema 固定；
- Planner 只能改变查询、源顺序和 provider 调用参数，不能修改 QuotaSpec、TaskFingerprint、
  RelevanceCriteria 或预算；
- Planner 失败时使用确定性 fallback，不把失败解释为“已无文献”；
- query planner 的 model/prompt version 写入 trace。

### D11. Loop 预算覆盖所有昂贵资源，并支持取消/无进展停止

```python
@dataclass(frozen=True)
class RetrievalLimits:
    max_iterations: int
    max_provider_calls: int
    max_search_calls: int
    max_verify_calls: int
    max_discovery_calls: int
    max_llm_calls: int
    max_candidates: int
    provider_time_budget_s: float
    max_no_progress_rounds: int
```

```python
class StopReason(str, Enum):
    QUOTA_SATISFIED = "quota_satisfied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_PROGRESS = "no_progress"
    SOURCE_EXHAUSTED = "source_exhausted"              # 至少有源成功响应，但策略已穷尽
    ALL_SOURCES_UNAVAILABLE = "all_sources_unavailable" # 所有尝试均为 provider error
    CANCELLED = "cancelled"
```

- 所有 provider 和 LLM 调用通过同一 BudgetLedger 记账；
- 接受注入的 monotonic clock 和 CancelToken，在每次外部调用前后检查；
- 相同 query/source/filter 的 strategy fingerprint 不重复执行；
- no-progress 比较规范化进度签名（`counted_reference_ids + QuotaGap 向量 + strict 状态升级`）；
  连续 N 轮既无新增可计数 ID、任一 gap 也未缩小且无记录升级为 strict verified，才停止；
- 所有 SearchProvider 已尝试且无可用策略 → `source_exhausted`；
- 任一预算触顶立即停止并保留成果；
- `BUDGET_EXHAUSTED` 必须在 diagnostics 中附具体触顶维度，不能只给笼统字符串；
- RetrievalQuota 必须含至少一个需要提升数量的下界目标；只有 MAX 的 quota 初始即满足，
  不启动无意义 Loop。

### D12. 配额进度只使用拟提交后的去重记录

每轮由 ProposedRecordSnapshot 构造只读 `countable_record_views`：

- ACCEPT_NEW → proposed_record_snapshot；
- REUSE_EXISTING → Ledger 现有记录；
- MERGE_DUPLICATE → proposed_record_snapshot（合并后的拟提交记录视图）；
- REJECT → 不进入；
- 按 target_reference_id 去重；
- 使用 Strict VerificationPolicy、当前 RelevanceAcceptancePolicy 和 A2b filters；
- 再调用 A2b `count_by_dimension/evaluate_constraints`。

这样 MERGE 后补齐 Crossref final 事件的记录可以在 ledger_total scope 中正确预览，但在
new_only scope 下仍不算新增。

### D13. Discovery 只做协议、隔离和 Stub，不接真实 Web 后端

接受“接口+占位、不接真实后端”的方案：

- 默认 `ProviderRegistry.discovery_sources=()`；
- 提供 DiscoveryProvider Protocol 和测试 Stub；
- 不提供一个永远返回空列表却伪装成功的生产 Provider；
- 若配置 `discovery_enabled=true` 但没有 provider，启动/运行时返回明确配置警告；
- 只有权威 SEARCH 源已尝试、仍有 gap、Discovery 显式启用且有 provider 时才调用。

IdentifierResolution：

1. DiscoveryLead 经清洗后形成 BibliographicQuery；
2. 对所有支持书目反查的 VerifyProvider 搜候选；
3. 标题、作者、年份达到阈值才取得 work identifier；
4. 再走 Strict CrossVerifier；
5. 未匹配 → UnresolvedLead，不入可引用集合、不计配额；
6. 模糊搜索没匹配不能产生 NOT_FOUND 或“疑似不存在”；只有取得精确 identifier 后的 exact
   fetch 明确未命中，才能记录 `NOT_FOUND_AT_SOURCE`，仍不得上升为全球不存在。

Discovery URL 作为不可信数据存储和展示，不能作为指令或自动访问目标。

### D14. RelevanceStore 持久化采用确定性同键合并

当前 Store 是 cache，同 key 的 `put()` 会覆盖。持久化合并规则明确为：

- 不同 cache key 全部保留；
- 同 key：SUCCESS 优先于 ERROR；状态相同则 `assessed_at` 较新者胜；仍相同取稳定内容哈希；
- 不因加载旧文件覆盖内存中不同 key；
- content_hash/judge_key 变化自然形成新 key，不复用旧结果。

语义审计历史由 trace 保留，RelevanceStore 只承担当前 cache，不把它伪装成 append-only 审计库。

### D15. Trace 不记录原始查询和候选原文

每 Attempt 记录：

- query_hash/query_length/strategy_type；
- provider name/role、候选数、核验调用数、相关性结果数；
- disposition 计数、quota progress、预算快照、耗时、stop reason；
- 不记录 query、title、abstract、DiscoveryLead 原文；
- trace 关闭时不改变业务行为。

### D16. 真实相关性评测另出报告，但它是 A2d Active 前置门槛

接受“真实评测不在单测宣称达成”：

- 单元测试只验证协议、错误隔离、缓存、预算与编排不变量；
- A2c 提供受控样本导出、评分脚本和报告模板；
- 真实模型使用人工标注集测错误接纳率、错误拒绝率、title_only 误接纳率，并按中/英文分层；
- 数据集版本、抽样方法、标注规范、分歧处理、指标阈值与置信区间必须在跑最终评测前冻结，
  报告同时给出相对上一版本的回归结果，避免事后移动达标线；
- 报告可作为独立交付，不阻塞 A2c 代码合并；
- **未达到文档化阈值前，不允许 A2d 把 relevance 判定作为无人监督的 Active 配额门槛**。

## Architecture

```text
RetrievalRequest{quota, criteria, initial_query}
  │
  ├─ ProviderRegistry{search, verify, discovery=[]}
  ├─ QueryPlanner（组合层注入，默认 writer）
  ├─ Strict VerificationPolicy
  ├─ BudgetLedger + Clock + CancelToken
  ▼
RetrievalLoop
  repeat:
    SearchPlan → SearchProvider
      → CandidateSnapshot
      → dedup/pre-filter
      → CrossVerifier(strict, cache, budget)
      → RelevanceJudge/Store
      → BatchItem(proposed_record_snapshot, disposition)
    count proposed record views → A2b quota gaps
    quota_satisfied / budget_exhausted / no_progress /
    source_exhausted / all_sources_unavailable / cancelled
  ▼
LoopResult{STAGED batch, gaps, diagnostics, stop_reason}
  │
  ├─ pending batch persistence（未入 Ledger）
  └─ A2d 用户确认 → CONFIRMED
       └─ project-facts transaction
            Ledger + RelevanceStore + committed_batch_id + COMMITTED status
```

## Components

### A2c-1

- `providers/retrieval/crossref.py`：Crossref VerifyProvider；
- `providers/retrieval/capabilities.py`：Search/Verify/Discovery Protocol 与 FetchResult；
- `literature/verification.py`：版本化 authority registry、`status_under_policy`；
- `literature/retrieval/cross_verify.py`：CrossVerifier、VerificationCache。

### A2c-2

- `literature/retrieval/registry.py`：ProviderRegistry；
- `literature/retrieval/batch.py`：CandidateSnapshot、BatchItem、RetrievalBatch 状态机；
- `literature/retrieval/transaction.py`：纯内存 CommitPlan 试算、revision/幂等校验骨架；
  本期不宣称跨磁盘原子性。

### A2c-3

- `literature/retrieval/attempt.py`：Attempt pipeline 与 disposition；
- RelevanceAcceptancePolicy；
- candidate/ProposedRecordSnapshot 转换。

### A2c-4

- `literature/retrieval/query_planner.py`：QueryPlanner、LLM/Deterministic 实现；
- `literature/retrieval/budget.py`：RetrievalLimits、BudgetLedger；
- `literature/retrieval/loop.py`：RetrievalLoop、LoopResult、停止原因；
- Attempt trace。

### A2c-5

- `literature/retrieval/discovery.py`：协议、Stub、IdentifierResolution、UnresolvedLead；
- pending batch persistence；
- RelevanceStore、pending batch 与 project facts 的单 envelope 确定性合并、原子写；
- 受控评测导出与报告模板。

## Correctness Properties

1. **严格/兼容隔离**：A2c 新 Loop 只用 STRICT_FINAL_V1；旧 search_and_verify 行为不变。
2. **历史事件不虚高**：旧 compat final 事件在 Strict View 中按 source/id 权限重新解释。
3. **Loop 有界**：所有昂贵调用和候选数量均受预算约束，取消与无进展可终止。
4. **配额诚实**：只有 strict verified、当前任务相关、通过 filters、去重后的拟提交记录推进配额。
5. **Batch 单一事实源**：所有计数由 items/proposed record snapshots 派生，不保存独立计数副本。
6. **状态机合法**：非法 Batch 转移失败；commit 幂等。
7. **事务原子**：磁盘失败时内存与磁盘 Ledger 均不变；重复提交不重复新增。
8. **未确认隔离**：staged candidates 可持久化，但不进入 Ledger/可引用集合。
9. **Discovery 不授信**：UnresolvedLead 与模糊无匹配均不计配额，不伪装 NOT_FOUND。
10. **隐私边界**：trace 不含查询、标题、摘要、Discovery 原文。
11. **相关性缓存正确**：同键确定性合并；内容/模型/prompt 变化自动失效。
12. **旧写作路径零影响**：A2d 切换前 rewrite/add_section 产物与现状一致。

## Error Handling

| 场景 | 处理 |
|---|---|
| 单个 SEARCH/VERIFY 源失败 | 记 provider_error、预算与 trace，继续其他可用源 |
| 所有权威源不可用 | `source_exhausted` 或 `all_sources_unavailable`，保留 staged 成果 |
| Crossref 未命中 DOI | NOT_FOUND_AT_SOURCE，保持未核验，不宣称论文不存在 |
| 模糊书目查询无匹配 | UnresolvedLead，不产生 NOT_FOUND |
| Reviewer ERROR | 不计配额；可在预算内重试，不映射 unrelated |
| QueryPlanner ERROR | 使用确定性 fallback；不改 quota/criteria |
| 连续无进展 | `stop_reason=no_progress` |
| CancelToken 触发 | `stop_reason=cancelled`，把已有成果收束为可审计 STAGED 状态 |
| discovery 开启但无 provider | 明确配置警告，不假装完成 Discovery |
| pending batch 子对象损坏 | 从可操作 pending 视图隔离该逻辑条目并报告；Ledger 不变；整个 envelope JSON 损坏则走 A1 备份恢复 |
| commit 落盘失败 | 内存/磁盘 Ledger 均回到提交前；batch 保持 CONFIRMED 可重试 |

## Testing Strategy

### 离线单元/属性/集成测试

1. Strict View 忽略旧 OpenAlex DOI compat-final；Crossref strict-final 后变 VERIFIED；旧路径不变。
2. CrossVerifier 的 final/corroboration/not-found/provider-error/cache/budget 分支。
3. CandidateSnapshot/ProposedRecordSnapshot 深不可变，外部修改原 Candidate 不影响 Batch。
4. Batch 全部合法/非法状态转移和重复 commit 幂等。
5. 单 envelope 事务在序列化、临时写、replace 等故障点失败时，内存/磁盘的 Ledger、
   RelevanceStore 与 Batch 状态均不变。
6. Attempt 的 identifier 精确去重不重复核验/评审；模糊低置信候选不被提前删除；
   disposition/rejection reasons 正确。
7. MERGE 使用拟合并记录预览配额；new_only/ledger_total 口径正确。
8. 每个预算单独触顶、CancelToken、no_progress、source_exhausted 的 stop reason。
9. QueryPlanner 输出不能修改 quota/criteria/fingerprint；失败走 fallback；重复策略不执行。
10. Discovery 无 provider 警告；Stub 成功/无匹配/精确 not-found；UnresolvedLead 永不计数。
11. pending batch 重载、revision 冲突、损坏隔离、未确认不进 Ledger。
12. RelevanceStore 同键合并、SUCCESS 优先 ERROR、新版本失效。
13. trace 只含 hash/计数/预算，不含原始查询和候选文本。
14. 默认测试不发网络请求；env-gated Crossref/OpenAlex 冒烟默认 skip。
15. 全套既有测试逐项通过，尤其 rewrite/add_section 旧 search_and_verify 行为不变。

### 真实语义评测（独立报告）

- 人工标注相关/弱相关/无关样本；
- 报告错误接纳率、错误拒绝率、title_only 误接纳率、中/英文分层和成本/延迟；
- 单元测试不以 Stub 结果冒充这些指标；
- 报告未达阈值时，A2d 不开启无人监督 Active relevance gate。

## 五期交付门槛

### A2c-1 核验地基

- Crossref/capability protocols/Strict Policy/CrossVerifier 完成；
- Strict/Compat 双路径回归通过；
- 不改变全局默认和旧写作路径。

### A2c-2 Registry + Batch

- Registry、不可变快照、Batch 状态机、纯内存 CommitPlan 与幂等校验完成；
- 尚不运行完整 Attempt。

### A2c-3 Attempt

- Stub provider/reviewer 下完整单轮流水线通过；
- strict verification、相关性、filter、disposition 和拟提交记录正确。

### A2c-4 Loop

- 所有预算、停止原因、QueryPlanner、QuotaGap 和 trace 完成；
- 无网络默认测试全绿。

### A2c-5 Discovery + 持久化

- 只交付 Discovery 协议/Stub/隔离和 IdentifierResolution；
- staged batch 与 relevance cache 可恢复；单 envelope commit 通过磁盘故障注入；
- 输出真实评测工具与独立报告入口。

## 已确认决策

- A2c 分五期实现，但 Batch 地基先于 Attempt。
- Strict `require_final` 只用于新 Loop；旧 rewrite/add_section 保持兼容，A2d 再切换。
- Web Discovery 只做接口、测试 Stub 和隔离规则，不接真实后端，也不提供伪成功占位实现。
- 查询改写由组合层注入 QueryPlanner，默认使用 writer；独立 reviewer 专注相关性评审。
- 真实相关性评测作为独立实测报告；不由单元测试宣称达标，但它是 A2d Active 前置门槛。
