# Requirements Document

> **Spec：Retrieval Throughput（文献检索吞吐优化）**
> 目标是把 `literature` 检索链路的端到端时延从当前 20–40 分钟降低到可稳定验收的分钟级，
> 第一阶段目标为典型 20 篇任务的中位时延不超过 10 分钟且相对基线降低至少 60%，而**不**改变检索结论的
> 正确性口径（严格核验、引用配额、相关性缓存语义）。本 spec 只优化**吞吐模型**
> （批量推理、管线重排、并发），不改配额算法、不改核验权威规则、不改状态机。

## Introduction

现状：检索 20 篇参考文献（如 en15/zh5）端到端要 20–40 分钟，而用户用通用聊天网页版
约 10 分钟即可完成且质量更好。经代码分析，瓶颈是**架构层面的吞吐模型**，不是预算配置：

1. **每候选一次独立 LLM 相关性调用**：`RelevanceJudge.assess` 对每篇候选发一次结构化
   LLM 调用（约 3–8 秒），100 篇候选即 100 次串行 LLM 往返。
2. **管线顺序低效**：`run_attempt` 当前是「先严格交叉核验（多次网络往返）、后判相关性」，
   对最终会被判不相关而丢弃的候选也付出了昂贵的核验代价。
3. **全程串行**：候选之间、单候选内多个标识符的核验之间都串行，无并发。

本 spec 从收益/风险比高到低解决三件事，并要求分阶段独立验收，不能一次性同时打开：

- **B1 批量相关性判定**：在候选数与输入 token 双重上限内，把多篇候选放进一次 LLM
  调用判定；第一遍调用数等于实际窗口数。收益最大。
- **B2 管线重排**：便宜的确定性过滤/去重 → 批量判相关 → **只对通过相关性的幸存者**做
  严格核验，把核验网络调用砍 3–5 倍。
- **B3 有界并行化**：核验/相关性 I/O 在候选或窗口之间并发；并发度受全局与 provider
  双重上限约束，结果在单线程确定性归并。并发只作为 B1/B2 稳定后的增量优化，不承诺近线性加速。

**硬约束（贯穿全 spec）**：不降低判准、旧路径零回归、架构红线（`literature` 不依赖上层与全局）、
token/调用/时间预算兼容、可中断、正确性保持、失败不伪装、provider 友好。这些约束的优先级高于吞吐目标——任何吞吐优化
不得以牺牲正确性或架构纪律为代价。

**范围边界（不做）**：不改配额维度/语言分类/去重算法/核验权威规则（`AuthorityRegistry`）、
不改批次状态机与落盘协议、不引入跨进程/分布式执行、不做全文 PDF 解析、不改 `RelevanceStore`
的缓存键 **schema/字段构成**（键仍为
`(reference_id, task_fingerprint, reference_content_hash, judge_key)`）、不改面向用户的检索结论口径。
**例外（明确在做）**：Requirement 5.6 会修正 reuse/merge 下 `reference_content_hash` 的**计算基准**
（从"候选文本"改为 Relevance_Subject 文本），使写入端（attempt）与读取端（preflight/build_bundle）
一致——这是修一个既有 seam 缺陷，而非改动键的字段构成；Requirement 5.7 会为批量路径引入**新的
`judge_key` 取值**（batch prompt/schema 版本），这是**使用**既有键 schema，不改其结构。

**分层与门槛（Tiering）**：本 spec 的验收分为两级，避免评测/发布基建阻塞代码本身：

- **必须有（gate for merging code + 有监督启用）**：三个独立 feature flag（B1/B2/B3）与手动一键回滚、
  原子 Budget_Reservation、Single_Flight、单线程 reducer、以及一个**小而冻结**的判准评测集
  （初始 ≥ 40 条、覆盖中英 × relevant/weakly/unrelated × title_only 等主要切面）用于验证批量不降判准；
  性能以 dogfood 配对样本 + 人工抽检为准。
- **可延后（gate for 无监督默认全开）**：≥ 200 条人工标注评测集、30 次 replay 的 P50/P95 报告、
  完整 canary 比例灰度与滚动错误率自动关闭。这些是"默认对所有用户开启批量/并发"的门槛，
  不是"合入代码 / 在受控 dogfood 下启用"的门槛。

除非另有说明，下文 Requirement 2 / 11 / 14 中标注"（Tier-2）"的条款属于"可延后"级，
其余属于"必须有"级。

## Glossary

- **Retrieval_Pipeline**：单轮 attempt 流水线，即 `literature/retrieval/attempt.py` 的
  `run_attempt`；对一批候选做去重/过滤/核验/相关性并产出 `BatchItem`。
- **Batch_Relevance_Judge**：批量相关性判定器（B1 新增能力）。在**一次** LLM 调用内对
  一个候选窗口（≤ M 篇）判定相关性，产出与逐篇 `RelevanceJudge` **同型**的
  `RelevanceAssessment`。
- **Relevance_Window**：一次批量判定送入模型的候选集合，大小 ≤ Batch_Size。
- **Batch_Size (M)**：单次批量判定的最大候选数，注入配置；默认值为 1，等价于逐篇。
- **Batch_Input_Budget**：单个 Relevance_Window 可使用的最大输入 token（实现无法获得可靠
  tokenizer 时使用保守字符预算）及最大输出 token；与 Batch_Size 共同约束窗口。
- **Window_Packer**：按稳定候选顺序，在 Batch_Size 与 Batch_Input_Budget 双重上限内生成
  Relevance_Window 的确定性组件。
- **Per_Item_Judge**：现有逐篇相关性判定器 `RelevanceJudge.assess`，作为等价路径与
  判准基线。
- **Cross_Verifier**：多标识符严格交叉核验器 `literature/retrieval/cross_verify.py` 的
  `CrossVerifier`。
- **Survivor**：通过确定性过滤/去重**且**被相关性判定接受（`is_accepted`）的候选，
  即需要进入严格核验的候选。
- **Concurrency_Executor**：注入的并发执行器（如线程池），用于在候选之间并发执行核验/
  相关性。默认 None 或并发度 1，等价于串行。
- **Concurrency_Limit**：并发上限（最大在飞任务数），注入配置。
- **Budget_Ledger**：统一预算记账器 `literature/retrieval/budget.py` 的 `BudgetLedger`，
  记 search / verify / llm / Loop 墙钟截止时间 / 迭代 / 候选数等维度，是唯一权威预算源；
  单次 provider latency 属于 trace 指标，不在并发下另行求和为第二套时间预算。
- **Relevance_Store**：相关性缓存 `RelevanceStore`，键为
  `(reference_id, task_fingerprint, reference_content_hash, judge_key)`。
- **Persistent_Reference_Id**：候选按 disposition 解析出的持久记录身份（reuse/merge 用现有
  记录 id，accept_new 用候选记录确定性 id），是 Relevance_Store 的入键身份。
- **Relevance_Subject**：实际送入相关性 Judge、并据此计算 content_hash 的规范记录：reuse
  使用 Ledger 现有记录，merge 使用确定性合并后的记录，accept_new 使用规范化候选记录。
- **Cancel_Token**：取消信号 `CancelToken`，`cancelled` 为真时应尽快停止。
- **Budget_Reservation**：并发任务提交前对调用名额做的原子预留；任务真正发出外部调用后结算为
  已消费，未开始即取消时释放，防止多个任务同时通过“尚未触顶”检查而集体超额。
- **Single_Flight**：同一缓存键并发 miss 时只允许一个外部调用在飞，其余调用等待并复用结果。
- **Provider_Concurrency_Policy**：按 provider/能力配置的最大在飞数、QPS、单次超时、退避与
  Retry-After 纪律；由组合层注入。
- **Legacy_Path**：批量、管线重排与并行能力均关闭时走的既有代码路径，其行为须与本 spec
  之前逐字节一致。
- **Verdict_Consistency**：同一候选集合，批量判定与逐篇判定所得 `verdict` 的一致比例，
  用于度量判准是否下降。
- **Acceptance_Consistency**：同一候选集合，批量路径与逐篇路径经同一
  RelevanceAcceptancePolicy 后，接受/不接受决定的一致比例。
- **Benchmark_Profile**：冻结的验收配置，至少包含任务配额、候选集、缓存冷热状态、provider
  集、模型与版本、batch/concurrency 参数、超时以及样本运行次数。
- **Architecture_Boundary**：`literature` 包不得 `import` `tasking` / `application` /
  `session` / `config`；阈值/预算/clock/LLM provider/executor 全部依赖注入，不读全局。

## Requirements

### Requirement 1: 批量相关性判定（吞吐核心）

**User Story:** 作为等待检索结果的用户，我希望相关性判定不再逐篇串行发 LLM 请求，而是一次判多篇，从而显著缩短检索时延。

#### Acceptance Criteria

1. THE Batch_Relevance_Judge SHALL 在**一次** LLM 调用内判定一个 Relevance_Window
   的相关性；每个窗口 SHALL 同时满足 `item_count <= Batch_Size` 与 Batch_Input_Budget。
2. THE Window_Packer SHALL 按稳定输入顺序确定性装箱；WHEN token/字符预算不构成额外约束且
   待判候选数为 N、Batch_Size 为 M，THEN 第一遍批量调用数 SHALL 等于 ⌈N / M⌉。
   因失败触发的有界重试 SHALL 单独计数，不得伪装成第一遍调用。
3. THE Batch_Relevance_Judge SHALL 为窗口内每篇候选产出一个与 Per_Item_Judge 同型的
   `RelevanceAssessment`（含 `reference_id`、`task_fingerprint`、
   `reference_content_hash`、`judge_key`、`status`、`verdict`、`confidence`、`basis`）。
4. THE Batch_Relevance_Judge SHALL 按 Per_Item_Judge 的既有规则对每篇候选计算
   `reference_content_hash` 与 `basis`（title_only 时 verdict 封顶 weakly_relevant）。
5. WHERE 候选无摘要，THE Batch_Relevance_Judge SHALL 对该候选应用 title_only 保守降级
   （若模型回 relevant 统一降为 weakly_relevant / low），与 Per_Item_Judge 一致。
6. THE Batch_Relevance_Judge SHALL 将每篇候选文本经既有 untrusted 清洗与包裹后才送入模型，
   AND SHALL NOT 执行候选文本中的任何指令性语句。
7. WHERE 单篇候选清洗后的文本仍超过 Batch_Input_Budget，THE Window_Packer SHALL 按与
   Per_Item_Judge 一致的字段级上限截断并单独成窗，SHALL NOT 静默丢弃该候选或突破上下文上限。
8. THE Batch_Relevance_Judge SHALL 对输入/输出 token（或保守字符估算）、实际窗口大小与
   截断数量进行计量；SHALL NOT 仅以候选篇数作为容量控制。
9. THE Batch_Relevance_Judge SHALL 使用独立且显式版本化的 batch prompt/schema；其
   JudgeVersion/judge_key SHALL NOT 与内容不同的 Per_Item_Judge prompt/schema 共用版本键。

### Requirement 2: 批量判定不降低判准

**User Story:** 作为对文献质量敏感的用户，我需要确信"一次判多篇"得到的相关性结论不比"逐篇判"差。

#### Acceptance Criteria

1. THE Batch_Relevance_Judge SHALL 使用与 Per_Item_Judge 语义等价的判定 rubric
   （relevant / weakly_relevant / unrelated 的定义、纳入/排除条件、"禁止仅凭关键词重合判
   relevant"），SHALL NOT 因批量而放宽判定标准。
2. THE Batch_Relevance_Judge SHALL 保证窗口内每篇候选的判定仅依据该候选自身的标题/摘要与
   任务标准，SHALL NOT 让同窗口内其他候选的内容影响本候选的 verdict。
3. THE system SHALL 提供冻结、可复现的批量相关性评测集，覆盖中文/英文、
   relevant/weakly_relevant/unrelated、title_only、长摘要、相邻主题、纳入/排除条件及提示注入样本；
   初始规模 SHALL 不少于 40 条，类别与语言分布 SHALL 在评测清单中公开记录。
   （Tier-2）作为无监督默认全开的门槛，该评测集 SHALL 扩充至不少于 200 条人工标注样本。
4. THE system SHALL 在固定 provider、model、temperature、prompt/schema version 与候选集的条件下，
   同时报告 Verdict_Consistency、Acceptance_Consistency 与 accepted precision / false
   acceptance rate（人工抽检即可，规模随评测集）；SHALL NOT 只以“与旧 Judge 一致”替代质量指标。
5. 批量能力在**受控 dogfood 下启用**前 SHALL 同时满足：Verdict_Consistency >= 0.90、
   Acceptance_Consistency >= 0.95、且抽检中未见相对 Per_Item_Judge 基线的明显 precision 退化。
   （Tier-2）**无监督默认全开**前，SHALL 在 ≥ 200 条标注集上进一步满足：accepted precision 相比
   基线下降不超过 2 个百分点，且 false acceptance rate 上升不超过 2 个百分点。
6. THE system SHALL 执行窗口组成/顺序变形测试：同一候选被放入不同窗口、窗口内重排时，
   Acceptance_Consistency SHALL >= 0.95，以验证其他候选不会实质影响本候选决定。
7. THE system SHALL NOT 将 Batch_Size 默认值设为大于 1，直至 Requirement 2.5（受控启用级）
   与 2.6 满足；WHERE 任一质量阈值未满足，THE system SHALL 保持批量能力默认关闭。

### Requirement 3: 批量调用的部分失败处理

**User Story:** 作为用户，我不希望批量判定里有几篇解析失败就拖垮整批，也不希望失败被伪装成"已判定"。

#### Acceptance Criteria

1. WHEN 批量响应中某候选的判定缺失、无法解析或不满足 schema，THE Batch_Relevance_Judge
   SHALL 为该候选产出 `status=ERROR`、`verdict=None`、`error_kind` 明确的
   `RelevanceAssessment`，SHALL NOT 将其伪装为 relevant / unrelated。
2. WHEN 批量响应中部分候选可解析、部分失败，THE Batch_Relevance_Judge SHALL 保留可解析
   候选的判定结果，SHALL NOT 因个别候选失败而丢弃整批的成功结果。
3. IF 整次批量 LLM 调用失败（provider / parse / schema），THEN THE Batch_Relevance_Judge
   SHALL 为窗口内**每篇**候选产出对应 `error_kind` 的 ERROR 判定。
4. THE system SHALL 仅将 `status=SUCCESS` 的判定计入相关性接受，
   SHALL NOT 将 `status=ERROR` 的候选计入引用配额。
5. THE Batch_Relevance_Judge SHALL 通过 `report_relevance` 式的结构化返回把每篇候选的判定
   与其身份（如候选序号或 reference_id）明确绑定，SHALL NOT 依赖模型返回顺序隐式对齐。
6. THE 批量结构化协议 SHALL 使用稳定、不含用户文本的 `candidate_key` 绑定输入输出，并拒绝
   重复 key、未知 key 与一个 key 多个结果；缺失 key SHALL 只使对应候选失败。
7. 为支持部分成功，THE 结构化调用 SHALL 先校验顶层 envelope/results 容器，再对每个 item
   独立执行字段/schema 校验；SHALL NOT 因一个 item 非法而在解析层丢弃其余合法 item。
8. WHERE 某些 item 首次失败且预算、取消状态允许，THE system MAY 对失败子集执行最多一次
   确定性拆窗或单项 batch-schema 重试；成功 item SHALL NOT 重试。重试仍失败时保留 ERROR。
9. THE system SHALL 将 first_pass、partial_retry、whole_batch_failure 分开记账与 trace；任何重试
   均受同一 Budget_Ledger、Batch_Input_Budget 与超时约束。

### Requirement 4: 管线重排——先便宜过滤再批量判相关，只对幸存者核验

**User Story:** 作为用户，我不希望系统对那些最终会被判不相关而丢弃的候选，也花时间去做多次网络交叉核验。

#### Acceptance Criteria

1. THE Retrieval_Pipeline SHALL 按"确定性精确去重 → 语言/资格预筛 → 相关性判定 → 严格交叉
   核验"的顺序处理候选，SHALL NOT 在相关性判定之前对候选执行严格交叉核验。
2. THE Retrieval_Pipeline SHALL 先在单线程准备阶段完成候选规范化、Persistent_Reference_Id/
   provisional disposition（none/reuse/merge）解析、批内精确重复分组、Relevance_Subject 构造
   及资格预筛，再把只读工作项交给
   批量/并发阶段；SHALL NOT 在 worker 中竞争修改 accepted_records、Batch 或 Ledger。
3. WHERE 多个候选解析到同一持久身份或同一高置信 work-id，THE 准备阶段 SHALL 先按既有确定性
   merge/字段质量规则构造一个 Relevance_Subject（元数据完整度优先、稳定输入序作 tie-breaker），
   并把其余项标为 batch_duplicate/merge participant；SHALL NOT 因并发完成顺序让同一作品重复
   进入相关性窗口或严格核验，也不得无条件选择信息更少的首项作为代表。
4. THE Retrieval_Pipeline SHALL 仅对 Survivor（通过去重/过滤且相关性被接受的候选）调用
   Cross_Verifier，SHALL NOT 对相关性未被接受的候选调用严格核验。
5. WHEN 相同候选集合经重排后管线处理，THE Retrieval_Pipeline SHALL 使 Cross_Verifier 的
   候选调用数不超过 Survivor 数，且 provider verify 调用数不超过这些 Survivor 的未缓存、
   可核验 identifier 数；无 Survivor 时 SHALL 为零。
6. THE Retrieval_Pipeline SHALL 对 reuse / merge disposition 的候选保持"不重复严格核验"的
   既有纪律（复用现有记录的核验事件）。
7. THE Retrieval_Pipeline SHALL 保留被各阶段拒绝的候选的 `BatchItem` 与拒绝原因，
   SHALL NOT 静默丢弃候选。
8. THE 重排路径 SHALL 把相关性视为核验前筛选，而不是可信元数据证明；只有 Requirement 5 的
   严格核验与最终单线程归并可以产生 ACCEPT_NEW。

### Requirement 5: 正确性保持——核验后置不放松引用门槛

**User Story:** 作为用户，我必须确信调整管线顺序后，进入引用配额的文献仍然是严格核验过的，不会因为"先判相关"就放进未核验的文献。

#### Acceptance Criteria

1. THE Retrieval_Pipeline SHALL 仅把通过严格核验（`meets_strict_threshold` 为真）的候选置为
   `ACCEPT_NEW`，SHALL NOT 因相关性已通过而接受未达严格核验门槛的候选。
2. THE Retrieval_Pipeline SHALL 保持"计入引用配额（CITATION）的记录必须为 strict verified"
   的既有口径，SHALL NOT 因管线重排而让未核验记录计入配额。
3. WHEN 某候选相关性被接受但严格核验未达门槛，THE Retrieval_Pipeline SHALL 以
   `verify:<status>` 原因拒绝该候选并保留其 `BatchItem`。
4. THE Retrieval_Pipeline SHALL 对相关性判定结果按 Persistent_Reference_Id 入键 Relevance_Store
   （reuse/merge 用现有记录 id，accept_new 用候选记录确定性 id），保持既有缓存语义，
   SHALL NOT 用候选的 transient id 入键。
5. THE Retrieval_Pipeline SHALL 保持配额只由已接受条目推进的既有纪律。
6. THE Retrieval_Pipeline SHALL 使用 Relevance_Subject 的实际 title/abstract 计算
   reference_content_hash，并将相同 Relevance_Subject 送入 Judge；SHALL NOT 评审候选文本却以
   reuse/merge 后不同内容的哈希写缓存。
7. THE Relevance_Store SHALL 把 batch prompt/schema 的 JudgeVersion 纳入 judge_key；批量与逐篇
   路径的不同版本结果可以共存，但 SHALL NOT 互相覆盖或错误命中。
8. WHERE 批量窗口部分失败并使用单项 fallback，THE fallback SHALL 使用与当前运行路径兼容、
   可被后续 preflight/build_bundle 明确查询的 JudgeVersion；SHALL NOT 产生下游不知道应读取
   哪个 judge_key 的混合缓存。

### Requirement 6: 跨候选与跨标识符并行化

**User Story:** 作为用户，我希望多篇候选的核验和相关性判定能并发跑，而不是一篇跑完再跑下一篇。

#### Acceptance Criteria

1. WHERE 注入了 Concurrency_Executor 且 Concurrency_Limit 大于 1，THE Retrieval_Pipeline
   MAY 在只读工作项之间并发执行批量相关性窗口与严格核验 I/O。
2. WHERE Cross_Verifier 需要回查同一候选的多个独立 identifier，THE system MAY 并发执行这些
   provider fetch，但 SHALL 按稳定 `(id_type, normalized_id)` 顺序构造 VerificationEvent.seq。
3. THE Retrieval_Pipeline SHALL 同时满足全局 Concurrency_Limit、阶段级上限
   （relevance/verify）与 Provider_Concurrency_Policy；实际在飞数 SHALL 取这些上限的最小值。
4. WHERE 未注入 Concurrency_Executor 或 Concurrency_Limit 为 1，THE Retrieval_Pipeline
   SHALL 串行执行，且行为走 Legacy_Path。
5. THE Retrieval_Pipeline SHALL 使并发执行产出的 `BatchItem` 集合（disposition、拒绝原因、
   核验事件、相关性结论）与串行执行在语义上等价，SHALL NOT 因并发引入不确定的接受/拒绝结果。
6. THE Retrieval_Pipeline SHALL 使并发执行产出的 `BatchItem` 具有确定性的稳定顺序
   （如按候选输入序或稳定键排序），SHALL NOT 让最终顺序依赖任务完成的先后。
7. THE Retrieval_Pipeline SHALL 在 worker 完成后由单线程 reducer 统一执行 BatchItem 追加、
   bibkey/numeric alias 分配、accepted_records 更新与最终缓存合并；worker SHALL NOT mutate Batch
   或主 Ledger。
8. Relevance_Store 与 VerificationCache SHALL 提供线程安全访问或由线程安全协调层保护；对于
   相同缓存键的并发 miss SHALL 使用 Single_Flight，避免重复 LLM/provider 调用。
9. THE Concurrency_Executor SHALL 由组合层注入，SHALL NOT 由 `literature` 包内部创建全局
   线程池或读取全局并发配置。
10. THE `literature` 包 SHALL NOT 关闭或长期持有外部注入的 executor；executor 生命周期归组合层。
11. WHERE 某 provider/LLM client 未声明线程安全或其 Provider_Concurrency_Policy 上限为 1，
    THE system SHALL 对该 provider 串行调用，即使全局并发上限更高。

### Requirement 7: 预算兼容——批量与并行正确记账，不绕过预算

**User Story:** 作为用户，我希望批量和并行执行仍然如实计入预算，不会因为优化就冲破调用上限或时间预算。

#### Acceptance Criteria

1. WHEN Batch_Relevance_Judge 发起一次批量 LLM 调用，THE system SHALL 向 Budget_Ledger
   记一次 llm 调用，SHALL NOT 因批量而少记或漏记 LLM 调用。
2. WHEN Cross_Verifier 在并发路径下发起核验调用，THE system SHALL 把每次 verify 调用及其
   调用次数计入 Budget_Ledger，并把单次 provider latency 记入 trace；SHALL NOT 绕过统一预算记账
   或把并发 latency 求和后当作另一套停止预算。
3. THE system SHALL 保持 Budget_Ledger 作为唯一权威预算源，SHALL NOT 在批量/并行路径引入
   独立的、不汇入 Budget_Ledger 的第二套预算。
4. WHILE 并发任务在飞，THE Budget_Ledger SHALL 使用线程安全的原子 Budget_Reservation；
   外部调用必须先成功预留名额才能提交，SHALL NOT 使用非原子的“检查后再 charge”。
5. WHEN 已预留任务在真正发出外部调用前被取消，THE system SHALL 释放预留；WHEN 外部调用
   已发出，无论成功、provider error、parse error 或 timeout，THE system SHALL 结算一次实际调用。
6. THE system SHALL 分开记录 `relevance_items_judged`、`relevance_windows` 与
   `relevance_llm_calls`；Batch 一次调用判多篇时只增加一次 llm_calls，但增加相应 item 数。
7. IF 任一预算维度触顶（含 max_llm_calls / max_verify_calls / max_provider_calls /
   provider_time_budget_s），THEN THE Retrieval_Pipeline SHALL 停止对剩余候选发起新的
   批量判定或核验调用。
8. 本 spec SHALL 将既有 `provider_time_budget_s` 解释为基于注入 monotonic clock 的 Loop
   墙钟截止预算，而不是并发 provider 耗时的简单求和；每次 provider latency 可单独进入 trace，
   SHALL NOT 在并发下重复累计成另一套停止口径。
9. 缓存命中 SHALL 不消费新的外部调用预算；Single_Flight 的等待者 SHALL 不重复预留调用名额。

### Requirement 8: 可中断——预算耗尽或取消时及时停止

**User Story:** 作为用户，当预算用完或我取消操作时，我希望批量和并行执行能尽快停下来，而不是把已排队的任务全跑完。

#### Acceptance Criteria

1. WHILE Cancel_Token 为已取消，THE Retrieval_Pipeline SHALL 停止发起新的批量判定与核验任务。
2. WHEN 预算耗尽或收到取消信号，THE Retrieval_Pipeline SHALL 在合理边界内停止处理剩余候选
   并如实标注 `budget_stopped`（或等价的取消标注）；“合理边界”由注入的单次调用 timeout 与
   shutdown_grace_s 定义，验收时不得使用无界等待。
3. WHILE 存在已在飞的并发任务且发生取消/预算耗尽，THE Retrieval_Pipeline SHALL 不再向
   Concurrency_Executor 提交新任务，SHALL 收束已提交任务后返回一致的结果集。
4. THE Retrieval_Pipeline SHALL 在中断返回时仍保证已产出的 `BatchItem` 与预算记账自洽
   （已记账的调用对应已产出或已丢弃的结果），SHALL NOT 留下半记账状态。
5. WHEN 批量窗口进行到一半时预算耗尽，THE Retrieval_Pipeline SHALL 停止发起后续窗口，
   SHALL NOT 因批量分组而无视预算继续发起下一窗口。
6. WHEN 发生取消/预算耗尽，THE system SHALL 尝试取消 executor 中尚未开始的 Future；无法取消的
   在飞调用 SHALL 受 per_call_timeout_s 约束。返回后 SHALL NOT 有后台任务继续修改 Batch/缓存。
7. THE Cancel_Token SHALL 至少在窗口装箱前、任务提交前与外部调用前检查；取消前已提交且最终完成
   的在飞任务 MAY 由 reducer 纳入一致结果集，但取消后 SHALL NOT 新建外部工作或接纳来源不明的
   后台结果。

### Requirement 9: 零回归——默认关闭或走等价路径

**User Story:** 作为现有用户，我希望在没开启批量/并行时，检索行为和现在完全一样。

#### Acceptance Criteria

1. WHERE 批量、管线重排与并行三个 feature flag 均未开启（Batch_Size=1、Concurrency_Limit=1、
   未注入 Concurrency_Executor），THE Retrieval_Pipeline / Batch_Relevance_Judge /
   Cross_Verifier SHALL 表现出与本 spec 之前逐字节一致的行为。
2. THE system SHALL 保证既有测试全部通过。
3. THE system SHALL 默认关闭批量、管线重排与并行能力（默认走 Legacy_Path）。
4. WHERE 仅开启管线重排但未开启批量/并行，THE Retrieval_Pipeline SHALL 产出与重排前在
   相同输入下**语义等价**的接受/拒绝集合（可减少核验调用次数，但不改变哪些候选被接受）。
   **例外**：对 reuse / merge disposition 的候选，因 Requirement 5.6 将相关性判定的文本与
   `reference_content_hash` 基准由"候选文本"改为 Relevance_Subject 文本，其相关性结论允许与
   legacy 不同——这是有意的 seam 修正，不视为回归；accept_new 候选（Relevance_Subject 即规范化
   候选）SHALL 保持与 legacy 等价。
5. THE system SHALL 通过配置注入的方式启用各能力，SHALL NOT 要求修改 `literature` 包内代码
   来切换能力开关。
6. THE 三个能力 SHALL 可独立启用与回滚；上线顺序 SHALL 为 B1 → B2 → B3，每阶段通过自身质量、
   错误率与时延门槛后才可启用下一阶段。

### Requirement 10: 架构红线——不依赖上层、全部注入

**User Story:** 作为维护者，我要求吞吐优化不破坏分层：literature 包不依赖上层，运行时依赖全部注入。

#### Acceptance Criteria

1. THE `literature` 包 SHALL NOT `import` `tasking` / `application` / `session` / `config`。
2. THE Batch_Relevance_Judge SHALL 通过构造函数接收 LLM provider 与 JudgeVersion，
   SHALL NOT 读取全局 config 选择 provider。
3. THE Retrieval_Pipeline SHALL 通过注入接收阈值、预算、clock、Concurrency_Executor 与
   Batch_Size、Batch_Input_Budget、Provider_Concurrency_Policy，SHALL NOT 读取全局配置或全局时钟。
4. THE Concurrency_Executor 与并发上限 SHALL 由组合层注入，SHALL NOT 在 `literature` 包内
   创建全局并发资源。
5. THE Batch_Relevance_Judge SHALL 复用现有统一 LLM 结构化调用层（`structured_call`）与
   untrusted 清洗层，SHALL NOT 自建 LLM 协议适配或自建文本清洗。
6. THE Budget_Ledger/协调层 SHALL 暴露原子 reservation 能力供并发路径使用；SHALL NOT 让
   worker 直接读写其内部计数器或各自维护局部预算。

### Requirement 11: 端到端时延目标

**User Story:** 作为用户，我需要一个可度量的时延目标来判断这次优化是否达标。

#### Acceptance Criteria

1. THE system SHALL 同时记录 `retrieval_loop_active_elapsed_s` 与
   `literature_preflight_active_elapsed_s`；计时 SHALL 使用 monotonic clock，并排除等待用户确认的
   人工停顿，但 SHALL 包含系统重试、排队、LLM、search 与 verify 时间。
2. THE system SHALL 建立版本化 Benchmark_Profile：典型任务为获取并最终可用于预检的 20 篇文献
   （示例 en15/zh5），并冻结候选集/查询、冷热缓存、provider/model/version、预算、batch/concurrency、
   timeout 与运行次数；没有完整 profile 的结果 SHALL NOT 宣称达到性能目标。
3. THE system SHALL 提供真实 provider dogfood 配对报告：运行不少于 5 个与 Legacy_Path 同环境的
   配对样本并报告中位数、范围、provider 错误率及有效样本数。
   （Tier-2）THE system SHALL 另提供确定性 replay benchmark：用同一录制响应隔离网络抖动，
   运行不少于 30 次并报告 P50/P95。
4. WHEN B1+B2+B3 开启且 Requirement 2 的受控启用级门槛满足，THEN 真实 provider 有效配对样本的中位
   active elapsed SHALL 不超过 10 分钟且相对 Legacy_Path 基线降低至少 50%。
   （Tier-2）replay profile 的 P50 SHALL 相对基线降低至少 60%，P95 SHALL 降低至少 50%。
5. provider 大面积不可用的运行 MAY 按 Benchmark_Profile 预先定义的无效条件排除，但 SHALL 在报告中
   保留数量与原因；SHALL NOT 在看到慢结果后临时删除样本。
6. THE system SHALL 分别报告 Legacy、B1、B1+B2、B1+B2+B3 的阶段结果，以归因性能收益与回归；
   SHALL NOT 只比较“全部关闭”和“全部开启”。
7. THE system SHALL 同时报告窗口数、实际 LLM 调用数、相关性 item 数、verify/provider 调用数、
   cache/single-flight 命中、错误/重试数与最终配额完成状态；未完成配额的快速失败 SHALL NOT 被计为达标。

### Requirement 12: 可观测——批量与并行的 trace

**User Story:** 作为开发者，我需要在检索变慢或结论异常时看清批量分组、并发度与各阶段调用数。

#### Acceptance Criteria

1. THE system SHALL 在既有检索 trace 字段基础上记录批量判定的窗口数、每窗 item 数、估算输入/
   输出 token（或字符数）、截断数、first-pass/重试 LLM 调用数与批量内 ERROR 候选数。
2. THE system SHALL 记录是否走并发路径、全局/阶段/provider 配置上限、实际最大在飞任务数、
   executor 排队时间与各阶段 wall-clock 时延。
3. THE system SHALL 保持既有隐私安全 trace 纪律：只记哈希/计数/预算，SHALL NOT 落原始 query
   或候选文本。
4. WHEN 发生批量部分失败或整批失败，THE system SHALL 在 trace 中区分 `error_kind`
   （provider_failed / parse_failed / schema_failed）并计数。
5. THE system SHALL 记录 Budget_Reservation 的成功/拒绝/释放/结算计数、触顶维度、取消的 queued/
   in-flight 数，便于证明并发没有绕过预算。
6. THE system SHALL 记录 Relevance_Store/VerificationCache 命中数、Single_Flight 等待者数量、
   provider 429/timeout/backoff 次数，但 SHALL NOT 记录原始标题、摘要、query 或 candidate_key 映射原文。
7. 所有新增 trace SHALL 复用统一脱敏/摘要设施，不得在并发 worker 中直接写普通日志；事件 SHALL
   带 operation/attempt/window 标识，以支持确定性关联而不依赖完成顺序。

### Requirement 13: 幂等与重试安全

**User Story:** 作为用户，我希望重试或缓存命中不会导致重复付出昂贵调用，也不会产生不一致结果。

#### Acceptance Criteria

1. WHEN 某候选的相关性结论已存在于 Relevance_Store（同 Persistent_Reference_Id、
   task_fingerprint、content_hash、judge_key），THE Retrieval_Pipeline SHALL 复用缓存结论，
   SHALL NOT 将该候选重复放入批量判定窗口。
2. WHEN 批量判定与缓存混合，THE Retrieval_Pipeline SHALL 仅对未命中缓存的候选组成 Relevance_Window。
3. THE Retrieval_Pipeline SHALL 使"对同一候选集合重复运行一次 attempt"在缓存命中后产出与首次
   语义一致的接受/拒绝结果（不因批量分组或并发顺序而不同）。
4. THE Batch_Relevance_Judge SHALL 将成功判定结果按既有规则写入 Relevance_Store，
   SHALL NOT 写入 `status=ERROR` 的伪判定覆盖既有成功结论。
5. WHEN 多个 worker 同时请求同一 relevance/verification cache key，THE system SHALL 通过
   Single_Flight 只发起一次外部调用；等待者复用同一完成结果，预算只结算一次。
6. THE Relevance_Store SHALL 保持既有“SUCCESS 优先 ERROR”的确定性合并规则；并发写入同键结果
   SHALL 与任一串行顺序得到相同最终状态。
7. 重试 SHALL 复用同一 operation/attempt/window 身份并产生新的 attempt ordinal；SHALL NOT 因
   retry 重复追加 ACCEPT_NEW、重复分配 numeric alias 或产生重复 BatchItem 身份。
8. Batch prompt/schema/JudgeVersion、Batch_Input_Budget 或影响判定的清洗版本变化 SHALL 使对应
   judge_key 变化并安全失效旧缓存；仅改变并发度/窗口装箱但不改变判定内容时 SHALL 不改变
   TaskFingerprint。

### Requirement 14: Provider 背压、超时与渐进发布

**User Story:** 作为维护者，我希望吞吐优化不会压垮外部服务，并且出现质量或错误率异常时可以快速回滚。

#### Acceptance Criteria

1. THE system SHALL 为每个 search/verify/LLM provider 注入 Provider_Concurrency_Policy，至少包含
   max_in_flight、per_call_timeout_s、是否线程安全以及可选 QPS/backoff 参数；未知 provider SHALL
   使用保守默认值而不是无限并发。
2. WHEN provider 返回 429/Retry-After 或可重试 transient error，THE system SHALL 在同一预算内
   执行有上限、带 jitter 的退避；SHALL NOT 无界重试或切换到不具 authority 的 provider 伪装成功。
3. 每个 operation SHALL 冻结 feature flags、Batch_Size、Batch_Input_Budget、并发策略与 JudgeVersion
   快照；运行中全局配置变化 SHALL 只影响后续 operation，不得让同一 Batch 中途换判准。
4. B1、B2、B3 SHALL 使用独立 feature flag 与手动一键回滚能力；B1 未通过 Requirement 2 的受控
   启用级质量门槛前，B2/B3 SHALL NOT 进入默认路径。（Tier-2）SHALL 支持按 canary 比例灰度。
5. THE system SHALL 报告批量 item ERROR rate、provider timeout rate 与任何预算 overshoot，
   使运维者可据此手动关闭对应能力；当前 operation SHALL 按已冻结策略完成、有界 fallback 或诚实失败，
   SHALL NOT 静默切换判准。（Tier-2）WHERE 批量 item ERROR rate 在至少 100 个 item 的滚动窗口内
   超过 5%、某 provider timeout rate 在至少 20 次调用的滚动窗口内超过 10%、发生预算 overshoot，或
   Requirement 2 质量门槛失守，THE system SHALL 对**后续 operation**自动关闭对应能力并告警。
6. 性能验收、质量评测报告 SHALL 作为启用默认值的发布门槛并可审计保存；仅有单元测试通过
   SHALL NOT 被视为可以默认开启批量或并发。（Tier-2）canary 报告纳入无监督默认全开的发布门槛。
