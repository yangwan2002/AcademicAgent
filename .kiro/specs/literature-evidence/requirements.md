# Requirements Document

> **Spec A：可信文献库 + 配额驱动检索 Loop + 引用配额闭环**
> Evidence Gate（引用与论断的语义支撑校验）已拆分为独立 spec `evidence-gate`，
> 消费本 spec 产出的 `EvidenceBundle`。本 spec 只保证「文献真实、去重、切题、
> 配额诚实（含正文实际引用数）、条目可用」，**不判断引用是否支撑某句论断**。

## Introduction

把文献能力从「只保证标识符存在」升级为「**可信文献库**」：完整元数据与作品级多标识符、
基于作品级标识符的去重、任务相关性筛选、配额驱动的**有界检索 Agent Loop**、暂存批次与
原子提交、**写作前文献预检（LiteraturePreflight）**、**正文引用配额的最终校验
（CitationQuotaGate）**、可用的参考文献条目（多引文格式），以及三个职责分明的文献意图。

现状缺口（审计 P0#6）：`KnownRef` 只有 id/bibkey/title/verified，丢失 DOI/作者/年份/摘要；
`bibkeys()` 不过滤 `verified=False`；检索不足时不补检索；写作 prompt 只拿到
`- bibkey: title` 一行；`fix_bibliography` 只写 `\bibitem{key} title.`；去重不用 DOI；
配额从解析到执行全程断链。

**关键事实约束（来自公开研究）**：OpenAlex 对中文期刊的基础字段较完整，但著录细节、
作者机构、参考文献常缺失；且其 `language` 字段本身**不总准确**，会高估英文、低估其他
语言（见 [OpenAlex 中文学术数据研究 2026](https://link.springer.com/article/10.1007/s11192-026-05664-4)、
[OpenAlex 语言覆盖评估](https://arxiv.org/abs/2409.10633)；内容为合规做了转述）。
因此语言判定**不得只依赖检索源字段**，且中文配额必须允许「不可保证」的诚实出口。

**范围边界（不做）**：全文 PDF 解析、BibTeX/EndNote 导入、**用户手动补充文献**、
**跨项目/跨用户文献共享**、CNKI/万方等无公开 API 源的接入、引用与论断的语义支撑校验（→ spec B）、
**Writer Loop 内嵌套的自动补充检索**（见 Requirement 19 分期）。

## Glossary

### 文献与标识符

- **Reference** / **ReferenceRecord**：一条文献记录，**唯一可写、唯一持久化**的文献实体。
- **RetrievalCandidate**：检索源返回的候选，**非持久化** DTO。它尚无身份
  （无 `reference_id`、无核验事件、无引用别名），只有经 Ledger 入库才成为 ReferenceRecord。
- **KnownRef**：面向既有写作/Gate 代码的**只读兼容投影**，迁移完成后移除。
- **reference_id**：Reference 的**内部稳定主键**，与外部标识符和 bibkey 均无关，一旦分配不变。
- **bibkey**：面向 `\cite{}` 的**渲染用键**，可因格式/冲突而重新分配，**不得**用作主键。
- **Work_Identifiers**：标识**这一篇作品本身**的标识符映射 `{类型: 值}`，
  类型 ∈ `doi | arxiv | openalex_work | semantic_scholar_paper | pmid`。
  **只有这类标识符可触发去重。**
- **Container_Identifiers**：标识**载体**（期刊/丛书/图书）的标识符映射，
  类型 ∈ `issn | eissn | isbn`。同一期刊的不同论文、同一本书的不同章节会共享这些值，
  因此 **SHALL NOT** 用于作品级去重。
- **Verification_Record**：一次核验尝试的不可变记录：
  `(event_id, source, provenance, identifier_type, identifier, outcome, checked_at, details)`，
  outcome ∈ `verified | conflict | not_found | provider_error`。
- **Verification_Event**：Verification_Record 的超集——除上述核验尝试外，还包括状态变迁
  事件 `conflict_resolved | quarantined | released`，后者带 `target_event_id` 指向被作用的
  事件。全部事件构成**只追加**的序列 `verification_events`，Verification_Status 由该序列
  折叠而来。**不使用可变布尔字段表达变迁**（见 Requirement 2.2）。
- **Verification_Status**：由全部 Verification_Record **聚合**出的文献状态：
  `UNVERIFIED | VERIFIED | CONFLICT | QUARANTINED`。
  provider 超时/不可用**只**产生 `provider_error` 记录，**绝不**成为文献状态。
- **legacy_verified**：旧 `KnownRef(verified=True)` 迁移而来、无核验记录支撑的兼容标记。

### 任务与相关性

- **Task_Fingerprint**：一次检索或写作任务的稳定指纹，由任务主题、目标章节、
  归一化查询意图派生。用于判断某个 Relevance_Assessment 能否被复用。
- **Relevance_Assessment**：`(reference_id, task_fingerprint, verdict, basis, assessed_at)`，
  verdict ∈ `relevant | weakly_relevant | unrelated`。
  **相关性是 (文献, 任务) 的函数，不是文献的固有属性**——同一篇论文对「深度学习医学诊断」
  相关、对「供应链风险管理」无关。
- **Language_Verdict**：`verdict ∈ zh | non_zh | unknown` + `confidence ∈ high | low`
  + `basis`（source_field / script_analysis / source_default）。
- **Language_Counting_Policy**：`high_only`（仅 high confidence 计入语言配额，严格模式默认）
  | `include_low_with_warning`（low 也计入，但必须在报告中标注）。

### 配额

- **Eligibility_Filter**：**硬过滤**条件，决定某 Reference 是否**有资格**被计数：
  `(field, operator, value)`，field ∈ `year | reference_type | language | topic`。
- **Count_Constraint**：**计数**要求：`(dimension, operator, value)`，
  dimension ∈ `total | language.zh | language.non_zh`，operator ∈ `exact | min | max | range`。
- **Quota_Spec**：`(eligibility_filters, count_constraints, scope)` 三元组。
  「最近 5 年至少 10 篇期刊论文」= filters `[year gte Y-4, reference_type in [journal_article]]`
  + constraints `[total min 10]`。**filter 与 constraint 不得混在同一结构里表达**。
- **Retrieval_Quota**：Quota_Spec 的一种，要求**选入候选集**多少篇。
- **Citation_Quota**：Quota_Spec 的一种，要求**正文实际引用**多少篇。二者不可混用。
- **Acquisition_Gap**：Citation_Quota 与「当前 Ledger 中对本任务可用的文献」之间的差额；
  是 LiteraturePreflight 派生内部 Retrieval_Quota 的依据。
- **Quota_Scope**：`new_only`（仅本批新增）| `ledger_total`（可复用库中已有）|
  `actually_cited`（正文实际引用的不同文献数）。
- **Quota_Gap**：逐维度未满足的差额 `(dimension, required, operator, achieved, gap)`。

### 批次与账本

- **Batch_Item**：Retrieval_Batch 的**唯一事实源**元素：
  `(candidate_snapshot, disposition, target_reference_id, relevance_assessment,
  rejection_reason)`。`candidate_snapshot` 持有候选的完整数据，使未入库的候选也可完整呈现。
- **Batch_Disposition**：单一枚举，四值互斥：
  - `ACCEPT_NEW`：Ledger 中不存在，本批新接受
  - `REUSE_EXISTING`：命中 Ledger 已有记录，元数据无需变更，仅复用计数
  - `MERGE_DUPLICATE`：命中 Ledger 已有记录或本批内已有条目，需要合并元数据
  - `REJECT`：未通过核验、Eligibility_Filter 或 relevance
- **Retrieval_Batch**：一次检索会话的**暂存容器**（`running | staged | confirmed |
  rejected | committed`），内含 `items: list[Batch_Item]`。用户确认前，结果只存在于此，
  **不进 Ledger**。
- **Retrieval_Attempt**：Batch 内一次「查询 → 候选 → 核验 → 去重 → 相关性判定」迭代。
- **Retrieval_Loop**：为满足 Retrieval_Quota 进行的有界自主循环。
- **Reference_Ledger**：**项目级**文献账本，持久化于 project facts（`project_facts_path`），
  同一论文的多个会话共享；**不跨项目、不跨用户**。
- **Provider_Role**：检索源的能力角色，一个 provider 可兼具多个：
  - `SEARCH`：按主题/关键词/作者/年份检索，返回候选与摘要，用于系统性搜索与初筛
    （如 OpenAlex、Semantic Scholar、arXiv）
  - `VERIFY`：按标识符**或书目文本**反查权威著录，可产生 `verified` 记录
    （如 Crossref、PubMed、arXiv、OpenAlex）
  - `DISCOVER`：只能发现候选，不能产生 `verified` 记录（如网页搜索）
- **Metadata_Provenance**：某条元数据的来源层级，决定字段冲突时谁说了算：
  `registry`（DOI/标识符注册机构，如 Crossref、DataCite）>
  `publisher`（出版方官网/平台）>
  `aggregator`（聚合库，如 OpenAlex、Semantic Scholar）>
  `web`（网页、二手站点）。
- **Discovery_Source**：`Provider_Role` 含 `DISCOVER` 且不含 `VERIFY` 的检索源（如网页搜索、
  LLM 联网检索）。其返回物通常是**自由文本**：有标题与大致出处，但无 DOI、无结构化
  元数据，且**候选本身可能不存在**（模型幻觉）。
- **Identifier_Resolution**：把 Discovery_Source 的自由文本候选升级为可核验记录的过程：
  「标题 + 作者 + 年份 → 权威源反查 → 取得 work identifier → 走常规核验」。
  这是 Discovery_Source 的**唯一**可信化路径。
- **Unresolved_Lead**：经 Identifier_Resolution 仍未取得 work identifier 的
  Discovery_Source 候选。可作为线索呈现给用户，但不入可引用集合、不计入任何配额。

### 证据

- **Evidence_Capability**：Evidence_Span 的可用范围：
  - `DISCOVERY_ONLY`：来自 `title` 的片段。只可用于文献发现、相关性初筛、
    无摘要时的有限提示，**不可**作为 Claim 支撑证据。
  - `CLAIM_SUPPORT`：来自 `abstract` / `abstract_section:<name>` 的片段，可作为支撑证据。
- **Evidence_Span**：Reference 某个文本字段中的一段原文引用，跨 spec 契约见 Requirement 12。
- **Evidence_Bundle**：交付给写作与校验的**不可变**证据集合，一次写作尝试全程冻结。
- **Untrusted_External_Text**：来自检索源的标题、摘要、venue、网页内容等，
  一律视为**不可信输入**（见 Requirement 17）。

## Requirements

### Requirement 1: Reference 数据模型、稳定主键与著录字段

**User Story:** 作为论文作者，我希望系统保存文献的完整元数据与全部标识符，以便生成规范条目并可靠去重。

#### Acceptance Criteria

1. THE system SHALL 以 `ReferenceRecord` 为**唯一可写、唯一持久化**的文献实体；
   检索源返回的候选 SHALL 以**非持久化** DTO（`RetrievalCandidate`）表达；
   `KnownRef` SHALL 降级为只读兼容投影。
   THE system SHALL NOT 存在第二个可写的文献真相源
   （类型数量不是约束，唯一可写真相源才是）。
2. THE system SHALL 为每条 Reference 分配 `reference_id` 作为内部稳定主键；
   THE system SHALL NOT 使用 `bibkey` 或任何外部标识符作为主键。
3. WHEN `bibkey` 因冲突或引文格式变化而重新分配，THE system SHALL 保持 `reference_id`
   及其全部关联（Evidence_Span、Batch_Item、Relevance_Assessment、Trace）不变。
4. THE system SHALL 把标识符分为两个**独立**字段存放：`work_identifiers`
   （doi / arxiv / openalex_work / semantic_scholar_paper / pmid）与
   `container_identifiers`（issn / eissn / isbn），
   SHALL NOT 把二者混入同一个 `identifiers` 映射。
5. THE system SHALL 从 `work_identifiers` 中选定 `canonical_identifier`
   （按配置的类型优先级），SHALL NOT 从 `container_identifiers` 中选定。
6. THE system SHALL 保存核心元数据：`work_identifiers`、`container_identifiers`、
   `canonical_identifier`、`title`、`authors`、`year`、`publication_date`、
   `language_verdict`、`abstract`、`evidence_spans`、`discovery_source`、
   `field_provenance`、`verification_events`、`verification_status`。
7. THE system SHALL 为每条 Reference 维护**按文档格式分类的引用别名**：
   `.tex` 使用 `bibkey` 及其历史别名；`.md` / `.docx` 使用**数字别名**。
   WHEN 新建 Reference（含经检索新入库者），THE system SHALL 分配数字别名，
   SHALL NOT 使数字别名仅存在于迁移而来的旧记录上。
8. THE system SHALL 记录**字段级来源**（哪个字段的当前取值来自哪个源），
   SHALL NOT 仅以整条记录的 `discovery_source` 表示全部字段的来源
   （否则 Requirement 11.19 的字段冲突裁决无法实现）。
9. THE system SHALL 保存**著录字段**以支撑完整引文格式：`reference_type`
   （journal_article / conference_paper / book / book_chapter / thesis / preprint /
   report / standard / web_page）、`container_title`、`volume`、`issue`、
   `pages` 或 `article_number`、`publisher`、`publisher_place`、`edition`、
   `url`、`accessed_at`、`conference_name`、`conference_place`。
10. WHERE 检索源未提供某字段，THE system SHALL 留空该字段，SHALL NOT 用占位值、
    默认值或模型推测值填充。
11. THE system SHALL 分别标记 `bibliographic_complete`（目标引文格式所需字段是否齐全）
    与 `evidence_available`（是否有可用于 Claim 支撑的摘要），SHALL NOT 用单一
    `metadata_complete` 混合二者。
12. WHEN `bibliographic_complete=false`，THE system SHALL 在生成条目时标注该条目
    信息不全并逐项列出缺失字段。
13. THE system SHALL 把 Ledger 持久化至 project facts；WHEN 加载，THE system SHALL 与
    已有记录**合并**，SHALL NOT 覆盖。
14. THE system SHALL NOT 在 Reference 上存储任何与具体任务相关的判定结论
    （relevance 属 Requirement 7 的独立结构）。

### Requirement 2: 核验记录与聚合状态

**User Story:** 作为论文作者，我需要知道每条文献是「怎么被核验的」，而不只是一个 true/false。

#### Acceptance Criteria

1. THE system SHALL 为每次核验尝试追加一条不可变 Verification_Record，
   SHALL NOT 用新结果原地覆盖旧记录。
2. THE system SHALL 以**追加事件**表达状态变迁（冲突消解、隔离、释放），
   每个变迁事件 SHALL 指向其作用的目标事件；THE system SHALL NOT 用可变布尔字段
   （如 `resolved` / `quarantined`）表达这些变迁——那会与「记录不可变」直接矛盾，
   且使状态失去审计来源。
3. THE system SHALL 按**因果引用**而非时间戳先后折叠变迁事件：
   `conflict_resolved` SHALL 只能指向 `conflict` 事件；
   `released` SHALL 只能指向 `quarantined` 事件；
   隔离状态成立当且仅当**存在未被有效 `released` 指向的 `quarantined` 事件**。
   THE system SHALL NOT 采用「同类事件按时序后者胜」的规则
   （跨源合并会产生相同或乱序时间戳）。
4. WHEN 某变迁事件的 `target_event_id` 不存在或类型不匹配，THE system SHALL 拒绝
   写入或忽略该事件并在报告中标明，SHALL NOT 静默应用之。
5. THE system SHALL 按全部 Verification_Event 派生 Verification_Status：
   存在最终核验源 `verified` 且无未消解 `conflict` → `VERIFIED`；
   存在最终核验源 `conflict` → `CONFLICT`；
   存在未被释放的 `quarantined` → `QUARANTINED`；
   其余 → `UNVERIFIED`。
6. THE system SHALL 把 provider 超时、限流、连接失败记为 `outcome=provider_error`
   的 Verification_Record，SHALL NOT 使其成为 Verification_Status 的取值，
   SHALL NOT 因此把文献判为「不存在」。
7. THE system SHALL 区分「标识符查无此文」（`not_found`）与「核验调用失败」
   （`provider_error`），并使该区分**自 HTTP 层起**以不同异常类型一路保留到
   Verification_Record，SHALL NOT 在中间层把二者折叠为同一个布尔或空值。
8. WHERE 某 Reference 只有 `provider_error` 记录，THE system SHALL 保持
   `Verification_Status=UNVERIFIED` 并在报告中说明「核验未完成，原因为检索源不可用」。
9. THE system SHALL 仅依据 `work_identifiers` 回查核验，SHALL NOT 依据
   `container_identifiers` 产生 `verified` 记录；且该次回查 SHALL 受
   Requirement 11.25 的**标识符类型级**核验权限约束。
10. WHEN 新增的最终核验源记录与已有 `VERIFIED` 状态**冲突**（作品级标识符指向不同论文、
    年份或标题实质不一致），THE system SHALL 把 Verification_Status 降级为
    `CONFLICT` 或 `QUARANTINED`，SHALL NOT 以「verified 更强」为由保留 `VERIFIED`。
11. THE system SHALL 记录每条 Reference 的 `discovery_sources`（**可多个**，
    多源合并后不得只保留一个）与各次核验源（二者可不同）。
12. THE system SHALL NOT 因某条 Reference 由 Discovery_Source 发现而授予 `VERIFIED`；
    Discovery_Source 的结果必须经最终核验源回查才能升级状态。

### Requirement 3: 基于作品级标识符的去重与元数据合并

**User Story:** 作为论文作者，我不希望同一篇论文因经不同检索源找到而重复出现，更不希望同一期刊的不同论文被错误合并成一条。

#### Acceptance Criteria

1. THE system SHALL 归一化 `work_identifiers` 后比对（DOI 转小写并去 URL 前缀、
   arXiv id 去版本号）。
2. WHERE 两条 Reference 的 `work_identifiers` 存在**任一类型**的同值命中，
   THE system SHALL 视为同一篇并合并，SHALL NOT 分配第二个 `reference_id`。
3. THE system SHALL NOT 以 `container_identifiers`（ISSN / eISSN / ISBN）的同值
   作为去重依据；THE system SHALL NOT 因两条记录同属一个期刊或一本书而合并之。
4. WHERE 双方均无可比 `work_identifiers`，THE system SHALL 退化为「标题归一化 + 年份
   + 首作者姓氏」比对并产出 `dedup_confidence=low` 的**去重建议**。
5. THE system SHALL NOT 依据 `dedup_confidence=low` 的匹配**自动合并**记录，
   AND SHALL 默认关闭低置信自动合并（中文姓名、团体作者、常见标题均易误判，
   且系统未提供拆分已合并记录的能力，故不得声称该合并可撤销）；
   THE system SHALL 把低置信匹配作为建议呈现，由用户或后续流程处置。
6. WHEN 合并两条记录，THE system SHALL 合并双方 `work_identifiers`、
   `container_identifiers` 与 `verification_events` 全集，并按 Requirement 2.2
   **重新派生** Verification_Status，SHALL NOT 直接取「较强的一方」。
7. WHEN 合并出现**非空著录字段冲突**（如两个不同年份），THE system SHALL 按
   Metadata_Provenance 层级裁决（见 Requirement 11.19）并记录冲突，
   SHALL NOT 静默取任一方。
8. WHEN 合并保留了被丢弃的 `bibkey`、数字别名或 `reference_id`，THE system SHALL 把它们
   登记为该记录的别名并**建立可查索引**，使正文中已有的旧标记仍能解析。
9. WHEN 某个引用标记在 Ledger 中命中**多条**记录（迁移遗留的重复 bibkey / 数字别名 /
   旧 id），THE system SHALL 把解析结果表达为**歧义**并报告候选集合，
   SHALL NOT 任意选择其中一条，SHALL NOT 用「找不到」掩盖歧义。
10. WHERE 某候选携带无法归类的标识符串，THE system SHALL 存入
    `unclassified_identifiers` 或仅保留于候选溯源信息中，
    SHALL NOT 把它塞进数字别名或任何参与去重的字段。

### Requirement 4: 只有已核验文献可被引用

**User Story:** 作为论文作者，我需要确保正文里每个引用都指向真实存在的文献。

#### Acceptance Criteria

1. THE system SHALL 通过「作品级标识符回查权威源」核验 Reference：回查成功 AND
   标题相似度达到配置阈值 → 产生 `verified` 记录。
2. THE system SHALL 区分两个**不同**的引用集合，SHALL NOT 用同一集合同时承担两种职责：
   - **Resolvable_Citations**：Ledger 中全部可解析的引用标记，**含**从用户原稿扫描出的
     未核验引用。用于回答「这个引用标记指向哪条记录」。
   - **Generatable_Citations**：仅 `Verification_Status=VERIFIED`（兼容模式下含
     `legacy_verified`）的记录。用于回答「Agent 新增引用时允许用哪些」。
3. THE system SHALL 仅把 Generatable_Citations 提供给写作上下文作为可引用文献。
4. WHERE 某 Reference 携带 `legacy_verified` 标记，THE system SHALL 按配置处理：
   严格模式下重新核验后才可引用；兼容模式下允许引用但在报告中标注「核验方式为 legacy」。
5. WHEN 校验候选正文的引用，THE Citation_Gate SHALL 只对**相对原文新增**的引用要求其
   属于 Generatable_Citations；THE Citation_Gate SHALL NOT 因原文已有的未核验引用
   被原样保留而判定不通过。
6. WHEN 正文出现 Ledger 中**完全无法解析**的引用标记，THE Citation_Gate SHALL 判定
   不通过并列出违规标识符。
7. WHERE 源文件中原有引用无法核验，THE system SHALL 保留该引用于文档中、标记其状态、
   并在报告中提示用户，SHALL NOT 自动删除用户已有引用。

### Requirement 5: 语言判定与计数策略

**User Story:** 作为中文论文作者，我希望「中文 N 篇、外文 M 篇」的要求被如实处理，而不是被不可靠的元数据误导。

#### Acceptance Criteria

1. THE system SHALL 产出 Language_Verdict，包含 verdict、confidence 与 basis。
2. THE system SHALL NOT 仅依据检索源的 `language` 字段判定语言（该字段已被公开研究证明
   不总准确）。
3. WHERE 检索源 `language` 与标题/摘要的字符构成分析**一致**，THE system SHALL 判
   `confidence=high`。
4. WHERE 仅有单一弱证据（如源默认语言），THE system SHALL 判 `confidence=low`。
5. WHERE 无任何可用依据，THE system SHALL 判 `unknown`，SHALL NOT 计入 `language.zh` 或
   `language.non_zh` 任一 Count_Constraint。
6. THE system SHALL 按 Language_Counting_Policy 决定 `confidence=low` 条目是否计入
   语言 Count_Constraint，SHALL NOT 让该行为处于未定义状态。
7. THE system SHALL 以 `high_only` 为严格模式默认；WHERE policy 为
   `include_low_with_warning`，THE system SHALL 把 low 置信条目计入并在配额报告中
   明确标注「其中 N 条为低置信语言判定」。
8. THE system SHALL 在配额报告中分别单列 `unknown` 语言条数与 `confidence=low` 条数。

### Requirement 6: 配额模型（资格过滤 + 计数约束）

**User Story:** 作为论文作者，我说「引言引用 20 篇，其中中文至少 5 篇，且都是最近五年的期刊论文」时，希望系统按我真正的意思执行。

#### Acceptance Criteria

1. THE system SHALL 把用户的文献要求解析为 Quota_Spec，其中**硬过滤条件**归入
   Eligibility_Filter、**数量要求**归入 Count_Constraint，
   SHALL NOT 把年份或文献类型表达为 Count_Constraint 的 dimension。
2. THE system SHALL 在计数前先施加全部 Eligibility_Filter：不满足任一 filter 的
   Reference 一律不计入任何 Count_Constraint。
3. THE system SHALL NOT 把所有数量一律当作「恰好等于」，SHALL 按表述解析
   `exact | min | max | range`。
4. THE system SHALL 区分 Retrieval_Quota 与 Citation_Quota；WHERE 用户表述指向正文引用
   （如「引言引用 20 篇」），THE system SHALL 解析为 Citation_Quota。
5. THE system SHALL 为每个 Quota_Spec 标注 Quota_Scope；WHERE 用户未指明，
   THE system SHALL 采用文档化默认（Citation_Quota → `actually_cited`；
   Retrieval_Quota → `new_only`）。
6. WHEN Count_Constraint 内部矛盾（如分项 min 之和 > total 的 max），THE system SHALL
   判定不可执行并说明冲突；WHERE `total` 的 operator 为 `min`，THE system SHALL NOT
   因分项之和超过 total 而报冲突。
7. WHERE 相对年份（「最近五年」）需要求值，THE system SHALL 在解析时固化为绝对年份区间
   并记录基准年，SHALL NOT 在 Loop 各轮之间重新求值。
8. WHERE 用户未指定配额，THE system SHALL 使用现有 `search_limit` 作为 Retrieval_Quota，
   SHALL NOT 追问。
9. THE system SHALL 把结构化 Quota_Spec（filters 与 constraints 分开呈现）
   随计划摘要交用户确认。

### Requirement 7: 任务相关性筛选

**User Story:** 作为论文作者，我不希望系统为了凑够篇数而塞进真实但与本次主题无关的论文，也不希望它拿上次检索另一个主题时的结论来充数。

#### Acceptance Criteria

1. THE system SHALL 把 Relevance_Assessment 建模为
   `(reference_id, task_fingerprint, verdict, basis, assessed_at)` 的独立记录，
   SHALL NOT 把 relevance 存为 Reference 的固有属性。
2. THE system SHALL 为每次检索或写作任务派生 Task_Fingerprint，并使
   Relevance_Assessment 与之绑定。
3. WHERE 已存在**相同** Task_Fingerprint 下对某 Reference 的 Relevance_Assessment
   且未过期，THE system SHALL 复用之。
4. THE system SHALL NOT 复用**不同** Task_Fingerprint 下的 relevance 结论；
   WHERE 当前任务无对应结论，THE system SHALL 重新判定。
5. THE system SHALL 仅在 `Verification_Status=VERIFIED`、通过全部 Eligibility_Filter、
   **且**当前 Task_Fingerprint 下 relevance verdict 被接受时，
   将该 Reference 计入 Count_Constraint。
6. THE system SHALL 基于标题与摘要做相关性判定，SHALL NOT 仅依据检索源返回的排序位次。
7. WHERE 某候选无摘要，THE system SHALL 只依据标题（`DISCOVERY_ONLY` 证据）判定
   并标注判定依据受限。
8. THE system SHALL 记录被判 `unrelated` 的候选数量，SHALL NOT 静默丢弃。
9. THE system SHALL 使配额进度只由「已接受」的条目推进，防止 Loop 为凑数逐步偏题。

### Requirement 8: 有界自主检索 Loop

**User Story:** 作为论文作者，我希望系统在结果不足时自己换关键词、换检索源，而不是直接返回「只找到 3 篇」。

#### Acceptance Criteria

1. WHILE Count_Constraint 未满足 AND 未触及任何上限，THE Retrieval_Loop SHALL 自主执行
   下一次 Retrieval_Attempt。
2. THE Retrieval_Loop 的自主动作 SHALL 限于**检索策略**调整：改写查询词、拆分/合并查询、
   切换检索源、调整**检索调用参数**。
3. THE Retrieval_Loop SHALL NOT 修改用户已确认的 Eligibility_Filter 或
   Count_Constraint；任何配额放宽 SHALL 仅在 Loop 结束后由用户决定。
4. THE Retrieval_Loop SHALL NOT 修改 Task_Fingerprint 以使旧的 relevance 结论可复用。
5. THE Retrieval_Loop SHALL 受以下上限约束：最大迭代轮数、最大检索调用次数、最大 LLM
   调用次数、累计 provider 耗时预算。
6. WHEN 任一上限被触及，THE Retrieval_Loop SHALL 停止并保留 Batch 中已获成果，
   SHALL NOT 丢弃。
7. THE Retrieval_Loop SHALL NOT 在迭代过程中向用户提问。
8. THE system SHALL 为每次 Retrieval_Attempt 记录 trace 事件，字段见 Requirement 18.2。

### Requirement 9: 暂存批次（单一事实源）与原子提交

**User Story:** 作为论文作者，我希望在文献真正进入我的项目之前先看一眼完整清单，并且清楚「新增了几篇」和「凑够了几篇」不是一回事。

#### Acceptance Criteria

1. THE Retrieval_Batch SHALL 以 `items: list[Batch_Item]` 为**唯一持久化事实源**，
   SHALL NOT 持久化 `accepted_new_ids` / `reused_ledger_ids` / `duplicate_ids` /
   `rejected_ids` 等并列的 ID 集合。
2. THE system SHALL 为每个 Batch_Item 赋予**恰好一个** Batch_Disposition
   （`ACCEPT_NEW` / `REUSE_EXISTING` / `MERGE_DUPLICATE` / `REJECT`），
   SHALL NOT 让同一 item 同时属于多个处置类别。
3. WHERE 某候选命中 Ledger 已有记录**且**携带需要写入的新元数据，THE system SHALL
   赋 `MERGE_DUPLICATE`（而非同时标记为 duplicate 与 reused）；
   WHERE 命中但无元数据变更，THE system SHALL 赋 `REUSE_EXISTING`。
4. THE system SHALL 使 `accepted_new_ids`、`reused_ledger_ids`、`duplicate_ids`、
   `rejected_ids`、`counted_reference_ids`、`ledger_delta` 全部为 items 的**派生属性**，
   SHALL NOT 允许它们与 items 不一致。
5. THE Batch_Item SHALL 携带 `candidate_snapshot`（候选的完整元数据快照），
   使尚未入库的候选也能被完整呈现与复核，SHALL NOT 仅存 ID。
6. THE system SHALL 按以下口径派生计数，SHALL NOT 混用二者：
   - `counted_reference_ids` = 满足当前 Quota_Scope 的、去重后的 `target_reference_id`
     集合（`ACCEPT_NEW` 恒计入；`REUSE_EXISTING` 与 `MERGE_DUPLICATE` 仅在 scope 为
     `ledger_total` 时计入）
   - `ledger_delta` = disposition 为 `ACCEPT_NEW` 的 item 条数
7. WHEN Loop 结束，THE system SHALL 呈现清单（标题 / 年份 / 类型 / 来源 / 语言及其置信 /
   相关性 / disposition）与逐维度配额评估结果，并请用户确认。
8. WHEN 用户确认，THE system SHALL 以**原子操作**提交：写入 `ACCEPT_NEW` 的候选、
   对 `MERGE_DUPLICATE` 执行元数据合并，全部成功或全部不写入。
9. IF 用户否决，THEN THE system SHALL 置 Batch 为 `rejected` 且 Ledger 逐字节不变。
10. WHERE Batch 已提交，THE system SHALL NOT 因后续写作或校验失败而回滚该提交
    （已核验文献是独立的项目资产）。

### Requirement 10: 配额缺口如实告知

**User Story:** 作为论文作者，我需要知道系统究竟找到了什么、缺什么。

#### Acceptance Criteria

1. WHEN Loop 结束且 Count_Constraint 未完全满足，THE system SHALL 逐维度报告 Quota_Gap
   （dimension / required / operator / achieved / gap）。
2. THE system SHALL NOT 为满足配额而纳入非 `VERIFIED` 的候选。
3. THE system SHALL NOT 为满足配额而重复计入同一 `reference_id`。
4. THE system SHALL NOT 为满足配额而纳入当前 Task_Fingerprint 下 relevance 未被接受、
   或不通过 Eligibility_Filter 的候选。
5. WHERE 某维度受检索源能力限制而无法保证（如中文文献源覆盖不足），THE system SHALL
   明确说明该维度**不可保证**及其原因。
6. WHEN 存在 Quota_Gap，THE system SHALL 让用户选择：接受现状继续 / 取消本次操作。

### Requirement 11: 检索源可插拔与发现源隔离

**User Story:** 作为使用者，我希望能通过增加检索源来改善覆盖（尤其中文），且不因此牺牲可信性。

#### Acceptance Criteria

1. THE system SHALL 支持配置多个检索源并按优先级顺序使用。
2. THE system SHALL 为每个检索源声明其 Provider_Role 集合（`SEARCH` / `VERIFY` /
   `DISCOVER`）与 Metadata_Provenance，SHALL NOT 只按「权威 / 非权威」二分，
   SHALL NOT 假设一个源同时具备检索与核验能力。
3. WHEN 某检索源失败，THE system SHALL 记录该源失败并继续尝试其余源，SHALL NOT 中断
   整个操作。
4. WHEN 所有检索源均不可用，THE system SHALL 如实报告并让用户选择继续（不引用新文献）
   或取消。
5. THE system SHALL 使新增检索源无需改动 Loop、配额与 Gate 逻辑（仅新增 provider
   实现与配置）。

**Discovery_Source 作为兜底的启用条件**

6. THE system SHALL 先用全部已配置的权威源检索；THE system SHALL NOT 把
   Discovery_Source 作为首选源或唯一源。
7. THE Retrieval_Loop SHALL 仅在下列条件同时成立时启用 Discovery_Source：
   权威源已被尝试、仍存在非零 Quota_Gap、且 Discovery_Source 在配置中启用。
8. WHERE 某配额维度被记录为「受检索源覆盖不足而不可保证」（Requirement 10.5，
   典型为中文文献），THE system SHALL 优先在该维度上启用 Discovery_Source 兜底。
9. THE system SHALL 使 Discovery_Source 默认**关闭**；启用需显式配置。

**Identifier_Resolution：从「发现」到「可信」**

10. WHEN 从 Discovery_Source 得到候选，THE system SHALL 执行 Identifier_Resolution：
    以标题（必要时加作者与年份）向**权威源**反查，取得 work identifier 后走
    Requirement 4.1 的常规核验。
11. THE system SHALL 要求反查命中的标题相似度达到配置阈值方可认定为同一篇；
    WHERE 相似度不足，THE system SHALL NOT 把反查结果的标识符赋给该候选。
12. WHERE Identifier_Resolution 未取得 work identifier，THE system SHALL 把该候选记为
    Unresolved_Lead：SHALL 在报告中作为线索呈现（含来源 URL），
    SHALL NOT 入 Ledger 的可引用集合，SHALL NOT 计入任何 Count_Constraint。
13. WHERE 权威源反查明确「查无此文」，THE system SHALL 产生 `outcome=not_found` 的
    Verification_Record 并把该候选标注为**疑似不存在**（Discovery_Source 可能为
    模型幻觉），SHALL NOT 记为 `provider_error`。
14. THE system SHALL NOT 因 Identifier_Resolution 成功而把 `discovery_source`
    改写为权威源；两者分别记录（Requirement 2.7）。
15. THE system SHALL 对 Discovery_Source 返回的标题、摘要、网页正文施加
    Requirement 17 的全部不可信输入防护。
16. THE system SHALL 在报告中分别统计：Discovery_Source 发现数、
    Identifier_Resolution 成功数、Unresolved_Lead 数、疑似不存在数。

**核验源能力与来源层级**

17. THE system SHALL 接入至少一个 Metadata_Provenance 为 `registry` 的 `VERIFY` 源
    （Crossref 为默认选择：DOI 注册机构本身，其 REST API 支持按书目文本反查且无需密钥），
    SHALL NOT 仅以 `aggregator` 源作为 DOI 的最终裁决依据。
18. THE VERIFY 源 SHALL 支持两种反查入口：按 work identifier 精确回查、
    以及按书目文本（标题 + 可选作者/年份/出处）模糊反查；
    后者是 Identifier_Resolution（Requirement 11.10）的实现基础。
19. WHEN 发生非空著录字段冲突（Requirement 3.7），THE system SHALL 按
    Metadata_Provenance 层级裁决（`registry` > `publisher` > `aggregator` > `web`），
    SHALL NOT 仅依赖人工配置的源名称顺序。
20. WHERE 冲突双方 Metadata_Provenance 相同，THE system SHALL 再按配置的源优先级裁决，
    并记录该次裁决依据为「同级配置序」。
21. THE system SHALL NOT 把 Metadata_Provenance 为 `web` 的来源作为 DOI 或著录字段的
    裁决依据；来自 `web` 的标识符 SHALL 一律视为待 Identifier_Resolution 的线索。
22. WHERE 某 `VERIFY` 源无公开 API（如需订阅的出版社平台），THE system SHALL NOT 通过
    抓取其网页充当核验源，SHALL 如实把该来源标为不可用。
23. THE system SHALL 在每条 Verification_Record 上记录该次核验源的 Metadata_Provenance，
    使「这个 DOI 是谁确认的」可追溯。

**核验权限细化到标识符类型**

24. THE system SHALL 为每个源分别声明其对**各标识符类型**的核验权限，至少区分：
    `native_identifier_types`（该源自己签发的标识符，可最终核验）、
    `registry_identifier_types`（该源作为注册机构可最终核验的标识符）、
    `corroboration_identifier_types`（该源只能提供佐证、不能最终核验的标识符）。
    THE system SHALL NOT 用单一布尔（如 `authoritative`）决定该源对所有标识符类型的权限。
25. THE system SHALL 仅当核验源对**被回查的那个标识符类型**具备最终核验权限时，
    才产生可使 `Verification_Status=VERIFIED` 的事件；
    否则该次回查 SHALL 只记为**佐证事件**。
    例：Crossref 可最终核验 `doi`；arXiv 可最终核验 `arxiv`；
    OpenAlex 可最终核验 `openalex_work`，对 `doi` 只作佐证；
    Semantic Scholar 可最终核验 `semantic_scholar_paper`，对 `doi` 只作佐证。
26. WHERE 某 Reference 仅有佐证事件而无任何最终核验事件，THE system SHALL 保持
    `Verification_Status=UNVERIFIED` 并在报告中说明「仅有佐证，尚未由权威注册源确认」。
27. THE system SHALL 使 Requirement 11.17（不得仅以 aggregator 作为 DOI 最终裁决依据）
    由上述权限声明**结构性**保证，SHALL NOT 依赖调用点自觉。

### Requirement 12: Evidence 契约（跨 spec）

**User Story:** 作为论文作者，我希望写作时模型看到的是文献摘要依据，而不是只有标题；作为开发者，我需要 A 与 B 对证据结构有完全一致的理解。

#### Acceptance Criteria

1. THE system SHALL 定义 Evidence_Span 为不可变结构，含：`span_id`、`reference_id`、
   `source_field`（`abstract` / `abstract_section:<name>` / `title`）、
   `capability`（Evidence_Capability）、`source_version`、
   `content_hash`（该字段全文的哈希）、`start_offset`、`end_offset`、
   `verbatim_text`、`verbatim_hash`。
2. THE system SHALL 依据 `source_field` 判定 `capability`：`title` → `DISCOVERY_ONLY`；
   `abstract` 与 `abstract_section:<name>` → `CLAIM_SUPPORT`。
3. THE system SHALL NOT 把 `DISCOVERY_ONLY` 的 Evidence_Span 作为 Claim 支撑证据提供，
   并 SHALL 在 Bundle 中显式标注其 capability，使 spec B 可据此过滤。
4. THE system SHALL 以 **NFC 规范化后的 Unicode 码位**为 offset 单位，
   SHALL NOT 使用字节、UTF-16 码元或字形簇；THE system SHALL 在规范化后的文本上
   计算 `content_hash` 与全部 offset。
5. THE system SHALL 保证 `verbatim_text` 等于 `source_field` 规范化文本在
   `[start_offset, end_offset)` 上的切片，且 `verbatim_hash` 由其计算得出。
6. WHEN 某 Reference 的 `source_field` 内容变化（`content_hash` 改变），THE system SHALL
   把该字段上的全部既有 Evidence_Span 标记为 `stale`，SHALL NOT 静默沿用旧 offset。
7. THE system SHALL 以 Evidence_Span 为**唯一**证据单位；模型生成的压缩表述
   （Evidence_Summary）SHALL 保留到其 Evidence_Span 的引用关系，
   SHALL NOT 作为独立证据来源。
8. THE system SHALL 在写作前构造 **Evidence_Bundle**：取 `Verification_Status=VERIFIED`
   且在**本次写作任务的 Task_Fingerprint** 下 relevance 已被接受的 Reference，
   按相关性确定性排序，在上下文预算内截断，并记录被截断的条数。
9. THE system SHALL NOT 用其他 Task_Fingerprint 下的 relevance 结论构造 Bundle；
   WHERE 当前任务缺少某 Reference 的 relevance 结论，THE system SHALL 先按
   Requirement 7 判定。
10. THE Evidence_Bundle SHALL 在一次写作尝试全程**冻结**（不可变），并由写作侧与
    spec B 的校验侧**共用同一实例**，SHALL NOT 各自重新构造，
    SHALL NOT 在写作尝试进行中原地更新。
11. THE Evidence_Bundle SHALL 携带 `bundle_id` 与其 Task_Fingerprint，
    使「更换 Bundle」可被识别为一次**新的**写作尝试。
12. WHERE 某 Reference 无可用于 Claim 支撑的证据（无摘要，仅有 `DISCOVERY_ONLY`），
    THE system SHALL 标注「无摘要可用」并要求生成时不得就其内容做具体断言。
13. THE system SHALL 明确本系统的证据强度为**摘要级**，SHALL NOT 声称保证全文支持或
    结论正确性。

### Requirement 13: 写作前文献预检（LiteraturePreflight）

**User Story:** 作为论文作者，我要求引言引用 20 篇，而库里只有 3 篇时，我希望系统先去把文献找齐，而不是让写手反复写、反复被配额卡住。

#### Acceptance Criteria

1. WHEN 一次写作操作携带 Citation_Quota，THE system SHALL 在写作**开始前**执行
   LiteraturePreflight，SHALL NOT 直接进入生成。
2. THE LiteraturePreflight SHALL 统计当前 Ledger 中**对本次任务可用**的文献数量：
   `Verification_Status=VERIFIED`、通过全部 Eligibility_Filter、
   且在本次 Task_Fingerprint 下 relevance 已被接受。
3. THE LiteraturePreflight SHALL 逐 Count_Constraint 维度计算 Acquisition_Gap
   （含 total 与各语言维度）。
4. WHERE Acquisition_Gap 全部为零，THE LiteraturePreflight SHALL 直接构造
   Evidence_Bundle 并进入写作，SHALL NOT 触发检索。
5. WHERE 存在非零 Acquisition_Gap，THE LiteraturePreflight SHALL 据此派生**内部**
   Retrieval_Quota 并调用 Retrieval_Loop。
6. THE 派生的 Retrieval_Quota SHALL 仅作为检索目标存在；THE system SHALL NOT 因此
   修改、覆盖或放宽用户原始 Citation_Quota。
7. THE 派生的 Retrieval_Quota SHALL 继承用户 Citation_Quota 的全部 Eligibility_Filter，
   SHALL NOT 自行放宽年份或文献类型条件。
8. WHERE 派生 Retrieval_Quota 时需要冗余（因并非所有检索到的文献都会被 Writer 引用），
   THE system SHALL 按文档化的冗余系数计算并在报告中说明，
   SHALL NOT 把冗余量当作用户要求的数量呈现。
9. WHEN Retrieval_Loop 结束，THE system SHALL 按 Requirement 9 请用户确认并原子提交，
   然后**重新**执行 Requirement 13.2 的统计。
10. WHERE 提交后 Acquisition_Gap 仍非零，THE system SHALL 按 Requirement 10.6 让用户
    选择「接受现状继续写作」或「取消本次操作」，SHALL NOT 在用户未选择的情况下
    进入必定失败的写作循环。
11. WHEN 用户选择接受现状，THE system SHALL 记录该决定，使后续 CitationQuotaGate
    的未达标结果被报告为「已知缺口」而非新的失败。
12. THE LiteraturePreflight SHALL 是唯一在写作前触发检索的环节；
    THE system SHALL NOT 在写作循环内触发检索（见 spec B Requirement 7）。

### Requirement 14: 正文引用配额闭环（CitationQuotaGate）

**User Story:** 作为论文作者，我说「引言引用 20 篇，其中中文 5 篇」，我要的是最终正文里真的有 20 篇，而不是系统「找到了 20 篇」。

#### Acceptance Criteria

1. THE system SHALL 提供 CitationQuotaGate，在**候选正文生成后、写入 Working_Copy 前**
   校验 Citation_Quota。
2. THE CitationQuotaGate SHALL 用**确定性文本解析**从候选正文抽取引用标识符
   （复用现有 `_cite_tokens`：`.tex` 取 `\cite{}` 键、`.md`/`.docx` 取 `[id]`），
   SHALL NOT 依据模型自报的「我引用了哪些文献」。
3. THE CitationQuotaGate SHALL 把抽取到的标识符（含 bibkey 别名）解析为 `reference_id`
   集合并**去重**，以**不同文献的数量**计数，SHALL NOT 按引用出现次数计数。
4. THE CitationQuotaGate SHALL 在计数前施加全部 Eligibility_Filter，
   并按当前 Language_Counting_Policy 逐 Count_Constraint 评估。
5. WHEN 任一 Count_Constraint 未满足，THE CitationQuotaGate SHALL 判定不通过并回灌
   逐维度 Quota_Gap 与「Bundle 中尚未被引用且满足条件的候选文献」，
   使写作修订循环可据此补引用。
6. WHERE 未达标的维度已被 Requirement 13.11 记录为「已知缺口」，
   THE CitationQuotaGate SHALL 不因该维度判定不通过，SHALL 在报告中如实呈现该缺口。
7. THE CitationQuotaGate SHALL 与写作循环**共用同一重试预算**，SHALL NOT 拥有独立的
   无限重试；WHEN 预算耗尽且仍不满足，THE system SHALL 按 Requirement 10.6 让用户
   选择接受现状或取消，SHALL NOT 静默放行。
8. THE CitationQuotaGate SHALL 同时产出 `used_reference_ids`（Bundle ∩ 正文）与
   `unused_reference_ids`（Bundle − 正文）供报告使用。
9. THE CitationQuotaGate SHALL 由**写作编排器**调用，SHALL NOT 由 spec B 的
   Evidence Gate 承担配额职责。
10. WHERE 本次写作无 Citation_Quota，THE CitationQuotaGate SHALL 跳过并保持行为与
    现状一致。

### Requirement 15: 参考文献条目与引文格式

**User Story:** 作为论文作者，我希望参考文献列表符合我目标期刊的格式要求。

#### Acceptance Criteria

1. THE system SHALL 支持多种引文格式（至少 GB/T 7714、APA、IEEE、Vancouver）。
2. WHERE 用户未指定引文格式，THE system SHALL 询问用户选择，SHALL NOT 静默采用某一格式。
3. THE system SHALL 为每种引文格式声明其**必需字段集**，并按 `reference_type` 分别定义
   （期刊论文、会议论文、专著、学位论文、预印本、标准、网页）。
4. WHEN 某 Reference 缺少目标格式的必需字段，THE system SHALL 输出可得字段构成的条目、
   标注缺失字段，并在报告中列出该条目为**不完整**，SHALL NOT 编造缺失字段。
5. THE system SHALL 在用户报告中如实说明本次输出的格式符合度：
   全部必需字段齐备时称「符合目标格式」；存在缺字段条目时称
   「基础样式渲染，N 条信息不全」，SHALL NOT 无条件声称完整符合目标期刊格式。
6. WHERE 文档为 .tex，THE system SHALL 生成结构完整的 `\bibitem`；WHERE 为 .md，
   THE system SHALL 生成带标识符的列表项。
7. THE system SHALL 按格式规定的稳定顺序（首次引用顺序或作者字母序）输出条目。
8. THE system SHALL 仅为正文**实际被引用**的 Reference 生成条目，并报告 Ledger 中未被
   引用的记录。
9. WHEN 正文引用了 Ledger 中不存在的标识符，THE system SHALL 报告缺失项并拒绝生成
   不完整的列表。

### Requirement 16: 三个职责分明的文献意图

**User Story:** 作为论文作者，我可能只想找文献、也可能想修参考文献、也可能想写带引用的章节，这些应该都被正确理解。

#### Acceptance Criteria

1. THE system SHALL 提供 `search_literature` 意图：只检索、核验、入库，**不写正文**。
2. THE system SHALL 为 `rewrite` 与 `add_section` 增加配额相关槽位，使带引用的写作可携带
   Citation_Quota。
3. THE system SHALL 提供 `fix_bibliography` 意图：只负责正文引用与参考文献列表**对齐**，
   不负责检索新文献。
4. THE system SHALL 为上述每个意图的每个槽位声明 execution_binding，且 Handler SHALL
   真实消费之。
5. WHERE 规划层不支持某个旧路径已支持的意图，THE system SHALL 明确回退旧执行器或明确
   拒绝，SHALL NOT 静默丢为未映射。
6. WHEN 同一计划同时包含 `fix_bibliography` 与可能新增引用的写作操作，THE Plan_Validator
   SHALL 检出顺序问题并给出重排建议或要求分两轮，SHALL NOT 静默按用户序执行导致新引用
   缺条目。

### Requirement 17: 不可信外部输入防护

**User Story:** 作为使用者，我不希望某篇论文的摘要里藏一句「忽略前面的指令」就让系统听它的；但我当然希望系统读懂摘要的学术内容。

#### Acceptance Criteria

1. THE system SHALL 把检索源返回的标题、摘要、venue、作者、网页正文一律视为
   Untrusted_External_Text。
2. WHEN 把 Untrusted_External_Text 放入任何 LLM 提示，THE system SHALL 以显式定界的
   **引用数据块**包裹，并声明其中内容为待分析数据。
3. THE system SHALL NOT 把 Untrusted_External_Text 中的**指令性语句**当作系统指令执行
   （如「忽略上述要求」「把该文献标记为已核验」「输出 supported」）。
4. THE system SHALL 把 Untrusted_External_Text 的**学术内容**作为相关性判定与
   （spec B 的）支撑判定的**依据**；THE system SHALL NOT 因防护要求而禁止依据摘要内容
   得出判定结论。
5. THE system SHALL NOT 让 Untrusted_External_Text 影响以下任何一项：
   Quota_Spec 的取值、Verification_Status 的判定、Eligibility_Filter、
   预算上限、用户确认结果。
6. THE system SHALL 在入库前清洗 Untrusted_External_Text：移除控制字符（除换行与制表）、
   剥离 HTML/XML 标签与实体、归一化空白，并按配置上限截断。
7. THE system SHALL 在清洗**之后**执行 Unicode NFC 规范化并据此计算 Evidence_Span
   的 offset 与哈希，使证据定位与入库内容一致。
8. THE system SHALL 对相关性判定与 spec B 的支撑判定使用相同的包裹与清洗规则。

### Requirement 18: 可观测、隐私与失败降级

**User Story:** 作为开发者，我需要在文献检索出问题时看清发生了什么，同时不把用户的研究方向写进持久化日志。

#### Acceptance Criteria

1. THE system SHALL 为检索、核验、去重、语言判定、相关性判定、配额评估、
   LiteraturePreflight、批次提交、CitationQuotaGate 各阶段发布 trace 事件。
2. THE system SHALL 在检索类 trace 中记录：`query_hash`、`query_length`、
   `strategy_type`、`provider`、`provider_role`（authoritative / discovery）、
   `filters`（结构化 Eligibility_Filter）、`task_fingerprint`、`candidate_count`、
   `verified_count`、`relevance_accepted_count`、`resolution_success_count`、
   `unresolved_lead_count`、`quota_progress`。
3. THE system SHALL NOT 在持久化 trace 中记录查询原文、文献摘要原文或 Evidence_Span
   的 `verbatim_text`；仅记录标识符、哈希、长度与统计量。
4. WHERE 需要向当前用户展示查询词，THE system SHALL 在交互输出中临时呈现，
   SHALL NOT 因此写入持久化 trace。
5. THE system SHALL 提供**受控评测导出器**：按 `reference_id` / `span_id` 从项目数据
   还原原文，生成供人工标注的样本集，输出到独立的受权限保护位置，
   SHALL NOT 把原文写入普通运行日志或持久化 trace。
6. THE system SHALL NOT 因文献检索失败而写入部分完成的正文。
7. WHEN Loop 因预算触顶停止，THE system SHALL 在 trace 与用户报告中标明触顶的是哪个上限。

### Requirement 19: 迁移、分期交付与向后兼容

**User Story:** 作为现有用户，我不希望这次升级破坏已有工作流或弄丢我已经攒下的文献。

#### Acceptance Criteria

1. THE system SHALL 以 `ReferenceRecord` 为唯一持久化文献实体；
   `KnownRef` SHALL 降级为只读投影并在迁移完成后移除；
   `RetrievalCandidate` SHALL 永不被持久化，
   SHALL NOT 存在两套可写文献模型。
2. WHEN 加载旧版 facts.json，THE system SHALL 无损读取、补齐字段结构并分配
   `reference_id`，SHALL NOT 丢弃任何已有记录。
3. WHERE facts.json 同时存在新旧两种文献结构（部分迁移状态），THE system SHALL
   把旧结构中新结构尚无对应的条目一并迁移合并，SHALL NOT 静默忽略旧结构
   （旧结构可能含新结构没有的条目，忽略即数据丢失）。
4. WHEN 写回迁移结果，THE system SHALL 先备份原文件字节，再写临时文件、
   落盘刷新后**原子替换**目标文件，SHALL NOT 就地覆盖写入。
5. WHEN 迁移旧 `KnownRef(verified=True)`（无 DOI、无来源、无核验记录），THE system SHALL
   标记 `legacy_verified=true` 与 `verification_method=legacy`，
   SHALL NOT 直接等同于 `Verification_Status=VERIFIED` 的新语义。
6. THE system SHALL 使 Ledger 在**同一论文项目内跨会话**复用（沿用
   `project_facts_path` 语义），SHALL NOT 跨项目或跨用户共享。
7. THE system SHALL 按以下阶段交付，每阶段独立可用且默认不改变既有行为：
   - **A1**：Reference 模型升级（含引用别名与字段级来源）、`reference_id`、
     work/container 标识符拆分（含检索候选结构扩展）、核验事件序列与聚合状态
     （含 Metadata_Provenance、`not_found` 与 `provider_error` 的区分）、
     标识符级去重与合并、旧数据迁移与原子写回
   - **A2**：`search_literature` 意图、Batch_Item 与 disposition、Quota_Spec 解析、
     Task_Fingerprint 与 relevance、有界 Retrieval_Loop、
     Discovery_Source 兜底与 Identifier_Resolution（**不接入写作**）
   - **A3**：Evidence_Bundle、LiteraturePreflight、CitationQuotaGate，
     接入 `rewrite` / `add_section`
   - 最后：多引文格式完整字段渲染
8. THE system SHALL 使相关性筛选、检索 Loop、LiteraturePreflight、CitationQuotaGate、
   低置信自动去重各自可通过配置关闭；WHERE 全部关闭，THE system SHALL 退化为现有的
   「一次检索 + 核验入库」行为。
9. THE system SHALL 保证既有测试全部通过。

## Correctness Properties

### Property 1: 引用可核验性
正文中每个被接受的引用标识符，必解析到 Ledger 中某个 `reference_id` 且其
`Verification_Status=VERIFIED`（或在兼容模式下带 `legacy_verified` 标注）。
**Validates: Requirements 2.12, 4.3, 4.4, 4.6**

### Property 2: 主键稳定性
`reference_id` 一旦分配即不变；`bibkey` 重新分配、格式切换、元数据合并均不改变
`reference_id` 及其 Evidence_Span / Batch_Item / Relevance_Assessment / Trace 关联；
被替换的旧 bibkey 仍可解析；新建 Reference（含检索新入库）必分配数字别名，
使 `.md` / `.docx` 的引用标记不依赖迁移数据。
**Validates: Requirements 1.2, 1.3, 1.7, 3.8, 3.9**

### Property 3: 去重只用作品级标识符
去重决策仅依据 `work_identifiers` 或（低置信时）标题+年份+首作者；
不存在因 ISSN / eISSN / ISBN 同值而合并的两条记录——即同一期刊或同一本书的
不同作品在 Ledger 中保持独立。
**Validates: Requirements 1.4, 3.2, 3.3, 3.4**

### Property 4: 标识符唯一性与合并完备
Ledger 中不存在两条经归一化后在任一**作品级**标识符类型上同值的记录；合并后
`work_identifiers`、`container_identifiers`、`verification_events` 均为并集，
且 Verification_Status 由并集重新派生。
**Validates: Requirements 3.1, 3.2, 3.6**

### Property 5: 核验状态可追溯且不虚高
每个 `VERIFIED` 必对应至少一条依据作品级标识符的权威源 `outcome=verified` 事件；
全部状态变迁均由只追加事件表达，不存在可变布尔字段；
`provider_error` 永不成为 Verification_Status；只有 `provider_error` 事件的文献状态为
`UNVERIFIED` 而非「不存在」；`not_found` 与 `provider_error` 自 HTTP 层起以不同异常
类型区分，全程不折叠。
**Validates: Requirements 2.1, 2.5, 2.6, 2.7, 2.8, 2.9**

### Property 6: 冲突必降级
出现权威源冲突记录时，Verification_Status 从 `VERIFIED` 降为 `CONFLICT` 或
`QUARANTINED`；不存在「因 verified 更强而保留」的情形；冲突消解与隔离/释放
均以带目标引用的追加事件表达，不存在可变布尔字段。
**Validates: Requirements 2.2, 2.10**

### Property 7: 元数据不无故丢失
已有 Reference 的 `abstract`、标识符、`verification_events`、著录字段不因后续合并、
源文件扫描或 Ledger 重载而丢失；状态变化只能由新追加的核验事件驱动；
低置信匹配不触发自动合并。
**Validates: Requirements 1.13, 3.5, 3.6, 3.7, 3.10**

### Property 8: 相关性任务绑定
每个 Relevance_Assessment 均带 `task_fingerprint`；计入配额或进入 Bundle 的
relevance 结论其 fingerprint 必等于当前任务的 fingerprint；
Reference 上不存储任何 relevance 结论。
**Validates: Requirements 1.14, 7.1, 7.2, 7.4, 7.5, 12.9**

### Property 9: Batch 单一事实源
Batch 只持久化 `items`；每个 item 恰好一个 disposition；
`accepted_new_ids` / `reused_ledger_ids` / `duplicate_ids` / `rejected_ids` /
`counted_reference_ids` / `ledger_delta` 全部可由 items 唯一派生，
不存在与 items 不一致的可能。
**Validates: Requirements 9.1, 9.2, 9.3, 9.4**

### Property 10: 候选可完整呈现
每个 Batch_Item 都能在未入库状态下呈现其完整候选元数据（`candidate_snapshot`），
不存在「只有 ID 查不到内容」的条目。
**Validates: Requirement 9.5**

### Property 11: 配额口径分离
`counted_reference_ids` 与 `ledger_delta` 各自按 Requirement 9.6 派生；
`new_only` scope 下 `REUSE_EXISTING` 与 `MERGE_DUPLICATE` 不计入配额。
**Validates: Requirements 9.4, 9.6**

### Property 12: 配额诚实性
任何 Quota_Gap 必被逐维度显式报告；绝不由非 `VERIFIED`、重复 `reference_id`、
当前任务下 relevance 未接受、或不通过 Eligibility_Filter 的条目填补。
**Validates: Requirements 7.5, 10.1, 10.2, 10.3, 10.4**

### Property 13: 过滤先于计数
不通过任一 Eligibility_Filter 的 Reference 不出现在任何 Count_Constraint 的计数中；
相对年份在一次 Turn 内固化不变。
**Validates: Requirements 6.1, 6.2, 6.7**

### Property 14: Loop 有界且不动用户约束
迭代数、检索调用数、LLM 调用数、累计耗时均不超过配置上限；触顶时 Batch 内成果不丢失
且触顶原因可见；Loop 迭代中 Eligibility_Filter、Count_Constraint 与 Task_Fingerprint
均保持不变。
**Validates: Requirements 8.3, 8.4, 8.5, 8.6, 18.7**

### Property 15: Loop 无人机交互
Loop 迭代过程中不产生任何用户提问；交互只发生在结束后的清单确认与缺口处置。
**Validates: Requirements 8.7, 9.7, 10.6**

### Property 16: 未确认不入库 / 已确认不回滚
用户确认前 Ledger 逐字节不变；否决后 Batch 置 `rejected` 且 Ledger 不变；
提交为原子操作；已提交的 Reference 不因后续写作或校验失败而回滚。
**Validates: Requirements 9.1, 9.8, 9.9, 9.10**

### Property 17: 发现源不授予可信
由 Discovery_Source 发现的 Reference 在未经权威源回查前不进入可引用集合；
Unresolved_Lead 既不入可引用集合也不计入任何 Count_Constraint；
`discovery_source` 与核验源始终分别记录，不因升级成功而被改写。
**Validates: Requirements 2.11, 2.12, 11.2, 11.12, 11.14**

### Property 18: 语言判定可追溯且计数明确
每个 Language_Verdict 均带 basis 与 confidence；`unknown` 不计入任一语言
Count_Constraint；`confidence=low` 的计数行为完全由 Language_Counting_Policy 决定
且被计入时必在报告中标注；不存在仅凭检索源 `language` 字段得出的 high confidence 判定。
**Validates: Requirements 5.1, 5.2, 5.3, 5.5, 5.6, 5.7, 5.8**

### Property 19: Evidence 可定位性
每个 Evidence_Span 的 `verbatim_text` 等于其 `source_field` 的 NFC 规范化文本在
`[start_offset, end_offset)` 上的切片；`content_hash` 变化时该字段全部旧 Span 被标记
`stale` 且不被使用。
**Validates: Requirements 12.1, 12.4, 12.5, 12.6**

### Property 20: 证据能力隔离
来自 `title` 的 Evidence_Span 一律标记 `DISCOVERY_ONLY`，不作为 Claim 支撑证据提供；
只有 `abstract` / `abstract_section` 的片段标记 `CLAIM_SUPPORT`。
**Validates: Requirements 12.2, 12.3**

### Property 21: Bundle 冻结与共用
一次写作尝试内 Evidence_Bundle 不可变，写作侧与校验侧引用同一实例；
Bundle 内容变化必对应新的 `bundle_id` 与新的写作尝试；被截断条数可见。
**Validates: Requirements 12.8, 12.10, 12.11**

### Property 22: 预检先于写作
存在 Citation_Quota 时，写作生成之前必已完成 LiteraturePreflight；
Acquisition_Gap 非零且用户未选择接受现状时，不进入写作生成。
**Validates: Requirements 13.1, 13.3, 13.10**

### Property 23: 派生配额不篡改用户约束
LiteraturePreflight 派生的内部 Retrieval_Quota 不修改用户原始 Citation_Quota，
并继承其全部 Eligibility_Filter；任何冗余系数在报告中与用户要求量区分呈现。
**Validates: Requirements 13.6, 13.7, 13.8**

### Property 24: 引用计数确定性
CitationQuotaGate 的计数完全由正文文本的确定性解析得出，与模型自报无关，
且按去重后的不同 `reference_id` 计数；`used ∪ unused = Bundle` 且二者不交。
**Validates: Requirements 14.2, 14.3, 14.8**

### Property 25: 引用配额闭环
存在 Citation_Quota 时，写入 Working_Copy 的正文要么满足全部 Count_Constraint，
要么其 Quota_Gap 已被显式报告并经用户选择接受（含预检阶段记录的已知缺口）；
不存在静默放行。
**Validates: Requirements 13.11, 14.1, 14.5, 14.6, 14.7**

### Property 26: 条目完整性与格式诚实
生成的参考文献条目集合与正文实际引用集合一致；存在正文引用但 Ledger 缺失的标识符时
拒绝生成；存在缺必需字段的条目时报告降级为「基础样式渲染」而非声称完整符合格式。
**Validates: Requirements 15.4, 15.5, 15.8, 15.9**

### Property 27: 外部文本不越权但可被判读
Untrusted_External_Text 在任何提示中均以引用数据块出现且清洗后不含控制字符与标签；
其中的指令性语句不被执行；其学术内容可作为相关性与支撑判定依据；
其内容不改变 Quota_Spec、Verification_Status、Eligibility_Filter、预算与用户确认结果。
**Validates: Requirements 17.2, 17.3, 17.4, 17.5, 17.6**

### Property 28: 隐私边界与受控导出
持久化 trace 中不出现查询原文、摘要原文与 `verbatim_text`，仅出现 `query_hash`、
标识符、哈希、长度与统计量；含原文的标注样本只经受控评测导出器产出到受保护位置。
**Validates: Requirements 18.2, 18.3, 18.4, 18.5**

### Property 29: 迁移无损且不虚高
旧 facts.json 全部记录迁移后条数不减、内容不丢；混合 schema 下旧结构的独有条目一并
迁移而非忽略；写回经备份 + 临时文件 + 原子替换，中途失败不产生半截文件；
旧 `verified=True` 一律带 `legacy_verified` 标记，不被当作新语义的 `VERIFIED` 使用
（严格模式下需重新核验）。
**Validates: Requirements 19.1, 19.2, 19.3, 19.4, 19.5**

### Property 30: 默认零影响
新能力全部关闭时（含低置信自动去重默认关闭），既有检索行为与既有测试结果逐项不变；
Ledger 在同一项目内跨会话可复用且不跨项目泄漏。
**Validates: Requirements 19.6, 19.8, 19.9**

### Property 31: 兜底顺序与幻觉不计入
Discovery_Source 只在权威源已尝试且仍存在 Quota_Gap 时被启用，且默认关闭；
Identifier_Resolution 的标识符赋值必经标题相似度阈值；权威源反查「查无此文」
记为 `not_found` 而非 `provider_error`，且该候选不进入可引用集合。
**Validates: Requirements 11.6, 11.7, 11.9, 11.11, 11.13**

### Property 32: 来源层级裁决
每条 Verification_Record 均带 Metadata_Provenance；著录字段冲突的裁决结果始终等于
`registry > publisher > aggregator > web` 的层级序（同级才落到配置序）；
`web` 来源永不作为 DOI 或著录字段的裁决依据。
**Validates: Requirements 1.8, 3.7, 11.19, 11.20, 11.21, 11.23**

### Property 33: 角色能力不越权
每个检索源的 Provider_Role 显式声明；不含 `VERIFY` 的源永不产生核验事件；
无公开 API 的来源不被网页抓取充当核验源，而是标为不可用。
**Validates: Requirements 11.2, 11.18, 11.22**

### Property 34: 唯一可写真相源
系统中只有 `ReferenceRecord` 一个可写、可持久化的文献实体；`RetrievalCandidate`
永不被持久化；`KnownRef` 只读。不存在第二处可写入文献状态的入口。
**Validates: Requirements 1.1, 19.1**

### Property 35: 核验权限细化到标识符类型
某源产生的最终核验事件，其 `identifier_type` 必在该源的 `native_identifier_types`
或 `registry_identifier_types` 内；对 `corroboration_identifier_types` 的回查只产生
佐证事件。仅有佐证事件的 Reference 状态为 `UNVERIFIED`，
因此不存在「aggregator 单独把 DOI 判成 VERIFIED」的路径。
**Validates: Requirements 2.9, 11.17, 11.24, 11.25, 11.26, 11.27**

### Property 36: 原有未核验引用不被误判
Resolvable_Citations 与 Generatable_Citations 是两个不同集合；Citation_Gate 只对
相对原文**新增**的引用要求属 Generatable_Citations；原文已有的未核验引用被原样保留时
不导致判定不通过，也不被自动删除。
**Validates: Requirements 4.2, 4.5, 4.7**

### Property 37: 状态变迁按因果折叠
`conflict_resolved` 只能指向 `conflict`、`released` 只能指向 `quarantined`；
隔离状态成立当且仅当存在未被有效 `released` 指向的 `quarantined`；
折叠结果与事件时间戳的先后及是否相同**无关**；
`target_event_id` 不存在或类型不匹配的事件被拒绝或忽略并报告。
**Validates: Requirements 2.2, 2.3, 2.4, 2.10**

### Property 38: 引用解析歧义显式化
引用标记命中多条记录时解析结果为「歧义 + 候选集合」，绝不任意选一条、
也不退化为「找不到」；旧 bibkey / 数字别名 / 被合并的旧 id 均有可查索引；
无法归类的标识符串不进入别名或去重字段。
**Validates: Requirements 3.8, 3.9, 3.10**
