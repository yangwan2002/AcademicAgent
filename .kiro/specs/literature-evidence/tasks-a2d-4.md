# Implementation Plan — A2d-4：LiteraturePreflight 领域服务 + 可恢复编排 + typed port

> 承接 design-a2d D3/D4/D9/D10。本批把「写作前把文献找齐」的**领域计算**与**可恢复状态机
> 编排**建起来，并定义 typed 交互 port + checkpoint 持久化。
> **仍不接写作链路、不翻 flag**——rewrite/add_section 接线与激活属 A2d-5。

## Overview

- **领域层** `literature/preflight.py`（纯，不接 interactor、不 import `tasking`/`application`）：
  可用文献统计、AcquisitionGap、内部 RetrievalQuota 派生（冗余系数）、
  `AcceptedQuotaDeviation` 与 `effective_constraints`。
- **编排层** `application/literature_flow.py`：`WritingLiteratureOrchestrator` 状态机
  （ASSESSING → RETRIEVING → NEEDS_BATCH_CONFIRMATION → CONFIRMED → COMMITTED →
  REASSESSING → NEEDS_GAP_DECISION → BUILDING_BUNDLE → NEEDS_BUNDLE_CONFIRMATION →
  READY_TO_WRITE / CANCELLED）+ `PreflightCheckpoint` + `LiteratureInteractionPort`。
- **持久化**：checkpoint 进 facts 单 envelope 的 `pending_literature_flows`（旧 schema 缺字段
  按空）。**不持久化 EvidenceBundle 原文**。

**验收铁律**：既有测试全绿（当前 778 快照）；写作路径逐字节不变（本批不接 rewrite/
add_section、flag 仍关、槽仍未挂）；状态机/领域计算全离线可测（stub loop/interactor）；
`literature/preflight.py` 不 import `tasking`/`application`；**非交互路径绝不默认接受**
（返回 NEEDS_* 或保守取消）。

关键纪律（承接 design-a2d D3/D4/D9/D10）：

- **派生 RetrievalQuota 不篡改原配额**（Req 13.6/13.7）：`kind=RETRIEVAL`/`scope=NEW_ONLY`、
  **完整复制** EligibilityFilter、只按 BELOW gap × 冗余系数（`>=1` 且有上限）推下界；
  **total/zh/en 是同一候选集上的重叠约束，不能三个数相加**；原 CitationQuota 全程 frozen。
- **只对 lower bound 触发检索**：纯 max/above 维度不触发检索（但仍由最终 Gate 执行）。
- **偏差是有数量的记录**（Req 13.11 同步后口径）：`AcceptedQuotaDeviation` 带
  `original_lower_bound` / `achieved_at_decision` / `effective_lower_bound` / stage /
  作用域（operation_id + task_fingerprint + quota_hash）；**不做维度级布尔豁免**、**不跨任务复用**。
- **commit 后必须重新统计**（Req 13.9）：不能直接相信 Loop 的预览计数。
- **交互用独立 typed port**（D9）：不复用 `PlanningInteractor.confirm_plan()`；literature 领域
  模块不 import 该 port。
- **资产提交不回滚**（D10）：commit 成功后 Bundle/LLM/Gate/写盘失败均不回滚 Ledger。

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2", "3"] },
    { "wave": 3, "tasks": ["4"] },
    { "wave": 4, "tasks": ["5"] }
  ]
}
```

- Task 1（领域服务）是编排的地基。
- Task 2（typed port + checkpoint 模型）与 Task 3（facts `pending_literature_flows` 持久化）
  不同文件，可并行。
- Task 4（状态机编排）依赖 1/2/3。
- Task 5 结构验收 + 全套回归。

## Tasks

- [ ] 1. Preflight 领域服务（`literature/preflight.py`）
  - `AcceptedQuotaDeviation`（frozen）：`stage`（`preflight` | `context_budget` | `final_gate`）/
    `dimension` / `original_lower_bound` / `achieved_at_decision` / `effective_lower_bound` /
    `original_gap` / `task_fingerprint` / `operation_id` / `quota_hash` / `accepted_at`。
  - `quota_hash(quota) -> str`：对 QuotaSpec 的 canonical 内容算稳定哈希（偏差作用域绑定用）。
  - `ensure_relevance(criteria, records, *, judge, store, budget, judge_key) ->
    RelevancePreparation`：对缺当前 fingerprint 结论的记录在预算内补判（复用 A2a Judge/Store）；
    `status=ERROR` **不伪装 unrelated、也不计可用**；返回补判数/错误数/预算是否耗尽。
  - `assess_availability(quota, criteria, ledger, relevance_store, *, classifier, policy,
    judge_key) -> Availability`：只统计**同时**满足 STRICT VERIFIED（`status_under_policy`）+
    过全部 filters + 当前 fingerprint relevance 已接受 的**不同 reference_id**；
    产 `counts`（复用 A2b `count_by_dimension`）+ `available_ids`。
  - `acquisition_gaps(availability, constraints) -> tuple[QuotaGap, ...]`：复用 A2b
    `evaluate_constraints`，**只保留 BELOW 方向**（下界缺口才触发检索；ABOVE 留给最终 Gate）。
  - `derive_retrieval_quota(gaps, citation_quota, *, redundancy_factor, max_target) -> QuotaSpec`：
    `kind=RETRIEVAL`/`scope=NEW_ONLY`；**完整复制** `citation_quota.eligibility_filters`；
    每维目标 = `min(ceil(gap.amount * redundancy_factor), max_target)`，`redundancy_factor >= 1`；
    **各维度独立**（total/zh/en 不相加）；原 quota 对象不被修改（返回新 QuotaSpec）。
  - `effective_constraints(citation_quota, deviations) -> tuple[CountConstraint, ...]`：
    对每个有匹配 `AcceptedQuotaDeviation`（同 dimension + 同 operation/fingerprint/quota_hash）
    的维度，把下界替换为 `effective_lower_bound`；**上界不变**（exact/range 的 high 保持）；
    无偏差的维度原样。
  - 不 import `tasking`/`application`；不读全局 config（redundancy/policy/judge 注入）。
  - 测试：availability 组合（未 STRICT / relevance ERROR / 不过 filter 各自不计）；
    gaps 只 BELOW；derive 复制 filters 且不改原 quota、冗余取整与上限、三维不相加；
    effective_constraints 只改下界、不跨任务复用（不同 fingerprint/quota_hash 不生效）；
    ensure_relevance 的 ERROR 不计可用 + 预算耗尽标注。
  - _Requirements: 13.2, 13.3, 13.5–13.8, 13.11, 14.6_
  - _Design: design-a2d.md D3, D4_

- [ ] 2. Typed 交互 port + Checkpoint 模型（`application/literature_ports.py`）
  - `LiteratureInteractionPort`（Protocol，窄）：
    `confirm_retrieval_batch(summary) -> BatchDecision` /
    `resolve_quota_gap(report) -> GapDecision` /
    `confirm_evidence_bundle(summary) -> BundleDecision` /
    `resolve_final_gate(report) -> FinalGateDecision`。
    各 Decision 为 frozen（`accept|reject|cancel` + 可选备注）。**不复用**
    `PlanningInteractor.confirm_plan()`。
  - `PreflightCheckpoint`（frozen）：`flow_id` / `operation_id` / `task_fingerprint` /
    `quota_hash` / `state` / `batch_id` / `revision` / `gaps`（可序列化形态）/
    `deviations: tuple[AcceptedQuotaDeviation, ...]` / `initial_query` / `created_at`。
    **不含** EvidenceBundle 原文、不含摘要/查询原文（隐私，D11）。
  - `checkpoint_to_dict` / `checkpoint_from_dict`（供 Task 3 持久化）。
  - `application` 可 import `literature`（方向正确）；`literature` **不** import 本模块。
  - 测试：Protocol 一致性（stub 实现 isinstance）；checkpoint 序列化往返；断言 checkpoint
    不含 bundle/摘要字段。
  - _Requirements: 9.7, 10.6, 13.10_
  - _Design: design-a2d.md D9, D3（checkpoint）_

- [ ] 3. facts 单 envelope 增 `pending_literature_flows`（`session/facts.py`）
  - envelope 增 `pending_literature_flows: list[dict]`（checkpoint 序列化）；`schema_version`
    升到 **4**；`load()` 缺该段按空（旧 v1/v2/v3 无损）；仍走**单次** `atomic_write_json`。
  - `SessionFacts.pending_literature_flows` 属性（默认 `[]`）；提供
    `put_literature_flow(dict)` / `drop_literature_flow(flow_id)` / `get_literature_flow(flow_id)`
    三个小方法（按 flow_id 覆盖/删除/取），保持 envelope 单一事实源。
  - 加载损坏的 flow 条目（缺 flow_id / 非 dict）→ **隔离该条目并报告**，不影响 Ledger 与其余
    flow（复用 A2c-5 pending batch 的隔离思路）。
  - 测试：v4 往返；旧 v3 加载后 flows 为空；损坏条目被隔离且 Ledger 不变；put/drop/get 语义。
  - _Requirements: 1.13（合并不覆盖精神）, 13.10（跨轮恢复）_
  - _Design: design-a2d.md D3（checkpoint 持久化）, D10_

- [ ] 4. WritingLiteratureOrchestrator 状态机（`application/literature_flow.py`）
  - `FlowState(str, Enum)`：`ASSESSING` / `RETRIEVING` / `NEEDS_BATCH_CONFIRMATION` /
    `CONFIRMED` / `COMMITTED` / `REASSESSING` / `NEEDS_GAP_DECISION` / `BUILDING_BUNDLE` /
    `NEEDS_BUNDLE_CONFIRMATION` / `READY_TO_WRITE` / `CANCELLED`。
  - `WritingLiteratureOrchestrator.run(request, *, deps, port, facts, limits, planner,
    redundancy_factor, supervised=True) -> FlowOutcome`：
    1. **ASSESSING**：`assess_availability` + `acquisition_gaps`；gap=0 → BUILDING_BUNDLE。
    2. **RETRIEVING**：`derive_retrieval_quota` → A2c `run_retrieval_loop`（STRICT）→
       batch 收束 STAGED → **先写入 facts pending 区** → NEEDS_BATCH_CONFIRMATION。
    3. **NEEDS_BATCH_CONFIRMATION**：`port.confirm_retrieval_batch(render_batch_summary(batch))`；
       reject → batch REJECTED + Ledger 不变 → CANCELLED；accept → CONFIRMED →
       A2c-5 `commit_batch`（单 envelope 原子）→ COMMITTED → REASSESSING。
    4. **REASSESSING**：**重新** `assess_availability`（不信 Loop 预览）；gap=0 → BUILDING_BUNDLE；
       否则 NEEDS_GAP_DECISION。
    5. **NEEDS_GAP_DECISION**：`port.resolve_quota_gap(report)`；cancel → CANCELLED；
       accept → 生成 `AcceptedQuotaDeviation(stage=preflight, effective_lower_bound=当前可用)`
       → BUILDING_BUNDLE。
    6. **BUILDING_BUNDLE**：`effective_constraints` → A2d-2 `build_bundle`；
       `CONTEXT_BUDGET_INSUFFICIENT` → 走 `port.resolve_quota_gap`（stage=context_budget）或
       CANCELLED，**不进 Writer**。
    7. **NEEDS_BUNDLE_CONFIRMATION**（supervised 默认 True，A2c-5 评测门槛）：
       `port.confirm_evidence_bundle` → accept → READY_TO_WRITE；reject → CANCELLED。
       `supervised=False` 仅在评测达标后由组合层显式传入。
  - **非交互路径**：port 为 None 或返回 `needs_input` → 保存 checkpoint、返回
    `FlowOutcome(state=NEEDS_*)`，**绝不默认 accept**。
  - **恢复**：`resume(flow_id, facts, ...)` 从 checkpoint 继续（校验 operation_id +
    task_fingerprint + quota_hash 一致，不一致明确拒绝）。
  - `FlowOutcome`（frozen）：`state` / `bundle`（READY_TO_WRITE 时）/ `effective_constraints` /
    `deviations` / `availability` / `gaps` / `checkpoint` / `message`。
  - 本模块**不**调 writer LLM、不写 Working Copy（那属 A2d-5）。
  - 测试（stub loop/port/facts）：gap=0 直达 READY_TO_WRITE（**不触发检索**）；
    gap>0 → 检索 → 批次确认 accept → commit → 重统计 → 满足 → READY；
    批次 reject → CANCELLED + Ledger 不变；重统计仍有 gap → NEEDS_GAP_DECISION →
    accept 产 deviation（effective=当前可用）→ READY；cancel → CANCELLED；
    Bundle 预算不足 → 不进 READY；**port=None 时返回 NEEDS_* 且不默认接受**；
    checkpoint 存/恢复（含 fingerprint 不一致被拒）；commit 后 Bundle 构造失败不回滚 Ledger。
  - _Requirements: 13.1–13.12, 9.7, 10.6_
  - _Design: design-a2d.md D3, D4, D9, D10_

- [ ] 5. 结构验收与全套回归
  - 全套既有测试绿（当前 778 快照，新增后总数上升）；新增单测全绿。
  - AST：`literature/preflight.py` 不 import `tasking`、不 import `application`、不 import `config`；
    `application/literature_flow.py` 可 import `literature`（正向）。
  - **写作路径逐字节不变**：`citation_quota` 仍不在 Active spec；`literature_writing_integration`
    仍 False；rewrite/add_section/generator 未被本批修改。
  - facts：v4 往返 + 旧版本无损；仍单 envelope 一次原子写。
  - 输出测试摘要并清理临时文件。
  - _Requirements: A2d-4 验收铁律_
  - _Design: design-a2d.md Correctness Properties 4/5/6/12_

## Notes

- 每完成一个 Task，先说明变更与验证结果，再进入下一项。
- **本批不接线**：槽激活 + Handler 路由 + Writer Loop 合并 Gate + trace + 回滚边界属 **A2d-5**。
- 非交互/Shadow 路径**绝不默认接受**偏差或批次；只返回 NEEDS_INPUT 或保守取消。
- Discovery 保持关闭（A2c-5 Task 3 挂账）。
- git 提交由用户自行完成。

## 已定决策

- 领域层（preflight.py）纯计算、不接 interactor；编排层（literature_flow.py）持有状态机与外部调用。
- 派生 RetrievalQuota 完整复制 filters、按 BELOW gap × 冗余系数（各维独立不相加）、原配额 frozen。
- 偏差是有数量的 `AcceptedQuotaDeviation`（含 effective_lower_bound + 作用域），不跨任务复用；
  `effective_constraints` 只改下界、不动上界。
- commit 后必须重新统计；commit 成功后的后续失败不回滚 Ledger。
- 交互用独立 typed `LiteratureInteractionPort`；literature 不 import 它。
- checkpoint 进 facts 单 envelope（schema v4），不含 Bundle/摘要/查询原文。
- supervised 默认 True（A2c-5 评测门槛）；非交互不默认接受。
