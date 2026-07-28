# Requirements Document

> **Spec B：Evidence Gate（引用支撑校验）**
> 依赖 spec `literature-evidence`（可信文献库）产出的 `Evidence_Span` 与冻结的
> `Evidence_Bundle`。本 spec 只负责「已引用的文献是否支撑对应论断」，
> **不负责检索、去重、配额**（配额闭环由 spec A 的 LiteraturePreflight 与
> CitationQuotaGate 承担）。

## Introduction

现有 Gate 只校验「引用的 key 是否存在于已知文献」——这只能防标识符编造，防不住
**引用与论断不匹配**：一篇真实、切题的论文被用来支撑它其实没说过的结论。本 spec 引入
Evidence Gate：把正文拆成原子 Claim、把 Claim 与其引用的 Evidence 配对、判定支撑关系，
不通过则回灌重试；重试耗尽则拒绝写入。

**能力边界（必须如实声明）**：系统只持有摘要级 Evidence（不做全文 PDF 解析，见 spec A 范围）。
因此本 Gate 最多能保证「**摘要级**证据支持该论断」，**不能**保证论文全文支持，**更不能**
保证该学术结论本身正确。所有面向用户的报告与术语都必须体现这一强度。

**范围边界（不做）**：全文级证据、跨文献的**联合支撑**判定（见 Requirement 3 说明）、
事实核查（claim 是否客观为真）、数值/统计结果的复算、自动改写正文（Gate 只判定与回灌，
改写属写作循环职责）、**在写作循环内嵌套检索 Loop 或原地更换 Evidence_Bundle**
（见 Requirement 7）、判断「某个 Claim 本来是否应该有引用」（属未来的 CitationCoverage Gate）。

**执行前提**：本 spec 必须先以 Shadow 模式运行并通过 Requirement 12 的语义评测门槛，
才可切为 Active（阻塞写入）。

## Glossary

- **Claim**：正文中一条**原子**断言——单一主张、单一强度、可独立判定。
  一句话可拆出多个 Claim。
- **Citation_Group**：某个 Claim 上出现的引用集合。
- **Minimal_Neutral_Context**：为消解指代而附带的最小上下文（Claim 的前后相邻句、
  被引用的论文内定义、Claim 所在小节标题）。**中立**指其中不含写作目标、风格指令、
  「这是你刚写的」等归属提示。
- **Evidence**：来自 spec A 的 `Evidence_Span`。本 Gate **只**消费
  `capability=CLAIM_SUPPORT` 的 Span（即来自 `abstract` / `abstract_section` 的片段）；
  `DISCOVERY_ONLY`（来自 `title`）的 Span 不可作为支撑依据。
- **Evidence_Bundle**：spec A 构造并**冻结**的证据集合，带 `bundle_id` 与
  `task_fingerprint`；写作侧与本 Gate 共用同一实例。
- **Evidence_Summary**：对 Evidence 的压缩表述，**仅作上下文**，不得作为独立证据来源。
- **Verdict**（单个 `(Claim, Reference)` 配对的判定，五态）：
  - `supported`：摘要级 Evidence 明确支持该 Claim 的内容与强度。
  - `insufficient_support`：Evidence 与 Claim **相关**，但推不出该强度的结论
    （如 Evidence 限定「在某数据集上」而 Claim 说「普遍适用」）。
  - `contradicted`：Evidence 与 Claim **方向相反**。
  - `unrelated`：Evidence 讨论的是别的内容，与该 Claim 无关。
  - `unverifiable`：无法判定，必须附 **Unverifiable_Reason**。
- **Unverifiable_Reason**：
  - `NO_EVIDENCE`：该 Reference 无 `CLAIM_SUPPORT` 证据（无摘要，仅有标题）
  - `STALE_EVIDENCE`：Evidence_Span 的 hash 与当前文献记录不一致
  - `REVIEWER_ERROR`：Reviewer 调用失败或响应解析/schema 失败
  - `AMBIGUOUS_MAPPING`：Claim 拆分或引用归属无法确定
  - `CONTEXT_TRUNCATED`：Evidence 因预算被截断，判定所需片段不在上下文中
- **Claim_Status**（四态）：
  - `PASSED`：Citation_Group 中每条引用均为 `supported`
  - `PASSED_WITH_WARNING`：不存在阻塞性 verdict，但存在按 Requirement 8 允许放行的
    `unverifiable` 配对；已放行且必在报告中列出
  - `BLOCKED`：存在 `contradicted` / `unrelated` / `insufficient_support`，
    或存在按 Requirement 8 必须阻塞的 `unverifiable`
  - `NOT_EVALUATED_NO_CITATION`：无引用，本 Gate 不评估
- **Claim_Evidence_Assessment**：`(claim_id, claim_offset, citation_group,
  per_reference_verdicts, claim_status, unverifiable_reasons)`。
- **Revision_Action**：Gate 不通过后允许写作侧采取的动作（见 Requirement 9）。
- **Gate_Outcome**：本次 Gate 执行的整体结论：`PASS` | `PASS_WITH_WARNING` |
  `NEEDS_REVISION` | `NEEDS_RETRIEVAL` | `STALE_BUNDLE`。
- **Gate_Mode**：`off` | `shadow`（只报告不阻塞）| `active`（可阻塞写入）。

## Requirements

### Requirement 1: Claim 拆分与引用归属

**User Story:** 作为论文作者，我希望系统正确理解「哪句话在引用哪几篇文献」，而不是机械地按句切分。

#### Acceptance Criteria

1. THE system SHALL 从候选正文中识别 Claim 及其 Citation_Group。
2. WHERE 一句话包含多个可独立判定的断言，THE system SHALL 拆成多个原子 Claim，
   每个 Claim 只含单一主张与单一强度。
3. WHERE 一个 Claim 引用多篇文献，THE system SHALL 将它们归入同一 Citation_Group。
4. WHERE 一个引用出现在段末且语义上覆盖前面多句，THE system SHALL 将其关联到被覆盖的
   各个 Claim，SHALL NOT 只关联紧邻的一句。
5. THE system SHALL 为无引用的句子标记 `NOT_EVALUATED_NO_CITATION`，
   SHALL NOT 判为 `unverifiable`，SHALL NOT 因此阻塞写入。
6. WHERE Claim 拆分或引用归属无法确定，THE system SHALL 判该配对
   `unverifiable / AMBIGUOUS_MAPPING` 并按 Requirement 8 处理，SHALL NOT 静默跳过。
7. THE system SHALL 为每个 Claim 记录其在候选正文中的定位（offset 区间），
   使回灌能精确指向待修正处。

### Requirement 2: 支撑判定

**User Story:** 作为论文作者，我需要确保「引用 A 支持论断 B」是真的，而不只是引用格式正确。

#### Acceptance Criteria

1. THE Evidence_Gate SHALL 为每个 `(Claim, Reference)` 配对产出五态 Verdict 之一。
2. THE Evidence_Gate SHALL 区分 `insufficient_support` 与 `contradicted`，
   SHALL NOT 用单一「不支持」概念混合「强度不够」与「方向相反」。
3. THE Evidence_Gate SHALL 把「Claim 强度超出 Evidence 支持范围」判为
   `insufficient_support`（如 Evidence 说「在某数据集上有效」而 Claim 说「普遍适用」）。
4. THE Evidence_Gate SHALL 仅依据该 Reference 的 `CLAIM_SUPPORT` 级 Evidence 做判定，
   SHALL NOT 依据模型的先验知识或对该领域的一般印象。
5. THE Evidence_Gate SHALL NOT 把 `DISCOVERY_ONLY` 的 Evidence_Span（文献标题）
   作为支撑判定依据；标题只可作为文献识别信息呈现。
6. WHERE 某 Reference 无 `CLAIM_SUPPORT` 级 Evidence，THE Evidence_Gate SHALL 判
   `unverifiable / NO_EVIDENCE`，SHALL NOT 判 `supported`，
   SHALL NOT 退化为依据标题判定支撑。
7. WHEN 判定为 `unverifiable`，THE Evidence_Gate SHALL 附带 Unverifiable_Reason，
   SHALL NOT 输出无原因的 `unverifiable`。
8. THE Evidence_Gate SHALL 为每个判定给出可追溯依据：所用 Evidence_Span 的 `span_id`
   与 offset 区间，SHALL NOT 只给出结论。

### Requirement 3: 逐引用独立支撑与 Claim 状态

**User Story:** 作为论文作者，我不希望「引用一堆文献，其中只有一篇沾边」也能通过校验；同时我要清楚被警告放行的句子到底算通过还是没通过。

> **设计说明**：真正的「多篇文献联合支撑一个 Claim」需要对支撑面（facet）建模——
> 判断各篇分别覆盖了 Claim 的哪个部分、并集是否覆盖全部。这无法靠若干枚举值的
> 布尔组合算出。第一版**不声称**支持联合支撑，改为「拆原子 Claim + 每条引用必须
> 独立支撑」，并把联合支撑后置。

#### Acceptance Criteria

1. THE Evidence_Gate SHALL 为每个被评估的 Claim 赋予**恰好一个** Claim_Status
   （`PASSED` / `PASSED_WITH_WARNING` / `BLOCKED` / `NOT_EVALUATED_NO_CITATION`）。
2. WHEN Citation_Group 中每一条引用均为 `supported`，THE Evidence_Gate SHALL 判
   `PASSED`。
3. WHEN Citation_Group 中存在任一 `contradicted`、`unrelated` 或
   `insufficient_support` 的配对，THE Evidence_Gate SHALL 判 `BLOCKED`，
   SHALL NOT 因「另有一篇支持」而整体放行。
4. WHEN 存在 `contradicted` 或 `unrelated`，THE Evidence_Gate SHALL 要求移除或替换
   该引用；WHEN 存在 `insufficient_support`，THE Evidence_Gate SHALL 回灌
   「降低断言强度」或「移除该引用」作为可选修正。
5. WHEN 不存在上述三类阻塞性 verdict，AND 存在按 Requirement 8 **必须阻塞**的
   `unverifiable` 配对，THE Evidence_Gate SHALL 判 `BLOCKED`。
6. WHEN 不存在阻塞性 verdict，AND 存在按 Requirement 8 **允许放行**的 `unverifiable`
   配对，THE Evidence_Gate SHALL 判 `PASSED_WITH_WARNING`，
   SHALL NOT 判 `PASSED`，SHALL NOT 判 `BLOCKED`。
7. THE system SHALL 把 `PASSED_WITH_WARNING` 的 Claim 计为不阻塞写入，
   AND SHALL 在用户报告中逐条列出（Claim 定位、文献、Unverifiable_Reason）。
8. THE system SHALL NOT 提供 `any_supports` 之类「至少一篇支持即可」的放行口径。
9. THE system SHALL NOT 在报告或文档中声称已支持「多篇文献联合支撑」。

### Requirement 4: 证据的原始性、能力级别与 Bundle 冻结

**User Story:** 作为论文作者，我不希望"证据"本身是模型生成的，也不希望校验用的证据和写作时看到的不是同一份。

#### Acceptance Criteria

1. THE Evidence_Gate SHALL 以 `capability=CLAIM_SUPPORT` 的 Evidence_Span
   （摘要原文片段）作为判定依据。
2. THE Evidence_Gate SHALL 使用写作侧**同一个**冻结的 Evidence_Bundle 实例
   （同一 `bundle_id`），SHALL NOT 自行重新构造、重新检索或原地更新证据。
3. WHERE 使用 Evidence_Summary 压缩上下文，THE system SHALL 保留其到 Evidence_Span 的
   引用关系，且最终判定 SHALL 可追溯到原始片段。
4. THE system SHALL NOT 将 Evidence_Summary 作为独立证据来源用于判定。
5. WHEN 任一 Evidence_Span 的 `verbatim_hash` 或所属字段的 `content_hash` 与当前文献
   记录不一致，THE Evidence_Gate SHALL 判该配对 `unverifiable / STALE_EVIDENCE`
   AND SHALL 按 Requirement 7.9 结束本次写作尝试，
   SHALL NOT 在 Gate 内部重新取证或更新当前 Bundle。
6. WHERE Evidence 因上下文预算被截断而缺少判定所需片段，THE system SHALL 判
   `unverifiable / CONTEXT_TRUNCATED` 并记录被截断条数，SHALL NOT 判 `supported`。

### Requirement 5: Reviewer 输入的构造

**User Story:** 作为论文作者，我希望校验者能看懂「该方法」「上述结果」指的是什么，但不被我的写作目标带偏。

#### Acceptance Criteria

1. THE Evidence_Gate SHALL 向 Reviewer 提供：Claim 文本、Minimal_Neutral_Context、
   Citation_Group 的文献元数据（标题/年份/类型）、以及对应的 `CLAIM_SUPPORT`
   Evidence_Span。
2. THE system SHALL NOT 只提供孤立 Claim 与 Evidence（孤立 Claim 中的指代词
   与论文内定义无法判定）。
3. THE Minimal_Neutral_Context SHALL 限于：Claim 的前后相邻句、Claim 所在小节标题、
   以及该 Claim 依赖的论文内定义；THE system SHALL 按配置限制其长度。
4. THE system SHALL NOT 在 Reviewer 输入中包含写作目标、润色/改写方向、风格指令、
   用户提出的写作约束。
5. THE system SHALL NOT 在 Reviewer 输入中包含「本文是你刚写的」「请确认你的输出」
   这类使模型倾向自我确认的归属提示。
6. THE system SHALL 按 spec A Requirement 17 的规则包裹与清洗 Evidence 文本：
   其中的**指令性语句不得执行**，其**学术内容应当作为支撑判定的依据**。

### Requirement 6: 生成与校验分离

**User Story:** 作为论文作者，我不信任「模型自己写完自己说没问题」。

#### Acceptance Criteria

1. THE Evidence_Gate SHALL 使用与正文生成**不同的 LLM 调用**，SHALL NOT 让同一次调用
   既生成正文又自评。
2. WHERE 系统配置了独立的 reviewer provider，THE Evidence_Gate SHALL 优先使用之。
3. THE Evidence_Gate SHALL 通过 function calling 获取结构化判定结果，
   并复用现有统一 LLM 层的 `forced_tool_name` 机制，SHALL NOT 自建协议适配。
4. WHEN Reviewer 响应缺少 tool call 或参数不是对象，THE system SHALL 判
   `unverifiable / REVIEWER_ERROR` 并区分 `parse_failed` 与 `schema_failed`。

### Requirement 7: 不通过时的处理、预算共享与检索解耦

**User Story:** 作为论文作者，我希望校验不通过时系统去修正，但不要没完没了地重试，也不要在写一半的时候突然停下来问我要不要检索文献。

> **设计说明**：现有 `runtime/handlers.py` 与 `PlanExecutor` 是同步执行模型，
> 只有 `pending → running → succeeded/failed`，**没有 WAITING_USER → RESUME 这类
> 可持久化的挂起态**。若允许 Gate 在写作循环中途触发 spec A 的「含用户确认」检索流程，
> 就要求在 Handler 内部挂起等人再恢复，这是本轮最大的复杂度来源。第一版据此解耦：
> 补文献一律前置到 spec A 的 LiteraturePreflight，Gate 只负责判定与「本轮不做」。

#### Acceptance Criteria

1. WHEN 存在 `Claim_Status=BLOCKED` 的 Claim，THE Evidence_Gate SHALL 输出
   `Gate_Outcome=NEEDS_REVISION`，判定本次生成不通过。
2. WHEN 判定不通过，THE Evidence_Gate SHALL 回灌具体的
   `(claim_offset, reference_id, verdict, unverifiable_reason, 依据 span_id)`
   供写作侧修正，SHALL NOT 只回灌「校验未通过」。
3. THE Evidence_Gate SHALL 与写作循环**共用同一重试预算**（复用理解层的
   `PlanningBudget` 机制约束 LLM 调用次数与累计 provider 耗时），
   SHALL NOT 拥有独立的无限重试。
4. WHEN 重试预算耗尽且仍不通过，THE system SHALL 拒绝写入 Working_Copy 并向用户报告
   未通过的配对。
5. THE system SHALL 依赖 spec A 的 LiteraturePreflight 在写作**开始前**完成文献预检与
   必要的补充检索（含用户确认），并据此构造冻结的 Evidence_Bundle。
6. WHEN Gate 判定「现有 Bundle 的证据不足以支撑本次写作」，THE Evidence_Gate SHALL
   输出 `Gate_Outcome=NEEDS_RETRIEVAL` 并把本次写作操作标记为**未完成**，
   SHALL NOT 在写作循环内启动检索 Loop，SHALL NOT 在 Handler 内部挂起等待用户确认。
7. WHEN 输出 `NEEDS_RETRIEVAL`，THE system SHALL 向用户说明缺口并建议下一轮先执行
   `search_literature`，且 Working_Copy SHALL 逐字节不变。
8. THE system SHALL NOT 在写作循环内把未经用户确认提交的文献计入可引用集合。
9. WHEN 检测到 `STALE_EVIDENCE`，THE Evidence_Gate SHALL 输出
   `Gate_Outcome=STALE_BUNDLE`：本次写作尝试判为失败、Working_Copy 逐字节不变、
   由编排器**重新构造新的 Evidence_Bundle**（新 `bundle_id`）并作为**一次新的
   写作尝试**重新开始，SHALL NOT 在当前尝试内替换 Bundle 后继续。
10. THE system SHALL 把因 `STALE_BUNDLE` 触发的重建尝试计入同一 Turn 的重试预算，
    并设置独立的最大重建次数上限，SHALL NOT 允许无限重建。

### Requirement 8: unverifiable 的分原因放行策略

**User Story:** 作为论文作者，我能接受「这篇没摘要所以判不了」被放行加个警告，但不能接受「校验器崩了」也算通过。

#### Acceptance Criteria

1. THE system SHALL 按 Unverifiable_Reason 分别决定处置，SHALL NOT 对全部
   `unverifiable` 采用单一策略。
2. WHERE reason 为 `NO_EVIDENCE`，THE system SHALL 按配置决定「阻塞」或
   「放行并标注证据不足」，默认为放行；放行时该 Claim 判 `PASSED_WITH_WARNING`
   并必在报告中列出。
3. WHERE reason 为 `REVIEWER_ERROR`，THE system SHALL **始终**阻塞（fail-closed），
   SHALL NOT 因调用失败而放行。
4. WHERE reason 为 `STALE_EVIDENCE`，THE system SHALL 按 Requirement 7.9 结束本次
   写作尝试并重建 Bundle，SHALL NOT 放行，SHALL NOT 在 Gate 内原地重取证据。
5. WHERE reason 为 `CONTEXT_TRUNCATED`，THE system SHALL 在预算允许时以提高该
   Reference 证据配额的方式重建 Bundle 并重新尝试；预算不足时按 `NO_EVIDENCE`
   的配置处理并标注真实原因为 `CONTEXT_TRUNCATED`。
6. WHERE reason 为 `AMBIGUOUS_MAPPING`，THE system SHALL 默认阻塞并在报告中
   列出歧义的 Claim 与候选归属。
7. THE system SHALL NOT 静默放行任何 `unverifiable` 配对——放行必表现为
   `PASSED_WITH_WARNING` 且必在报告中出现。

### Requirement 9: 允许与禁止的修正动作

**User Story:** 作为论文作者，我希望系统修正引用问题的方式是诚实的。

#### Acceptance Criteria

1. THE system SHALL 允许的 Revision_Action 限于：删除不相关或矛盾的引用、
   替换为**当前 Bundle 内**已核验且相关的文献、降低 Claim 的断言强度、
   删除无法支撑的 Claim。
2. THE system SHALL NOT 修改 Evidence 内容以迎合 Claim。
3. THE system SHALL NOT 编造新的事实或新的引用以通过校验。
4. THE system SHALL NOT 把「触发补充检索」作为写作循环内的 Revision_Action
   （按 Requirement 7.6，证据不足时输出 `NEEDS_RETRIEVAL` 并结束本轮）。
5. THE system SHALL NOT 通过「删掉引用使 Claim 变成无引用句子」来规避校验；
   WHEN 修订后某 Claim 由有引用变为无引用，THE system SHALL 在报告中列出该 Claim
   作为「疑似需要引用但已无引用」。
6. THE system SHALL 在报告中说明本次采取了哪些 Revision_Action。

### Requirement 10: 强度声明与用户报告

**User Story:** 作为论文作者，我需要清楚系统的校验到底保证了什么、放过了什么。

#### Acceptance Criteria

1. THE system SHALL 在校验报告中明确本次为**摘要级**支撑校验。
2. THE system SHALL NOT 表述或暗示已保证论文全文支持该 Claim，或该 Claim 客观为真。
3. THE system SHALL 报告五态 verdict 的分布、四态 Claim_Status 的分布，
   以及 `unverifiable` 按 Unverifiable_Reason 的细分计数。
4. THE system SHALL 逐条列出 `PASSED_WITH_WARNING` 的 Claim（定位、文献、原因），
   SHALL NOT 把它们与 `PASSED` 合并统计。
5. THE system SHALL 在报告中列出 `NOT_EVALUATED_NO_CITATION` 的 Claim 中
   **疑似需要引用**的部分（如包含比较、数值结论、他人工作描述），
   并说明本 Gate 未对其评估、该职责属未来的 CitationCoverage Gate，
   SHALL NOT 把「无引用」表述为「无需校验」。
6. THE system SHALL NOT 声称已保证正文中所有该有引用的地方都有引用。
7. WHERE 本次执行发生过 `STALE_BUNDLE` 重建，THE system SHALL 在报告中说明重建次数
   与原因。

### Requirement 11: 可观测与隐私

**User Story:** 作为开发者，我需要在校验出问题时看清判定过程。

#### Acceptance Criteria

1. THE system SHALL 为 Claim 抽取、配对、判定、修订、重试、Bundle 重建各阶段发布
   trace 事件。
2. THE system SHALL 在 trace 中记录 Claim_Evidence_Assessment 的
   `claim_hash`、`claim_offset`、`reference_id`、`verdict`、`unverifiable_reason`、
   `span_id`、`claim_status`、`bundle_id`，SHALL NOT 落 claim 原文或 Evidence 原文。
3. WHEN 判定调用失败，THE system SHALL 区分失败类型（provider_error / timeout /
   parse_failed / schema_failed）并记录，SHALL NOT 把调用失败当作 `supported`。
4. THE system SHALL 记录 Reviewer 的调用次数、累计耗时与被截断的证据条数，
   供 Requirement 12 的评测使用。

### Requirement 12: Active 前的语义评测门槛

**User Story:** 作为使用者，我不希望一个会拦住我写作的语义判定器，只靠单元测试就上线。

#### Acceptance Criteria

1. THE Evidence_Gate SHALL 支持 Gate_Mode ∈ `off | shadow | active`，默认 `shadow`。
2. WHERE Gate_Mode 为 `shadow`，THE system SHALL 完整执行判定并产出报告与 trace，
   SHALL NOT 阻塞写入、SHALL NOT 触发修订重试、SHALL NOT 触发 Bundle 重建。
3. THE system SHALL 提供基于人工标注集的评测，至少度量：
   - Claim 拆分准确率与引用归属准确率
   - **错误放行率**（应 BLOCKED 而判 PASSED 或 PASSED_WITH_WARNING）
   - **错误阻塞率**（应通过而判 BLOCKED）
   - relevance 判定准确率（与 spec A 的筛选口径一致性）
   - 语言分类准确率（供 spec A 配额复核）
   - Reviewer 的调用次数、累计耗时与单次写作的额外成本
4. THE system SHALL NOT 把 Gate_Mode 默认切为 `active`，直至上述指标达到文档化阈值。
5. THE system SHALL 在评测报告中区分**架构不变量**（如「调用失败绝不判 supported」，
   由代码保证、恒定）与**模型能力指标**（有采样波动），
   SHALL NOT 用单次跑分对模型能力指标下结论。
6. THE system SHALL 通过 spec A Requirement 18.5 的**受控评测导出器**产出可人工标注的
   样本：按 `claim_offset` 从候选正文、按 `span_id` 从项目数据还原原文，
   输出到受权限保护的位置。
7. THE system SHALL NOT 依赖仅含 `claim_hash` / `span_id` 的持久化 trace 作为人工标注
   材料（标注者无法据哈希判断支撑关系）。
8. THE system SHALL NOT 把还原出的 claim 原文或 Evidence 原文写入普通运行日志或
   持久化 trace。

### Requirement 13: 可关闭与向后兼容

**User Story:** 作为现有用户，我希望能在需要时关掉这层校验。

#### Acceptance Criteria

1. WHERE Gate_Mode 为 `off`，THE system SHALL 退化为现有的引用存在性校验，
   且行为与现状逐字节一致。
2. THE system SHALL 保证既有测试全部通过。
3. WHERE 文献无 `CLAIM_SUPPORT` 级 Evidence（如迁移而来的 `legacy_verified` 记录），
   THE system SHALL 按 Requirement 8.2 处理，
   SHALL NOT 因缺少 Evidence 而阻断既有写作流程。
4. THE system SHALL 按以下阶段交付：**B1** Shadow 模式（判定 + 报告 + trace + 评测）；
   **B2** Active 模式接入有界 Writer–Reviewer 修订循环。
   THE system SHALL NOT 在 spec A 的 A1–A3 全部就绪前进入 B2。

## Correctness Properties

### Property 1: 判定依据性
每个非 `unverifiable` 的 verdict 必可追溯到具体 Reference 的 `CLAIM_SUPPORT`
Evidence_Span（`span_id` + offset）；不存在仅凭模型先验知识得出的 `supported`。
**Validates: Requirements 2.4, 2.8, 4.1, 4.3**

### Property 2: 证据能力隔离
`DISCOVERY_ONLY`（标题）片段不出现在任何支撑判定的依据链上；仅有标题可用的 Reference
一律判 `unverifiable / NO_EVIDENCE`，不存在「靠标题判 supported」的路径。
**Validates: Requirements 2.5, 2.6**

### Property 3: 无证据不放行
无 `CLAIM_SUPPORT` 证据、证据陈旧、证据被截断、Reviewer 调用或解析失败的配对，
一律判 `unverifiable` 并附对应 reason，绝不判 `supported`。
**Validates: Requirements 2.6, 2.7, 4.5, 4.6, 6.4, 11.3**

### Property 4: 失败一律 fail-closed
`REVIEWER_ERROR` 一律阻塞；`STALE_EVIDENCE` 一律结束本次尝试并重建 Bundle；
不存在因调用失败或证据失效而放行的路径。
**Validates: Requirements 8.3, 8.4**

### Property 5: Claim 状态完备且互斥
每个 Claim 恰好一个 Claim_Status；`PASSED` 当且仅当每条引用均为 `supported`；
存在 `contradicted` / `unrelated` / `insufficient_support` 或必须阻塞的
`unverifiable` 时必为 `BLOCKED`；仅存在允许放行的 `unverifiable` 时必为
`PASSED_WITH_WARNING`（既不是 `PASSED` 也不是 `BLOCKED`）。
**Validates: Requirements 3.1, 3.2, 3.3, 3.5, 3.6**

### Property 6: 无联合放行口径
不存在「另有一篇支持即整体放行」的路径；系统不提供 `any_supports` 口径，
也不声称支持多篇联合支撑。
**Validates: Requirements 3.3, 3.8, 3.9**

### Property 7: verdict 语义不混合
`insufficient_support` 与 `contradicted` 互不替代；每个 `unverifiable` 均带
Unverifiable_Reason，且报告中的原因等于实际原因（`CONTEXT_TRUNCATED` 不被记为
`NO_EVIDENCE`）。
**Validates: Requirements 2.2, 2.3, 2.7, 8.5**

### Property 8: 放行必留痕
任何被放行的 `unverifiable` 配对必表现为 `PASSED_WITH_WARNING` 并逐条出现在用户报告中；
`PASSED_WITH_WARNING` 不与 `PASSED` 合并统计；不存在静默放行。
**Validates: Requirements 3.7, 8.2, 8.7, 10.4**

### Property 9: 生成校验分离
Gate 的判定调用与正文生成调用相互独立，不共享同一次 LLM 响应；判定输入不含写作目标、
风格指令与自我归属提示。
**Validates: Requirements 6.1, 5.4, 5.5**

### Property 10: Reviewer 输入充分且中立
每次判定输入均含 Claim、Minimal_Neutral_Context、Citation_Group 元数据与
`CLAIM_SUPPORT` Evidence_Span 四者；Context 长度受限且不含写作意图。
**Validates: Requirements 5.1, 5.2, 5.3**

### Property 11: 外部文本可判读但不越权
Evidence 中的指令性语句不被执行；其学术内容被用作支撑判定依据；
Gate 的判定流程与预算不受 Evidence 内容控制。
**Validates: Requirement 5.6**

### Property 12: 证据同一且不可原地更换
一次写作尝试内 Gate 与写作侧引用同一 `bundle_id` 的 Bundle；Evidence 内容不被修改；
Evidence_Summary 绝不作为唯一依据；Bundle 内容变化必对应新 `bundle_id` 与新的写作尝试。
**Validates: Requirements 4.2, 4.4, 7.9, 9.2**

### Property 13: 无嵌套 Loop
Gate 在写作循环内不启动检索 Loop、不产生用户提问、不挂起等待确认；证据不足时输出
`NEEDS_RETRIEVAL` 并结束本轮，Working_Copy 逐字节不变。
**Validates: Requirements 7.6, 7.7, 7.8, 9.4**

### Property 14: 重试与重建均有界且共享预算
Gate 的修订重试与 `STALE_BUNDLE` 重建次数受写作循环同一预算约束，且重建有独立上限；
预算耗尽时拒绝写入而非放行。
**Validates: Requirements 7.3, 7.4, 7.10**

### Property 15: 失败不产半成品
Gate 输出 `NEEDS_REVISION`（重试耗尽）、`NEEDS_RETRIEVAL` 或 `STALE_BUNDLE` 时，
Working_Copy 逐字节不变。
**Validates: Requirements 7.4, 7.7, 7.9**

### Property 16: 修正诚实性
所有修正动作均属允许集合且替换引用只能取自当前 Bundle；不存在为通过校验而新增的
未核验引用；不存在通过删除引用把 Claim 变成无引用句子来规避校验且未被报告的情形。
**Validates: Requirements 9.1, 9.3, 9.5**

### Property 17: 无引用不等于无需校验
无引用 Claim 状态为 `NOT_EVALUATED_NO_CITATION`；报告中列出其中疑似需要引用的部分；
系统不声称已保证引用覆盖完整。
**Validates: Requirements 1.5, 10.5, 10.6**

### Property 18: 强度不夸大
系统报告中不出现「已保证全文支持」「已保证结论正确」「支持多篇联合支撑」的表述；
始终标明为摘要级。
**Validates: Requirements 3.9, 10.1, 10.2**

### Property 19: 隐私边界与受控导出
持久化 trace 与普通日志中不出现 claim 原文与 Evidence 原文，仅出现哈希、offset、
标识符、verdict 与统计量；含原文的标注样本只经受控评测导出器产出到受保护位置。
**Validates: Requirements 11.2, 12.6, 12.8**

### Property 20: 标注材料可判读
受控导出的评测样本包含还原后的 Claim 与 Evidence 原文，使人工标注者可独立判断支撑关系；
不存在仅凭哈希要求标注的流程。
**Validates: Requirements 12.6, 12.7**

### Property 21: Shadow 不影响产物
Gate_Mode 为 `shadow` 时，写入结果与 `off` 时逐字节一致，仅多出报告与 trace；
不发生修订重试与 Bundle 重建。
**Validates: Requirements 12.1, 12.2**

### Property 22: 默认零影响
Gate_Mode 为 `off` 时行为与现状一致，既有测试全部通过；无 `CLAIM_SUPPORT` Evidence 的
legacy 记录不阻断既有流程。
**Validates: Requirements 13.1, 13.2, 13.3**
