# Implementation Plan — A2c-4：配额驱动的有界 RetrievalLoop（QueryPlanner + 预算 + QuotaGap + trace）

## Overview

A2c 的第四期，把 A2c-3 的单轮 `run_attempt` 编排成**配额驱动、有界、可停、可诊断**的多轮
Retrieval_Loop：给定 Retrieval_Quota + RelevanceCriteria + 初始查询，反复执行 Attempt，
每轮用 A2b 评估配额缺口，未满足且预算允许则由 QueryPlanner 调整查询/换源继续，直到满足
或触及上限/无进展/取消。

**本期不做**：不接 Discovery（A2c-5）、不落盘 / 不接 project facts（A2c-5）、不接写作链路、
不改状态机的 commit（A2c-2 已就位）。多轮 Loop 全程用 stub provider + stub reviewer +
stub planner 离线可测。

**验收铁律**：既有测试全绿（当前 698 快照）；Loop / 预算 / QueryPlanner / QuotaGap 全部
离线可测、不发网络；`literature` 不 import `tasking`；**配额只由「已接受」的拟提交记录推进**
（Req 7.9 / 10.2–10.4）；Loop 全程不改 filters/constraints、不改 Task_Fingerprint、不提问
（Req 8.3/8.4/8.7）。

关键纪律（承接 design-a2c D10/D11/D12/D15）：

- **QueryPlanner 独立、默认 writer、不复用写作 prompt/历史**（D10）：只能改查询/源/检索参数，
  **不能**改 QuotaSpec / TaskFingerprint / RelevanceCriteria / 预算；失败走确定性 fallback，
  不把失败当「已无文献」。
- **预算覆盖所有昂贵资源 + 取消 + 无进展**（D11）：迭代/检索/核验/LLM/discovery/候选数/
  provider 时间；注入 monotonic clock + CancelToken；no-progress 比较规范化进度签名；
  只含 MAX 的配额初始即满足，不启动无意义 Loop。
- **配额进度用拟提交后的去重记录**（D12）：ACCEPT_NEW→proposed record、MERGE→合并后 record、
  REUSE→Ledger 现有、REJECT→不计；按 target_reference_id 去重后经 A2b
  `count_by_dimension`/`evaluate_constraints`（严格核验 + 当前 relevance + filters）。
- **trace 不落原始 query/候选文本**（D15）：每 Attempt 发隐私安全记录（query_hash/长度/
  strategy、源名/角色、候选数/核验数/相关性数、disposition 计数、配额进度、预算快照、
  耗时、stop_reason）。经**注入的 trace sink**（组合层接 EventBus），本期不改 observability 的
  EventKind 枚举（属 streaming-and-trace spec，避免跨 spec 改动）。

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2"] },
    { "wave": 2, "tasks": ["3"] },
    { "wave": 3, "tasks": ["4"] }
  ]
}
```

- Task 1（预算 + 停止原因）与 Task 2（QueryPlanner）不同文件、互不依赖，可并行。
- Task 3（RetrievalLoop）依赖 Task 1/2 + A2c-3 `run_attempt`。
- Task 4 结构验收 + 全套回归。

## Tasks

- [ ] 1. 预算、停止原因与取消（`literature/retrieval/budget.py`）
  - `RetrievalLimits`（frozen）：`max_iterations` / `max_provider_calls` / `max_search_calls` /
    `max_verify_calls` / `max_discovery_calls` / `max_llm_calls` / `max_candidates` /
    `provider_time_budget_s` / `max_no_progress_rounds`。
  - `StopReason(str, Enum)`：`QUOTA_SATISFIED` / `BUDGET_EXHAUSTED` / `NO_PROGRESS` /
    `SOURCE_EXHAUSTED`（有源成功响应但策略穷尽）/ `ALL_SOURCES_UNAVAILABLE`（所有尝试均
    provider error）/ `CANCELLED`。
  - `CancelToken`：`cancelled` 属性 + `cancel()`；Loop 在每次外部调用前后检查。
  - `BudgetLedger`：**统一记账**所有昂贵调用（search/verify/llm/discovery/provider 时间/
    迭代/候选数）；注入 monotonic `clock`。`can_continue()` / `charge_*()` /
    `exhausted_dimensions() -> tuple[str,...]`（BUDGET_EXHAUSTED 时 diagnostics 附具体触顶维度，
    不给笼统字符串）。
  - **与 A2c-1 VerifyBudget 的对账**：CrossVerifier 的 verify 调用必须计入本 BudgetLedger，
    不得双重记账——提供 `verify_budget_view()` 返回一个把 charge 转发进 BudgetLedger 的
    VerifyBudget 适配（或等价机制），使 Loop 只有一个权威预算源。
  - 不 import `tasking`、不读全局 config/时钟（clock 注入）。
  - 测试：各维度分别触顶 → `can_continue` 转 False + `exhausted_dimensions` 正确；
    provider 时间用 fake clock 触顶；CancelToken 触发；verify_budget_view 的 charge 汇入 ledger。
  - _Requirements: 8.5, 8.6_
  - _Design: design-a2c.md D11_

- [ ] 2. QueryPlanner（`literature/retrieval/query_planner.py`）
  - `SearchPlan`（frozen）：`queries: tuple[str,...]` / `source_names: tuple[str,...]` /
    `provider_filters: tuple[tuple[str,object],...]` / `strategy_reason: str`。
  - `QueryPlanner`（Protocol）：`propose(request, gaps, history, diagnostics) -> SearchPlan`。
  - `DeterministicQueryPlanner`：无 LLM 的确定性兜底（如首轮用初始查询，后续对未满足维度做
    固定改写/换源）；Planner 失败时 Loop 退到它，**不**把失败解释为「已无文献」。
  - `LLMQueryPlanner`：走 A2c-1/A2a 的 `structured_call`，**注入 writer 模型客户端 + 固定输出
    schema**；**不复用 writer 的写作 prompt / 对话历史 / 可变上下文**（组合层只传模型客户端）。
    model/prompt version 供 trace 记录。
  - **约束（结构性保证）**：Planner 只能产出查询/源名/检索参数，其输出**不含**也**无法**修改
    QuotaSpec / TaskFingerprint / RelevanceCriteria / 预算（这些不作为 propose 的可写入参）。
  - 不 import `tasking`、不读全局 config。
  - 测试（stub LLM）：`DeterministicQueryPlanner` 对给定 gaps 产稳定 SearchPlan；
    `LLMQueryPlanner` 走 structured_call、解析 schema、失败→由 Loop 用 fallback（此处验证 planner
    返回可被 Loop 识别的失败/空信号）；planner 输出不携带 quota/criteria/fingerprint 字段。
  - _Requirements: 8.2, 8.3, 8.4_
  - _Design: design-a2c.md D10_

- [ ] 3. 有界 RetrievalLoop（`literature/retrieval/loop.py`）
  - `run_retrieval_loop(request, *, deps, limits, planner, clock, cancel_token, trace_sink=None)
    -> LoopResult`，其中 `request` 携带 quota（Retrieval kind）/ criteria / initial_query /
    task_fingerprint；`deps` 复用 A2c-3 `AttemptDeps`（registry/ledger/cross_verifier/judge/
    store/acceptance_policy/language_*），预算改由本 Loop 的 `BudgetLedger` 统管。
  - **循环**：初始查询 → `run_attempt`（A2c-3）→ 用**拟提交记录视图**（D12）经 A2b
    `count_by_dimension`/`evaluate_constraints` 得逐维度 gap → 未满足且预算允许 → `planner.propose`
    调整查询/源 → 下一轮。每轮前后检查 CancelToken 与 BudgetLedger。
  - **进度视图（D12）**：`countable_record_views(batch, scope)`——ACCEPT_NEW→proposed record、
    MERGE→合并后 record、REUSE→Ledger 现有（scope 允许时）、REJECT→不计；按 target_reference_id
    去重；再 count/evaluate。这样 MERGE 补齐 final 事件的记录在 ledger_total 正确预览、new_only
    下不算新增。
  - **停止原因（D11）**：配额满足→`QUOTA_SATISFIED`；任一预算触顶→`BUDGET_EXHAUSTED`（附维度）；
    连续 `max_no_progress_rounds` 轮进度签名不变→`NO_PROGRESS`；所有 SEARCH 源已试且无可用策略→
    `SOURCE_EXHAUSTED`；所有尝试均 provider error→`ALL_SOURCES_UNAVAILABLE`；取消→`CANCELLED`。
    **只含 MAX 的配额初始即满足**，不启动无意义 Loop。触任一上限即停并**保留** batch 成果（8.6）。
  - **进度签名**：`(counted_reference_ids, QuotaGap 向量, 本轮新增 strict verified 数)`；连续 N 轮
    既无新增可计数 ID、任一 gap 未缩小、且无记录升级为 strict verified，才判 NO_PROGRESS。
  - **策略去重**：相同 query/source/filter 的 strategy fingerprint 不重复执行。
  - `LoopResult`（frozen）：`batch`（STAGED）/ `gaps: tuple[QuotaGap,...]` /
    `unguaranteeable: tuple[str,...]`（受源覆盖不足无法保证的维度 + 原因，10.5）/ `stop_reason` /
    `diagnostics`（触顶维度/迭代数/各计数）。Loop 结束把 batch **收束为 STAGED**（正常停止/取消/
    预算触顶都收束成可审计 STAGED；用户确认与 commit 属 A2c-2 已就位机制 / A2d 交互）。
  - **Loop 全程不改 quota.filters/constraints、不改 task_fingerprint、不向用户提问**（8.3/8.4/8.7）。
  - **trace**：每 Attempt 经 `trace_sink`（可空，注入）发隐私安全记录（见上，D15）；关时零行为影响。
  - 不 import `tasking`、不读全局 config/时钟；不落盘、不接 Discovery。
  - 测试（stub search/verify/reviewer/planner + fake clock）：
    - 首轮即满足 → QUOTA_SATISFIED；
    - 需多轮 + planner 改写才凑够 → 迭代推进、最终满足；
    - 各预算维度分别触顶 → BUDGET_EXHAUSTED + 正确触顶维度 + 保留成果；
    - 连续无进展 → NO_PROGRESS；CancelToken → CANCELLED；
    - 全源 provider error → ALL_SOURCES_UNAVAILABLE；策略穷尽 → SOURCE_EXHAUSTED；
    - 只含 MAX 的配额 → 不启动、直接 QUOTA_SATISFIED；
    - 剩余 gap 的语言维度 → unguaranteeable 标注；
    - 进度视图去重正确（同 rid 不重复计）、MERGE 在 ledger_total 计入 / new_only 不计；
    - trace_sink 收到隐私安全记录（无原始 query/title/abstract 文本）；
    - Loop 未改 quota/criteria/fingerprint（前后相等断言）。
  - _Requirements: 8.1–8.8, 10.1–10.5_
  - _Design: design-a2c.md D8, D10, D11, D12, D15, Correctness Properties 3/4_

- [ ] 4. 结构验收与全套回归
  - 全套既有测试绿（当前 698 快照，新增 A2c-4 测试后总数上升）；新增单测全绿。
  - AST 结构断言：`literature/retrieval/{budget,query_planner,loop}.py` 不 import `tasking`、
    不 import `config`；`loop.py` 不 import `session.facts`（未落盘）、不含 Discovery 调用（属 A2c-5）。
  - 确认**未接写作链路、未落盘、未接 Discovery**；旧 `search_and_verify` 与 rewrite/add_section
    产物逐字节不变；默认测试套件不发网络请求。
  - 确认未改 observability 的 EventKind 枚举（trace 走注入 sink）。
  - 输出测试摘要并清理临时文件；不修改 Working Copy 和 facts.json schema。
  - _Requirements: A2c-4 验收铁律_
  - _Design: design-a2c.md D11/D12/D15, Correctness Properties 3/4_

## Notes

- 每完成一个 Task，先说明变更与验证结果，再进入下一项。
- 全套测试以本轮开始时实际收集到的既有测试为基线；新增 A2c-4 测试后总数应上升。
- A2c-4 **只跑内存多轮 Loop**：Discovery 兜底 + IdentifierResolution + 持久化 + 单 envelope
  commit 属 A2c-5；用户确认交互属 A2d。
- trace 经注入 sink，隐私安全字段（hash/计数/预算），**不落原始 query/候选文本**；不改
  streaming-and-trace 的 EventKind 枚举。
- 真实相关性/召回质量不在本期宣称；真实评测属 A2c-5 独立报告、A2d Active 前置门槛。
- git 提交由用户自行完成。

## 已定决策

- QueryPlanner 独立、默认注入 writer 模型客户端但不复用写作 prompt/历史；只改查询/源/参数，
  不改 quota/criteria/fingerprint/预算；失败走确定性 fallback，不当「已无文献」。
- BudgetLedger 是唯一权威预算源，CrossVerifier 的 verify 调用汇入之（不双重记账）；注入 clock。
- 配额进度用拟提交后去重记录（D12），经 A2b count/evaluate；只已接受推进（Req 7.9）。
- 停止原因六态明确；只含 MAX 的配额不启动 Loop；触上限保留成果。
- Loop 不改 filters/constraints/fingerprint、不提问；结束把 batch 收束为 STAGED。
- trace 隐私安全 + 经注入 sink；不改 observability EventKind；不落盘、不接 Discovery（留 A2c-5）。
