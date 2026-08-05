# Implementation Plan — Retrieval Throughput（B0 → B1 → B2 → B3 → Release Gate）

## Overview

按“**先冻结基线，再逐能力启用**”实施文献检索吞吐优化：

1. **B0 基线与兼容地基**：冻结 Legacy 行为、Benchmark Profile、文本/窗口契约和真实调用预算契约；
2. **B1 批量相关性判定**：保持 legacy 的 verify→relevance 顺序，只把相关性改为批量；
3. **B2 管线重排**：prepare→relevance→verify Survivor→reduce，独立验证顺序等价与核验调用下降；
4. **B3 有界并行**：惰性提交、原子 reservation、Single-Flight、provider gate、确定性归并；
5. **Release Gate**：≥200 条人工标注、≥30 次 replay、≥5 个 live 配对、canary/熔断均达标后，
   才允许讨论默认开启；未达标时功能可以存在，但 flag 必须保持关闭。

核心纪律：

- B1/B2/B3 三个 flag 独立、默认全关；全关直接调用 `_run_attempt_legacy`，不经过新协调层；
- 每完成一期先产出阶段报告并过门槛，再进入下一期；不得一次实现完 B1+B2+B3 后统一调试；
- `literature` 不 import `tasking` / `application` / `session` / `config`；所有运行时策略由组合层注入；
- `BudgetLedger` 是唯一权威预算源；实际外部调用在物理 `complete/fetch` 边界预留和结算；
- 批量、并发或重排不得放松 strict verify、CitationQuota、RelevanceAcceptancePolicy；
- 单元/属性测试全离线；真实网络 dogfood 必须显式运行，不能混进默认测试；
- 不硬编码当前测试总数，实施开始时动态收集基线；不覆盖用户现有工作区修改。

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2", "3"] },
    { "wave": 3, "tasks": ["4"] },
    { "wave": 4, "tasks": ["5"] },
    { "wave": 5, "tasks": ["6"] },
    { "wave": 6, "tasks": ["6A"] },
    { "wave": 7, "tasks": ["7"] },
    { "wave": 8, "tasks": ["8"] },
    { "wave": 9, "tasks": ["9"] },
    { "wave": 10, "tasks": ["10"] },
    { "wave": 11, "tasks": ["11"] },
    { "wave": 12, "tasks": ["12"] }
  ]
}
```

- Task 1 必须先完成，后续代码变更才能与可信 Legacy/性能基线比较。
- Task 2（文本/窗口纯契约）与 Task 3（预算/物理调用控制）文件边界独立，可并行。
- Task 4/5/6 构成 B1；Task 6 未过门槛时不得进入 B2 默认发布，但可继续在 flag-off 下开发。
- Task 6A 是真实评测暴露出的 B1.1 稳定化补丁；完成工程项后仍须等待人工仲裁与真实复测，
  不得因为草拟标签上的诊断指标达标而宣称发布 GO。
- Task 7/8 构成 B2；Task 8 未过门槛时不得启用 B3 canary。
- Task 9 先实现纯并发机制，Task 10 才接 CrossVerifier/Attempt，避免机制和业务同时调试。
- Task 11 完成可观测、benchmark、canary；Task 12 做最终 go/no-go 验收。

## Tasks

- [ ] 1. B0：冻结 Legacy、测试基线与 Benchmark Profile
  - 在任何生产代码变更前动态记录：既有测试收集数/通过数、Legacy Attempt 关键调用计数、
    当前 20 篇任务 dogfood 的 active elapsed、LLM/verify/search 调用数和 provider 错误率。
  - 新建 `tests/eval/retrieval_throughput/`：
    - `benchmark_profile.json`（或等价版本化格式）：quota、候选/录制响应版本、冷热缓存、
      provider/model/version、预算、batch/concurrency、timeout、无效样本规则、运行次数；
    - replay runner 骨架：支持 Legacy / B1 / B1+B2 / B1+B2+B3 四模式，暂不要求新能力存在；
    - 报告必须引用 profile hash，不能运行后临时修改无效样本规则。
  - 为 `_run_attempt_legacy` 建立离线 golden/characterization tests，覆盖：
    accept_new、reuse、merge、batch duplicate、filter reject、verify reject、relevance reject/error、
    缓存命中、预算停止、title_only、中英候选和多 identifier。
  - golden 比较排除明确非语义字段（如实时 timestamp/随机 item_id）时，必须列出规范化规则；
    不得用“删掉所有字段”制造伪等价。
  - 本 Task 不增加 feature flag、不改执行路径、不发真实网络；只产基线资产与报告。
  - 验收：默认测试全绿；characterization tests 在未改代码时全绿；profile 可重复加载；
    报告区分 cold/warm cache 与系统 active time/人工等待。
  - _Requirements: 9.1–9.3, 11.1–11.7_
  - _Design: Feature 组合执行矩阵, Testing Strategy, Property 24, D8/D10_

- [ ] 2. B0：RelevanceTextPolicy、WindowPacker 与稳定容量契约
  - 新增 `literature/relevance_runtime.py`：
    - `RelevanceTextPolicy`（frozen/versioned）：title/abstract 字段上限；
    - `PreparedRelevanceText`：最终 clean title/abstract、basis、content_hash、truncated；
    - `prepare_relevance_text()`：**先清洗/截断，再对实际送 Judge 的文本算 hash**；
    - legacy 默认 policy 产生与现有 `clean_relevance_fields/relevance_content_hash` 相同结果。
  - 新增 `literature/retrieval/window_packer.py`：
    - `BatchInputBudget` / `WindowLimits` / `WindowItem` / `RelevanceWindow`（frozen）；
    - `WindowPacker.pack(items, base_prompt_cost)` 按稳定输入序贪心装箱；
    - 每窗同时满足 `count <= batch_size`、token 上限（有可靠 estimator 时）和保守字符上限；
    - 成本包含 system prompt、RelevanceCriteria、schema/envelope、candidate_key 与全部 item；
    - Packer 不修改 PreparedRelevanceText，不在 content_hash 计算后再次截断；
    - 单项超预算由 text policy 先压到可独立成窗，再重新计算 hash；仍无法容纳则 typed config/input error，
      绝不突破上下文上限。
  - batch 有效 judge_key 的版本材料包含 text_policy.version + BatchInputBudget canonical hash；
    仅改变并发度不改变 TaskFingerprint。
  - 属性测试：空集/单项/边界刚好/双上限竞争/长文本/顺序稳定/随机候选；至少覆盖 Properties 1–3、
    15、29；fake tokenizer 与字符 fallback 均测试。
  - AST：新文件不 import 上层/config，不读取全局 tokenizer/时钟。
  - 本 Task 不接 `run_attempt`，不改变 RelevanceStore，不新增 LLM 调用。
  - _Requirements: 1.1, 1.2, 1.4, 1.7–1.9, 5.6, 10.1, 10.3, 13.8_
  - _Design: Components 2, Data Models, Properties 1–3/15/29, D3/D13_

- [ ] 3. B0：原子 BudgetReservation 与物理 ExternalCallControl
  - 扩展 `literature/retrieval/budget.py`，保持旧 API/默认行为：
    - BudgetLedger 内部锁保护 used/reserved；
    - `try_reserve(dims)` / `mark_started()` / `settle()` / `release()`；
    - Reservation 状态机只允许 `RESERVED→STARTED→SETTLED` 或 `RESERVED→RELEASED`；
    - 重复 settle/release、跨 ledger ticket、非法状态转换明确报错，不静默改计数；
    - `used + reserved` 不得超过任一调用上限；墙钟截止继续使用注入 monotonic clock。
  - 新增 `literature/retrieval/call_control.py`：`CallOutcome` / `ExternalCallControl`；执行顺序固定为
    cache/single-flight leader → reserve → provider gate（可空/serial）→ 再检 cancel/deadline →
    mark_started → physical call → finally settle/release lease → telemetry。
  - 为 `structured_call` 增可选 physical-attempt hook，或实现 budget-aware LLM wrapper（二选一）：
    - 每次真实 `llm.complete` 都单独 reserve/settle；
    - forced-tool 参数不支持导致的 compatibility retry 必须计为第二次物理调用；
    - 旧调用未传 hook/wrapper 时行为与返回逐字节不变。
  - `_LedgerVerifyBudget` 保持单一预算源；新路径不允许外层再按 item 事后 `charge_llm` 造成双记。
  - 失败/timeout 只要 physical call 已发出就 settle；调用未发出即取消才 release。
  - 测试：多线程 reservation 压力、各种非法状态、fake clock 截止、provider error/timeout、
    compatibility retry=两次结算、旧 structured_call 回归、verify adapter 对账。
  - 本 Task 先提供 serial/no-op ProviderCallGate，不实现 B3 线程池或 QPS 限流。
  - _Requirements: 7.1–7.9, 8.1–8.5, 10.6_
  - _Design: Components 6, Properties 20–22, D5_

- [ ] 4. B1：BatchRelevanceJudge、两级校验与有界失败重试
  - 新增 `literature/relevance_batch.py`：
    - `BATCH_PROMPT_VERSION` / `BATCH_SCHEMA_VERSION`；batch JudgeVersion 必须与 per-item 不同；
    - `BatchRelevanceJudge.assess_window(window, criteria, call_control)`；
    - 复用既有 rubric、RelevanceAcceptancePolicy、untrusted 包裹和 PreparedRelevanceText；
    - 每个 WindowItem 使用窗口内稳定 `candidate_key`，不依赖模型输出顺序。
  - `_BATCH_TOOL` 顶层只要求 `results` 容器；item 至少要求 candidate_key。`structured_call` 返回后：
    - 先校验 envelope，再逐 item 校验 verdict/confidence/list/reason/rationale；
    - 缺失/重复/未知 key、一 key 多结果只影响对应候选；合法项保留；
    - 整批 provider/parse/schema 失败 → 窗口全部 ERROR；
    - title_only 若返回 relevant，统一降为 weakly_relevant/low；
    - `matched_aspects/mismatch_aspects/rationale/reason_code` 使用与单篇一致的有界规则。
  - 有界重试：仅首次失败子集可执行最多一次确定性拆窗/单项 **batch-schema** 重试；
    成功项不重试；仍失败保留 ERROR；不得调用 legacy Judge 后把结果冒充 batch judge_key。
  - `BatchJudgeStats` 分开记录 items/windows/first_pass_llm_calls/retry_llm_calls/error_kind/truncation；
    实际 llm_calls 以 Task 3 的物理结算为准。
  - 本 Task 只实现纯 Judge，不写 RelevanceStore、不接 Attempt、不翻 flag。
  - 单元/属性测试覆盖 Properties 4–10；特别验证重排输出、部分失败不拖垮整批、prompt injection
    只作为数据、batch/per-item key 共存且不互相命中。
  - _Requirements: 1.1–1.9, 2.1–2.2, 3.1–3.9, 5.7–5.8, 10.2, 10.5_
  - _Design: Components 1, Error Handling, Properties 4–10, D1/D2_

- [ ] 5. B1：ActiveRelevanceRuntime、legacy-order 批量接线与可恢复策略快照
  - config 增 B1 字段并保持默认关闭：batch enabled/size/input+output token/char/retry limit；
    校验 enabled 时 size>=2、retry<=1、预算为正；未知/非法值启动失败或明确回退，不能静默开启。
  - 新增/装配 `ActiveRelevanceRuntime`：evaluator、active judge_key、RelevanceTextPolicy、batch flag；
    同一实例/不可变快照贯穿 Attempt、preflight relevance preparation、availability、bundle。
  - B1-only 执行路径保持 **verify→batch relevance**：
    - 以有界窗口为提交单元，窗口开始读取当前 accepted_records；
    - legacy exact/filter/strict verify 后才入 batch relevance window；
    - 窗口 reduce 完成后再构造下一窗口；同一 high-confidence work-key 只让元数据最完整代表进入昂贵阶段；
    - B2 flag 仍关，不得提前切换为 relevance→verify。
  - `run_attempt` 三 flag 全关时第一行直接分派 `_run_attempt_legacy`；不得进入 WindowPacker、runtime、
    新 backfill、reservation 协调或新 trace 字段的行为分支。
  - B1 开启时，在 availability 前调用 batch-aware `ensure_relevance`：只对 strict verified、过 filter、
    缺 active judge_key 的 Ledger 记录批量补判；与 RetrievalLoop 共用同一 BudgetLedger。
  - `assess_availability/build_bundle` 接收 active judge_key + content hasher，禁止各自猜 key/hash policy；
    rollback 后恢复 legacy runtime，batch 缓存保留但不错误命中。
  - 持久化 `ThroughputPolicySnapshot`：flags、WindowLimits、text policy/version、JudgeVersion、
    timeout/retry；checkpoint 增 snapshot_id/hash，facts 旧 envelope 缺字段时迁移为 legacy snapshot；
    resume hash 不一致或 provider/model 不可重建 → typed error，不重采样/不静默换策略。
  - `AttemptStats/Loop` 改用 relevance_items/windows/physical_llm_calls；新路径不按 item 双重 charge。
  - 测试：执行矩阵的 all-off/B1-only；active key 写读贯通；Ledger backfill cache hit/miss/预算耗尽；
    snapshot 往返/迁移/篡改；Property 24/26/29/30/33/34；全套旧测试绿。
  - 本 Task 完成后 B1 仍默认关闭，只允许显式 dogfood。
  - _Requirements: 5.4–5.8, 7.1, 9.1–9.6, 13.1–13.8, 14.3–14.4_
  - _Design: Feature 组合执行矩阵, Components 3/4/7, Properties 24/26/29/30/33/34, D8/D11/D14_

- [ ] 6. B1 Gate：人工判准评测、窗口变形与 B1 阶段报告
  - 在 `tests/eval/relevance_batch/` 建冻结 manifest 与 runner：固定 provider/model/temperature、
    per-item/batch prompt+schema、text policy、acceptance policy、数据版本、随机种子。
  - 人工标注验收集不少于 200 条，覆盖中英、三 verdict、title_only、长摘要、相邻主题、
    include/exclude、提示注入；记录类别/语言分布。开发期 40 条 smoke 不算完成本 Task。
  - 同一候选运行 per-item baseline 与 batch，报告：
    Verdict_Consistency、Acceptance_Consistency、accepted precision、false acceptance rate；
    对不同窗口组成/顺序做变形评测。
  - 必须同时达到：Verdict>=0.90、Acceptance>=0.95、precision 下降<=2pp、false acceptance
    上升<=2pp、窗口变形 Acceptance>=0.95。
  - 运行 B1-only replay/dogfood，记录 active elapsed、窗口数、物理 LLM calls、token/字符、错误/重试；
    与 Legacy 同 profile 配对，不把未完成配额的快速失败算加速。
  - 若任何质量门槛失败：Task 如实产出 no-go 报告，B1 默认 flag 保持关闭；不得调低阈值后勾完成。
  - Task 的“工程完成”可以是 runner/数据/报告齐全；“发布门槛通过”必须在报告中单独标记。
  - _Requirements: 2.1–2.7, 11.1–11.7_
  - _Design: Testing Strategy（判准评测/端到端）, D10_

- [ ] 6A. B1.1：真实评测稳定化、兼容能力记忆与可恢复执行
  - [x] provider 实例记忆 forced-tool-choice 三态能力；首次明确不支持时允许一次兼容降级，
    后续 structured call 直接复用兼容模式，不再每次重复探测；包装器显式透传能力所有者，
    不使用 URL/API Key 或全局 provider 身份缓存。
  - [x] `StructuredCallResult` 区分 provider attempts 与 compatibility retries；评测报告同时记录
    logical structured calls、provider attempts、兼容开销和本次/累计计数，保留旧 physical 字段兼容。
  - [x] live runner 增原子 checkpoint、显式 `--resume` 与隐私安全进度；checkpoint 绑定
    manifest/profile/provider/model/evaluation kind，只保存 case ID、判定和计数，不保存 query/title/abstract。
  - [x] 离线测试证明：70 次逻辑调用在不支持强制工具的 provider 上最多 71 次 wrapper-level
    provider attempt；完整 checkpoint 恢复时新增调用为 0；旧支持强制工具的路径仍为 70 次。
  - [ ] 由两名独立标注者完成 40 条 controlled smoke 仲裁；不得用被测模型生成标签冒充人工金标准。
  - [ ] 使用仲裁 manifest 重新运行真实评测并归档报告；确认兼容开销<=1、无解析错误、质量阈值通过后，
    才允许把 B1 从默认关闭推进到受控 Shadow/dogfood。
  - 验收：工程测试全绿；中断后最多重做一个在途窗口；未完成人工仲裁时 gate 继续诚实返回 NO-GO。
  - _Requirements: 2.1–2.7, 7.1–7.9, 11.1–11.7_
  - _Design: Components 6/8, Testing Strategy, D5/D10_

- [x] 7. B2：单线程 prepare/reduce、Relevance_Subject 与管线重排
  - 新增 `WorkItem` / `WorkItemOutcome` 与单线程阶段：
    `_prepare` → `_judge` → `_verify_survivors` → `_reduce`；中间对象 frozen/只读。
  - `_prepare`：clean/snapshot、语言、provisional exact kind、Persistent_Reference_Id、资格预筛、
    高置信 work-id 分组、Relevance_Subject/PreparedRelevanceText。
  - 分组规则：先用既有 deterministic merge；仍需代表时按
    Ledger existing > 有摘要 > 高置信 identifier 完整度 > 核心元数据完整度 > 输入序；
    所有 group member 都保留 BatchItem/group link，不能静默丢弃。
  - Relevance_Subject：reuse=Ledger existing、merge=确定性 merged record、accept_new=规范化候选；
    Judge 文本、content_hash、Persistent_Reference_Id 三者必须对应同一 subject。
  - `_judge` 先查 active RelevanceStore：SUCCESS hit 不入窗；ERROR 不作为成功缓存；B1 开/关分别
    走 batch/per-item，但 active runtime 不变。
  - `_verify_survivors` 只对 relevance accepted 且 exact kind=none 的 Survivor 严格核验；
    reuse/merge 复用现有核验事件；相关性拒绝不发 verify。
  - `_reduce` 单线程按稳定输入/group 序：写 RelevanceStore.merge_put、落 disposition/reject reason、
    分配 bibkey/numeric alias、构造 ProposedRecordSnapshot、更新 accepted_records/Batch。
  - 建 verify-first reference pipeline（同 Relevance_Subject/Judge/Verifier）用于顺序等价比较；
    seam 修复相对旧 bug 的差异单独报告，不能豁免重排等价。
  - config 增 B2 flag，默认 False；B2-only、B1+B2 与 all-off 三条路径显式测试。
  - 测试 Properties 11–15/25/34：随机重复组、代表选择稳定、全部候选有 BatchItem、无 Survivor 零 verify、
    strict 门槛、same-subject reordered==verify-first reference。
  - 本 Task 不接 B3 executor；阶段仍串行，便于隔离 B2 行为。
  - _Requirements: 4.1–4.8, 5.1–5.8, 9.4–9.6_
  - _Design: Components 3, Properties 11–15/25/34, D4/D6/D7/D11_

- [x] 8. B2 Gate：顺序等价、核验调用下降与阶段性能报告
  - 对 characterization + 随机属性数据运行同 Relevance_Subject 的 verify-first reference 与 B2：
    disposition/拒因/严格核验/相关性/计数语义一致，稳定排序不依赖实现细节。
  - 核验调用证明：CrossVerifier 候选调用数<=Survivor 数，provider verify calls<=未缓存可核验
    identifier 数；无 Survivor 时为零；reuse/merge 不重复核验。
  - 报告 Legacy / B1 / B1+B2 三阶段：active elapsed、物理 LLM calls、verify calls、候选各阶段淘汰率、
    quota completion、ERROR/timeout；同 Benchmark_Profile 配对。
  - 验证 B2 flag 回滚后恢复 B1-only verify→batch relevance，不残留新阶段顺序或 active key 漂移。
  - 任一语义等价或 strict gate 失败 → B2 no-go、默认 flag 保持 False；不得用性能收益覆盖正确性失败。
  - 全套测试、AST 边界、默认全关 Legacy golden 全绿。
  - 阶段报告：`tests/eval/retrieval_throughput/b2_gate_report.json`；30 次离线配对中最终语义一致，
    verify 28→22（-21.43%），模拟 active time 4.6s→4.3s（-6.52%），零 Survivor 时 verify=0；
    工程 Gate=`B2_ENGINEERING_GO_DEFAULT_OFF`。B1 人工仲裁仍为 0/40，因此发布 Gate 继续 NO-GO，
    B2 默认 flag 保持 False。
  - _Requirements: 4.1–4.8, 5.1–5.8, 9.1–9.6, 11.6–11.7_
  - _Design: Property 12/13/16/25/34, Testing Strategy, 分阶段上线 B2_

- [ ] 9. B3 地基：Single-Flight、惰性有界 Executor 与 ProviderCallGate
  - 新增 `literature/retrieval/single_flight.py`：operation-scoped 泛型 `SingleFlight[K,V]`；
    实例级 Lock/Condition，无全局单例、不 import store/provider/上层；leader success/error/cancel 都唤醒
    waiters 并清理 in-flight key；ERROR 可共享给本轮等待者但不写永久 relevance success cache。
  - 新增 `literature/retrieval/concurrency.py`：
    - `ConcurrencyExecutor` Protocol / `TaskOutcome`；
    - `ProviderConcurrencyPolicy` / `ProviderCallGate` / `ProviderLease`；
    - 全局、阶段、provider 三上限取最小；未知/非线程安全 provider 上限=1；
    - QPS、Retry-After、退避、jitter、clock/sleep 全注入；等待退避时不得持有 provider semaphore。
  - 新增 `application/concurrency_executor.py`：ThreadPoolExecutor adapter 的 `map_ordered` 必须惰性提交，
    最多 max_in_flight；完成一个才补一个；取消时停止提交并尝试 cancel queued future；
    返回 TaskOutcome 按输入序，不按完成序；application 负责 shutdown，literature 不 close executor。
  - ExternalCallControl 接入 SingleFlight key 与 ProviderCallGate，但本 Task 不改 Attempt/CrossVerifier。
  - 测试：同键 100 并发只一次 call；leader 抛错/取消无死锁；不同 key 可并发；三上限<=min；
    rate limit/fake clock/Retry-After/timeout；非线程安全串行；惰性提交/取消/宽限后无后台写。
  - 属性测试 Properties 18–20/23/27/31/32；真实线程压力测试与 deterministic shuffled executor 都覆盖。
  - _Requirements: 6.2–6.11, 7.4–7.9, 8.1–8.7, 13.5–13.6, 14.1–14.2_
  - _Design: Components 5/6, Properties 18–20/23/27/31/32, D5/D9/D12_

- [ ] 10. B3：CrossVerifier/Attempt 并发接线、确定性归并与取消
  - config 增 B3 flag、global/stage/provider limits、per-call timeout、shutdown grace；默认关闭/1。
  - CrossVerifier：同一候选多个 identifier 可并发 fetch；每个 physical fetch 走
    SingleFlight→reservation→provider gate→call；结果仍按 sorted `(id_type, normalized_id)` 赋 seq；
    VerificationCache 加锁或只经协调层访问。
  - 执行矩阵：
    - 仅 B3：保持原逐候选顺序，只并发单候选内部 identifier，避免破坏 accepted_records；
    - B1+B3：可并发已形成的 batch relevance window/安全 verify 工作项；
    - B2+B3：并发只读 WorkItem 的 judge/verify 阶段，prepare/reduce 恒单线程；
    - B1+B2+B3：完整路径；B3 不改变 B1/B2 决定的阶段先后。
  - reducer 统一处理 worker telemetry/cache merge/BatchItem；worker 禁止 mutate Batch/Ledger、分配别名、
    写普通日志或关闭 executor。
  - 取消/预算耗尽：装箱/提交/physical call 前检查；取消 queued future；已提交 in-flight 在 timeout+
    grace 内收束；返回后无后台任务修改 Batch/缓存；所有 reservation 最终 settled 或 released。
  - AttemptStats/Loop trace 汇总实际 max in-flight、queue time、provider latency/429/backoff/timeout、
    single-flight waiters、reservation 状态；不重复 charge。
  - 测试 Properties 16–23/27/34：并发==串行语义、稳定顺序、不同完成排列、预算各维触顶、取消各阶段、
    provider error、cache race、无后台写；B3-only/B1+B3/B2+B3/full 组合均测。
  - B3 完成后默认仍关闭，只进入显式 canary。
  - _Requirements: 6.1–6.11, 7.1–7.9, 8.1–8.7, 12.2, 13.5–13.7, 14.1–14.3_
  - _Design: Feature 组合执行矩阵, Components 5/6, Properties 16–23/27/34, 分阶段上线 B3_

- [ ] 11. 可观测、Benchmark、canary 与自动关闭
  - trace/diagnostics 保留 Requirement 18 既有字段，并新增：窗口 item/token/char/truncation、first/retry
    physical calls、阶段耗时、global/stage/provider 配置与实际在飞、queue time、reservation 四态、
    cache/single-flight、429/timeout/backoff、quota completion。
  - worker 只返回结构化 telemetry，由单线程 sink 发事件；禁止 worker 直接写普通日志。
  - `observability/sanitize.py` 增 `digest_text`（hash+length，无 preview）；不得误用会携带 preview 的
    `sanitize_text` 存 query/title/abstract/candidate mapping；隐私测试扫描 trace JSON。
  - active time 指标：`retrieval_loop_active_elapsed_s` 与 `literature_preflight_active_elapsed_s` 使用
    monotonic clock，排除人工等待，包含系统排队/重试/search/LLM/verify。
  - 完成 replay benchmark：同一录制响应不少于 30 次，四模式分别报告 P50/P95；目标 P50 相对 Legacy
    降>=60%、P95 降>=50%。
  - 完成 live dogfood：不少于 5 个有效配对样本；中位 active elapsed<=10 分钟且相对 Legacy 降>=50%；
    provider 大面积不可用仅按预先冻结规则排除，并保留样本数/原因。
  - 实现 canary/自动关闭（只影响后续 operation）：
    batch item ERROR >5%/100、provider timeout >10%/20、任何 budget overshoot、质量门槛失守 →
    关闭对应能力并告警；当前 operation 使用冻结 ThroughputPolicySnapshot 有界完成/诚实失败。
  - canary 比例、熔断状态和一键回滚由 application/config 管理；literature 不读全局状态。
  - 测试：trace 字段/隐私、fake clock、报告聚合、无效样本规则不可事后改、rolling window 边界、
    熔断只影响后续 operation、snapshot 恢复不换策略。
  - _Requirements: 11.1–11.7, 12.1–12.7, 14.3–14.6_
  - _Design: Components 8, Properties 30/32/33, Testing Strategy, D10/D14_

- [ ] 12. 最终验收、全组合回归与 go/no-go 报告
  - 运行默认离线全套测试并记录动态基线/新增数；所有既有测试与新增单元/属性/迁移测试全绿。
  - 34 条 Correctness Properties 均有可定位测试；属性测试默认不少于 100 examples，时间较长的真实
    线程压力测试可单独 marker，但发布验收必须运行。
  - 执行组合矩阵：all-off、B3-only、B1-only、B1+B3、B2-only、B2+B3、B1+B2、full；验证阶段顺序、
    active judge key、预算、取消、缓存、稳定排序和回滚。
  - AST/架构验收：literature 无上层/config import；executor 生命周期归 application；无全局线程池/
    SingleFlight；无 worker 普通日志；ProviderCallGate/clock/sleep/jitter 均注入。
  - schema/迁移：旧 facts/checkpoint 加载为 legacy snapshot；新 snapshot 往返/hash/篡改拒绝；
    RelevanceStore key schema、RetrievalBatch 落盘协议、Quota/Language/Authority 规则未改变。
  - 对照 Requirements 1–14 形成追溯表与证据链接：测试、评测报告、replay/live 报告、canary 报告。
  - 输出明确 go/no-go：
    - 工程正确但人工质量/性能/canary 任一未达标 → **NO-GO，三 flag 默认保持关闭**；
    - 全部门槛达标 → 只允许进入用户批准的 canary/发布配置变更，不在本 Task 自动全局翻默认值。
  - 清理临时文件与测试产生的项目数据；不提交或覆盖用户未授权的 Git 变更。
  - _Requirements: 1–14_
  - _Design: 全文，尤其 Correctness Properties 1–34、Testing Strategy、分阶段上线_

## Notes

- 每完成一个顶层 Task，先记录改动、测试、风险与是否达到该阶段 gate，再进入下一项。
- B1/B2/B3 的“代码完成”和“允许默认开启”是两件事；评测门槛未过时不得把 flag 默认改 True。
- ≥200 人工标签不能由实现者用同一 LLM 自动生成后冒充人工金标准；缺少人工标签即诚实报告阻塞发布。
- 性能报告必须同时带 quota completion/质量结果；未完成任务的快速失败不是吞吐提升。
- `structured_call` compatibility retry、provider retry、batch partial retry 是三种不同重试，分别记账与 trace。
- Task 7 的 seam 修复必须与同 Relevance_Subject 的 verify-first reference 比较；不能用旧 bug 为重排放宽等价。
- 真实网络测试必须显式 opt-in；默认 CI 只运行离线 replay/stub。
- Git 提交由用户决定；实施过程不擅自 commit/push。

## 已定决策

- 实施顺序固定为 B0 → B1 → B1 Gate → B2 → B2 Gate → B3 → Release Gate。
- `_run_attempt_legacy` 是全关路径的行为锚点；新能力不能以“重构整洁”为由删除它。
- WindowPacker 只装箱，不在 hash 后截断；active text policy/judge_key 贯穿写入与读取。
- Batch 与 per-item JudgeVersion 隔离；B1 单项 fallback 仍使用 batch schema/key。
- B1-only 保持 verify→batch relevance；B2 才切 relevance→verify Survivor。
- prepare/reduce 单线程；并发仅处理只读工作项/physical provider I/O。
- SingleFlight 属 literature 内 operation-scoped 纯机制；无全局单例、无上层依赖。
- BudgetLedger 原子 reserve/settle/release；每个 physical provider attempt 都单独结算。
- ProviderCallGate 与 executor 分离：executor 控任务，gate 控 provider max-in-flight/QPS/backoff。
- ThroughputPolicySnapshot 跨 turn 持久化；恢复不得静默更换判准或吞吐策略。
- ≥200 人工评测、≥30 replay、≥5 live、canary/熔断均为发布门槛，不降级为占位。
