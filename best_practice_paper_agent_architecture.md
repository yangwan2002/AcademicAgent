# 最佳实践版学术论文写作 Agent 架构设计

> 设计目标：构建一个真正可用于科研工作的学术论文写作 Agent。  
> 核心要求：可靠、可追溯、可验证、可恢复、可扩展，而不是追求表面的“自主性”。

---

# 1. 总体结论

最优架构不是纯 ReAct，也不是简单的“意图识别 + 固定次数 LLM”。

推荐采用：

> **分层控制架构：确定性工作流为主，局部有限自治；证据驱动生成；候选版本优先；程序、模型和用户分层验证。**

整体结构：

```text
用户请求
→ 项目/会话解析
→ 任务契约
→ 风险与执行策略
→ 上下文编译
→ 工作流执行
→ 结构验证
→ 来源验证
→ 语义验证
→ 用户审批
→ 事务提交
→ 记忆与评测写回
```

---

# 2. 核心设计原则

## 2.1 论文不是聊天结果，而是受控 Artifact

Agent 不直接“回答一篇论文”，而是操作：

```text
Project
→ Artifact
→ Candidate Version
→ Validation
→ Commit
```

正式论文文件永远不能被生成节点直接覆盖。

---

## 2.2 生成和验证必须分离

写作模型负责生成，验证系统负责判断。

```text
Writer
→ Program Validator
→ Evidence Validator
→ Semantic Verifier
→ Human Review
```

不要让同一次模型调用同时负责“写”和“判断自己写得对不对”。

---

## 2.3 Claim 与 Evidence 必须显式建模

学术写作的核心不是段落，而是：

```text
Claim：论文想表达什么
Evidence：依据是什么
Relation：证据能支持到什么程度
```

论文正文只是 Claim 的语言化表达。

---

## 2.4 自主性必须局部化

顶层工作流不使用自由 ReAct。

只有以下场景使用受限 Agent Loop：

- 开放式文献探索；
- 搜索策略动态调整；
- 多步证据定位；
- 复杂诊断。

其余任务由程序工作流控制。

---

## 2.5 能程序验证的，不交给 LLM

```text
字数
文件 Hash
引用键
表格数据
公式 AST
文档结构
版本
权限
```

由程序验证。

```text
Claim 是否被证据支持
语义是否漂移
是否过度推断
是否改变因果强度
```

由 LLM/NLI 验证。

高风险或低置信度问题交给用户。

---

# 3. 系统分层

推荐六层架构。

```text
Layer 1  接口层
Layer 2  控制平面
Layer 3  认知执行层
Layer 4  学术知识层
Layer 5  Artifact 与工具层
Layer 6  评测与治理层
```

---

# 4. Layer 1：接口层

支持：

- Web UI；
- CLI；
- IDE 插件；
- API；
- 文件上传；
- Git 仓库；
- 文献库连接。

接口层只负责：

- 用户输入；
- 文件选择；
- 项目选择；
- Diff 展示；
- 人工审批；
- 任务状态展示。

不负责业务推理。

---

# 5. Layer 2：控制平面

控制平面是整个系统最重要的部分。

包含：

```text
Project Resolver
Session Manager
Intent Router
Slot Filler
Task Contract Builder
Risk Classifier
Execution Policy Resolver
Workflow Orchestrator
Context Compiler
Tool Permission Resolver
Budget Manager
Gate Manager
Transaction Manager
```

---

# 6. Task Contract

任何任务执行前，都必须转化为结构化任务契约。

```yaml
task_contract:
  task_id: task_001
  project_id: paper_001
  session_id: session_001

  intent: rewrite

  target:
    artifact: manuscript/main.tex
    section: introduction
    paragraph_ids:
      - intro_p03
      - intro_p04

  objectives:
    - problem_first_structure
    - improve_argument_flow

  permissions:
    add_new_claims: false
    add_new_references: false
    change_numbers: false
    change_citations: false
    change_other_sections: false

  evidence_policy:
    verified_claims_only: true
    allow_abstract_only_reference: false

  risk_level: medium

  execution_policy:
    mode: writer_verifier
    max_llm_calls: 3
    max_tool_calls: 8
    max_retries: 2

  required_gates:
    - target_scope
    - numeric_binding
    - citation_binding
    - claim_support
    - semantic_consistency

  output:
    type: candidate_patch
```

Task Contract 是所有节点的共同约束来源。

---

# 7. Execution Policy

系统支持六种执行模式。

## 7.1 Program Only

用于：

- 字数统计；
- 引用键检查；
- LaTeX 编译；
- 格式转换；
- 表格一致性；
- 文件 Hash；
- DOCX 样式操作。

---

## 7.2 Single LLM

用于：

- 审稿意见结构化；
- 标题候选；
- 术语归一；
- 段落角色分类；
- 低风险局部写作。

---

## 7.3 Writer–Verifier

用于：

- 润色；
- 改写；
- 摘要；
- Discussion；
- 审稿回复；
- Cover Letter。

流程：

```text
Writer
→ Structural Gate
→ Provenance Gate
→ Semantic Verifier
→ Repair
```

---

## 7.4 Planner–Executor

用于：

- 全章重构；
- 新增章节；
- 论文提纲；
- 多条审稿意见修订；
- 全文一致性修改。

---

## 7.5 Bounded Search Agent

用于：

- 文献探索；
- 研究空白发现；
- 关键词动态扩展；
- 证据定位。

必须有：

```yaml
max_rounds: 5
max_queries: 8
max_papers: 50
max_runtime_seconds: 180
```

并且只能使用检索类工具。

---

## 7.6 Human Review

用于：

- 核心 Claim 低置信度；
- 证据冲突；
- 未能获取全文；
- 用户新提供的实验结果；
- 高风险写入；
- 不支持的复杂格式；
- 任意代码执行。

---

# 8. 工作流引擎

推荐使用 LangGraph 或自定义状态图。

核心节点：

```text
RESOLVE_TASK
INTERVIEW
BUILD_CONTRACT
LOAD_CONTEXT
RETRIEVE
SEARCH
VERIFY_EVIDENCE
PLAN
WRITE
VERIFY_STRUCTURE
VERIFY_PROVENANCE
VERIFY_SEMANTIC
REPAIR
HUMAN_REVIEW
COMMIT
EXPORT
FAIL
```

推荐图：

```text
RESOLVE_TASK
   ↓
BUILD_CONTRACT
   ↓
LOAD_CONTEXT
   ↓
EXECUTION_MODE
   ├── PROGRAM
   ├── WRITE
   ├── PLAN → WRITE
   └── SEARCH → VERIFY_EVIDENCE → WRITE
                         ↓
                VERIFY_STRUCTURE
                         ↓
                VERIFY_PROVENANCE
                         ↓
                 VERIFY_SEMANTIC
                   ├── PASS → COMMIT
                   ├── REPAIR → WRITE
                   └── HUMAN_REVIEW
```

---

# 9. Graph State

```python
class PaperAgentState(TypedDict, total=False):
    user_id: str
    project_id: str
    session_id: str
    run_id: str

    user_message: str
    task_contract: dict
    execution_policy: dict

    artifact_id: str
    artifact_version: int
    target_section: str | None

    active_claim_ids: list[str]
    active_evidence_ids: list[str]
    active_decision_ids: list[str]

    source_text: str | None
    plan: dict | None
    candidate_text: str | None
    candidate_path: str | None

    structural_result: dict | None
    provenance_result: dict | None
    semantic_result: dict | None

    retry_count: int
    llm_call_count: int
    tool_call_count: int
    search_round: int

    phase: str
    pending_human_action: dict | None
    errors: list[dict]
```

Graph State 只保存运行时数据与 ID，不保存所有项目知识。

---

# 10. 工具权限模型

采用三级权限。

```text
意图级白名单
∩
节点级白名单
∩
任务运行时权限
```

```python
effective_tools = (
    intent_policy.allowed_tools
    & node_policy.allowed_tools
    & task_contract.permitted_tools
)
```

---

## 10.1 工具分类

### Read

```text
read_section
read_artifact
retrieve_claim
retrieve_evidence
read_reference
read_analysis_output
```

### Candidate Write

```text
save_candidate
save_reference_candidate
save_evidence_candidate
create_draft
```

### Commit

```text
commit_candidate
update_project_state
write_decision
```

### High Risk

```text
modify_canonical_artifact
delete_reference
execute_python
submit_manuscript
send_email
```

高风险工具默认不暴露。

---

# 11. 学术知识层

这是 Paper Agent 区别于普通写作 Agent 的核心。

包含：

```text
Project Brief
Terminology
Claims
Evidence
Claim–Evidence Links
Numeric Facts
References
Citation Attachments
Decisions
Revision History
Reviewer Comments
```

---

# 12. Claim 模型

Claim 是可独立判断真假的原子主张。

```yaml
claim:
  claim_id: claim_014
  project_id: paper_001

  canonical_text: >
    MedAlign achieved higher AUROC than Baseline-A
    on the external validation test set.

  type: comparative_result

  slots:
    subject: MedAlign
    metric: AUROC
    comparator: Baseline-A
    dataset: external_validation
    split: test
    relation: greater_than

  scope:
    allowed_sections:
      - results
      - abstract
      - discussion

  strength_policy:
    allowed_level: selected_baseline_superiority
    forbidden_levels:
      - universal_superiority
      - statistical_significance
      - state_of_the_art

  status: verified
  version: 3
```

---

# 13. Evidence 模型

Evidence 是可定位、可重新验证的最小证据。

```yaml
evidence:
  evidence_id: ev_031
  project_id: paper_001
  type: table_comparison

  source:
    artifact: tables/table_3.csv
    artifact_version: 12
    document_hash: "sha256:..."
    proposed_row: MedAlign
    baseline_row: Baseline-A
    column: AUROC
    dataset: external_validation
    split: test

  measurements:
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

# 14. Claim–Evidence Link

```yaml
claim_evidence_link:
  link_id: link_041
  claim_id: claim_014
  evidence_id: ev_031

  verdict: supports
  support_scope: partial
  confidence: 0.96

  allowed_inference:
    - higher_than_baseline_a

  forbidden_inference:
    - universal_superiority
    - statistical_significance
    - state_of_the_art

  status: active
```

Evidence 本身真实，不代表它支持所有 Claim。

支持关系必须单独存储。

---

# 15. 文献系统

## 15.1 五级文献验证

```text
L0 metadata_verified
L1 abstract_available
L2 fulltext_available
L3 evidence_passage_located
L4 claim_support_verified
```

---

## 15.2 漏斗流程

```text
搜索候选
→ 元数据核验
→ 摘要筛选
→ 获取计划引用文献全文
→ 定位 Evidence Passage
→ Claim Support 验证
→ Evidence Store
```

不需要为所有候选文献下载全文。

---

## 15.3 文献 Evidence

```yaml
evidence:
  type: literature_passage

  source:
    reference_id: ref_023
    document_hash: "sha256:..."
    page: 7
    section: Results
    paragraph: 3

  passage:
    text: "..."

  verification:
    provenance_status: program_verified
    semantic_mapping_status: llm_verified
```

---

# 16. Numeric Fact

```yaml
numeric_fact:
  fact_id: num_031

  raw_text: "0.87 ± 0.03"

  normalized:
    value: 0.87
    uncertainty: 0.03

  meaning:
    metric: AUROC
    method: MedAlign
    dataset: external_validation
    split: test
    aggregation: mean

  source:
    artifact: tables/table_3.csv
    artifact_version: 12
    row_key: MedAlign
    column_key: AUROC
    document_hash: "sha256:..."

  verification:
    provenance_status: program_verified
    semantic_mapping_status: verified
```

数字必须绑定：

```text
值
+ 实体
+ 指标
+ 数据集
+ split
+ 来源
+ 版本
```

不能只保存一个字符串。

---

# 17. Citation Attachment

```yaml
citation_attachment:
  attachment_id: ca_021
  citation_id: ref_003
  paragraph_id: p_12
  sentence_id: s_31
  claim_id: claim_014
  relation: supports
```

Polish 前后可以检查：

- 引用是否跨句移动；
- 是否绑定到不同 Claim；
- 是否改变引用对象；
- 是否出现未核验引用。

---

# 18. 记忆系统

采用四层作用域。

```text
User Memory
Project Memory
Session State
Run Trace
```

---

## 18.1 User Memory

保存：

- 写作语言；
- 英式/美式英语；
- 解释语言；
- 输出风格；
- 稳定学术偏好。

---

## 18.2 Project Memory

保存：

- 研究问题；
- 核心贡献；
- 目标期刊；
- 术语；
- Claim–Evidence；
- 决策；
- 审稿意见；
- 项目状态。

---

## 18.3 Session State

保存：

- 当前任务；
- 当前候选；
- 活跃约束；
- 未解决问题；
- 最近对话；
- 待审批动作。

---

## 18.4 Run Trace

保存：

- 模型版本；
- Prompt 版本；
- 工具调用；
- Gate 结果；
- Token；
- 成本；
- 错误；
- 最终 Hash。

---

# 19. 上下文工程

上下文工程与 Agent Loop 无关。

任何 LLM 调用前都需要编译上下文。

推荐顺序：

```text
1. 系统规则
2. Task Contract
3. 当前有效约束
4. 项目简报
5. 相关用户偏好
6. 当前工作材料
7. 相关 Claim–Evidence
8. 关键历史决定
9. 必要的最近对话
10. 当前节点工具
11. 输出 Schema
```

---

## 19.1 按节点编译上下文

### Writer Context

```text
任务目标
当前材料
允许使用的 Claim
允许使用的 Evidence
禁止事项
用户风格偏好
输出格式
```

### Verifier Context

```text
原文
候选文本
Claim–Evidence
验证标准
结构化输出 Schema
```

### Repair Context

```text
原文
候选文本
Gate 失败项
必须保留内容
允许修改范围
```

### Search Context

```text
研究问题
检索范围
已有证据
当前覆盖缺口
搜索预算
```

---

# 20. 冲突解决

## 20.1 实验数字

```text
最新分析脚本输出
>
正式结构化结果表
>
program_verified Numeric Fact
>
user_confirmed Numeric Fact
>
论文旧正文
>
历史聊天
>
Agent 推断
```

---

## 20.2 用户偏好

```text
用户最新明确表达
>
用户历史明确表达
>
重复行为推断
>
Agent 单次推断
```

---

## 20.3 期刊规则

```text
当前官方投稿指南
>
项目配置
>
用户历史描述
>
模型已有知识
```

---

# 21. Gate 体系

## 21.1 Structural Gate

程序检查：

- 目标范围；
- 章节结构；
- 数字位置；
- 引用位置；
- 公式；
- 表格；
- 文件可打开；
- 输出 Schema。

---

## 21.2 Provenance Gate

程序检查：

- 文献来源；
- 数字来源；
- Evidence Passage；
- 文件版本；
- Hash；
- Claim–Evidence 绑定。

---

## 21.3 Semantic Gate

LLM/NLI 检查：

- 含义漂移；
- 否定词变化；
- 因果强度变化；
- 比较对象变化；
- Claim 是否受支持；
- 过度推断；
- novelty、significance、SOTA 强度。

---

## 21.4 Human Gate

触发：

- 核心 Claim 置信度低；
- Evidence 冲突；
- 全文不可用；
- 数字来源不唯一；
- 高风险写入；
- 复杂格式不支持；
- 任意代码执行。

---

# 22. Polish 工作流

```text
LOAD
→ WRITER
→ STRUCTURAL GATE
→ CITATION ATTACHMENT
→ NUMERIC ATTACHMENT
→ MEANING VERIFIER
→ REPAIR?
→ CANDIDATE
→ USER REVIEW
→ COMMIT
```

核心目标：

```text
只改变表达，不改变学术含义
```

---

# 23. Rewrite 工作流

```text
BUILD CONTRACT
→ LOAD PROJECT CONTEXT
→ OPTIONAL SEARCH
→ VERIFY EVIDENCE
→ WRITER
→ STRUCTURAL GATE
→ PROVENANCE GATE
→ SEMANTIC GATE
→ REPAIR?
→ USER REVIEW
→ COMMIT
```

---

# 24. Add Section 工作流

```text
SLOT FILL
→ SECTION PLAN
→ CLAIM PLAN
→ OPTIONAL SEARCH
→ VERIFY EVIDENCE
→ WRITER
→ GATES
→ USER REVIEW
→ COMMIT
```

Section Plan：

```yaml
section_plan:
  section: discussion
  purpose: interpret_results

  paragraphs:
    - role: summarize_result
      claim_ids:
        - claim_014

    - role: compare_prior_work
      evidence_ids:
        - ev_051

    - role: limitations
      decision_ids:
        - dec_021
```

---

# 25. Literature Review 工作流

```text
DEFINE QUESTION
→ DEFINE SEARCH PROTOCOL
→ BOUNDED SEARCH
→ DEDUPLICATE
→ ABSTRACT SCREEN
→ FULLTEXT RETRIEVE
→ EVIDENCE EXTRACT
→ EVIDENCE MATRIX
→ SYNTHESIS PLAN
→ WRITE
→ VERIFY
```

搜索 Agent 只能输出候选 Evidence，不能直接写入正式论文。

---

# 26. Reviewer Response 工作流

```text
PARSE COMMENTS
→ CLASSIFY REQUESTS
→ MAP TO MANUSCRIPT
→ BUILD RESPONSE PLAN
→ MODIFY CANDIDATE
→ VERIFY CHANGE
→ WRITE RESPONSE
→ CROSS-CHECK PROMISES
→ HUMAN APPROVAL
```

每条 Reviewer Comment 维护：

```yaml
review_item:
  comment_id: R2-C4
  requested_action: clarify_dataset_split
  response_strategy: agree
  manuscript_change_ids:
    - patch_031
  status: verified
```

---

# 27. Convert Format 工作流

```text
CAPABILITY SCAN
→ SOURCE IR
→ CONVERT
→ TARGET IR
→ IR COMPARE
→ COMPILE / OPEN
→ VISUAL DIFF
→ EXPORT
```

LLM 不作为最终格式审批者。

---

# 28. Canonical Document IR

```yaml
document:
  nodes:
    - id: section_01
      type: section
      order: 1
      text_hash: "..."

    - id: paragraph_04
      type: paragraph
      order: 4
      text_hash: "..."

    - id: equation_01
      type: equation
      order: 5
      math_ast_hash: "..."

    - id: table_01
      type: table
      order: 6
      matrix_hash: "..."
      caption_hash: "..."
```

程序可以检测：

- 公式互换；
- 图题错位；
- 表格数据改变；
- 段落顺序改变；
- 引用映射变化；
- 脚注丢失。

---

# 29. DOCX 格式系统

推荐：

```text
OOXML Capability Scan
→ 精确 XML Patch
→ Part Audit
→ Text Hash
→ Formula/Citation Check
→ Candidate
```

不要默认通过 `python-docx` 整体重写复杂文档。

---

# 30. 任意代码执行

默认关闭。

只有满足：

```text
用户明确同意
+
确定性工具无法完成
+
独立沙箱
```

才允许。

沙箱要求：

```text
无网络
只读输入
唯一可写输出
CPU/内存/时间限制
环境变量隔离
模块白名单
完整审计
```

---

# 31. Candidate 与事务

任何修改产生事务：

```yaml
transaction:
  run_id: run_028
  base_artifact_hash: "..."
  base_project_version: 17
  base_memory_version: 21

  candidate_artifact_hash: "..."
  candidate_project_version: 18
  candidate_memory_version: 22

  status: prepared
```

只有文件、Claim、Evidence、Decision 全部提交成功，才：

```text
prepared → committed
```

否则回滚。

---

# 32. 幂等与并发

## 32.1 幂等

```text
idempotency_key
=
project_id + task_id + operation_hash
```

避免重复插入章节、引用或提交。

## 32.2 并发

提交前检查：

```text
current_artifact_hash == base_artifact_hash
```

否则返回冲突，要求重新生成或合并。

---

# 33. 可观测性

每次 Run 记录：

```text
Task Contract
执行模式
模型版本
Prompt 版本
上下文摘要
工具调用
Gate 结果
重试
Token
成本
延迟
用户接受/拒绝
最终 Artifact Hash
```

---

# 34. 评测体系

## 34.1 Router

- 意图准确率；
- Slot 抽取准确率；
- 冲突处理率。

## 34.2 Writer

- 一次通过率；
- 用户接受率；
- 平均修改轮数；
- 事实错误率。

## 34.3 Gate

- 数字交换拦截率；
- 引用移动拦截率；
- 否定词变化拦截率；
- 因果增强拦截率；
- 未支持 Claim 拦截率。

## 34.4 Memory

- 跨 Session 恢复率；
- 跨项目污染率；
- 过期记忆误用率。

## 34.5 Tool

- 工具选择准确率；
- 参数正确率；
- 无效调用率；
- 重复调用率。

---

# 35. Mutation Test

主动构造错误：

```text
交换两个数字
移动引用
删除否定词
把 may 改成 causes
把 higher 改成 significantly higher
修改公式负号
改变表格单元格
新增未核验引用
修改非目标章节
```

确保 Gate 真正生效。

---

# 36. 推荐存储结构

```text
users/
  <user_id>/
    profile.json
    preferences.json

projects/
  <project_id>/
    project.json
    terminology.json
    claims.json
    evidence.json
    claim_evidence_links.json
    numeric_facts.json
    references.json
    decisions.json
    reviewer_comments.json
    artifacts/

sessions/
  <session_id>/
    state.json
    recent_turns.json
    pending_actions.json

runs/
  <run_id>/
    trace.json
    prompts.json
    tool_calls.json
    gate_results.json
```

生产环境建议使用 PostgreSQL + Object Storage。

---

# 37. 推荐代码目录

```text
src/paper_agent/
├── api/
├── cli/
├── graph/
│   ├── state.py
│   ├── builder.py
│   ├── routing.py
│   └── nodes/
├── tasks/
│   ├── contract.py
│   ├── router.py
│   └── slot_filler.py
├── policies/
│   ├── intent.py
│   ├── node.py
│   ├── risk.py
│   └── budget.py
├── context/
│   ├── compiler.py
│   ├── templates.py
│   ├── resolver.py
│   └── budgeter.py
├── memory/
│   ├── user_store.py
│   ├── project_store.py
│   ├── session_store.py
│   ├── event_store.py
│   └── extractor.py
├── claims/
│   ├── models.py
│   ├── claim_store.py
│   ├── evidence_store.py
│   ├── links.py
│   └── verifier.py
├── tools/
│   ├── literature/
│   ├── citation/
│   ├── numeric/
│   ├── document/
│   ├── latex/
│   └── docx/
├── gates/
│   ├── structural.py
│   ├── provenance.py
│   ├── semantic.py
│   └── manager.py
├── artifacts/
│   ├── store.py
│   ├── versioning.py
│   ├── transaction.py
│   └── ir.py
├── observability/
├── evals/
└── tests/
```

---

# 38. 推荐技术栈

```text
Python
FastAPI
Pydantic
PostgreSQL
Redis
Object Storage
LangGraph
Git
Pandoc
OOXML XML Patch
GROBID / PDF Parser
OpenAlex / Crossref
```

---

# 39. 实施顺序

## Phase 1：可靠编辑

```text
Task Contract
Writer–Verifier
Candidate Version
Structural Gate
Numeric/Citation Attachment
```

## Phase 2：证据系统

```text
Claim
Evidence
Claim–Evidence Link
Numeric Fact
Literature Passage
```

## Phase 3：记忆与上下文

```text
User/Project/Session/Run
Context Compiler
Conflict Resolver
Decision Log
```

## Phase 4：复杂工作流

```text
Planner–Executor
Bounded Search
Reviewer Response
Literature Review
```

## Phase 5：文档可靠性

```text
Canonical IR
Formula AST
OOXML Capability Scan
Visual Diff
```

## Phase 6：生产化

```text
Transaction
Idempotency
Optimistic Lock
Eval Registry
Regression Runner
Sandbox
```

---

# 40. MVP 建议

MVP 只做：

```text
polish
rewrite
citation_audit
```

核心能力：

```text
Task Contract
Writer–Verifier
Candidate Version
Claim–Evidence MVP
Numeric Binding
Citation Binding
Semantic Gate
```

不要一开始做：

- 多 Agent；
- 任意代码执行；
- 自动全文写作；
- 全量 DOCX IR；
- 自动投稿；
- 复杂长期规划。

---

# 41. 最终架构摘要

```text
Task Contract
+
Workflow Orchestrator
+
Node-level Tool Policy
+
Context Compiler
+
Layered Memory
+
Claim–Evidence System
+
Writer–Verifier
+
Bounded Search
+
Structural / Provenance / Semantic / Human Gates
+
Candidate-first Artifact Management
+
Transactions / Idempotency / Recovery
+
Evaluation / Tracing
```

最终目标不是“一个会写论文的聊天机器人”，而是：

> **一个以项目状态为核心、以证据为约束、以工作流为控制、以候选版本为交付、以评测和审计为保障的学术写作操作系统。**
