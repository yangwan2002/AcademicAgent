# Implementation Plan — A2d-1：前置同步 + Quota materialize 机制（未激活槽）

> A2d（内部编号）== `requirements.md` Req 19.7 的高层 **A3（写作接入）**；能力名
> `literature_writing_integration`。本批只建**配额落地机制**并做需求同步，
> **不激活** `citation_quota` 槽——挂槽必须与消费（Preflight/Gate，A2d-4/5）**原子交付**
> （承接 A2b D11：不制造「抽取了却没人执行」）。

## Overview

A2d-1 交付：Req 13.11/14.6/Property 25 的 **effective_lower_bound** 措辞同步（前置门槛）；
`materialize_citation_quota`（Raw→executable，确定性 finalize + base_year 注入）；
`RewritePlan/AddSectionPlan.citation_quota` 字段；**条件式澄清**规则（用户显式给出的可选
配额槽若 INVALID/AMBIGUOUS 必须追问）；`literature_writing_integration` feature flag（默认关）。

**本批不做**（留 A2d 后续期）：LiteraturePreflight（A2d-4）、EvidenceSpan/Bundle（A2d-2）、
CitationQuotaGate（A2d-3）、**把 `citation_quota` 槽挂进 Active `REWRITE_SPEC/ADD_SECTION_SPEC`
与 Handler 路由到写作编排**（A2d-5 原子激活）；Discovery Loop 接线（A2c-5 Task 3 挂账，
本项目 Discovery 保持关闭，等真实 Web 后端立项再做）。

**验收铁律**：既有测试全绿（当前 741 快照）；**写作路径逐字节不变**——`citation_quota` 槽
未进 Active spec、flag 默认关，rewrite/add_section 的 prompt/搜索/写回与现状完全一致；
新机制全部**离线单测**（materializer 纯函数、clarification 规则用构造 Operation 测）；
`literature` 不 import `tasking`。

关键纪律（承接 design-a2d D1 + 前置门槛）：

- **已知缺口 = 有数量的偏差,不是维度级布尔豁免**（前置门槛 #3）：Req 13.11/14.6/Property 25
  同步为「原始差额不再判为**新**失败,但 Gate 仍执行用户接受时冻结的 `effective_lower_bound`」。
- **原子激活**：机制先建、flag 默认关、槽不进 Active spec;A2d-5 才连同 Preflight/Gate 一起
  翻 flag + 挂槽 + Handler 消费。
- **配额是确定性 materialize**：LLM 只产 RawQuotaSpec;kind=CITATION / scope 默认
  ACTUALLY_CITED / base_year 由代码注入,不由 LLM;裸数量按 A2b 的 `min`。

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2", "3"] },
    { "wave": 3, "tasks": ["4"] }
  ]
}
```

- Task 1（前置同步）是需求门槛,最先做（纯文档）。
- Task 2（materializer,新文件）与 Task 3（Plan 字段 + 条件澄清 + flag,改 interview/clarifier/config）
  不同文件,可并行。
- Task 4 结构验收 + 回归。

## Tasks

- [ ] 1. 前置同步：effective_lower_bound + Discovery 关闭 + supervised 默认（Req 13.11/14.6/Property 25）
  - 修改 `requirements.md`：
    - Req 13.11：用户接受「已知缺口」时,记录冻结的 `effective_lower_bound`（= 接受时的可用数）;
      后续 CitationQuotaGate **不因原始差额判为新失败,但仍按 effective_lower_bound 执行**——
      不得解释成「整个维度无条件跳过」（否则接受 18/20 后正文只引 2 篇也会通过）。
    - Req 14.6：同上收紧——已知缺口维度按 effective_lower_bound 校验,原始 gap 仍如实报告。
    - Property 25：措辞同步为「满足全部 Count_Constraint,**或**其 Quota_Gap 已被显式报告并经
      用户接受(记录 effective_lower_bound);Gate 按 effective 下界执行,无静默放行」。
  - 在 requirements.md 或 design 注记：Discovery Loop 接线属 A2c-5 Task 3（挂账,Discovery
    默认关闭进入 A2d,等真实 Web 后端立项再接）;真实 relevance 评测达标前默认 **supervised**
    路径（用户确认 EvidenceBundle 清单）,不作无人监督 Active 门槛。
  - `getDiagnostics` 校验 requirements.md 格式;确认与 design-a2d D4 的 AcceptedQuotaDeviation 一致。
  - _Requirements: 13.11, 14.6, Property 25（同步）_
  - _Design: design-a2d.md 前置门槛 #3, D4_

- [ ] 2. QuotaMaterializer（`tasking/quota_materializer.py`）
  - `materialize_citation_quota(raw, *, base_year) -> QuotaMaterializationResult`：
    - 薄包 A2b `finalize_quota(raw, kind=QuotaKind.CITATION, base_year=base_year,
      bare_count_policy="min")`;**kind/scope 由代码注入,不由 LLM**;成功时 scope 缺省为
      ACTUALLY_CITED。
    - `QuotaMaterializationResult`（frozen）：`spec: QuotaSpec | None` / `issues`（透传
      QuotaFinalizeResult.issues）/ `needs_clarification: bool`（含 NEEDS_CLARIFICATION）/
      `executable: bool`。
    - 成功产出的 `spec` **必是** executable QuotaSpec（kind=CITATION、scope=ACTUALLY_CITED）;
      **绝不**把 RawQuotaSpec / QuotaFinalizeResult 当成品外传。
    - `raw` 为 None/空 → 返回 `spec=None, needs_clarification=False`（用户没提配额,不追问）。
  - `tasking` 可 import `literature.quota`（方向正确）;不读全局 config（base_year 注入）。
  - 测试：中英+年份+类型 raw → executable CITATION/ACTUALLY_CITED;裸数量 min;冲突/近似 →
    needs_clarification;None raw → spec None 不追问;断言 kind/scope 恒为 CITATION/ACTUALLY_CITED。
  - _Requirements: 6.4, 6.5, 16.2_
  - _Design: design-a2d.md D1_

- [ ] 3. Plan 字段 + 条件式澄清 + feature flag（`interview.py` / `clarifier.py` / `config.py`）
  - `RewritePlan` / `AddSectionPlan` 增 `citation_quota: QuotaSpec | None = None`（默认 None →
    现有构造点与行为不变）。
  - **条件式澄清**（`clarifier.py`）：现有 `_is_gap` 只对 required/must_ask 追问。新增规则：
    **可选槽若用户显式给出值但 `field_status ∈ {INVALID, AMBIGUOUS}` → 也构成缺口需追问**
    （USER_ABSENT 的可选槽仍不追问）。追问答案再走同一 Raw 配额 schema + `materialize_citation_quota`,
    **不把自然语言字符串直接塞进 QuotaSpec**。用一个可配置的「条件澄清槽名集合」或槽级标记
    实现,避免影响其它可选槽（constraints/presets 等 USER_ABSENT 不受影响）。
  - `config.py` 增 `literature_writing_integration`（默认 **False**）：A2d 全链路的总开关;
    本批只定义、**不翻开**（槽激活 + Handler 路由属 A2d-5）。
  - **不**把 `citation_quota` 槽加入 `INTENT_SPECS` 的 REWRITE/ADD_SECTION（A2d-5 才挂）;
    本批的槽/澄清用**构造的 Operation**离线测,不经 Active pipeline。
  - 测试：Plan 字段默认 None、既有构造不受影响;条件澄清——构造带 citation_quota=INVALID 的
    可选槽 Operation → `_is_gap` 判为缺口;USER_ABSENT 的可选槽不判缺口;config flag 默认 False +
    env 解析。
  - _Requirements: 16.2, 16.4_
  - _Design: design-a2d.md D1, D8_

- [ ] 4. 结构验收与全套回归
  - 全套既有测试绿（当前 741 快照,新增 A2d-1 测试后总数上升）;新增单测全绿。
  - **写作路径逐字节不变**：`citation_quota` 未进 `spec_for(REWRITE/ADD_SECTION).slots`;
    `literature_writing_integration` 默认 False;rewrite/add_section 现有测试与产物不变。
  - AST 结构断言：`tasking/quota_materializer.py` 不 import `application`;`literature` 仍不
    import `tasking`。
  - 确认 Requirements（13.11/14.6/Property 25）与 design-a2d D4 口径一致（effective_lower_bound）。
  - 输出测试摘要并清理临时文件;不修改 Working Copy 和 facts.json schema。
  - _Requirements: A2d-1 验收铁律_
  - _Design: design-a2d.md D1, Correctness Properties 1/2_

## Notes

- 每完成一个 Task,先说明变更与验证结果,再进入下一项。Task 1（需求同步）先做。
- 全套测试以本轮开始时实际收集到的既有测试为基线;新增 A2d-1 测试后总数应上升。
- **A2d-1 只建机制不激活**：`citation_quota` 槽不进 Active spec、flag 默认关,故写作路径
  逐字节不变。真正激活（挂槽 + Handler 路由到 Preflight/Bundle/Gate）在 **A2d-5** 与消费链
  原子交付。
- Discovery Loop 接线归 A2c-5 Task 3（挂账,Discovery 保持关闭）;A2d 不动 RetrievalLoop 核心。
- git 提交由用户自行完成。

## 已定决策

- A2d == Requirements 高层 A3（写作接入）;能力名 `literature_writing_integration`。
- 已知缺口用有数量的 `effective_lower_bound`（AcceptedQuotaDeviation）,不做维度级布尔豁免;
  Req 13.11/14.6/Property 25 先同步。
- 配额确定性 materialize：LLM 只产 Raw;kind=CITATION/scope=ACTUALLY_CITED/base_year 代码注入;
  裸数量 min。
- 原子激活：本批机制先建、flag 默认关、槽不进 Active;A2d-5 才翻 flag + 挂槽 + Handler 消费。
- 条件式澄清：可选槽被用户显式给出且 INVALID/AMBIGUOUS 时追问;USER_ABSENT 不追问。
- Discovery 保持关闭（A2c-5 Task 3 挂账）;A2d 不改 RetrievalLoop 核心。
