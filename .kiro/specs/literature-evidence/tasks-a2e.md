# Implementation Plan — A2e：真实源 VerifyProvider 适配

> 承接 design-a2e。纯协议适配 + 组合层装配,让 OpenAlex/arXiv/S2 参与 A2c 交叉核验。
> **零回归线**：mock 路径、旧 `search_and_verify`、既有 CrossVerifier 测试逐字不变;
> 集成开关默认关。全程离线可测（注入 stub inner）。

## 已定决策（用户未另指定 → 采用 design 建议默认值）
- S2 仅当 `S2_API_KEY` 存在才注入（匿名 429）。
- OpenAlex 本期不支持 arxiv 回查（返回 NOT_FOUND_AT_SOURCE）;arxiv 交 arxiv 源 final。
- 归入 `literature-evidence` spec,命名 A2e。

## Task Dependency Graph
```json
{ "waves": [ { "wave": 1, "tasks": ["1"] }, { "wave": 2, "tasks": ["2"] }, { "wave": 3, "tasks": ["3"] } ] }
```

## Tasks

- [x] 1. VerifyProvider 适配器（`providers/retrieval/verify_adapters.py`）
  - 新增 `OpenAlexVerifyProvider` / `ArxivVerifyProvider` / `SemanticScholarVerifyProvider`,
    各持一个 `inner`（默认惰性构造真实 provider,可注入 stub）。
  - `fetch_metadata(id_type, id) -> FetchResult`（design D2/D3）：
    - id_type 属本源能查的类型 → 调 `inner.fetch_metadata(ident)`：
      Entry→FOUND / None→NOT_FOUND_AT_SOURCE / `RetrievalUnavailable`→PROVIDER_ERROR /
      `RetrievalNotFound`→NOT_FOUND_AT_SOURCE。
    - S2 需把 id_type 映射为前缀（`DOI:` / `ARXIV:` / `PMID:` / 裸 paperId）。
    - 不能查的 id_type → 直接 `FetchResult(NOT_FOUND_AT_SOURCE, detail=...)`,**不调 inner、不抛错**。
  - `search_bibliographic(query, *, limit=5) -> list`（design D4）：
    由 `BibliographicQuery` 拼查询串调 `inner.search`;无匹配 `[]`;
    `RetrievalUnavailable` **上抛**。
  - 各适配器满足 `isinstance(x, VerifyProvider)`;不 import tasking/literature。
  - 测试：三态忠实映射;不支持类型不调 inner；S2 前缀映射正确；search_bibliographic
    三情形（[]/上抛/命中）。
  - _Requirements: 2.6, 2.7, 4.1, 11.5, 11.17_
  - _Design: design-a2e D1/D2/D3/D4_

- [x] 2. 组合层注入真实 verify 源（`application/literature_composition.py`）
  - `_build_registry`：非 mock 时 verify_sources 注入 crossref + openalex + arxiv
    （+ s2 仅当 `S2_API_KEY`）;search 源不变;mock 路径不变。
  - `named(...)` 校验角色（profile 已声明 VERIFY）。
  - 测试：非 mock registry 的 verify 源集合正确、含/不含 s2 随 env;
    mock registry 逐字不变;CrossVerifier 能对 arxiv/openalex_work 选到 final 源。
  - _Requirements: 11.5, 4.1_
  - _Design: design-a2e D5_

- [x] 3. 端到端（离线 stub inner）+ 全套回归
  - 端到端：构造 stub inner（arxiv 源对某 arxiv-only 候选 FOUND）→ 经组合 orchestrator
    检索→交叉核验→入库→证据集,断言该 arxiv-only 候选**出现在 bundle**
    （对照：仅 crossref 时它被排除）。
  - DOI-仍须-registry-final：openalex 对 DOI 命中只 corroboration,单靠它不 VERIFIED。
  - PROVIDER_ERROR 不塌缩：inner 抛 Unavailable → 该标识符记 PROVIDER_ERROR 事件,
    不误判 not-found。
  - 全套既有测试绿（当前 838 快照）;输出摘要;清理临时文件。
  - _Requirements: 2.7, 4.1_
  - _Design: design-a2e 正确性属性 1–7_

## Notes
- 每个 Task 后跑相关子集,Task 3 跑全套。
- git 提交由用户自行完成。
- 真实连通性（真打 OpenAlex/arXiv）属 env-gated 冒烟,不进默认 CI。
