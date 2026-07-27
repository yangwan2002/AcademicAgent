# Requirements Document

## Introduction

本功能把文献能力从「只保证标识符存在」升级为「**可信引用**」：完整元数据、DOI 去重、配额驱动的**有界检索 Agent Loop**、引用与论断的**语义支撑校验**（Evidence Gate）、以及可用的参考文献条目。

现状缺口（P0#6 审计）：`KnownRef` 只有 id/bibkey/title，丢失 DOI/作者/年份/摘要；`bibkeys()` 不过滤 `verified=False`；检索不足时不补检索；写作 prompt 只拿到标题、没有证据摘要；Gate 只查引用 key 是否存在、不查引用是否**支持**对应论断；`fix_bibliography` 只写 `\bibitem{key} title.`；去重不用 DOI；「20篇、外文15、中文5」这类配额无法保证。

**范围边界**：不做全文 PDF 解析、不做本地文献库导入（BibTeX/EndNote）、不做跨会话文献共享、不做 CNKI/万方等无公开 API 源的接入（→ P1）。

## Glossary

- **Reference**：一条文献记录，含可核验标识符（DOI / arXiv id / OpenAlex id）与完整元数据。
- **Verified_Reference**：经检索源二次核验、标识符与标题一致的 Reference。**只有它可被引用**。
- **Evidence**：一条 Reference 中可用于支撑论断的文本片段（摘要或摘要句），随引用一起进入写作上下文。
- **Claim**：正文中一句需要文献支撑的断言。
- **Claim_Evidence_Link**：`(claim, reference, verdict)` 三元组，verdict ∈ supported | unsupported | unrelated。
- **Citation_Quota**：用户对文献总量与构成的要求，如「共 20 篇、外文 ≥15、中文 ≥5、近五年 ≥10」。
- **Quota_Gap**：配额中**未能由检索满足**的部分（如中文缺 3 篇）。
- **Retrieval_Loop**：为满足配额而进行的有界自主检索循环（改写查询词 / 换检索源 / 补检索）。
- **Retrieval_Attempt**：Loop 中的一次「查询 → 候选 → 核验 → 入库」迭代。
- **Evidence_Gate**：判定「已引用的文献是否真的支撑对应论断」的校验闸。
- **Reference_Ledger**：本项目的文献账本（持久化于 facts），记录每条 Reference 的来源、核验状态与被引用位置。

## Requirements

### Requirement 1: 完整文献元数据

**User Story:** 作为论文作者，我希望系统保存文献的完整元数据，以便生成规范的参考文献条目并判断文献是否切题。

#### Acceptance Criteria

1. THE system SHALL 为每条 Reference 保存 `identifier`（DOI / arXiv id / OpenAlex id 之一）、`title`、`authors`、`year`、`venue`、`language`、`abstract`、`source`（检索源）、`verified`。
2. WHERE 检索源未提供某字段，THE system SHALL 将该字段留空并标记 `metadata_complete=false`，SHALL NOT 用占位值或推测值填充。
3. WHEN 生成参考文献条目，IF `metadata_complete=false`，THEN THE system SHALL 明确标注该条目信息不全并提示用户补充。
4. THE system SHALL 持久化 Reference_Ledger 至项目级 facts，且**加载时合并、不覆盖**已有记录。
5. WHEN 同一 Reference 被再次检索到，THE system SHALL 保留已有的 `verified` 与 `abstract`，SHALL NOT 因新记录字段缺失而降级。

### Requirement 2: 基于标识符的去重

**User Story:** 作为论文作者，我不希望同一篇论文以不同 bibkey 重复出现在参考文献里。

#### Acceptance Criteria

1. THE system SHALL 按 `identifier`（归一化后：DOI 转小写去 URL 前缀、arXiv id 去版本号）判定 Reference 是否重复。
2. WHERE 两条 Reference 的 `identifier` 相同，THE system SHALL 视为同一篇并合并元数据（缺字段互补），SHALL NOT 分配第二个 bibkey。
3. WHERE `identifier` 缺失，THE system SHALL 退化为「标题归一化 + 年份」比对，并把该条标记为 `dedup_confidence=low`。
4. WHEN 合并两条记录，THE system SHALL 保留 `verified=true` 的一方的核验状态。

### Requirement 3: 只有已核验文献可被引用

**User Story:** 作为论文作者，我需要确保正文里的每个引用都指向真实存在的文献。

#### Acceptance Criteria

1. THE system SHALL 仅将 `verified=true` 的 Reference 提供给写作上下文作为可引用文献。
2. THE system SHALL 在引用可用性查询（bibkey / ref id 集合）中**排除** `verified=false` 的记录。
3. WHEN 正文出现引用了未核验或不存在的标识符，THE Citation_Gate SHALL 判定不通过并列出违规标识符。
4. WHERE 源文件中原有引用无法核验，THE system SHALL 保留该引用于文档中、标记 `verified=false`、并在报告中提示用户手动确认，SHALL NOT 自动删除用户已有引用。

### Requirement 4: 配额驱动的检索需求

**User Story:** 作为论文作者，我希望能说「找 20 篇文献，其中外文至少 15 篇、近五年至少 10 篇」，并且系统按此执行。

#### Acceptance Criteria

1. THE system SHALL 从用户请求中抽取 Citation_Quota，支持维度：总篇数、语言构成、年份范围、主题关键词。
2. WHERE 用户未指定配额，THE system SHALL 使用文档化默认值（总篇数由现有 `search_limit` 决定），SHALL NOT 追问。
3. WHEN 抽取到的配额内部矛盾（如外文 15 + 中文 8 > 总数 20），THE system SHALL 判定为不可执行并说明冲突，SHALL NOT 静默取近似值。
4. THE system SHALL 把配额随计划摘要一并呈现给用户确认。

### Requirement 5: 有界自主检索 Loop

**User Story:** 作为论文作者，我希望系统在检索结果不足时自己想办法（换关键词、换检索源），而不是直接返回「只找到 3 篇」。

#### Acceptance Criteria

1. WHILE 配额未满足 AND 未超过迭代上限，THE Retrieval_Loop SHALL 自主执行下一次 Retrieval_Attempt。
2. THE Retrieval_Loop SHALL 支持的自主动作限于：改写查询词、拆分/合并查询、切换检索源、放宽或收紧年份范围。
3. THE Retrieval_Loop SHALL 受以下上限约束：最大迭代轮数、最大检索调用次数、最大 LLM 调用次数、累计 provider 耗时预算。
4. WHEN 任一上限被触及，THE Retrieval_Loop SHALL 停止并保留已获得的 Verified_Reference，SHALL NOT 丢弃已有成果。
5. THE Retrieval_Loop SHALL NOT 在迭代过程中向用户提问。
6. WHEN Loop 结束，THE system SHALL 呈现文献清单（标题 / 年份 / 来源 / 语言）并请用户确认后才写入 Reference_Ledger。
7. IF 用户否决清单，THEN THE system SHALL 丢弃本次检索结果并保持 Ledger 不变。
8. THE system SHALL 为每次 Retrieval_Attempt 记录 trace 事件（查询词、检索源、候选数、核验通过数、当前配额进度）。

### Requirement 6: 配额缺口如实告知

**User Story:** 作为论文作者，我需要知道系统到底找到了什么、缺什么，而不是被凑数的结果误导。

#### Acceptance Criteria

1. WHEN Loop 结束且配额未完全满足，THE system SHALL 报告每个维度的 Quota_Gap（要求值 / 实得值 / 缺口）。
2. THE system SHALL NOT 为满足配额而纳入未通过核验的候选。
3. THE system SHALL NOT 为满足配额而重复计入同一 identifier。
4. WHERE 某配额维度受检索源能力限制而无法验证（如中文文献源不可用），THE system SHALL 明确说明该维度不可保证并建议用户手动补充。
5. WHEN 存在 Quota_Gap，THE system SHALL 询问用户：接受现状继续写作 / 手动补充文献 / 取消本次操作。

### Requirement 7: 证据进入写作上下文

**User Story:** 作为论文作者，我希望生成的引用是有依据的，而不是模型看着标题瞎编。

#### Acceptance Criteria

1. THE system SHALL 在写作上下文中为每条可引用文献提供 Evidence（摘要或摘要摘录），而非仅提供标题。
2. WHERE 某条 Reference 无 `abstract`，THE system SHALL 标注该文献「无摘要可用」，并要求生成时不得就其内容做具体断言。
3. THE system SHALL 限制单次写作上下文中的 Evidence 总量，超出时按与主题相关性截断并记录被截断的条数。
4. THE system SHALL 在 trace 中记录本次生成实际提供了哪些 Reference 的 Evidence（仅标识符与长度，不落原文）。

### Requirement 8: Evidence Gate（引用支撑校验）

**User Story:** 作为论文作者，我需要确保「引用 A 支持论断 B」这件事是真的，而不只是引用格式正确。

#### Acceptance Criteria

1. WHEN 生成的正文包含引用，THE Evidence_Gate SHALL 为每个 (Claim, Reference) 配对判定 verdict ∈ supported | unsupported | unrelated。
2. THE Evidence_Gate SHALL 仅依据该 Reference 的 Evidence 做判定，SHALL NOT 依据模型的先验知识。
3. WHERE 某 Reference 无 Evidence 可用，THE Evidence_Gate SHALL 判定为 `unverifiable` 而非 supported。
4. WHEN 存在 verdict ∈ {unsupported, unrelated} 的配对，THE Evidence_Gate SHALL 判定本次生成不通过并回灌具体配对供重试。
5. THE system SHALL 限制 Evidence_Gate 的重试次数；WHEN 重试耗尽，THE system SHALL 拒绝写入并向用户报告未通过的配对。
6. THE Evidence_Gate SHALL 与写作使用**不同的 LLM 调用**（生成与校验分离），SHALL NOT 让同一次调用既生成又自评。
7. THE system SHALL 记录每个 Claim_Evidence_Link 至 trace（claim 摘要哈希、reference 标识符、verdict），SHALL NOT 落 claim 原文。

### Requirement 9: 可用的参考文献条目

**User Story:** 作为论文作者，我希望自动生成的参考文献列表是能直接投稿的，而不是只有 key 和标题。

#### Acceptance Criteria

1. WHEN 生成参考文献条目，THE system SHALL 包含作者、年份、标题、出版物、标识符（DOI/arXiv）。
2. WHERE 文档为 .tex，THE system SHALL 生成结构完整的 `\bibitem`；WHERE 文档为 .md，THE system SHALL 生成带标识符的列表项。
3. THE system SHALL 按稳定顺序（首次引用顺序或字母序，可配置）输出条目。
4. THE system SHALL 仅为正文中**实际被引用**的 Reference 生成条目，并报告 Ledger 中未被引用的记录。
5. WHEN 正文引用了 Ledger 中不存在的标识符，THE system SHALL 报告缺失项并拒绝生成不完整的列表。

### Requirement 10: 参考文献修复作为规划层意图

**User Story:** 作为论文作者，我希望说「帮我修一下参考文献」时系统能理解并执行，即使我用的是新的规划层。

#### Acceptance Criteria

1. THE system SHALL 将「修复/补全参考文献」作为规划层可识别的意图，具备 IntentSpec 与 Handler。
2. THE system SHALL 为该意图声明的每个槽位提供 execution_binding，且 Handler SHALL 真实消费它们。
3. WHEN 该意图与其他操作出现在同一请求中，THE system SHALL 按用户表达顺序纳入同一计划。
4. WHERE 规划层不支持某个旧路径意图，THE system SHALL 明确回退旧执行器或明确拒绝，SHALL NOT 静默丢为未映射。

### Requirement 11: 可观测与失败降级

**User Story:** 作为开发者，我需要在文献检索出问题时能看清发生了什么。

#### Acceptance Criteria

1. THE system SHALL 为检索、核验、配额评估、Evidence_Gate 各阶段发布 trace 事件。
2. WHEN 检索源不可用，THE system SHALL 记录失败源并尝试其余已配置源，SHALL NOT 中断整个操作。
3. WHEN 所有检索源均不可用，THE system SHALL 如实报告并让用户选择继续（不引用新文献）或取消。
4. THE system SHALL NOT 因文献检索失败而写入部分完成的正文。
5. THE system SHALL 在 trace 中脱敏处理文献摘要与 claim 原文（仅记录哈希/长度/标识符）。

### Requirement 12: 向后兼容与默认行为

**User Story:** 作为现有用户，我不希望这次升级破坏我已有的工作流。

#### Acceptance Criteria

1. WHERE 新能力未启用，THE system SHALL 保持现有 rewrite / add_section 的检索行为不变。
2. THE system SHALL 保证既有测试全部通过。
3. WHEN 加载旧版 facts.json（无新字段），THE system SHALL 正常读取并按 Requirement 1 补齐字段结构，SHALL NOT 丢失已有记录。
4. THE Evidence_Gate SHALL 可通过配置关闭；WHERE 关闭，THE system SHALL 退化为现有的引用存在性校验。

## Correctness Properties

### Property 1: 引用可核验性
正文中每个被接受的引用标识符，必在 Reference_Ledger 中且 `verified=true`。
**Validates: Requirements 3.1, 3.2, 3.3**

### Property 2: 标识符唯一性
Ledger 中不存在两条归一化 `identifier` 相同的记录；合并后核验状态取二者中较强者。
**Validates: Requirements 2.1, 2.2, 2.4**

### Property 3: 配额诚实性
报告的实得数量等于 Ledger 中本次新增且 `verified=true` 的去重条数；任何 Quota_Gap 必被显式报告，绝不由未核验或重复条目填补。
**Validates: Requirements 6.1, 6.2, 6.3**

### Property 4: Loop 有界性
Retrieval_Loop 的迭代数、检索调用数、LLM 调用数、累计耗时均不超过配置上限；触顶时已获成果不丢失。
**Validates: Requirements 5.3, 5.4**

### Property 5: Loop 无人机交互
Retrieval_Loop 迭代过程中不产生任何用户提问；用户交互只发生在 Loop 结束后的清单确认与缺口处置。
**Validates: Requirements 5.5, 5.6**

### Property 6: 证据依据性
Evidence_Gate 的每个 verdict 必可追溯到具体 Reference 的 Evidence；无 Evidence 者判 `unverifiable`，绝不判 supported。
**Validates: Requirements 8.2, 8.3**

### Property 7: 生成校验分离
Evidence_Gate 的判定调用与正文生成调用相互独立，不共享同一次 LLM 响应。
**Validates: Requirement 8.6**

### Property 8: 未确认不入库
用户确认前，检索结果不写入 Reference_Ledger；用户否决后 Ledger 逐字节不变。
**Validates: Requirements 5.6, 5.7**

### Property 9: 元数据不降级
已有 Reference 的 `verified` 与 `abstract` 不因后续合并或源文件扫描而丢失或置为更弱的值。
**Validates: Requirements 1.4, 1.5, 2.4**

### Property 10: 失败不产半成品
检索或 Evidence_Gate 失败时，Working_Copy 与 Ledger 均不被部分写入。
**Validates: Requirements 11.4, 8.5**

### Property 11: 隐私边界
trace 中不出现文献摘要原文与 claim 原文，仅出现标识符、哈希、长度与统计量。
**Validates: Requirements 7.4, 8.7, 11.5**

### Property 12: 默认零影响
新能力未启用时，既有检索行为与既有测试结果保持不变；旧 facts.json 可无损加载。
**Validates: Requirements 12.1, 12.2, 12.3**
