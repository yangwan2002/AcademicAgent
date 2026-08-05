# Implementation Plan — A2c-1：核验地基（Crossref + 能力协议 + 严格核验策略 + CrossVerifier）

## Overview

A2c 的第一期，只建**核验地基**：真实 registry 级核验源（Crossref）、按能力拆分的
provider 协议、版本化的严格核验策略（读时投影，不改历史事件）、多标识符交叉核验器。
**不接写作链路、不跑完整 Attempt/Loop、不改全局默认、不动旧 `search_and_verify`。**

**验收铁律**：既有测试全绿（当前 615 快照）；Crossref/CrossVerifier **全部用 stub
provider 离线可测**，默认套件不发网络请求（真实 Crossref 只做 env-gated 冒烟）；
`literature` 不 import `tasking`；打开严格策略**只在新组件内**，旧 rewrite/add_section 的
`search_and_verify(require_final=False)` 行为逐字节不变；`config.literature_require_final_verification`
全局默认**不改**。

关键纪律（承接 design-a2c D1–D4）：

- **严格核验是读时投影，不改历史事件**（D2）：A1 曾把 aggregator 对 DOI 的成功回查固化为
  `authority=final`；不得改写这些历史事件（会造第二真相源）。改为 `status_under_policy(events,
  policy, authority_registry)` 按 `(source, identifier_type)` 重算，版本化 authority registry
  绑定 `STRICT_FINAL_V1`，同一批事件在任何时间得同一严格状态。
- **`not_found` ≠ `provider_error` 全程保持**（D3，Req 2.6/2.7）：Crossref exact DOI 404 →
  `NOT_FOUND_AT_SOURCE`（候选保持 UNVERIFIED，**不宣称论文不存在**，DataCite DOI 可能不在
  Crossref）；连接/超时/限流 → `PROVIDER_ERROR`；模糊书目查询无匹配 → `UNRESOLVED_NO_MATCH`
  （**绝不**产生 not_found / 疑似不存在）。
- **交叉核验有预算、有缓存、不无界遍历**（D4）：每个 work identifier 选对该类型有 final/
  corroboration 权限的源；DOI→Crossref、arxiv→arXiv、openalex_work→OpenAlex、
  semantic_scholar_paper→S2；至少一条 strict final VERIFIED 且无 open final conflict 才达严格门槛。

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

- Task 1（能力协议）与 Task 2（严格核验策略）不同文件、互不依赖，可并行。
- Task 3（Crossref）实现 Task 1 的 VerifyProvider 协议，排其后。
- Task 4（CrossVerifier）依赖 1（协议）+ 2（策略）+ 3（Crossref），排最后。
- Task 5 结构验收 + 全套回归。

## Tasks

- [ ] 1. 按能力拆分的 provider 协议（`providers/retrieval/capabilities.py`）
  - 定义 `SearchProvider` / `VerifyProvider` / `DiscoveryProvider` 三个 `Protocol`
    （`runtime_checkable`），不强迫一个源实现全部能力（Crossref 不该伪造 `search`）：
    - `SearchProvider.search(query, *, limit, filters=()) -> list[RetrievalCandidate]`
    - `VerifyProvider.fetch_metadata(identifier_type, identifier) -> FetchResult`
    - `VerifyProvider.search_bibliographic(query: BibliographicQuery, *, limit) -> list[RetrievalCandidate]`
    - `DiscoveryProvider.discover(query, *, limit) -> list[DiscoveryLead]`（A2c-5 才用，先声明）
  - 定义 `FetchResult`（frozen）：`status ∈ {FOUND, NOT_FOUND_AT_SOURCE, PROVIDER_ERROR}`
    + 可空 `candidate: RetrievalCandidate` + `detail`。**网络错误绝不伪装成空结果**
    （Req 2.7）；`NOT_FOUND_AT_SOURCE` 表示「该源未命中此标识符」，不等于「论文不存在」。
  - 定义 `BibliographicQuery`（frozen：title/authors/year）与 `DiscoveryLead`
    （frozen：title/authors/year/url/source，A2c-5 用；此期只定义占位不实现逻辑）。
  - 不新增网络代码；不 import `tasking`。既有 `providers/retrieval/base.py` 的
    `RetrievalProvider` / `RetrievalError` / `RetrievalNotFound` / `RetrievalUnavailable`
    **保持不变**（旧 provider 继续用），新协议为并行增量。
  - 测试：`isinstance(stub, VerifyProvider)` 等协议一致性；`FetchResult` 三态构造与不可变。
  - _Requirements: 2.7, 11.2（能力声明）_
  - _Design: design-a2c.md D1_

- [ ] 2. 版本化 authority registry + 严格核验策略（读时投影）（`literature/verification.py`）
  - 新增 `VerificationPolicy(str, Enum)`：`COMPAT_A1` | `STRICT_FINAL_V1`。
  - 新增**版本化 authority registry**：一张绑定 `STRICT_FINAL_V1` 的不可变
    `(source, identifier_type) -> final|corroboration|none` 权限表 + 稳定 `version` 字符串。
    复用 A1 `sources.verification_authority` 的口径（Crossref/doi=final、OpenAlex/doi=corroboration
    等），但**冻结成版本化快照**，不随后续 config 变化（同一批事件任何时间得同一严格状态）。
  - 新增 `status_under_policy(events, policy, authority_registry) -> VerificationStatus`：
    - `COMPAT_A1`：等价现有 `aggregate_status`（读事件里存的 `authority`，行为不变）。
    - `STRICT_FINAL_V1`：**忽略事件里存的 `authority`**，按 `(source, identifier_type)` 用
      版本化 registry 重算每条事件的有效 authority，再走与 `fold_events` **相同**的因果折叠
      （QUARANTINED > final CONFLICT > final VERIFIED > UNVERIFIED）。OpenAlex/S2 的 DOI 事件
      即使历史写了 final，严格视图只算 corroboration；Crossref DOI、arXiv 原生 id 才算 final。
  - **不把 `policy_version` 写进 `VerificationEvent`**（D2）：原始事件只记「发生了什么」，
    策略版本写在 `VerificationAssessment`（Task 4）/ Batch / trace。若将来需要迁移事件 JSON，
    只新增纯序列化用途的 `event_schema_version`，**不得**用它决定 authority。
  - 既有 `aggregate_status` / `fold_events` / `make_event` **保持不变**；`ReferenceRecord.
    verification_status` 仍是 COMPAT 投影（现状）。严格可用性判断走 `status_under_policy`，
    **不得**只依赖 `ReferenceRecord.verification_status`。
  - 测试：旧 OpenAlex/DOI compat-final 事件 → COMPAT 下 VERIFIED、STRICT 下仅 corroboration →
    UNVERIFIED；加一条 Crossref/DOI final 事件 → STRICT 下 VERIFIED；arXiv 原生 id STRICT final；
    final CONFLICT 在 STRICT 下阻塞；registry `version` 稳定且被读出。
  - _Requirements: 2.9, 4.1, 11.17, 11.24, 11.25_
  - _Design: design-a2c.md D2_

- [ ] 3. Crossref registry 核验源（`providers/retrieval/crossref.py`）
  - 实现 `CrossrefVerifyProvider`（符合 Task 1 的 `VerifyProvider`；profile 已在 A1
    `sources.py`：VERIFY / registry / final={doi}）：
    - `fetch_metadata("doi", doi)`：`GET https://api.crossref.org/works/{doi}` →
      命中构造 `RetrievalCandidate`（source="crossref"，work_identifiers 含 doi）→
      `FetchResult(FOUND)`；HTTP 404 → `FetchResult(NOT_FOUND_AT_SOURCE)`；
      连接/超时/限流 → `FetchResult(PROVIDER_ERROR)`（复用 `http_util.get_with_retry` 的
      `RetrievalNotFound`/`RetrievalUnavailable` 契约翻译，**不吞成空**）。
    - `search_bibliographic(BibliographicQuery, *, limit)`：
      `GET /works?query.bibliographic=<title...>&rows=limit` → 候选列表（供 A2c-5
      IdentifierResolution 比对）。**无匹配不产生 not_found**（返回空列表 = `UNRESOLVED_NO_MATCH`
      语义，调用方据此判定，见 D3）。
    - polite pool：可配 `mailto` / User-Agent；超时、退避、速率限制走 http_util。
    - 非 DOI 的 `identifier_type` → `FetchResult(NOT_FOUND_AT_SOURCE)` 或明确「不支持该类型」，
      不假装成功。
  - 测试用 **stub HTTP / 注入响应**离线覆盖：FOUND / 404→NOT_FOUND_AT_SOURCE /
    超时→PROVIDER_ERROR / 书目查询命中与空结果；解析正确构造 work_identifiers。
  - 真实 Crossref 单查询连通性作 **env-gated 冒烟**（如 `PAPER_LIVE_RETRIEVAL=1` 才跑），
    默认 skip。
  - _Requirements: 4.1, 2.6, 2.7, 11.17, 11.18_
  - _Design: design-a2c.md D3_

- [ ] 4. 多标识符交叉核验器 + 缓存（`literature/retrieval/cross_verify.py`）
  - 新建子包 `literature/retrieval/`（`__init__.py`）。
  - `CrossVerifier.assess(candidate, *, policy, budget, cache) -> VerificationAssessment`：
    - 对候选的**每个**规范化 work identifier，选一个对该类型有 **final** 权限的 VerifyProvider
      回查（doi→Crossref、arxiv→arXiv、openalex_work→OpenAlex、semantic_scholar_paper→S2）；
      无 final 源则退而记 corroboration。产出**多条** `VerificationEvent`（复用 A1 `make_event`）。
    - `VerificationAssessment`（frozen）：`events`、`strict_status`（= `status_under_policy(events,
      STRICT_FINAL_V1, registry)`）、`policy_version`、`authority_registry_version`、`issues`。
    - **预算**：所有 verify 调用计入注入的 `budget`（`max_verify_calls` / `max_provider_calls` /
      `provider_time_budget_s`）；触顶即停并如实标注，不无界遍历标识符。
    - **缓存**：`(provider, identifier_type, normalized_id)` 级，跨 Attempt 复用；
      `PROVIDER_ERROR` 用短 TTL、`FOUND`/`NOT_FOUND_AT_SOURCE` 在本次 Loop 内可固定复用。
    - **门槛**：至少一条 strict final VERIFIED 且无 open final conflict → 达严格核验门槛；
      canonical identifier 在 final 源 `NOT_FOUND_AT_SOURCE` → 不可接受；
      次要 identifier not-found → 记 issue、不静默删除、候选默认不计配额待后续消解。
  - `CrossVerifier` 只依赖注入的 `ProviderRegistry` 视图 / VerifyProvider 集合 + 版本化 registry；
    不读全局 config、不读时钟（budget/clock 注入）。**不改**旧 `ReferenceVerifier` / `search_and_verify`。
  - 测试（stub VerifyProvider）：aggregator-only DOI → STRICT 下未达门槛；加 Crossref final →
    达门槛；arxiv→arXiv final；final CONFLICT 阻塞；canonical not-found 不可接受；次要 not-found
    记 issue 不删；预算触顶停止；缓存命中不重复调用、error 短 TTL。
  - _Requirements: 2.9, 4.1, 11.24, 11.25, 2.6, 2.7_
  - _Design: design-a2c.md D4_

- [ ] 5. 结构验收与全套回归
  - 全套既有测试绿（当前 615 快照，新增 A2c-1 测试后总数上升）；新增单测/属性测试全绿。
  - AST 结构断言：`literature/retrieval/cross_verify.py`、`literature/verification.py`
    不 import `tasking`；`providers/retrieval/crossref.py`、`capabilities.py` 不 import `tasking`。
  - 确认**旧路径零影响**：`search_and_verify(require_final=False)` 与 rewrite/add_section
    产物逐字节不变；`config.literature_require_final_verification` 全局默认仍为 False；
    `ReferenceRecord.verification_status` 仍走 COMPAT 投影。
  - 确认默认测试套件不发网络请求（真实 Crossref 冒烟 env-gated、默认 skip）。
  - 输出测试摘要并清理临时文件；不修改 Working Copy 和 facts.json schema。
  - _Requirements: A2c-1 验收铁律_
  - _Design: design-a2c.md D1–D4, Correctness Properties 1/2_

## Notes

- 每完成一个 Task，先说明变更与验证结果，再进入下一项。
- 全套测试以本轮开始时实际收集到的既有测试为基线；新增 A2c-1 测试后总数应上升，验收标准
  是既有测试无回归且新增测试全绿。
- A2c-1 **只建核验地基**，不跑 Attempt/Loop、不接写作链路、不改全局默认。严格策略只在新组件
  （CrossVerifier / 未来 Loop）通过注入生效。
- Crossref/CrossVerifier 的正确性由 stub 离线验证；真实源连通性属 env-gated 冒烟，不进默认套件、
  不引入网络 flakiness。
- **Requirements 11.13/11.16 的「模糊反查无匹配 → not_found/疑似不存在」口径与本设计
  （`UNRESOLVED_NO_MATCH`）冲突**——该同步属 A2c-5（Discovery/IdentifierResolution 落地时），
  A2c-1 不触及该路径，暂不改 requirements。
- git 提交由用户自行完成。

## 已定决策

- 严格核验是**读时投影**（`status_under_policy` + 版本化 authority registry），**不改写历史
  事件**、不把 policy_version 写进原始 `VerificationEvent`（避免第二真相源）。
- `require_final` / `STRICT_FINAL_V1` **只在新组件内**通过注入生效；旧 `search_and_verify` 与
  全局 `config` 默认不变，写作链路迁移留 A2d。
- `not_found`（exact fetch 404 → `NOT_FOUND_AT_SOURCE`）与 `provider_error`（连接/超时/限流）
  与 `UNRESOLVED_NO_MATCH`（模糊无匹配）三者严格区分，自 HTTP 层保持；「Crossref 没找到」
  不等于「论文不存在」。
- provider 按能力拆协议（Search/Verify/Discovery），不强迫一源实现全部；未知源无默认 VERIFY 权。
- CrossVerifier 有统一预算与 `(provider,id_type,normalized_id)` 缓存，不无界遍历；至少一条
  strict final VERIFIED 且无 open final conflict 才达严格门槛。
- 离线可测：真实 Crossref 实现但默认套件不发网络;真实源冒烟 env-gated。
