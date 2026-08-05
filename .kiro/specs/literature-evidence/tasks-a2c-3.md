# Implementation Plan — A2c-3：Attempt 流水线（清洗 → 去重 → 预筛 → 严格核验 → 相关性 → disposition）

## Overview

A2c 的第三期，把 A1/A2a/A2b/A2c-1/A2c-2 的零件串成**单轮 Retrieval_Attempt**：
给定查询 + RelevanceCriteria + QuotaSpec 的 filters，执行
`SEARCH → 清洗(A2a) → 语言判定(A2a) → 精确去重(A1) → 资格预筛(A2b filter) → 严格核验
(A2c-1 CrossVerifier) → 保守模糊合并建议 → 相关性(A2a) → 生成 BatchItem(disposition +
ProposedRecordSnapshot)`。

**本期不做**：不跑多轮 Loop / QueryPlanner / 预算跨轮记账（A2c-4）、不接 Discovery
（A2c-5）、不落盘、不接写作链路。**单轮**用 stub SEARCH provider + stub reviewer 离线可测。

**验收铁律**：既有测试全绿（当前 685 快照）；Attempt 全程 stub 离线可测、不发网络；
`literature` 不 import `tasking`；**配额只由「已接受」条目推进**（Req 7.9），拒绝的候选
保留 BatchItem + 原因，不静默丢。

关键纪律（承接 design-a2c D9）——**先便宜后昂贵、保守不误删**：

- **核验前只做确定性精确去重**（work identifier 同值 / 完全相同稳定键），避免对重复候选
  重复核验；模糊相似度**只在核验 + 元数据规范化后**形成合并建议，**不在昂贵调用前
  不可逆删除**候选（低置信匹配保持独立 BatchItem）。
- **硬过滤预筛只对明确缺失/不满足的候选**（如 year 越界、reference_type 不符），且
  **不依据不可信字段做不可逆删除**；被预筛拒绝的候选仍保留 BatchItem + rejection_reason。
- **只有 strict verified 后才调相关性 Reviewer**（不为虚假候选浪费 LLM）。
- **title_only 的接受与否由显式 `RelevanceAcceptancePolicy` 决定**，不在 Attempt 内猜
  （A2a 已把 title_only 的 verdict 封顶 weakly_relevant）。
- **REUSE/MERGE 在当前 TaskFingerprint 下仍需相关性结论**，不复用其他任务结论（Req 7.4）。
- 候选文本（title/abstract）经 A2a `clean_external_text` 清洗后才进任何 LLM 提示（Req 17）。

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2"] },
    { "wave": 3, "tasks": ["3"] }
  ]
}
```

- Task 1（Attempt 支撑件：接受策略 + 拟提交快照构造 + 两阶段精确去重分类）是地基。
- Task 2（`run_attempt` 全流水线）依赖 Task 1，且同文件，故串行其后。
- Task 3 结构验收 + 全套回归。

## Tasks

- [ ] 1. Attempt 支撑件（`literature/retrieval/attempt.py`：策略 / 快照构造 / 精确去重分类）
  - `RelevanceAcceptancePolicy`（frozen）：`accepted_verdicts: frozenset[str]`（默认
    `{"relevant"}`；宽松可含 `weakly_relevant`）、`accept_title_only: bool`（默认 False，
    严格配额默认拒绝 title_only 的 relevant）。`is_accepted(assessment) -> bool`：
    仅 `status==SUCCESS` 且 `verdict in accepted_verdicts` 且（`basis != title_only`
    或 `accept_title_only`）为真。`status==ERROR` 恒 False（失败不伪装成接受/拒绝语义外）。
  - `build_proposed_record(candidate, *, ledger, verification_events) -> ReferenceRecord`：
    复用 A1 `migration.candidate_to_record` 得候选记录，挂上交叉核验事件；ACCEPT_NEW 时
    补 bibkey / 数字别名（复用 Ledger 的 `next_numeric_alias` / `make_bibkey` 口径，但**在
    副本或只读查询下取**，不 mutate 主 Ledger）。产出的记录即 `ProposedRecordSnapshot.
    payload_of` 的输入。
  - `classify_exact(candidate_record, *, ledger, batch) -> ExactMatch`：**两阶段确定性精确
    去重**——先批内（已 ACCEPT_NEW 的 work-key / target_reference_id），再对 Ledger
    （A1 `dedup.find_duplicate`，只取 high 置信 work-id 命中）。返回：
    - 命中 Ledger 已有 → `(REUSE_EXISTING | MERGE_DUPLICATE, target_reference_id)`：
      候选带需写入的新元数据（合并后记录 != 现有）→ MERGE_DUPLICATE（附 `expected_base_hash`
      = 现有记录当前哈希 + 合并后 payload）；否则 REUSE_EXISTING。
    - 命中批内已 ACCEPT_NEW → 标记 `batch_duplicate`（不产生第二个 ACCEPT_NEW）。
    - 无命中 → `none`（候选走后续核验/相关性，可能 ACCEPT_NEW）。
  - 不 mutate 主 Ledger；不读全局 config（阈值注入）；不 import `tasking`。
  - 测试（内存 Ledger + stub）：`is_accepted` 各组合（含 title_only 被默认拒、ERROR 恒 False）；
    `classify_exact` 的 Ledger 命中 REUSE vs MERGE（有无新元数据）、批内重复、无命中；
    MERGE 的 expected_base_hash 取自现有记录当前哈希。
  - _Requirements: 3.2, 3.6, 7.4, 7.9, 9.2, 9.3_
  - _Design: design-a2c.md D5, D9_

- [ ] 2. 单轮 Attempt 流水线（`literature/retrieval/attempt.py`：`run_attempt`）
  - `run_attempt(query, criteria, quota, *, registry, ledger, cross_verifier, judge,
    relevance_store, acceptance_policy, language_thresholds, language_policy, budget,
    verify_cache, now_iso) -> AttemptResult`。`AttemptResult`：`items: tuple[BatchItem,...]`
    + `stats`（候选数 / 各 disposition 计数 / 核验调用数 / 相关性结果数，供 A2c-4 trace）。
  - 流水线**严格按 D9 顺序**，逐候选产**恰好一个** BatchItem：
    1. **SEARCH**：调 registry 的 search_sources（本期单轮，按顺序取候选；失败源跳过、
       记 stats，不中断 Req 11.3）。
    2. **清洗 + 快照**：`CandidateSnapshot.from_candidate`（A2c-2）；title/abstract 经 A2a
       `clean_external_text`（进 LLM 前，Req 17）。
    3. **语言判定**：A2a `classify_language`（阈值注入）→ `language_verdict`。
    4. **精确去重**（Task 1 `classify_exact`）：命中 Ledger → REUSE/MERGE（仍需过 filter +
       当前任务 relevance 才计入，见下）；批内重复 → 不产第二个 ACCEPT_NEW。
    5. **资格预筛**（A2b `filter_matches` / `passes_all_filters`，用 quota.eligibility_filters）：
       明确不满足 → `REJECT`（rejection_reason=`filter:<field>`），**保留 BatchItem**；
       缺字段保守不通过（A2b 已定）。
    6. **严格核验**（A2c-1 `CrossVerifier.assess`，预算 + 缓存注入）：未达
       `meets_strict_threshold` → `REJECT`（rejection_reason=`verify:<...>`）。
    7. **相关性**（A2a `RelevanceJudge` + `RelevanceStore`，仅对 strict verified 候选）：
       先查 store（键含 content_hash + judge_key，命中且当前 task_fp 复用）；否则 assess 并
       存储。`acceptance_policy.is_accepted` 为假（含 unrelated / ERROR / title_only 被拒）→
       `REJECT`（rejection_reason=`relevance:<verdict|error>`）。
    8. **disposition 落定**：全通过且 Ledger 无 → `ACCEPT_NEW`（`build_proposed_record` +
       预留 target_reference_id）；命中 Ledger 且通过 → `REUSE_EXISTING` / `MERGE_DUPLICATE`。
  - **配额只由已接受推进**：只有 disposition ∈ {ACCEPT_NEW, (scope 允许时)REUSE/MERGE}
    且 relevance 已接受、过 filter、strict verified 的 item 计入（Req 7.9 / 10.2–10.4）；
    被拒候选一律 REJECT + 原因，不静默丢（Req 7.8）。
  - `run_attempt` 把新 items **追加**进传入的 `RetrievalBatch`（RUNNING 态），不改状态机、
    不落盘、不 mutate 主 Ledger。不读全局 config/时钟（全部注入）。
  - 测试（stub SEARCH + stub reviewer + 内存 Ledger）：
    - happy path：clean→zh/en 判定→strict verified→relevant→ACCEPT_NEW，proposed 快照正确；
    - filter 拒（年份越界）→ REJECT filter，保留 item；
    - 未达严格门槛 → REJECT verify，**且未调相关性 Reviewer**（先便宜后昂贵）；
    - unrelated / relevance ERROR / title_only relevant（默认策略）→ REJECT relevance；
    - 命中 Ledger 无新元数据 → REUSE_EXISTING；有新元数据 → MERGE_DUPLICATE；
    - 批内重复不产第二个 ACCEPT_NEW；
    - 单 SEARCH 源失败不中断、记 stats；
    - relevance 复用 store（同 content_hash + judge_key 不重复调用）；
    - 不可信 abstract（含「ignore previous instructions」）经清洗后才进 reviewer（结构断言）。
  - _Requirements: 5.x（消费）, 7.1–7.9, 9.2–9.5, 10.2–10.4, 11.3, 11.15, 17_
  - _Design: design-a2c.md D4, D9, Correctness Properties 2/4_

- [ ] 3. 结构验收与全套回归
  - 全套既有测试绿（当前 685 快照，新增 A2c-3 测试后总数上升）；新增单测全绿。
  - AST 结构断言：`literature/retrieval/attempt.py` 不 import `tasking`、不 import `config`。
  - 确认**未接写作链路、未落盘、未跑多轮**：`run_attempt` 不 mutate 主 Ledger、不写文件；
    旧 `search_and_verify` 与 rewrite/add_section 产物逐字节不变。
  - 确认默认测试套件不发网络请求（SEARCH/verify/reviewer 全 stub）。
  - 输出测试摘要并清理临时文件；不修改 Working Copy 和 facts.json schema。
  - _Requirements: A2c-3 验收铁律_
  - _Design: design-a2c.md D9, Correctness Properties 2/4_

## Notes

- 每完成一个 Task，先说明变更与验证结果，再进入下一项。
- 全套测试以本轮开始时实际收集到的既有测试为基线；新增 A2c-3 测试后总数应上升。
- A2c-3 **只跑单轮 Attempt**：多轮 Loop / QueryPlanner / 跨轮预算 / QuotaGap 汇总属 A2c-4；
  Discovery / 持久化 / 单 envelope commit 属 A2c-5。
- 真实相关性语义准确率不在本期宣称（stub reviewer 只验协议与失败语义）；真实评测属 A2c-5
  的独立报告，且为 A2d Active 前置门槛。
- git 提交由用户自行完成。

## 已定决策

- 流水线顺序**先便宜后昂贵**：确定性精确去重 → 预筛 → 严格核验 → 相关性；模糊合并建议
  推迟到核验后，**不在昂贵调用前不可逆删除**候选。
- 只有 strict verified 候选才调相关性 Reviewer；被各阶段拒绝的候选保留 BatchItem + 原因，
  不静默丢（Req 7.8）。
- title_only 接受与否由 `RelevanceAcceptancePolicy` 显式决定，默认拒绝 title_only 的 relevant。
- REUSE/MERGE 在当前 TaskFingerprint 下仍需相关性结论，不复用其他任务结论。
- 配额只由已接受条目推进（Req 7.9）；`run_attempt` 不 mutate 主 Ledger、不落盘、不改状态机。
- 单轮 Attempt 全 stub 离线可测；多轮 / QueryPlanner / Discovery / 持久化留后续期。
