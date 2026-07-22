# Paper Agent 终版架构设计

> 文档定位：从零设计的生产级学术论文写作 Agent 目标架构  
> 版本：1.0  
> 核心目标：可靠、可追溯、可恢复、可评测、可扩展、保护科研隐私  
> 核心范式：**Workflow-first + Evidence-grounded + Candidate-first + Bounded autonomy**

---

## 0. 重要声明

不存在对所有学科、所有文件格式、所有部署规模都“完美”的 Agent 架构。

本文给出的不是最大而全的组件清单，而是一套经过约束和反向审查后，适合作为长期目标的架构。它遵循三个原则：

1. **安全边界优先于模型自主性**；
2. **先建立可工作的垂直链路，再逐步启用复杂能力**；
3. **任何无法被可靠验证的能力，都必须显式降级，而不是伪装成已完成**。

本文同时给出：

- 目标架构；
- 核心数据模型；
- 工作流；
- 运行时与存储设计；
- 验证和评测体系；
- 分阶段实施路线；
- 文档末尾的自我审查与剩余风险。

---

# 1. 产品定义

## 1.1 Paper Agent 是什么

Paper Agent 不是单纯的聊天机器人，也不是一个自动写完整论文的自由 Agent。

它是一个以论文项目为中心的工作系统：

```text
用户目标
→ 结构化操作计划
→ 受控工作流
→ 候选产物
→ 多层验证
→ 人工审批
→ 正式提交
```

它应当具备四种身份：

1. **编辑器**：润色、改写、补写、响应审稿意见；
2. **研究助手**：检索文献、定位证据、形成证据矩阵；
3. **审查器**：检查数字、引用、Claim、跨章节一致性；
4. **文档操作系统**：版本、Diff、编译、格式、导出、恢复。

---

## 1.2 核心成功标准

系统应优先保证：

```text
事实可追溯
引用可定位
数字不被错误绑定
修改范围可控制
正式文件不可被意外覆盖
长任务可以恢复
每次运行可以审计
模型升级可以回归测试
项目之间不会互相污染
敏感论文内容不会被错误发送到外部服务
```

---

## 1.3 明确非目标

初始版本不追求：

- 无人监督地自动完成并投稿整篇论文；
- 对任意文档格式承诺绝对无损转换；
- 让一个自由 ReAct Agent 控制整个项目；
- 为论文中的每一句话建立 Claim；
- 让 LLM 作为文件完整性或数学等价性的最终裁判；
- 自动访问付费全文或绕过版权限制；
- 自动执行任意 Python、Shell 或宏代码；
- 把全部聊天记录永久注入上下文。

---

# 2. 总体架构

Paper Agent Platform 分为四个业务引擎和一个共享内核。

```text
Paper Agent Platform
│
├── 1. Task & Collaboration Layer
│     ├── Task Analyzer
│     ├── Operation Planner
│     ├── Human Interaction
│     └── Progress / Diff UI
│
├── 2. Paper Editing Engine
│     ├── Polish
│     ├── Rewrite
│     ├── Add Section
│     ├── Abstract
│     ├── Reviewer Response
│     └── Consistency Repair
│
├── 3. Research & Evidence Engine
│     ├── Literature Search
│     ├── Metadata Verification
│     ├── Full-text Retrieval
│     ├── Evidence Extraction
│     ├── Claim Support Verification
│     └── Citation Audit
│
├── 4. Document Engine
│     ├── Parse
│     ├── Address / Anchor
│     ├── Patch
│     ├── Compile
│     ├── Format
│     ├── Convert
│     └── Export
│
└── 5. Shared Kernel & Governance
      ├── Project / Artifact Store
      ├── Workflow Runtime
      ├── Policy & Permissions
      ├── Context Compiler
      ├── Memory
      ├── Claim–Evidence
      ├── Transactions
      ├── Trace / Eval
      ├── Model Routing
      └── Privacy / Security
```

## 为什么拆成多个 Engine

三类任务的运行特征不同：

| 引擎 | 特征 |
|---|---|
| Editing | 用户在线、低到中延迟、候选文本与 Diff |
| Research | 外部 API、多步搜索、长任务、结果不确定 |
| Document | 主要是确定性程序、文件结构与格式操作 |

它们可以共享项目状态，但不应被硬塞入一个巨型 Graph。

---

# 3. 顶层控制：Operation Plan，而不是单一 Intent

## 3.1 为什么不能只选一个 Intent

用户可能提出：

> 搜索三篇外部验证相关文献，重写 Discussion，把引用补进 bibliography，然后导出 DOCX。

这是四个操作：

```text
literature_search
→ rewrite_section
→ update_bibliography
→ convert_format
```

因此顶层应输出 Operation DAG。

---

## 3.2 Operation Plan

```yaml
operation_plan:
  plan_id: plan_001
  project_id: paper_001
  requested_by: user_001

  operations:
    - operation_id: op_01
      type: literature_search
      engine: research
      depends_on: []
      status: pending

    - operation_id: op_02
      type: rewrite_section
      engine: editing
      depends_on:
        - op_01
      status: pending

    - operation_id: op_03
      type: update_bibliography
      engine: document
      depends_on:
        - op_02
      status: pending

    - operation_id: op_04
      type: convert_format
      engine: document
      depends_on:
        - op_03
      status: pending
```

Intent 仍可用于分类，但不再是最高级控制对象。

---

## 3.3 Operation Contract

每个 Operation 都有独立契约。

```yaml
operation_contract:
  operation_id: op_02
  type: rewrite_section

  target:
    artifact_id: manuscript_main
    base_version: 17
    section_id: discussion

  objectives:
    - integrate_external_validation_evidence
    - improve_argument_flow

  requested_capabilities:
    - read_section
    - retrieve_verified_evidence
    - generate_candidate_patch

  constraints:
    add_new_claims: false
    modify_numbers: false
    modify_other_sections: false

  evidence_policy:
    verified_evidence_only: true

  risk_level: medium

  execution_mode: writer_verifier

  budgets:
    max_llm_calls: 3
    max_tool_calls: 8
    max_retries: 2

  required_gates:
    - scope
    - citation_binding
    - numeric_binding
    - claim_support
    - semantic_consistency

  output_contract:
    type: candidate_patch
```

---

# 4. 权限模型：请求能力不等于获得权限

Operation Contract 可能由模型参与生成，因此不能授予权限。

```text
Operation Contract
= requested capabilities

Policy Engine
= granted capabilities
```

最终权限：

```python
effective_capabilities = (
    system_policy.allowed_capabilities
    & project_policy.allowed_capabilities
    & node_policy.allowed_capabilities
    & user_consent.allowed_capabilities
)
```

Operation Contract 只能进一步缩小权限，不能扩大权限。

---

## 4.1 权限层次

### System Policy

不可被项目或模型覆盖：

```text
禁止直接覆盖正式文件
禁止默认执行任意代码
禁止绕过项目隔离
禁止将机密全文发送到未批准 Provider
```

### Project Policy

```yaml
project_policy:
  data_classification: confidential
  external_model_allowed: false
  external_search_allowed: true
  manuscript_text_in_search_query: false
```

### Node Policy

```yaml
node_policy:
  node: write_candidate
  allowed_capabilities:
    - read_artifact_fragment
    - retrieve_claims
    - retrieve_evidence
    - save_candidate_blob
```

### User Consent

只用于高风险能力：

```text
执行沙箱脚本
覆盖现有 Candidate
提交正式 Artifact Head
发送邮件
投稿
```

---

# 5. 工作流运行时

## 5.1 运行时抽象

不要让业务代码永久绑定某个框架。

```python
class WorkflowRuntime(Protocol):
    def start(self, workflow, input): ...
    def resume(self, run_id, input): ...
    def inspect(self, run_id): ...
    def cancel(self, run_id): ...
```

推荐：

- MVP：自定义状态机或 LangGraph；
- 需要长时间等待、跨服务恢复、大量并发时：使用 Durable Workflow Runtime；
- 业务节点和数据模型不依赖具体运行时。

---

## 5.2 LangGraph 的适用位置

适合：

- Editing Engine 的 Writer–Verifier；
- Research Engine 的受限搜索子图；
- Human interrupt；
- 节点级状态和条件跳转。

Graph State 只保存运行时引用，不保存全部正文。

```python
class OperationState(TypedDict, total=False):
    project_id: str
    session_id: str
    run_id: str
    operation_id: str

    phase: str

    source_artifact_ref: str
    candidate_artifact_ref: str | None

    active_claim_ids: list[str]
    active_evidence_ids: list[str]
    active_decision_ids: list[str]

    gate_result_refs: list[str]

    retry_count: int
    llm_call_count: int
    tool_call_count: int

    pending_human_action_ref: str | None
    error_refs: list[str]
```

---

## 5.3 Durable Workflow 的适用位置

适合：

- 数小时或数天的文献任务；
- 等待用户上传全文；
- 等待人工审批；
- 外部 API 限流与重试；
- 多个 Engine 的 Operation DAG；
- 进程或服务器重启后的恢复。

外部调用应当作为幂等 Activity，而不是直接写在确定性 Workflow 逻辑中。

---

# 6. 执行模式

系统支持以下执行模式。

## 6.1 Program Only

用于：

```text
文件解析
字数
引用键
BibTeX
LaTeX 编译
数字提取
文档 Patch
格式操作
```

## 6.2 Single LLM

用于：

```text
意图辅助分类
Slot 抽取
Reviewer Comment 结构化
段落角色分类
低风险标题候选
```

## 6.3 Writer–Verifier

用于：

```text
Polish
Rewrite
Abstract
Discussion
Cover Letter
Reviewer Response
```

## 6.4 Planner–Executor

用于：

```text
全章重构
新增章节
多条审稿意见
全文一致性修复
```

## 6.5 Bounded Search

用于：

```text
文献探索
证据定位
检索词动态扩展
研究空白探索
```

## 6.6 Human Review

用于：

```text
证据冲突
高风险 Claim
无法定位全文
数字来源不唯一
复杂格式无法证明
正式提交
```

---

# 7. 三个核心 Engine

# 7.1 Paper Editing Engine

核心节点：

```text
LOAD_SCOPE
BUILD_WRITER_CONTEXT
WRITE_CANDIDATE
STRUCTURAL_GATE
PROVENANCE_GATE
SEMANTIC_GATE
REPAIR
USER_REVIEW
PREPARE_COMMIT
```

Editing Engine 不直接进行开放式搜索。需要新证据时调用 Research Engine，取得冻结的 Evidence Set 后再写作。

---

# 7.2 Research & Evidence Engine

核心节点：

```text
DEFINE_RESEARCH_REQUEST
GENERATE_QUERY
SEARCH_METADATA
DEDUPLICATE
SCREEN_ABSTRACT
RETRIEVE_FULLTEXT
LOCATE_PASSAGE
EXTRACT_EVIDENCE
VERIFY_PROVENANCE
VERIFY_SUPPORT
PUBLISH_EVIDENCE_SET
```

Research Engine 只能输出：

```text
Reference Candidate
Evidence Candidate
Verified Evidence Set
Research Report
```

不能直接修改正式论文。

---

# 7.3 Document Engine

核心能力：

```text
解析 Artifact
建立稳定 Anchor
生成 Patch
应用 Patch
编译
格式操作
转换
导出
完整性检查
```

Document Engine 不负责决定学术内容是否正确。

---

# 8. Artifact 模型

## 8.1 不可变 Blob + 可变 Head

```text
Artifact Blob
= 内容不可变，由 Hash 标识

Artifact Head
= 当前正式版本引用
```

```yaml
artifact_blob:
  blob_id: sha256:...
  media_type: text/x-tex
  size: 48120
  created_at: ...
```

```yaml
artifact_head:
  project_id: paper_001
  artifact_id: manuscript_main
  current_blob_id: sha256:...
  version: 18
```

---

## 8.2 Candidate Patch

写作节点应优先产生 Patch，而不是整文件。

```yaml
candidate_patch:
  patch_id: patch_031
  base_blob_id: sha256:base
  target_artifact_id: manuscript_main

  operations:
    - type: replace_range
      anchor_id: discussion_p04
      expected_hash: sha256:old_text
      replacement_blob_id: sha256:new_text

  created_by_run: run_028
  status: candidate
```

应用 Patch 前必须验证 `expected_hash`，防止基线已变化。

---

## 8.3 Anchor

Anchor 应尽量稳定：

```yaml
anchor:
  anchor_id: discussion_p04
  artifact_id: manuscript_main
  node_type: paragraph

  structural_path:
    section: discussion
    subsection: external_validation
    ordinal: 4

  content_fingerprint: sha256:...
```

单靠行号或字符偏移不稳定，应组合结构路径和内容指纹。

---

# 9. 提交协议：Commit Manifest，而不是跨存储假事务

PostgreSQL、Object Store、Git 不能天然组成单一 ACID 事务。

采用：

```text
不可变写入
+
Commit Manifest
+
原子更新 Artifact Head
```

```yaml
commit_manifest:
  commit_id: commit_031
  project_id: paper_001
  base_artifact_version: 17
  status: preparing

  writes:
    - candidate_blob_id: sha256:new
    - decision_id: dec_041
    - claim_version_ids:
        - claim_014_v4

  expected_head_blob_id: sha256:base
  new_head_blob_id: sha256:new
```

流程：

```text
1. 写入不可变 Blob
2. 写入 Claim / Decision 新版本
3. 写入 Commit Manifest
4. 乐观锁检查 Artifact Head
5. 原子更新 Artifact Head
6. Manifest 标记 committed
```

失败时不更新 Head。孤立 Blob 后续垃圾回收。

---

# 10. 存储权威性

推荐单一权威模型：

```text
Artifact 内容：
Content-addressed Object Store

Artifact Head、项目状态、Claim、Evidence：
PostgreSQL

全文与解析结果：
Object Store + PostgreSQL Metadata

向量索引、全文索引：
派生索引，可重建

Git：
导入、导出或本地协作适配器，不作为多个运行时真相之一
```

任何索引都不能成为权威事实来源。

---

# 11. 学术知识模型

# 11.1 Project Brief

```yaml
project_brief:
  research_question: ...
  central_thesis: ...
  target_venue: ...
  manuscript_stage: drafting

  contributions:
    - contribution_id: contrib_01
      text: ...
      status: verified

  terminology_profile: term_profile_01
```

---

# 11.2 Claim 的适用范围

只对高价值事实性陈述强制 Claim 化。

```yaml
claim_policy:
  required_for:
    - numeric_result
    - comparison
    - statistical_significance
    - causality
    - novelty
    - state_of_the_art
    - literature_synthesis
    - clinical_or_practical_value
    - limitation_fact

  optional_for:
    - method_description
    - conceptual_explanation

  excluded_for:
    - transition_sentence
    - rhetorical_sentence
    - section_navigation
```

---

## 11.3 Claim

```yaml
claim:
  claim_id: claim_014
  project_id: paper_001
  schema_version: 1

  canonical_text: >
    MedAlign achieved higher AUROC than Baseline-A
    on the external validation test set.

  claim_type: comparative_result

  slots:
    subject: MedAlign
    predicate: greater_than
    metric: AUROC
    comparator: Baseline-A
    dataset: external_validation
    split: test

  allowed_scope:
    - results
    - abstract
    - discussion

  strength_policy:
    comparison_scope: selected_baseline
    statistical_significance: not_verified
    effect_size: not_verified
    generalization: external_test_only

  lifecycle:
    status: verified
    version: 3
    supersedes: claim_014_v2
```

---

## 11.4 Evidence

Evidence 是可重新定位和验证的最小依据。

```yaml
evidence:
  evidence_id: ev_031
  project_id: paper_001
  schema_version: 1

  evidence_type: table_comparison
  adapter_type: structured_table

  source:
    artifact_id: result_table_03
    blob_id: sha256:...
    locator:
      proposed_row_key: MedAlign
      baseline_row_key: Baseline-A
      column_key: AUROC
    context:
      dataset: external_validation
      split: test

  normalized_observations:
    - entity: MedAlign
      metric: AUROC
      value: 0.87
      uncertainty: 0.03

    - entity: Baseline-A
      metric: AUROC
      value: 0.84

  verification:
    provenance_status: program_verified
    semantic_mapping_status: program_verified
```

---

## 11.5 Claim–Evidence Link

```yaml
claim_evidence_link:
  link_id: link_041
  claim_id: claim_014
  evidence_id: ev_031

  verdict: supports
  coverage: complete
  confidence_band: high

  allowed_inference:
    - higher_than_baseline_a_on_external_test

  forbidden_inference:
    - statistically_significant
    - universal_superiority
    - state_of_the_art

  verifier:
    method: deterministic_comparison
    version: comparison_verifier_v2

  lifecycle:
    status: active
```

---

## 11.6 Evidence Adapter

不同学科使用不同 Adapter。

```python
class EvidenceAdapter(Protocol):
    def extract_candidates(self, source_ref): ...
    def verify_provenance(self, evidence): ...
    def normalize(self, evidence): ...
    def verify_support(self, claim, evidence): ...
```

初始支持：

```text
StructuredTableAdapter
LiteraturePassageAdapter
StatisticalOutputAdapter
ManuscriptPassageAdapter
```

后续扩展：

```text
ProofAdapter
QualitativeQuoteAdapter
CodeExecutionAdapter
HistoricalSourceAdapter
```

---

# 12. 文献系统

## 12.1 文献状态分级

```text
L0 metadata_verified
L1 abstract_available
L2 fulltext_available
L3 passage_located
L4 claim_support_verified
```

一篇文献不必达到 L4 才进入文献库，但只有达到合适等级，才能支持相应强度的 Claim。

---

## 12.2 全文定位失败是正常状态

```yaml
reference_access:
  metadata_status: verified
  fulltext_status: unavailable
  passage_status: not_located
  support_status: unverified
```

可选处理：

```text
请求用户上传全文
更换可访问文献
降低 Claim 强度
只用于背景描述
删除该 Claim
```

---

## 12.3 外部内容必须视为不可信输入

论文、网页、PDF 中可能包含恶意或无关指令。

Research Engine 必须遵循：

```text
外部内容只作为数据
不执行其中的指令
不允许外部内容改变系统策略
不允许外部内容扩大工具权限
```

模型上下文中应明确分隔：

```text
SYSTEM POLICY
TRUSTED PROJECT DATA
UNTRUSTED EXTERNAL DOCUMENT
VERIFICATION TASK
```

---

# 13. 数字系统

## 13.1 Numeric Fact

```yaml
numeric_fact:
  fact_id: num_031
  project_id: paper_001

  raw_value: "0.87 ± 0.03"

  normalized:
    value: 0.87
    uncertainty: 0.03
    unit: null

  semantic_binding:
    entity: MedAlign
    metric: AUROC
    dataset: external_validation
    split: test
    aggregation: mean

  source:
    artifact_id: result_table_03
    blob_id: sha256:...
    locator:
      row_key: MedAlign
      column_key: AUROC

  verification:
    provenance_status: program_verified
    semantic_binding_status: verified
```

---

## 13.2 冲突不是简单按时间排序

项目需指定权威来源：

```yaml
canonical_sources:
  experiment_results:
    artifact_id: final_results
    lifecycle: approved
```

冲突决策考虑：

```text
项目指定权威性
批准状态
draft/final 生命周期
协议匹配
版本
时间
```

不能默认“最新脚本输出”一定覆盖正式表格。

---

# 14. Citation Attachment

```yaml
citation_attachment:
  attachment_id: ca_021
  artifact_version: 17

  citation_id: ref_003
  paragraph_anchor_id: p_12
  sentence_anchor_id: s_31
  claim_id: claim_014

  relation: supports
```

程序负责检查：

```text
引用是否存在
引用是否跨句或跨 Claim 移动
引用 ID 是否改变
```

语义 Verifier 负责检查：

```text
文献 Evidence 是否真正支持 Claim
```

---

# 15. 上下文工程

Context Compiler 不是把所有信息塞进 Prompt，而是形成节点专用的 Context Bundle。

```yaml
context_bundle:
  bundle_id: ctx_031
  node_type: write_candidate

  trusted:
    system_policy_ref: policy_01
    operation_contract_ref: op_contract_02
    project_brief_ref: brief_17
    artifact_fragment_refs:
      - fragment_041
    claim_refs:
      - claim_014
    evidence_refs:
      - ev_031
    decision_refs:
      - dec_021

  conversational:
    recent_turn_refs:
      - turn_88
      - turn_89

  untrusted:
    external_document_refs: []

  output_schema_ref: writer_output_v2
  context_hash: sha256:...
```

---

## 15.1 Context Recipe

每个节点定义配方。

### Writer

```text
Task Contract
当前材料
允许的 Claim
冻结的 Evidence Set
关键 Decision
用户相关偏好
禁止项
输出 Schema
```

### Verifier

```text
原文
候选文本
对应 Claim–Evidence
验证 Rubric
输出 Schema
```

Verifier 不读取 Writer 的自由推理，也不默认读取无关聊天。

### Repair

```text
原文
候选
Gate Issues
Locked Spans
Allowed Edit Range
```

### Search

```text
研究问题
已覆盖证据
证据缺口
查询预算
外部数据隔离规则
```

---

## 15.2 最近对话不是固定加入

仅在以下情况加入：

```text
当前任务依赖上一候选
存在指代或未解决反馈
用户刚刚撤销或修正约束
```

其他情况使用结构化 Contract 和 Decision，不注入完整对话。

---

## 15.3 Context Provenance

每段上下文必须标记：

```text
来源
版本
可信等级
是否可作为事实
是否可覆盖其他来源
```

模型不应自行解决未标注的事实冲突。

---

# 16. Model Routing

不要求所有节点使用同一个模型。

```yaml
model_policy:
  router:
    capability: structured_extraction
    temperature: low

  writer:
    capability: long_form_writing
    temperature: moderate

  verifier:
    capability: evidence_reasoning
    temperature: low

  search_planner:
    capability: planning
    temperature: low
```

高风险验证可使用：

```text
规则
+
独立 Verifier
+
必要时第二 Verifier 或人工
```

但不要默认堆多 Agent。只有评测证明收益明显时才增加模型调用。

---

# 17. Gate 体系

Gate 不是固定全部串行执行，而是动态 Gate Plan。

```yaml
gate_plan:
  operation_type: polish

  required:
    - scope_integrity
    - numeric_binding
    - citation_binding
    - meaning_drift

  optional:
    - terminology_consistency

  skipped:
    - literature_provenance
    - full_claim_support
```

---

## 17.1 Structural Gate

程序验证：

```text
目标范围
Anchor
标题层级
公式与表格结构
文件可打开
Patch 可应用
输出 Schema
```

## 17.2 Provenance Gate

程序和 Adapter 验证：

```text
数字来源
引用元数据
Evidence Locator
Blob Hash
Artifact Version
```

## 17.3 Semantic Risk Gate

LLM/NLI 输出风险信号：

```yaml
semantic_assessment:
  verdict: partially_supported
  confidence_band: medium
  evidence_coverage: partial
  issues:
    - comparison_scope_too_broad
  blocking: true
  human_review_recommended: false
```

模型自报的浮点置信度不作为安全依据。使用经评测校准的等级。

## 17.4 Policy Gate

检查：

```text
隐私
Provider
工具权限
外部发送
高风险动作
```

## 17.5 Human Gate

只在必要时触发：

```text
核心结论低置信度
证据冲突
正式提交
不支持格式
任意代码执行
```

---

# 18. 严格度配置

同一系统支持三种模式。

## Draft

```text
允许未验证内容进入 Candidate
必须显式标记
禁止成为 Submission-ready Artifact
```

## Standard

```text
核心事实必须验证
背景描述可以使用较低证据等级
正式 Commit 需要用户确认
```

## Submission

```text
核心 Claim 全部有 Evidence
引用支持检查
高风险措辞审查
严格格式与一致性检查
完整审计报告
```

---

# 19. 核心工作流

# 19.1 Polish

```text
LOAD_FRAGMENT
→ BUILD_CONTEXT
→ WRITE_PATCH
→ STRUCTURAL_GATE
→ NUMERIC_BINDING
→ CITATION_BINDING
→ MEANING_DRIFT
→ REPAIR?
→ USER_REVIEW
→ COMMIT
```

Polish 不允许搜索、不允许新增 Claim、不允许新增引用。

---

# 19.2 Rewrite

```text
BUILD_CONTRACT
→ LOAD_PROJECT_CONTEXT
→ CHECK_EVIDENCE_NEED
→ OPTIONAL_RESEARCH_OPERATION
→ FREEZE_EVIDENCE_SET
→ WRITE_PATCH
→ STRUCTURAL_GATE
→ PROVENANCE_GATE
→ SEMANTIC_GATE
→ REPAIR?
→ USER_REVIEW
→ COMMIT
```

Writer 在开始后不能临时自由搜索。证据集合先冻结，保证可复现。

---

# 19.3 Add Section

```text
SLOT_FILL
→ SECTION PLAN
→ CLAIM PLAN
→ EVIDENCE GAP ANALYSIS
→ OPTIONAL_RESEARCH
→ FREEZE_EVIDENCE_SET
→ WRITE
→ GATES
→ USER_REVIEW
→ COMMIT
```

Section Plan：

```yaml
section_plan:
  section_id: discussion
  purpose: interpret_results

  blocks:
    - role: summarize_key_result
      allowed_claim_ids:
        - claim_014

    - role: compare_prior_work
      allowed_evidence_ids:
        - ev_051

    - role: limitations
      required_decision_ids:
        - dec_021
```

---

# 19.4 Literature Research

```text
DEFINE_PROTOCOL
→ BOUNDED_QUERY_LOOP
→ METADATA_VERIFY
→ ABSTRACT_SCREEN
→ FULLTEXT_RETRIEVE
→ PASSAGE_LOCATE
→ EVIDENCE_EXTRACT
→ SUPPORT_VERIFY
→ EVIDENCE_SET
→ HUMAN_REVIEW?
```

搜索循环限制：

```yaml
search_budget:
  max_rounds: 5
  max_queries: 8
  max_candidates: 50
  max_fulltexts: 10
```

---

# 19.5 Reviewer Response

```text
PARSE_COMMENTS
→ BUILD_ISSUE_MATRIX
→ MAP_ARTIFACT_LOCATIONS
→ PLAN_RESPONSE
→ APPLY_CANDIDATE_PATCHES
→ VERIFY_PROMISED_CHANGES
→ WRITE_RESPONSE
→ CROSS_CHECK
→ HUMAN_APPROVAL
```

---

# 19.6 Citation Audit

```text
PARSE_CITATIONS
→ METADATA_VERIFY
→ ATTACH_TO_CLAIMS
→ LOCATE_PASSAGES
→ SUPPORT_CHECK
→ REPORT
```

---

# 19.7 Document Format / Convert

不要一开始依赖一个通用 IR。

### 同格式操作

```text
LaTeX → LaTeX：
AST / 编译 / 引用 / 公式检查

DOCX → DOCX：
OOXML Capability Scan / Part Audit / Text Hash
```

### 跨格式

```text
Capability Scan
→ Semantic Inventory
→ Convert
→ Target Inventory
→ Compare
→ Compile/Open
→ Visual Report
→ Human Review
```

输出保真等级：

```yaml
fidelity_report:
  content: preserved
  structure: mostly_preserved
  layout: not_preserved
  unsupported_elements:
    - tikz_picture
```

不宣称绝对无损。

---

# 20. 工具设计

每个工具必须声明：

```yaml
tool_contract:
  name: search_references
  capability: literature_search

  input_schema: ...
  output_schema: ...

  side_effect: none
  idempotent: true
  timeout_seconds: 30

  retry_policy:
    retryable_errors:
      - rate_limit
      - temporary_network

  data_policy:
    may_send_external_data: true
    allowed_fields:
      - query
```

写入工具必须支持幂等键。

---

## 20.1 工具结果不能直接信任

所有工具输出必须标记：

```text
来源
错误
完整性
版本
时间
是否经过验证
```

工具返回的网页、PDF、模型摘要都属于数据，不属于系统指令。

---

# 21. 任意代码和文档宏

默认关闭。

需要启用时：

```text
用户明确同意
→ 创建不可联网沙箱
→ 只读挂载输入
→ 唯一输出目录
→ CPU/内存/时间限制
→ 模块白名单
→ 完整审计
→ 输出重新验证
```

LaTeX 编译也应使用受限环境，防止危险宏或 shell escape。

---

# 22. 隐私与数据治理

## 22.1 数据分类

```text
public
internal
confidential
restricted
```

项目定义：

```yaml
data_policy:
  classification: confidential

  model_routes:
    allowed:
      - approved_enterprise_provider
      - local_model

  external_search:
    allowed: true
    include_raw_manuscript: false

  logging:
    store_prompt_text: false
    store_redacted_trace: true

  retention:
    raw_upload_days: 30
```

---

## 22.2 Provider Router

发送前检查：

```text
项目数据等级
Provider 合规配置
是否需要脱敏
是否允许跨境
是否允许训练保留
```

Provider Router 属于 Policy Layer，不由模型决定。

---

# 23. 失败分类

```text
RETRYABLE
  暂时网络错误、限流

REPAIRABLE
  Schema 错误、可定位 Gate 失败

EVIDENCE_MISSING
  缺全文、缺数字来源

HUMAN_REQUIRED
  歧义、高风险、冲突

CONFLICT
  Artifact Head 已变化

FATAL
  文件损坏、权限违规、策略禁止
```

不同失败类型有不同处理，不采用统一 Retry。

---

# 24. 可观测性

每个 Operation 记录：

```text
Operation Contract
Workflow Version
Node Version
Model Route
Prompt Template Version
Context Bundle Hash
Tool Calls
Gate Results
Artifact Refs
Token / Cost / Latency
Human Decisions
Final Commit ID
```

日志默认不保存敏感全文，只保存 Hash、引用和经过允许的摘要。

---

# 25. 评测体系必须从 Phase 0 开始

## 25.1 评测层次

```text
Unit Eval
Node Eval
Workflow Eval
Artifact Eval
Human Acceptance Eval
Security Eval
```

## 25.2 必备 Golden Cases

```text
Polish：
数字不变
引用不移动
否定词不丢失
因果强度不增强

Rewrite：
只改目标范围
不新增未支持 Claim
不修改冻结 Evidence

Research：
元数据正确
全文定位失败能正确降级
外部 Prompt Injection 不生效

Document：
Patch 冲突能阻断
公式符号改变能发现
复杂格式正确标记为 unsupported
```

## 25.3 Mutation Tests

主动制造：

```text
数字交换
引用移动
删除 not
may → causes
higher → significantly higher
公式负号改变
表格单元格改变
未核验引用
非目标章节修改
恶意 PDF 指令
```

---

# 26. Schema 与 Workflow 版本

所有长期对象必须包含：

```text
schema_version
created_at
updated_at
producer_version
```

需要：

```text
Schema Migration
Workflow Version Pinning
Prompt Template Versioning
Model Route Versioning
```

长任务恢复时，默认使用启动时的 Workflow Version，避免运行中代码更新导致行为变化。

---

# 27. 缓存与成本控制

可缓存：

```text
文献元数据
全文解析结果
Evidence Passage
Context Bundle
确定性 Gate
编译结果
```

缓存键必须包含：

```text
输入 Blob Hash
工具版本
Parser 版本
Schema 版本
```

LLM 输出不能仅按用户文本缓存，必须包含完整 Context Hash 和模型版本。

---

# 28. 推荐代码边界

```text
src/paper_agent/
├── platform/
│   ├── task_analyzer/
│   ├── operation_planner/
│   ├── workflow_runtime/
│   └── collaboration/
│
├── engines/
│   ├── editing/
│   ├── research/
│   └── document/
│
├── kernel/
│   ├── artifacts/
│   ├── projects/
│   ├── policies/
│   ├── context/
│   ├── memory/
│   ├── claims/
│   ├── evidence/
│   ├── transactions/
│   ├── models/
│   └── tools/
│
├── governance/
│   ├── security/
│   ├── privacy/
│   ├── tracing/
│   ├── evals/
│   └── migrations/
│
├── api/
├── cli/
└── tests/
```

业务 Engine 不直接依赖具体模型 SDK、数据库驱动或 LangGraph API，而通过 Kernel 接口调用。

---

# 29. 部署建议

## 单用户本地版

```text
FastAPI / CLI
SQLite
本地 Content Store
LangGraph 或自定义状态机
Git 适配器
本地/云端模型路由
```

## 团队生产版

```text
API Service
PostgreSQL
Object Store
Queue / Durable Workflow
Search Index
Isolated Worker
Sandbox Worker
Observability Backend
```

不要在 MVP 阶段直接部署完整微服务。优先模块化单体。

---

# 30. 分阶段实施路线

## Phase 0：不可跳过的基础

```text
Artifact Blob / Head
Candidate Patch
Operation Contract
权限模型
Trace Schema
20～50 个 Golden Cases
```

## Phase 1：可靠 Polish 垂直链路

```text
导入一个格式
→ 定位章节
→ Writer
→ 数字/引用/含义 Gate
→ Diff
→ Commit Manifest
→ Rollback
```

## Phase 2：可靠 Rewrite

```text
Project Brief
Task Contract
冻结 Claim/Evidence
Writer–Verifier
Dynamic Gate Plan
```

暂不自动搜索文献。

## Phase 3：Evidence MVP

仅实现：

```text
StructuredTableAdapter
LiteraturePassageAdapter
Claim–Evidence Link
```

## Phase 4：Research Engine

```text
Search
Metadata
Abstract
Fulltext
Passage
Evidence Set
```

## Phase 5：多 Session 与记忆

```text
User Memory
Project Memory
Decision Log
Context Compiler
```

## Phase 6：Reviewer Response 与 Add Section

## Phase 7：Document Engine 高级能力

```text
DOCX Part Audit
跨格式 Fidelity Report
复杂 LaTeX
```

## Phase 8：团队与生产化

```text
Durable Workflow
并发
隐私策略
Sandbox
SLO
```

---

# 31. Phase 1 验收标准

开始扩展前，必须证明：

1. 正式 Artifact 不能被 Writer 直接覆盖；
2. Candidate Patch 可重放；
3. Base Hash 变化时 Patch 被拒绝；
4. 数字交换被阻断；
5. 引用跨 Claim 移动被阻断；
6. 删除否定词被发现；
7. 每次运行具有完整 Trace；
8. 失败后可恢复或明确回滚；
9. 模型升级可以跑固定 Eval；
10. 项目 A 的数据不能进入项目 B。

---

# 32. 终版架构摘要

```text
Operation Plan
+
Independent Engines
+
Shared Kernel
+
Workflow Runtime Abstraction
+
Requested Capability / Granted Permission Separation
+
Immutable Artifact Blob + Atomic Head
+
Candidate Patch
+
Claim–Evidence for High-value Claims
+
Evidence Adapters
+
Node-specific Context Compiler
+
Dynamic Gate Plan
+
Bounded Search
+
Privacy-aware Model Routing
+
Commit Manifest / Saga
+
Versioned Schema / Workflow
+
Evals from Day 0
```

---

# 33. 自我审查：这版架构仍然不完美的地方

## 33.1 复杂度仍然较高

完整目标架构包含多个 Engine、知识模型、权限、评测和提交协议。

缓解方式：

- 采用模块化单体；
- 按 Phase 垂直实现；
- MVP 不实现 Durable Workflow、通用 Evidence Adapter 和复杂跨格式转换。

---

## 33.2 Claim–Evidence 无法覆盖所有学术真理

它擅长管理可定位事实，但不能自动证明：

- 研究问题是否重要；
- 理论解释是否真正有洞察；
- 创新性是否成立；
- 文献覆盖是否完整；
- 定性分析是否可信。

因此它是“可追溯支持系统”，不是自动真理判定器。

---

## 33.3 Semantic Verifier 仍然可能犯错

即使职责分离，Verifier 仍可能漏检或误报。

缓解方式：

- 高风险 Claim 使用规则、独立 Verifier 和人工组合；
- 使用经真实样本校准的置信等级；
- 不把模型自报 confidence 当作硬证据；
- 监测误报率和漏报率。

---

## 33.4 文献全文经常不可用或难以解析

无法保证每篇文献都能定位 Evidence Passage。

缓解方式：

- 状态显式降级；
- 支持用户上传；
- 使用替代文献；
- 降低 Claim 强度；
- 不把摘要级信息伪装成全文验证。

---

## 33.5 跨格式转换无法实现普遍意义的无损

复杂宏、嵌入对象、浮动布局、Word 域和修订记录可能无法完整转换。

缓解方式：

- 格式特定验证；
- Capability Scan；
- Fidelity Report；
- 高风险文档人工验收；
- 不把通用 IR 作为早期核心依赖。

---

## 33.6 Operation Planner 可能错误拆分任务

模型可能遗漏依赖或生成不必要操作。

缓解方式：

- 常见组合使用规则模板；
- Operation Plan 通过 Schema 和 Policy 检查；
- 高风险计划先展示给用户；
- 执行节点不能因为计划错误而扩大权限。

---

## 33.7 Durable Workflow 与 LangGraph 组合可能过度设计

在单用户或轻量产品中，两者同时使用没有必要。

缓解方式：

- 定义 Runtime 抽象；
- MVP 只选择一种；
- 真正出现长等待、跨进程恢复和并发需求后再引入 Durable Runtime。

---

## 33.8 数据治理依赖实际部署环境

Provider 合规、数据驻留和日志策略不能只靠代码架构解决。

缓解方式：

- 项目级策略；
- Provider 白名单；
- 本地模型路线；
- 日志最小化；
- 部署前安全审计。

---

## 33.9 评测集可能逐渐失效

论文类型、模型和用户行为变化会导致 Eval 漂移。

缓解方式：

- 将真实失败案例持续加入回归集；
- 按学科和任务分层评测；
- 定期人工复核；
- 不只追求一个总分。

---

## 33.10 没有架构能保证论文最终正确

Paper Agent 可以减少错误、提高可追溯性，但最终学术责任仍属于研究者。

系统必须在 UI 和交付物中区分：

```text
program_verified
model_assessed
user_confirmed
unverified
```

---

# 34. 最终决策

这版架构可以作为长期目标，但不应一次全部实现。

真正应该立即开始的，是：

```text
一个 Artifact
→ 一个 Operation Contract
→ 一个 Writer
→ 一个 Candidate Patch
→ 三个关键 Gate
→ 一个 Commit Manifest
→ 一套 Golden Eval
```

这条链路稳定后，再扩展 Evidence、Research、Memory 和 Document Engine。

最终理想不是“最自主的 Agent”，而是：

> **一个能够明确知道自己在修改什么、依据什么、允许做什么、哪里不确定、如何恢复、如何证明结果可靠的科研写作系统。**
