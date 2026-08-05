# Implementation Plan — A2c-2：ProviderRegistry + Batch 地基（不可变快照 + 状态机 + 内存 CommitPlan）

## Overview

A2c 的第二期，建 **Registry + Batch 地基**：能力感知的 `ProviderRegistry`、不可变
`CandidateSnapshot` / `ProposedRecordSnapshot`、`RetrievalBatch` 显式幂等状态机、
以及**纯内存**的 CommitPlan 试算 + revision/幂等校验骨架。

**本期不做**：不跑完整 Attempt（disposition 判定属 A2c-3）、不跑 Loop、不接写作链路、
**不宣称跨磁盘原子事务**（单 envelope 落盘 + 崩溃恢复属 A2c-5，本期 CommitPlan 只在内存
副本上试算）、不改旧 `search_and_verify`。

**验收铁律**：既有测试全绿（当前 653 快照）；Registry/Batch/CommitPlan 全部**离线纯逻辑
可测**，不发网络请求；`literature` 不 import `tasking`；快照真正不可变（深冻结，外部改动
原候选不影响 Batch）。

关键纪律（承接 design-a2c D1/D5/D6/D7）：

- **快照不可变、且不是第二套可写文献实体**（D5 + Glossary）：`ReferenceEntry/
  RetrievalCandidate` 含可变 dict/list，不能直接放进 Batch；`CandidateSnapshot` 用 tuple +
  `content_hash` 深冻结。`ProposedRecordSnapshot` 是**拟提交命令**（canonical JSON + 目标
  reference_id + MERGE 的 `expected_base_hash`），**不是** `ReferenceRecord`——真正的
  `ReferenceRecord` 只在 commit 时构造并写入 Ledger。
- **disposition 恰好一个 + 派生计数**（Req 9.1/9.2/9.4/9.6）：`items` 是唯一事实源；
  accepted/reused/merge/reject/counted/ledger_delta 全为派生视图，不独立存储。
- **状态机显式、commit 幂等**（D6）：`RUNNING→STAGED→CONFIRMED→COMMITTED`，另 `→REJECTED`；
  非法跳转拒绝；`commit(COMMITTED)` 幂等；不引入 requirements 之外的 ABORTED 状态。
- **本期 CommitPlan 只在内存试算**（D7 内存部分）：校验 CONFIRMED + revision + `expected_base_hash`
  + batch_id 幂等键，在 **Ledger 副本**上应用 ACCEPT_NEW/MERGE 得「拟提交后账本」，
  **不落盘、不宣称跨文件原子性**（那属 A2c-5）。

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2"] },
    { "wave": 2, "tasks": ["3"] },
    { "wave": 3, "tasks": ["4"] },
    { "wave": 4, "tasks": ["5"] }
  ]
}
```

- Task 1（快照与 disposition 值对象）与 Task 2（ProviderRegistry）不同文件、互不依赖，可并行。
- Task 3（RetrievalBatch 状态机 + 派生计数）依赖 Task 1 的 BatchItem/快照。
- Task 4（内存 CommitPlan 试算 + 幂等/revision 校验）依赖 Task 3。
- Task 5 结构验收 + 全套回归。

## Tasks

- [ ] 1. 不可变快照、disposition 与 BatchItem（`literature/retrieval/batch.py` 值对象部分）
  - `CandidateSnapshot`（frozen，深冻结）：`title` / `authors: tuple[str,...]` / `year` /
    `abstract` / `work_identifiers: tuple[tuple[str,str],...]` / `container_identifiers` /
    `source` / `content_hash`。提供 `from_candidate(RetrievalCandidate) -> CandidateSnapshot`：
    把可变 dict/list 转 tuple、算 `content_hash`（对规范化后的关键字段算 sha1，NFC 一致）。
    **外部事后修改原候选的 dict/list 不得影响已建快照**（值拷贝 + tuple）。
  - `BatchDisposition(str, Enum)`：`ACCEPT_NEW` / `REUSE_EXISTING` / `MERGE_DUPLICATE` / `REJECT`。
  - `ProposedRecordSnapshot`（frozen）：`target_reference_id` / `expected_base_hash: str|None`
    （ACCEPT_NEW 为 None；MERGE 用于乐观并发校验）/ `canonical_record_payload: str`
    （规范 JSON，commit 时校验并构造 ReferenceRecord）。
  - `BatchItem`（frozen）：`item_id` / `candidate_snapshot` / `proposed_record_snapshot|None` /
    `disposition` / `target_reference_id|None` / `language_verdict|None` / `relevance|None` /
    `verification_events: tuple[...]` / `rejection_reasons: tuple[str,...]`。
  - **item 一致性校验**（Req 9.2，供构造时/测试断言）：
    - ACCEPT_NEW / MERGE_DUPLICATE 必须有 `proposed_record_snapshot`；REUSE_EXISTING / REJECT 必须没有；
    - ACCEPT_NEW / REUSE_EXISTING / MERGE_DUPLICATE 的 `target_reference_id` 非空，REJECT 可空；
    - 一个候选可有多个 `rejection_reasons`，不压成单字符串。
    提供 `validate_item(item) -> tuple[str,...]`（返回违规项，空=合法）。
  - 不 import `tasking`；`language_verdict`/`relevance` 复用 A2a 类型（可为 None，A2c-3 才填）。
  - 测试：`from_candidate` 深冻结（改原 dict 不影响快照）；`content_hash` 稳定/NFC 一致；
    `validate_item` 对各 disposition 的必填/禁填组合判定正确。
  - _Requirements: 9.1, 9.2, 9.5_
  - _Design: design-a2c.md D5_

- [ ] 2. 能力感知的 ProviderRegistry（`literature/retrieval/registry.py`）
  - `NamedProvider`（frozen）：`name` / `provider` / `profile: SourceProfile`（复用 A1 `sources.py`）。
  - `ProviderRegistry`：`search_sources` / `verify_sources` / `discovery_sources`（默认空）三组
    `tuple[NamedProvider,...]`，按注入顺序（组合层据 `config.literature_source_priority` 排）。
  - 构造时**校验声明角色与实际能力一致**：`search_sources` 的每个 provider 符合 `SearchProvider`
    协议且 profile.roles 含 SEARCH；verify/discovery 同理。不一致 → 明确报错，不静默接受。
  - `verify_providers_for(id_type) -> tuple[(name, provider),...]`：按类型给出可核验源（供 A2c-1
    CrossVerifier 消费），final 优先、corroboration 其次，用 `sources.verification_authority` 判定。
  - 未知源无默认 VERIFY 权（复用 A1 `profile_for` 的兜底档案）。
  - 不 import `tasking`；不读全局 config（组合层构造后注入）。
  - 测试（stub provider）：角色/能力一致性校验（含不一致报错）；`verify_providers_for("doi")`
    含 crossref 优先、openalex 靠后；discovery 默认空。
  - _Requirements: 11.1, 11.2, 11.5_
  - _Design: design-a2c.md D1_

- [ ] 3. RetrievalBatch 状态机 + 派生计数（`literature/retrieval/batch.py`）
  - `BatchStatus(str, Enum)`：`RUNNING` / `STAGED` / `CONFIRMED` / `COMMITTED` / `REJECTED`。
  - `RetrievalBatch`（运行态；`items` 是唯一事实源）：`batch_id` / `status` / `task_fingerprint` /
    `quota: QuotaSpec`（A2b）/ `verification_policy`（A2c-1）/ `authority_registry_version` /
    `items: tuple[BatchItem,...]` / `created_at` / `revision`。
  - **状态转移**：`stage()` / `confirm()` / `reject()` / `mark_committed()`——只允许
    `RUNNING→STAGED→CONFIRMED→COMMITTED` 与任意非终态 `→REJECTED`；非法跳转抛明确错误；
    `mark_committed()` 在已 COMMITTED 时**幂等**（不重复动作、返回既有结果）。不引入 ABORTED。
  - **派生计数**（Req 9.4/9.6，全部由 items 派生，不独立存储）：
    - `accepted_new_ids()` / `reused_ids()` / `merged_ids()` / `rejected_items()`；
    - `counted_reference_ids(scope)`：ACCEPT_NEW 恒计；REUSE_EXISTING / MERGE_DUPLICATE 仅
      scope=`ledger_total` 时计；按 `target_reference_id` 去重；
    - `ledger_delta()` = ACCEPT_NEW 条数。
  - `render_batch_summary(batch)`：结构化清单 DTO（标题/年份/来源/语言及置信/相关性/disposition
    + 派生计数），供 A2d 呈现（Req 9.7 数据面）；本期不接交互 UI。
  - 测试：合法/非法状态转移；`mark_committed` 幂等；派生计数在 new_only / ledger_total 各 scope
    正确、按 rid 去重；summary 字段完整。
  - _Requirements: 9.1, 9.2, 9.4, 9.6, 9.7_
  - _Design: design-a2c.md D6_

- [ ] 4. 内存 CommitPlan 试算 + 幂等/revision 校验（`literature/retrieval/transaction.py`）
  - `plan_commit(batch, ledger) -> CommitPlan`：**纯内存**试算，不落盘、不改传入 ledger。
    - 前置校验：`batch.status == CONFIRMED`（否则拒绝）；batch_id 幂等键；对每个 MERGE_DUPLICATE
      校验目标记录当前 hash == `expected_base_hash`（不符 → `revision_conflict`，禁止覆盖新数据）。
    - 在 **Ledger 副本**上应用：ACCEPT_NEW → 由 `canonical_record_payload` 构造 ReferenceRecord
      并 `upsert`；MERGE_DUPLICATE → 合并进目标记录。得「拟提交后账本」快照。
    - `CommitPlan`（frozen）：`ok: bool` / `projected_ledger`（副本，或其 to_json）/
      `conflicts: tuple[str,...]` / `ledger_delta` / `idempotency_key`。
  - `plan_commit` **不** mutate 主 Ledger、不写磁盘；真正落盘 + 单 envelope 原子事务 +
    崩溃恢复属 **A2c-5**（本期只交付内存试算 + 冲突检测骨架，如实标注）。
  - 已 COMMITTED 的 batch 再 `plan_commit` → 返回幂等结果（不重复增加）。
  - 测试（内存 Ledger）：CONFIRMED 才可 plan；非 CONFIRMED 拒绝；ACCEPT_NEW 试算得正确
    projected delta 且**主 Ledger 不变**；MERGE `expected_base_hash` 不符 → revision_conflict；
    幂等键重复 → 不重复计入。
  - _Requirements: 9.8, 9.9, 9.10（内存试算层；落盘属 A2c-5）_
  - _Design: design-a2c.md D7（内存部分）_

- [ ] 5. 结构验收与全套回归
  - 全套既有测试绿（当前 653 快照，新增 A2c-2 测试后总数上升）；新增单测/属性测试全绿。
  - AST 结构断言：`literature/retrieval/{batch,registry,transaction}.py` 不 import `tasking`、
    不 import `config`。
  - 确认**未接写作链路、未落盘**：`plan_commit` 不 mutate 主 Ledger、不写文件；旧
    `search_and_verify` 与 rewrite/add_section 产物逐字节不变。
  - 确认默认测试套件不发网络请求。
  - 输出测试摘要并清理临时文件；不修改 Working Copy 和 facts.json schema。
  - _Requirements: A2c-2 验收铁律_
  - _Design: design-a2c.md D1/D5/D6/D7, Correctness Properties 5/6_

## Notes

- 每完成一个 Task，先说明变更与验证结果，再进入下一项。
- 全套测试以本轮开始时实际收集到的既有测试为基线；新增 A2c-2 测试后总数应上升。
- A2c-2 **只建 Registry + Batch 地基**，不跑 Attempt/Loop、不接写作链路、不落盘。
- **本期 CommitPlan 只在内存副本上试算冲突/delta，不宣称跨磁盘原子事务**——真正的单
  envelope 原子落盘 + 崩溃恢复 + 故障注入测试属 A2c-5，届时 pending batch 与 RelevanceStore
  一并接入 project facts。
- git 提交由用户自行完成。

## 已定决策

- `items` 是 Batch 唯一事实源；disposition 恰好一个；所有计数派生、不独立存储。
- `CandidateSnapshot` 深冻结（tuple + content_hash），外部改原候选不影响快照；
  `ProposedRecordSnapshot` 是拟提交命令，**不是**第二套可写 `ReferenceRecord`（真正 record
  只在 commit 时构造）。
- 状态机 `RUNNING→STAGED→CONFIRMED→COMMITTED / →REJECTED`；非法跳转拒绝；commit 幂等；
  不引入 requirements 之外的 ABORTED。
- MERGE_DUPLICATE 用 `expected_base_hash` 做乐观并发；确认期间目标已变 → revision_conflict，
  禁止覆盖。
- 本期 CommitPlan 纯内存试算，不落盘、不宣称跨文件原子性；落盘属 A2c-5。
- Registry 构造时校验声明角色与实际能力一致；未知源无默认 VERIFY 权。
