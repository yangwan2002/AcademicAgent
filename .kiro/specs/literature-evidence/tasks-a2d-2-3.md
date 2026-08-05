# Implementation Plan — A2d-2 + A2d-3：EvidenceSpan/Bundle + CitationQuotaGate（纯领域，未接线）

> 合批理由：两者都是**纯领域/纯函数**（span 算 offset、Bundle 选文献、Gate 解析计数），
> 全离线 stub 可测，且互相只有一处依赖（Gate 用 Bundle 算 used/unused）。
> **均不接写作链路、不翻 feature flag**——接线与激活属 A2d-4/A2d-5。

## Overview

- **A2d-2**：Req 12 全字段 `EvidenceSpan`（NFC offset/content_hash/verbatim_hash）+
  **quota-aware** `EvidenceBundle`（先保 zh/en 下限再补 total，非只按相关性截断）+
  ContextBuilder 的「外部引用数据块」。
- **A2d-3**：把 `_cite_tokens` 提升为公开 `extract_citation_tokens` + `CitationQuotaGate`
  （original/effective **双评估** + 新增引用边界硬失败）。

**验收铁律**：既有测试全绿（当前 752 快照）；写作路径逐字节不变（本批不接 rewrite/
add_section、不改 generator 循环、flag 仍关）；`literature` 不 import `tasking`；
Bundle/Span/Gate 全离线可测、不发网络。

关键纪律（承接 design-a2d D5/D6/D7 + 前置门槛 #4）：

- **EvidenceSpan 本期实现最小完整契约**（不推给 spec B）：`verbatim_text == normalized_source
  [start:end)`、hash 可复算、title→DISCOVERY_ONLY / abstract→CLAIM_SUPPORT、content_hash 变则 stale。
- **Bundle 必须 quota-aware**：只按相关性截断会把凑语言配额所需的中文文献截掉。顺序为
  ①STRICT verified+当前 relevance+filters → ②rewrite 原文已合法引用者优先固定 → ③按
  effective constraints 保 zh/en 下限 → ④补 total → ⑤剩余预算补高相关；预算不足 →
  `CONTEXT_BUDGET_INSUFFICIENT` + 逐维度 bundle gap（不进 Writer Loop）。
- **Gate 分两类失败**：配额未达（可被 effective_lower_bound / 用户偏差影响）vs
  **非法引用**（新增 unresolved / ambiguous / Bundle 外 → 永不 waiver、硬失败）。
- **effective vs original 双报告**：Gate 同时产 `effective_gaps`（按 AcceptedQuotaDeviation
  的 effective_lower_bound）与 `original_gaps`（按用户原始配额），诚实呈现。
- **rewrite 保留原引用 / add_section 全须在 Bundle**：rewrite 原文已有引用可原样保留（不推进
  新配额、不被自动删除），**新增**引用必须来自当前 Bundle。

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2"] },
    { "wave": 3, "tasks": ["3", "4"] },
    { "wave": 4, "tasks": ["5"] }
  ]
}
```

- Task 1（EvidenceSpan）是 Bundle 的地基。
- Task 2（quota-aware Bundle）依赖 Task 1，同文件故串行。
- Task 3（公开 token parser + Gate）依赖 Task 2 的 Bundle；Task 4（ContextBuilder 数据块）
  依赖 Task 2，两者不同文件可并行。
- Task 5 结构验收 + 全套回归。

## Tasks

- [ ] 1. EvidenceSpan 最小完整契约（`literature/evidence.py`）
  - `EvidenceCapability(str, Enum)`：`DISCOVERY_ONLY` / `CLAIM_SUPPORT`。
  - `EvidenceSpan`（frozen）：`span_id` / `reference_id` / `source_field`
    （`title` | `abstract` | `abstract_section:<name>`）/ `capability` / `source_version` /
    `content_hash` / `start_offset` / `end_offset` / `verbatim_text` / `verbatim_hash`。
  - `normalize_source(text) -> str`：**先** A2a `clean_external_text` **再** NFC；offset 与
    两个 hash 全部基于**同一** normalized 文本（Req 12.4）。
  - `make_span(reference_id, source_field, text, *, start=None, end=None, source_version="")`：
    默认整段一个连续 span；给定 start/end 时产确定性切片。**必须保证**
    `verbatim_text == normalized[start:end)`（Req 12.5）；`verbatim_hash` 由其算出。
  - `capability_of(source_field)`：`title` → DISCOVERY_ONLY；`abstract` /
    `abstract_section:*` → CLAIM_SUPPORT（Req 12.2）。
  - `is_stale(span, current_source_text)`：当前字段 normalized 文本的 content_hash 与 span
    记录不一致 → True（Req 12.6，不静默沿用旧 offset）。
  - `span_id` 由 `(reference_id, source_field, source_version, start, end, verbatim_hash)`
    稳定派生（同输入同 id）。
  - 不 import `tasking`；offset 单位为 **Unicode 码位**（不用字节/UTF-16）。
  - 测试（属性/单元）：`verbatim_text == normalized[start:end)` 恒成立（含中文/含 HTML 的
    脏文本）；hash 可复算；capability 映射；stale 判定；span_id 稳定且对 offset 敏感；
    NFC 等价输入得同一 hash。
  - _Requirements: 12.1, 12.2, 12.4, 12.5, 12.6, 17_
  - _Design: design-a2d.md D5_

- [ ] 2. quota-aware EvidenceBundle（`literature/evidence.py`）
  - `BundleEntry`（frozen）：`reference_id` / `bibkey` / `numeric_alias` / `title` / `year` /
    `language_bucket` / `relevance_verdict` / `relevance_confidence` / `spans:
    tuple[EvidenceSpan,...]` / `evidence_available: bool`（有无 CLAIM_SUPPORT span）。
  - `EvidenceBundle`（frozen）：`bundle_id` / `task_fingerprint` / `policy_version` /
    `authority_registry_version` / `entries` / `effective_constraints` / `truncated_count` /
    `budget_chars`。`bundle_id` 由内容 canonical hash 派生（**内容变 → 新 bundle_id → 新
    writing run**，Req 12.11）。
  - `build_bundle(records, *, criteria, quota, relevance_store, effective_constraints,
    budget_chars, preexisting_cited_ids=(), classifier, policy) -> BundleBuildResult`：
    1. **筛**：只取 `status_under_policy(STRICT_FINAL_V1)==VERIFIED` + 当前 task_fingerprint
       relevance 已接受 + 过全部 EligibilityFilter；
    2. **固定原有合法引用**：`preexisting_cited_ids` 中同时满足①的优先进 Bundle（rewrite 的
       原文引用可推进「正文实际引用」配额）；
    3. **保语言下限**：按 effective_constraints 的 `language.zh` / `language.en` 下界各取足；
    4. **补 total**：补到 total 下界；按 `reference_id` 去重；
    5. **稳定排序**：relevance verdict/confidence → evidence capability → year desc →
       reference_id（确定性）；
    6. **剩余预算补高相关**：预算内继续加，记 `truncated_count`；
    7. **预算不足**：若预算装不下 effective 下界 → `BundleBuildResult(status=
       CONTEXT_BUDGET_INSUFFICIENT, bundle=None, bundle_gaps=...)`，**不产可用 Bundle**
       （调用方须先决策，A2d-4）。
  - 预算计费只算送进 prompt 的 span 文本 + 条目元数据的字符数；**系统提示/原章节/用户要求的
    预算由调用方预留**（本函数只收 `budget_chars` 这个"给证据的额度"）。
  - `无 CLAIM_SUPPORT span 的条目`标注 `evidence_available=False`（Req 12.12，供 prompt 声明
    「不得就其内容做具体断言」）。
  - 不 import `tasking`；不读全局 config（classifier/policy/budget 注入）。
  - 测试：语言下限优先（zh min 2 时即使 zh 相关性低于某些 en 也必须入选）；total 补足；
    去重；稳定排序确定性；预算不足 → CONTEXT_BUDGET_INSUFFICIENT + bundle_gaps；
    `preexisting_cited_ids` 被固定；truncated_count 正确；bundle_id 对内容敏感；
    未过 STRICT / relevance 未接受 / 不过 filter 的记录不入 Bundle。
  - _Requirements: 12.8, 12.10, 12.11, 12.12, 12.13_
  - _Design: design-a2d.md D6_

- [ ] 3. 公开 token parser + CitationQuotaGate（`gates/citation_quota_gate.py` + `gates/diff_gate.py`）
  - **提升为公开**：`gates/diff_gate.py` 增 `extract_citation_tokens(text, ext) ->
    tuple[str, ...]`（确定性、保序去重），`_cite_tokens` 改为薄包它（**逐字行为不变**，
    既有 RewriteGate/AddSectionGate 测试全绿）。
  - `CitationQuotaGateResult`（frozen）：`passed` / `effective_gaps` / `original_gaps` /
    `used_reference_ids` / `unused_reference_ids` / `new_unresolved_tokens` /
    `new_ambiguous_tokens` / `new_out_of_bundle_ids` / `preserved_legacy_tokens` / `violations`。
  - `CitationQuotaGate.check(candidate, *, ext, bundle, ledger, quota,
    effective_constraints, original_text="", classifier, policy) -> CitationQuotaGateResult`：
    1. `extract_citation_tokens(candidate, ext)`；`original_text` 非空时同样抽取，得
       **新增 token 集合**（rewrite 语义：原文已有的原样保留者不算新增）；
    2. 每个 token 经 `ledger.resolve_token(token, ext=ext)` 保留 **三态**：
       RESOLVED / AMBIGUOUS / NOT_FOUND（**不把歧义当未找到、不任选一条**）；
    3. **非法引用（硬失败，永不 waiver）**：新增 token 中 NOT_FOUND → `new_unresolved_tokens`；
       AMBIGUOUS → `new_ambiguous_tokens`；RESOLVED 但**不在 Bundle** →
       `new_out_of_bundle_ids`。三者任一非空 → `passed=False` 且列入 violations；
       原文保留的 legacy/unresolved token 记 `preserved_legacy_tokens`，**单独报告、不推进
       配额、不因本次 rewrite 被自动删除**；
    4. **计数**：只 `Bundle ∩ candidate resolved` 参与配额；按 `reference_id` 去重；施加
       EligibilityFilter + language policy（复用 A2b `count_by_dimension`）；
    5. **双评估**：`evaluate_constraints(counts, effective_constraints)` → `effective_gaps`；
       `evaluate_constraints(counts, quota.count_constraints)` → `original_gaps`（诚实报告）；
       `passed` 取决于 **effective_gaps 为空且无非法引用**；
    6. `used_reference_ids = Bundle ∩ candidate`、`unused = Bundle − used`；断言二者不交、
       并集 == Bundle 的 reference_ids（Property 24）。
  - Gate **不检索、不换 Bundle**；本批**不接生成循环**（接线属 A2d-5）。
  - 测试：tex/md 抽取 + 多 token + 别名；三态（NOT_FOUND/AMBIGUOUS/Bundle 外）各自硬失败；
    rewrite 原引用保留不算新增、不推进配额；add_section（original_text="")全部引用须在 Bundle；
    去重按 reference_id；**effective 18 而 original 20 时：引 18 篇 passed 且 original_gaps
    非空；引 2 篇仍 failed**；used/unused 划分正确。
  - _Requirements: 14.2, 14.3, 14.4, 14.5, 14.6, 14.8, 4.5, 4.6_
  - _Design: design-a2d.md D4, D7_

- [ ] 4. ContextBuilder 外部引用数据块（`generate/context_builder.py`）
  - 新增 `build_evidence_block(bundle) -> str`：把 Bundle 条目渲染为**明确的外部数据块**
    （复用 A2a `wrap_as_data` 语义：声明其中指令不可执行）；每条含引用标记（tex 用 bibkey /
    md 用数字别名）、标题、年份、语言桶、以及 **CLAIM_SUPPORT span 文本**；
    `evidence_available=False` 的条目明确标注「无摘要可用，不得据标题推导具体结论」。
  - **本批只加函数、不改现有 `build_rewrite_messages`/`build_polish_messages` 的默认行为**
    （bundle 参数默认 None → 消息逐字节不变）；真正接入生成属 A2d-5。
  - 测试：数据块含定界与"指令不可执行"声明；无摘要条目带禁止推导标注；bundle=None 时
    既有消息构造逐字节不变（快照/相等断言）。
  - _Requirements: 12.3, 12.12, 17.2, 17.3_
  - _Design: design-a2d.md D6（ContextBuilder 部分）_

- [ ] 5. 结构验收与全套回归
  - 全套既有测试绿（当前 752 快照，新增测试后总数上升）；新增单测全绿。
  - AST：`literature/evidence.py` 不 import `tasking`、不 import `config`；
    `gates/citation_quota_gate.py` 不 import `tasking`。
  - **写作路径逐字节不变**：`citation_quota` 仍不在 Active spec；
    `literature_writing_integration` 仍 False；rewrite/add_section/generator 未被本批修改
    （只新增函数、`_cite_tokens` 薄包后行为不变）；既有 diff_gate 测试全绿。
  - 输出测试摘要并清理临时文件；不修改 Working Copy 和 facts.json schema。
  - _Requirements: A2d-2/3 验收铁律_
  - _Design: design-a2d.md Correctness Properties 7/8/9/10_

## Notes

- 每完成一个 Task，先说明变更与验证结果，再进入下一项。
- **本批不接线**：Preflight 编排（A2d-4）、槽激活 + Handler/生成循环接线 + trace + 回滚边界
  （A2d-5）。故写作路径逐字节不变。
- Bundle 的「预算不足」不是失败而是**需决策**状态；决策交互属 A2d-4。
- Discovery 保持关闭（A2c-5 Task 3 挂账）。
- git 提交由用户自行完成。

## 已定决策

- EvidenceSpan 本期实现 Req 12 最小完整契约（offset/hash/verbatim），不推给 spec B。
- Bundle 是 quota-aware 的：先保 zh/en 下限 → 补 total → 剩余预算补高相关；预算不足返回
  CONTEXT_BUDGET_INSUFFICIENT，不产可用 Bundle。
- rewrite 原文已合法引用者优先固定进 Bundle（可推进「正文实际引用」配额）。
- Gate 双评估 effective/original；非法引用（新增 unresolved/ambiguous/Bundle 外）硬失败、
  永不 waiver；原文保留的 legacy token 单独报告、不推进配额、不自动删除。
- `_cite_tokens` 提升为公开 `extract_citation_tokens`，薄包保证既有行为逐字不变。
- ContextBuilder 本批只加 `build_evidence_block`，默认不改既有消息构造。
