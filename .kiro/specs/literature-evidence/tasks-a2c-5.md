# Implementation Plan — A2c-5：Discovery 契约 + IdentifierResolution + 单 envelope 持久化 + 真实评测入口

## Overview

A2c 的**最后一期、也是最重的一期**：补齐 Discovery 兜底（只做协议/隔离/IdentifierResolution，
不接真实 Web 后端）、把 staged batch 与 RelevanceStore **持久化进 project facts 的单
envelope**、以**一次原子写**提交（Ledger + RelevanceStore + pending batch 状态同落），以及
真实相关性评测的受控入口（独立报告，A2d Active 前置门槛）。

**先决同步（design「Requirements 同步前置」）**：Req 11.13/11.16 现把「权威源模糊反查无匹配」
定义为 `not_found/疑似不存在`，与 design D3/D13 的 `UNRESOLVED_NO_MATCH` 冲突。**冻结本期
实现前必须先同步 Requirements**（Task 1），否则实现无法同时满足 Requirements 与 Design。

**本期不做**：不接真实 Web 搜索后端（无凭据/后端）、不接写作链路（A2d）、不把
`citation_quota` 槽激活（A2d）、不做真实模型语义评测的达标结论（只交付工具 + 报告模板）。

**验收铁律**：既有测试全绿（当前 717 快照）；Discovery/持久化/事务**全部离线可测**
（stub DISCOVER + 故障注入，不发网络）；`literature` 不 import `tasking`；
**单 envelope 提交在任一故障点失败时，内存与磁盘的 Ledger / RelevanceStore / Batch 状态均
不变**（原子性）；已 COMMITTED 重复提交不重复增加。

关键纪律（承接 design D3/D7/D8/D13/D14/D16）：

- **Discovery 只做接口 + Stub + 隔离，不接真实后端**（D13）：默认 `discovery_sources=()`；
  提供协议与测试 Stub；**不提供**永远返回空却伪装成功的生产 provider；`discovery_enabled=true`
  但无 provider → 明确配置警告，不假装完成。
- **模糊反查无匹配 ≠ 论文不存在**（D3/D13）：`search_bibliographic` 空结果 →
  `UNRESOLVED_NO_MATCH`（不产 not_found/疑似不存在）；只有取得精确 identifier 后的 exact
  fetch 明确未命中，才记 `NOT_FOUND_AT_SOURCE`，仍不上升为「全球不存在」。
- **UnresolvedLead 不入可引用集合、不计任何配额**（11.12）；Discovery URL 作不可信数据
  存储/展示，不作指令或自动访问目标（11.15）。
- **单 envelope 原子事务**（D7）：复制完整 ProjectFactsEnvelope → 在副本应用 →
  序列化整体 → 一次 `atomic_write_json`（tmp+fsync+replace）→ 成功才换内存。**不**用多次
  `os.replace` 假装跨文件原子；本期选单 envelope。
- **RelevanceStore 确定性同键合并**（D14）：SUCCESS 优先 ERROR；同状态取 `assessed_at` 较新；
  仍同取稳定内容哈希；加载旧文件不覆盖内存中不同 key；content/judge 变化自然形成新 key。
- **真实评测另出报告、是 A2d Active 前置门槛**（D16）：单测只验协议/隔离/合并/原子性；
  真实模型的错接纳/错拒绝/title_only 误接纳率、中英分层，数据集与阈值**评测前冻结**。

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2", "4", "5"] },
    { "wave": 3, "tasks": ["3"] },
    { "wave": 4, "tasks": ["6"] }
  ]
}
```

- Task 1（Requirements 同步）是先决门槛，最先做。
- Task 2（discovery.py）、Task 4（单 envelope 持久化）、Task 5（评测入口 + config）不同文件、
  只依赖前面各期，可并行。
- Task 3（Loop 接入 Discovery 兜底）依赖 Task 2（+ A2c-4 loop）。
- Task 6 结构验收 + 全套回归。

## Tasks

- [ ] 1. Requirements 同步（Req 11.13 / 11.16，先决门槛）
  - 修改 `requirements.md` Req 11.13：把「权威源模糊反查无匹配」从 `not_found/疑似不存在`
    改为 `UNRESOLVED_NO_MATCH`（模糊无结果不证明作品不存在）；**只有精确 identifier 的
    exact fetch 明确未命中**才产 `not_found` + 疑似不存在（模型幻觉）。
  - 修改 Req 11.16 的统计项：分别统计 Discovery 发现数 / IdentifierResolution 成功数 /
    `UNRESOLVED_NO_MATCH`（模糊无匹配）数 / 疑似不存在（exact 未命中）数——四者互斥不混。
  - 用 `getDiagnostics` 校验 requirements.md 格式;确认与 design-a2c D3/D13 口径一致。
  - _Requirements: 11.13, 11.16（同步）_
  - _Design: design-a2c.md「Requirements 同步前置」, D3, D13_

- [ ] 2. Discovery 契约 + IdentifierResolution + 隔离（`literature/retrieval/discovery.py`）
  - `resolve_identifier(lead, *, registry, cross_verifier, budget, cache, title_similarity_threshold)
    -> ResolutionOutcome`：
    1. `DiscoveryLead` 经 A2a 清洗 → `BibliographicQuery`；
    2. 对 registry 中支持 `search_bibliographic` 的 VerifyProvider 反查候选；
    3. 标题（必要时作者/年份）相似度达阈值才取得 work identifier；
    4. 取得后走 A2c-1 `CrossVerifier` 严格核验；
    5. 未匹配 → `UnresolvedLead`（不入可引用集合、不计配额）；
    6. **模糊无匹配 → `UNRESOLVED_NO_MATCH`，绝不产 not_found/疑似不存在**；仅取得精确
       identifier 后的 exact fetch 未命中才 `NOT_FOUND_AT_SOURCE`（疑似不存在），仍不上升
       为「全球不存在」。
  - `ResolutionOutcome`（frozen）：`kind ∈ {resolved, unresolved_no_match, suspected_nonexistent}`
    + 可空 `verification_assessment` + `candidate` + `lead`。
  - `UnresolvedLead`（frozen）：清洗后的 title/authors/year/url/source（URL 作不可信数据）。
  - `DiscoveryStats`（frozen）：discovered / resolved / unresolved_no_match / suspected_nonexistent
    四计数（11.16）。
  - `discovery_enabled_precondition(*, authoritative_tried, has_gap, enabled, has_provider)
    -> bool`：四条件同时成立才启用（11.7）；`enabled=true` 但 `has_provider=false` → 返回
    False 并产**明确配置警告**（不假装完成）。
  - 提供测试 Stub DISCOVER provider；**不提供**伪成功生产 provider。
  - 不 import `tasking`、不读全局 config。
  - 测试（stub）：resolve 成功（相似度达标→work id→strict verified）；相似度不足 → 不赋标识符
    → UnresolvedLead；模糊无匹配 → UNRESOLVED_NO_MATCH（非 not_found）；exact 未命中 → 疑似不存在；
    UnresolvedLead 不计配额（无 disposition 进 batch 计数）；启用前置四条件（缺一不启用）；
    enabled 无 provider → 警告 + 不启用；DiscoveryLead 文本经清洗。
  - _Requirements: 11.6–11.16, 17_
  - _Design: design-a2c.md D13_

- [ ] 3. Loop 接入 Discovery 兜底（`literature/retrieval/loop.py` 扩展）
  - 在 RetrievalLoop 中：**仅当权威 SEARCH 源已尝试、仍有非零 gap、Discovery 显式启用且有
    provider** 时，才调用 Discovery（11.7）；受源覆盖不足标注「不可保证」的维度（典型中文，
    10.5）优先启用兜底（11.8）。discovery 调用计入 BudgetLedger 的 `max_discovery_calls`。
  - Discovery 得到的 lead 经 Task 2 `resolve_identifier`：`resolved` → 作为候选进 Attempt 后续
    去重/相关性/disposition；`unresolved_no_match` / `suspected_nonexistent` → 记入 LoopResult 的
    `discovery_stats` 与 leads 报告，**不入 batch 可计数项、不计配额**。
  - `LoopResult` 增 `discovery_stats: DiscoveryStats` 与 `unresolved_leads: tuple[UnresolvedLead,...]`。
  - Discovery 默认关（`discovery_sources=()`）→ Loop 行为与 A2c-4 完全一致（回归保护）。
  - 测试：Discovery 关 → 行为同 A2c-4；Discovery 开 + 权威源穷尽 + gap → 触发兜底；resolved lead
    进入计数、unresolved/suspected 不计但入报告；discovery 预算触顶正确；未满足前置不触发。
  - _Requirements: 10.5, 11.6–11.16_
  - _Design: design-a2c.md D13, Architecture_

- [ ] 4. 单 envelope 持久化 + 原子提交 + RelevanceStore 合并（`session/facts.py` + `retrieval/transaction.py` + `relevance.py`）
  - **RelevanceStore 确定性同键合并**（D14，`relevance.py`）：新增 `merge_from(rows)` /
    `merge(other)`——同 key：SUCCESS 优先 ERROR；同状态 `assessed_at` 较新胜；仍同取稳定内容
    哈希；不同 key 全保留；加载旧文件不覆盖内存中不同 key。
  - **facts envelope 扩展**（`session/facts.py`）：`save()` 的单 dict 增 `relevance_store`
    与 `pending_batches` 两段，`schema_version` 升到 3；`load()` 缺这两段时按空处理（无损向后
    兼容，复用 A1 混合 schema 思路）；仍走 `migration.atomic_write_json` 一次原子写。
    staged/confirmed batch 存 pending 区，**不写 ReferenceLedger**、不暴露为可引用文献。
  - **单 envelope 原子提交**（`transaction.py` 扩展 A2c-2 的 `plan_commit`）：新增
    `commit_batch(facts, batch) -> CommitOutcome`：
    1. 校验 batch=CONFIRMED + revision + `expected_base_hash`（MERGE）+ batch_id 幂等键；
    2. **复制完整 envelope**（Ledger + RelevanceStore + pending batches + 幂等记录）；
    3. 在副本应用 ACCEPT_NEW/MERGE，并在**同一副本**把 batch 置 COMMITTED、记 committed_batch_id；
    4. 序列化整体 → **一次** `atomic_write_json`（tmp+fsync+replace）；
    5. 磁盘成功后才换内存 envelope；
    6. **任一步失败 → 内存与磁盘的 Ledger/RelevanceStore/Batch 状态均保持提交前**。
  - 已 COMMITTED 的 batch 重复 `commit_batch` → 幂等，不重复增加；REJECTED batch 不改 Ledger；
    加载损坏的 pending batch → 隔离该逻辑条目并报告，Ledger 不变（整个 envelope JSON 损坏走
    A1 备份恢复）。
  - **不**用多个 `os.replace` 假装跨文件原子；单 envelope 是本期前提（如需拆多文件另加 WAL，
    不在本期）。
  - 测试（内存 + tmp 目录 + 故障注入）：
    - RelevanceStore 合并各规则（SUCCESS>ERROR、新 assessed_at 胜、不同 key 保留）+ 往返；
    - facts v3 往返 + 旧 v2 无损加载（缺 relevance_store/pending_batches 按空）；
    - commit_batch 成功：Ledger + RelevanceStore + batch=COMMITTED 一次落盘、内存磁盘一致；
    - **故障注入**：序列化/写 tmp/replace 各故障点抛错 → 内存与磁盘 Ledger/Store/Batch 均不变；
    - 幂等：已 COMMITTED 再提交不重复增加；MERGE `expected_base_hash` 不符 → revision_conflict 拒绝；
    - 损坏 pending batch 隔离、Ledger 不变。
  - _Requirements: 1.13, 9.8, 9.9, 9.10, 7.3, 7.4（持久化）_
  - _Design: design-a2c.md D7, D8, D14_

- [ ] 5. 真实评测入口 + config（`tests/eval/literature_relevance/` + `config.py`）
  - `config.py` 增：`PAPER_LIT_DISCOVERY_ENABLED`（默认 False）；检索 Loop 上限
    `PAPER_LIT_RETRIEVAL_MAX_ITERS` / `_MAX_SEARCH_CALLS` / `_MAX_VERIFY_CALLS` /
    `_MAX_LLM_CALLS` / `_TIME_BUDGET_S`（非法/越界降级默认，复用 `_env_int/_env_float`）。
    `config.py` 只解析原始值；`RetrievalLimits` 由组合层构造注入（literature 不 import config）。
  - **受控评测骨架**（离线，不发网络、不宣称达标）：标注样本 schema（relevant/weakly/unrelated
    + 中英分层 + title_only 标记）、评分脚本（错接纳率/错拒绝率/title_only 误接纳率，按中/英
    分层）、报告模板。**数据集版本/抽样/阈值/置信区间在跑最终评测前冻结**（D16）；报告给出
    相对上一版本的回归结果。
  - 明确文档化：**真实模型评测未达文档化阈值前，A2d 不得把 relevance 作为无人监督的 Active
    配额门槛**（D16）。
  - 测试：config 新项默认值 + env 解析 + 非法降级；评分脚本对合成标注集算指标正确（离线，
    stub 判定，不宣称真实模型达标）。
  - _Requirements: 5.6, 5.7（config）, 7（评测口径）_
  - _Design: design-a2c.md D16, D11（limits config）_

- [ ] 6. 结构验收与全套回归
  - 全套既有测试绿（当前 717 快照，新增 A2c-5 测试后总数上升）；新增单测全绿。
  - AST 结构断言：`literature/retrieval/{discovery,loop,transaction}.py` 不 import `tasking`；
    `discovery.py` 不读全局 config。
  - 确认**未接真实 Web 后端**（无伪成功生产 provider）、**未接写作链路**、`citation_quota` 槽
    仍未激活；Discovery 默认关时 Loop 行为与 A2c-4 一致；旧 `search_and_verify` 与
    rewrite/add_section 产物逐字节不变。
  - 确认默认测试套件不发网络请求；真实 Crossref/OpenAlex 冒烟 env-gated、默认 skip。
  - 确认 Requirements（11.13/11.16）与 design/实现口径一致（UNRESOLVED_NO_MATCH）。
  - 输出测试摘要并清理临时文件。
  - _Requirements: A2c-5 验收铁律_
  - _Design: design-a2c.md 五期交付门槛 / A2c-5_

## Notes

- 每完成一个 Task，先说明变更与验证结果，再进入下一项。Task 1（Requirements 同步）先做。
- 全套测试以本轮开始时实际收集到的既有测试为基线；新增 A2c-5 测试后总数应上升。
- **单 envelope 原子性以「一个物理 facts 文件」为前提**；不用多次 os.replace 假装跨文件事务。
- Discovery 只做协议/Stub/隔离/IdentifierResolution，**不接真实 Web 后端、不提供伪成功占位**。
- 真实相关性评测是**独立报告**、**A2d Active 前置门槛**；单测不以 stub 结果冒充真实指标。
- A2c-5 完成即 A2c 全部收官；写作链路接入（LiteraturePreflight/CitationQuotaGate/槽激活）
  与 Shadow/Active 去冗余属 A2d。
- git 提交由用户自行完成。

## 已定决策

- 先同步 Requirements 11.13/11.16（UNRESOLVED_NO_MATCH），再冻结 A2c-5 实现。
- Discovery 默认关、仅兜底、经 IdentifierResolution 才可信；UnresolvedLead 不计配额；
  模糊无匹配 ≠ 论文不存在；不接真实 Web 后端、不提供伪成功占位。
- 持久化用单 envelope 一次原子写（Ledger + RelevanceStore + pending batch 同落）；
  任一故障点内存与磁盘均不变；已 COMMITTED 幂等；不假装跨文件原子。
- RelevanceStore 同键确定性合并（SUCCESS>ERROR>新时间>稳定哈希），加载不覆盖不同 key。
- 检索 Loop 上限 + discovery 开关进 config，组合层构造 RetrievalLimits 注入（literature 不 import config）。
- 真实评测独立报告、阈值冻结、A2d Active 前置门槛；单测不冒充真实指标。
