# Design — A2e：真实源的 VerifyProvider 适配（把交叉核验从「仅 DOI」扩到多源）

> 承接 A2c-1 CrossVerifier（design-a2c D3/D4）。**不新增检索能力,只补协议适配**：
> 让已有的真实源（OpenAlex / arXiv / Semantic Scholar）能作为 A2c 的 `VerifyProvider`
> 参与交叉核验,使 arXiv-only、无规范 DOI 的论文也能达严格核验门槛并进入证据集。

## 背景与问题

现状（本轮联调已确认）：

- **SEARCH 是真的**：`OpenAlexRetrievalProvider` / `ArxivRetrievalProvider` /
  `SemanticScholarRetrievalProvider` 真打 HTTP,签名与 A2c `SearchProvider` 兼容,已能作
  检索源接入 Loop。
- **VERIFY 只有 Crossref**：只有 `CrossrefVerifyProvider` 实现了新的 `VerifyProvider`
  协议（`fetch_metadata(id_type, id)` + `search_bibliographic`）。Crossref 只对 `doi` 有
  final 权（`sources.py`）。
- **后果**：`CrossVerifier._pick_provider(id_type)` 只有 DOI 能选到 final 源。arXiv-only、
  或只有聚合库 id 的候选 → 拿不到 strict final VERIFIED → `assess_availability` 排除 →
  证据集恒缺这类文献。真实可用性卡在「仅 DOI」。

旧的 `RetrievalProvider.fetch_metadata(identifier)`（单参、无 id_type、无
`search_bibliographic`）**不满足** `VerifyProvider` 协议,故不能直接当核验源。

## 目标 / 非目标

**目标**
- 为 OpenAlex / arXiv / Semantic Scholar 各写一个 `VerifyProvider` 适配,复用其既有真实
  HTTP 实现,不重写网络层。
- 组合层把这些 verify 源按 `sources.py` 已声明的权限注入 `ProviderRegistry.verify_sources`,
  使 CrossVerifier 能对 doi / arxiv / openalex_work / semantic_scholar_paper 都选到源。
- 全程离线可测（注入 stub,不发网络）。

**非目标**
- 不做 web Discovery（另立 spec）。
- 不改 CrossVerifier / status_under_policy / sources.py 权限表 / 旧 `search_and_verify`。
- 不改相关性、配额、Bundle、写作路径。
- 不追求「每个源都能按每种 id 回查」——只覆盖各源真实支持的查法（见 D3）。

## 权限表（**不新增,直接引用 `sources.py::SOURCE_PROFILES`**）

| 源 | final | corroboration | 适配器实际支持的精确回查 |
|----|-------|---------------|--------------------------|
| crossref | doi | — | doi（已存在,不动） |
| arxiv | arxiv | doi | arxiv（按 id_list）;doi/其它 → NOT_FOUND_AT_SOURCE |
| openalex | openalex_work | doi, arxiv, pmid | doi、openalex_work（`/works/{id}`）;其它 → NOT_FOUND_AT_SOURCE |
| semantic_scholar | semantic_scholar_paper | doi, arxiv, pmid | doi、arxiv、pmid、paperId（`/paper/{prefix}:{id}`） |

> CrossVerifier 只会拿「对该 id_type 有 final/corroboration 权」的源去查（`_pick_provider`）。
> 各源都对**自己的 final id 类型**具备真实回查能力（arxiv→arxiv、openalex→openalex_work、
> s2→paperId）,故**不会**出现「canonical final 源查不了自己的 id」这种假 not-found。
> 对 corroboration 类型,能查的就查、查不了的按 D3 返回 NOT_FOUND_AT_SOURCE（记 issue,
> 不阻断,不静默删）。

## 设计要点

### D1 适配器位置与形态
新增 `providers/retrieval/verify_adapters.py`：

- `OpenAlexVerifyProvider(inner: OpenAlexRetrievalProvider | None = None)`
- `ArxivVerifyProvider(inner: ArxivRetrievalProvider | None = None)`
- `SemanticScholarVerifyProvider(inner: SemanticScholarRetrievalProvider | None = None)`

每个实现 `VerifyProvider` 两方法。**只做协议翻译**,网络与解析仍在被包裹的旧 provider。
`inner` 默认惰性构造真实 provider;测试注入 stub inner（或 stub `http_get`）即离线全测。

不 import `tasking` / `literature`。属 providers 层。

### D2 `fetch_metadata(id_type, id)` → FetchResult 三态映射（Req 2.7 铁律）
统一翻译规则（**区分「查无此文」与「调用失败」是硬约束**）：

- 旧 `inner.fetch_metadata(id)` 返回 `Entry` → `FetchResult(FOUND, candidate=Entry)`。
- 返回 `None`（旧契约：查无此文）→ `FetchResult(NOT_FOUND_AT_SOURCE)`。
- 抛 `RetrievalUnavailable`（超时/限流/连接/重试耗尽）→ `FetchResult(PROVIDER_ERROR, detail=...)`。
- 抛 `RetrievalNotFound`（若直接冒泡）→ `NOT_FOUND_AT_SOURCE`。

**绝不用空/None 掩盖失败**——PROVIDER_ERROR 必须显式,否则 CrossVerifier 会把网络故障
误读成「该源没有此文」,进而误判严格状态。

### D3 各源按 id_type 的回查策略
适配器先看 id_type 是否是本源能真实回查的类型：

- **能查** → 调 inner 的对应查法,按 D2 映射。
  - arxiv：`id_type=="arxiv"` → `inner.fetch_metadata(arxiv_id)`。
  - openalex：`id_type in {"doi","openalex_work"}` → `inner.fetch_metadata(ident)`
    （OpenAlex `/works/` 同时支持 DOI 与 openalex id,现有实现已处理 DOI 前缀）。
  - s2：`id_type in {"doi","arxiv","pmid","semantic_scholar_paper"}` →
    映射为 S2 的 id 前缀（`DOI:`、`ARXIV:`、`PMID:`、裸 paperId）后 `inner.fetch_metadata`。
- **不能查**（如 arxiv 适配被问 doi）→ 直接 `FetchResult(NOT_FOUND_AT_SOURCE, detail="arxiv 不按 doi 回查")`,
  **不发网络、不抛错**。沿用 `crossref.py` 对非 doi 的既有先例,保持一致。

**标题相似度校验不在适配器做**——CrossVerifier 已用返回 candidate 的 title 做
`_title_conflict` 判定（命中却标题不符 → CONFLICT）。适配器只忠实返回候选。

### D4 `search_bibliographic(query, *, limit)` → 书目模糊反查
- 由 `BibliographicQuery(title, authors, year)` 拼查询串,调 `inner.search(q, limit)`。
- **无匹配返回 `[]`**（语义 UNRESOLVED_NO_MATCH,绝不产生 not_found）。
- `inner.search` 抛 `RetrievalUnavailable` → **上抛**（provider 不可用绝不伪装成「没有匹配」,
  与 `crossref.py` 一致）。
- 此方法当前仅 A2c-5 Identifier_Resolution 消费（Discovery 未接）,但按协议完整实现并测。

### D5 组合层装配（`application/literature_composition.py`）
`_build_registry` 扩展 verify 源集合（search 源不变）：

- `retrieval_provider == "mock"` → 保持现状（MockVerifyProvider,离线自洽）。
- 否则 verify_sources 注入**全部可用真实核验源**,让 CrossVerifier 对各 id 类型都能选源：
  - `named("crossref", CrossrefVerifyProvider())`
  - `named("openalex", OpenAlexVerifyProvider())`
  - `named("arxiv", ArxivVerifyProvider())`
  - 仅当 `S2_API_KEY` 存在 → `named("semantic_scholar", SemanticScholarVerifyProvider())`
    （匿名 S2 极易 429,不默认注入）。
- search 源仍是配置选定的单一 provider（openalex/arxiv/…）。
- 注入顺序：**registry 用于 final 优先**（`verification_authority` 已保证 aggregator 对
  DOI 只作 corroboration,故即便 openalex 在列,DOI 仍须 crossref final 才 VERIFIED）。

`named(name, provider)` 会用 `profile_for(name)` 校验角色——上述名字在 `SOURCE_PROFILES`
均已声明 VERIFY,校验通过。

### D6 错误态与预算（复用,不新增）
- 所有 verify 调用经 CrossVerifier 计入注入的 `VerifyBudget`/`BudgetLedger`,触顶即停。
- 适配器不自建预算、不自建缓存（缓存在 CrossVerifier 的 `VerificationCache`）。
- 适配器无状态、可安全复用同一 inner。

## 正确性属性（测试锚点）
1. **三态忠实**：inner 返回 Entry/None/抛 Unavailable → FOUND/NOT_FOUND_AT_SOURCE/PROVIDER_ERROR,
   一一对应;PROVIDER_ERROR 绝不塌缩成 NOT_FOUND。
2. **不支持的 id_type 不发网络**：arxiv 适配被问 doi → 直接 NOT_FOUND_AT_SOURCE,inner 未被调用。
3. **final 权限真实可查**：各源对自己的 final id 类型能产 FOUND→VERIFIED（配合 CrossVerifier）。
4. **DOI 仍须 registry final**：openalex 对 DOI 命中只记 corroboration,单靠它不构成 strict
   VERIFIED（结构性保证仍由 status_under_policy 提供,不被本适配削弱）。
5. **端到端（离线 stub inner）**：arXiv-only 候选经 arxiv 适配 → strict VERIFIED → 入库 →
   出现在证据集;此前（仅 crossref）该候选被排除。
6. **search_bibliographic**：无匹配 []、provider 不可用上抛、命中返回候选。
7. **零回归**：mock 路径、旧 `search_and_verify`、既有 CrossVerifier 测试不变。

## 需要你拍板的点
1. **S2 默认注入策略**：建议「仅当 `S2_API_KEY` 存在才注入」（匿名 429）。是否同意?
2. **openalex 对 arxiv 的 corroboration**：OpenAlex `/works/` 不直接按 arxiv id 回查(需
   filter 查询)。建议本期 openalex 适配**不支持** arxiv 回查(返回 NOT_FOUND_AT_SOURCE),
   arxiv 交给 arxiv 源 final。是否可接受这个简化?
3. **命名**：沿用 A2e 归入 `literature-evidence` spec（design-a2e / tasks-a2e）。是否可以?

## 影响面
- 新增 1 文件（verify_adapters.py）+ 组合层 `_build_registry` 小改 + 测试。
- 不改领域层、不改 CrossVerifier、不改权限表、默认仍 supervised、集成开关默认关。
