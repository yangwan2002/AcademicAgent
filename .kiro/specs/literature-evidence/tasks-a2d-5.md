# Implementation Plan — A2d-5：原子激活（挂槽 + Handler 路由 + Writer Loop 合并 Gate）

> A2d 最后一批，也是**唯一真正改变现有写作路径行为**的一批。承接 design-a2d D1/D7/D8/D10/D11。
> 前三批（A2d-1/2/3/4）已把机制全部建好且离线测住，本批只做**接线 + 原子激活**。

## Overview

- **接线**：`build_rewrite_messages` / `build_add_section_messages` 加默认 `None` 的
  `bundle` 参数（None → 消息逐字节不变）；`execute_rewrite` / `execute_add_section` 加默认
  `None` 的 `citation_context`（None → 完全走旧路径）。
- **Writer Loop 合并 Gate**：带配额时每轮同时跑既有 RewriteGate/AddSectionGate 与
  CitationQuotaGate，**合并违规一次回灌**、**共用同一 retry 预算**、**循环内绝不检索**。
- **原子激活**：`citation_quota` 槽挂进 `REWRITE_SPEC`/`ADD_SECTION_SPEC` + Handler 真实
  读取并传给写作编排 + `literature_writing_integration` flag 控制。
- **feature-off 但用户给了配额** → 明确 `UNSUPPORTED`，**不降级成"忽略配额继续写"**（D8）。

**验收铁律**：既有测试全绿（当前 807 快照）；**无 Citation_Quota 的 rewrite/add_section 输出
与现状逐字节一致**（硬回归线）；带配额路径全离线可测（stub LLM/loop/port）；
写作循环内不检索；未通过不可豁免 Gate 不写 Working Copy。

关键纪律（承接 design-a2d D8/D10）：

- **带配额路径替代旧搜文献 SOP**：有 CitationQuota 时 Preflight 是唯一检索入口，
  `want_search/search_query` 只提供 initial_query，**禁止再调 `search_and_verify`**（避免双重检索）。
- **无配额路径完整保留** `want_search → search_and_verify(require_final=False)`。
- **资产提交不回滚**（D10）：commit 成功后即使 Bundle/LLM/Gate/写盘失败，Ledger 仍保留。
- **不可豁免 Gate**：Structural Gate、新增 unresolved/ambiguous/Bundle 外引用永不被 quota
  偏差豁免；预算耗尽仍未满足 → 返回失败或经 `port.resolve_final_gate` 明确接受
  `stage=final_gate` 偏差，不静默写回。

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2"] },
    { "wave": 3, "tasks": ["3"] },
    { "wave": 4, "tasks": ["4"] }
  ]
}
```

- Task 1（ContextBuilder bundle 参数）是生成侧地基。
- Task 2（写作编排接 Preflight + 合并 Gate）依赖 Task 1。
- Task 3（原子激活：挂槽 + Handler + flag）依赖 Task 2。
- Task 4 端到端 + 结构验收 + 全套回归。

## Tasks

- [x] 1. ContextBuilder 接入 Bundle（`generate/context_builder.py`）
  - `build_rewrite_messages(..., bundle=None)` / `build_add_section_messages(..., bundle=None)`：
    - `bundle is None` → **消息逐字节不变**（现有 `_fmt_refs(refs)` 路径原样）；
    - `bundle` 非 None → 用 `build_evidence_block(bundle)` **替代**「可用文献列表」段，
      提供摘要级证据；并在 system 段追加声明：只能引用数据块中的文献、
      无摘要条目不得据标题推导结论。
  - 参数加在**末尾且默认 None**，既有全部调用点零改动。
  - 测试：bundle=None 时两个构造函数输出与改前逐字节一致（对照字符串断言）；
    bundle 非 None 时含数据块定界 + 摘要 + 「指令不可执行」声明。
  - _Requirements: 12.3, 12.12, 12.13, 17_
  - _Design: design-a2d.md D6（ContextBuilder）_

- [x] 2. 写作编排：Preflight 接入 + 合并 Gate（`intents/rewrite.py` / `intents/add_section.py`）
  - 两个 `execute_*` 加末尾默认参数 `citation_context=None`（frozen 小对象：
    `bundle` / `effective_constraints` / `quota` / `deviations` / `port` / `classifier` /
    `policy`）。`None` → **完全走旧路径**（含 `want_search → search_and_verify`），逐字节不变。
  - `citation_context` 非 None（带配额）：
    - **跳过旧 `search_and_verify`**（Preflight 已在编排层完成检索，D8：禁止双重检索）；
    - 生成循环每轮：`build_*_messages(..., bundle=ctx.bundle)` → LLM →
      既有 Gate（RewriteGate/AddSectionGate）**与** `CitationQuotaGate.check(...)`
      （rewrite 传 `original_text=original` 使原引用可保留；add_section 传 ""）；
    - **合并违规一次回灌**：`structural.violations + citation.violations`，并把
      「Bundle 中未被引用且满足维度的候选」附给下一轮 prompt；
    - **共用 retry 预算**（`max_retry`）；循环内**不检索**、**不换 Bundle**；
    - 全部通过（含 CitationQuotaGate `passed`）→ 才 splice 写回；
    - 预算耗尽仍未满足 → 若有 `ctx.port` 调 `resolve_final_gate`：accept → 记
      `stage=final_gate` 偏差并写回；cancel/None → 返回失败**不写回**。
    - **非法引用（unresolved/ambiguous/Bundle 外）永不被偏差豁免**。
  - 结果对象补充配额报告字段（`quota_report`：original/effective gaps + used/unused），
    默认空 → 无配额路径的结果结构不变。
  - 测试（stub LLM）：无 ctx → 与现状一致（含 search_and_verify 被调用）；
    带 ctx → **search_and_verify 不被调用**；配额满足 → 写回；配额不足 → 回灌重试；
    预算耗尽 + 无 port → 不写回；预算耗尽 + port accept → 写回并记 final_gate 偏差；
    新增 Bundle 外引用 → 即使 port accept 也不写回。
  - _Requirements: 13.12, 14.1, 14.5, 14.7, 14.9, 14.10, 4.5_
  - _Design: design-a2d.md D7, D8, D10_

- [x] 3. 原子激活：挂槽 + Handler 路由 + flag（`intent_spec.py` / `handlers.py`）
  - 把 `CITATION_QUOTA_SLOT_SPEC` 挂进 `REWRITE_SPEC` / `ADD_SECTION_SPEC` 的 slots
    （**与 Handler 消费同批交付**，兑现 A2b D11 的原子激活）。
  - `HandlerContext` 增可选依赖：`literature_orchestrator` / `literature_port` /
    `writing_integration_enabled` / `base_year` / `classifier` / `language_policy`（全部默认
    None/False → 无配额路径不受影响）。
  - `RewriteHandler` / `AddSectionHandler`：
    - 从槽读 raw quota → `materialize_citation_quota(raw, base_year=ctx.base_year)`；
    - **flag 关但用户给了配额** → 返回 `OperationStatus.REJECTED` +
      「该能力未启用」（**不降级忽略配额**，D8）；
    - materialize 不可执行（needs_clarification / 冲突）→ REJECTED + 明确说明
      （**不进写作**，Property 2）；
    - 可执行 → 调 `WritingLiteratureOrchestrator.run(...)`；
      非 `READY_TO_WRITE`（NEEDS_* / CANCELLED）→ 返回对应状态与说明，**不进写作**；
      READY → 组 `citation_context` 传给 `execute_*`。
    - 槽为空（用户没提配额）→ 走原路径，行为逐字节不变。
  - 测试：槽已在 Active spec；无配额 → 旧路径（断言 orchestrator 未被调用）；
    flag 关 + 有配额 → REJECTED「未启用」；materialize 冲突 → REJECTED 不写作；
    orchestrator 返回 NEEDS_* → 对应状态不写作；READY → execute_* 收到 citation_context。
  - _Requirements: 16.2, 16.4, 13.1, 13.10_
  - _Design: design-a2d.md D1, D8_

- [x] 4. 端到端 + 结构验收 + 全套回归
  - 端到端（全 stub：search/verify/reviewer/writer LLM/port）：一次带配额 rewrite
    从 raw quota → materialize → Preflight（检索+确认+commit）→ Bundle → 生成 →
    合并 Gate → 写回，断言：Working Copy 已更新、Ledger 保留新文献、配额报告含
    original/effective、trace 不含原始 query/摘要。
  - **硬回归线**：无配额 rewrite/add_section 输出与现状逐字节一致；
    `literature_writing_integration` 默认 False 时全链路行为不变。
  - **资产不回滚**：commit 成功后令生成失败 → Ledger 仍保留、Working Copy 未变。
  - AST：`literature/*` 不 import `tasking`/`application`；`intents/*` 不直接管理
    RetrievalLoop 状态（只消费 citation_context）。
  - 全套既有测试绿（当前 807 快照）；输出测试摘要并清理临时文件。
  - _Requirements: A2d-5 验收铁律, Property 25_
  - _Design: design-a2d.md Correctness Properties 1/2/9/11/12/13_

## Notes

- 每完成一个 Task，先说明变更与验证结果，再进入下一项；本批每个 Task 后都建议跑一次相关
  子集，Task 4 跑全套。
- **无配额零影响是硬回归线**：所有新参数默认 None/False，任何默认路径不得改变。
- Discovery 保持关闭（A2c-5 Task 3 挂账）；真实 relevance 评测未达阈值前 supervised 默认。
- A2d 完成后另开去冗余 spec（Shadow/Active 合并、可派生字段、quota.py 拆分）。
- git 提交由用户自行完成。

## 已定决策

- 接线一律「末尾默认 None 参数」：bundle / citation_context / Handler 可选依赖，
  保证无配额路径逐字节不变。
- 带配额时 Preflight 是唯一检索入口，跳过旧 `search_and_verify`；无配额路径完整保留旧 SOP。
- Writer Loop 每轮跑 Structural + CitationQuota 两类 Gate，合并违规一次回灌、共用预算、
  循环内不检索不换 Bundle。
- 非法引用永不被 quota 偏差豁免；预算耗尽须显式 accept（stage=final_gate）才写回。
- flag 关但用户给了配额 → 明确 UNSUPPORTED，不降级忽略。
- 槽激活与 Handler 消费同批交付（原子激活）。
