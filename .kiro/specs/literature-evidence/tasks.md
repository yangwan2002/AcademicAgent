# Implementation Plan — A1：文献模型、核验事件、去重与迁移

## Overview

A1 只建**数据层地基**：`ReferenceRecord` 唯一可写真相源、`reference_id` 稳定主键、
work/container 标识符分离、只追加核验事件流、标识符级去重与合并、旧 facts 无损迁移。
零 LLM 调用、零用户交互变化、**网络调用次数不变**。

**验收铁律**：全套 476 测试绿；`.tex` / `.md` 写作链路行为逐字节不变。

两条最容易破的红线，实施时反复回看：

1. `facts.bibkeys()` / `ref_ids()` **必须保持 resolvable 语义**（含用户原稿的未核验引用）。
   一旦换成只含 VERIFIED 的 citable 语义，`RewriteGate` 会把用户原有引用判成非法（D9）。
2. `SessionFacts(path=..., known_refs=[...])` 有**四处**调用，`__init__` 必须继续接受
   这个参数（D8）。

A1 **不做**：Evidence_Span、Language_Verdict 判定、Relevance、Retrieval_Batch、
Retrieval_Loop、LiteraturePreflight、CitationQuotaGate、引文格式渲染、
`search_literature` 意图、Crossref / web_search 的 provider 实现。

## Task Dependency Graph

同一 wave 内无相互依赖、可并行；后一 wave 依赖前面所有 wave。

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2", "3"] },
    { "wave": 2, "tasks": ["4", "5"] },
    { "wave": 3, "tasks": ["6", "7"] },
    { "wave": 4, "tasks": ["8"] },
    { "wave": 5, "tasks": ["9", "10"] },
    { "wave": 6, "tasks": ["11"] },
    { "wave": 7, "tasks": ["12", "13"] },
    { "wave": 8, "tasks": ["14"] }
  ]
}
```

Wave 1–4（任务 1–8）**不改任何既有代码路径**，每完成一个都应跑全套确认 476 仍绿。
风险集中在 wave 5（改 HTTP 与 provider 异常行为）与 wave 6（`facts.py` 是五处消费方
的共同依赖）。

## Tasks

- [ ] 1. 标识符归一化与分类（`literature/identifiers.py`）
  - `WORK_TYPES = {doi, arxiv, openalex_work, semantic_scholar_paper, pmid}`；
    `CONTAINER_TYPES = {issn, eissn, isbn}`；`WORK_PRIORITY` 元组按
    doi > arxiv > pmid > openalex_work > semantic_scholar_paper
  - `normalize(id_type, raw)`：doi 去 `https?://(dx.)?doi.org/` 前缀 + strip + lower；
    arxiv 去 `arXiv:` 前缀与尾部 `vN` + lower；openalex_work 取 URL 末段 + upper；
    issn/eissn/isbn 去连字符与空白 + upper
  - `classify(raw)`：`10.\d{4,}/` → doi；`\d{4}\.\d{4,5}` 或 `[a-z-]+/\d{7}` → arxiv；
    `^W\d+` → openalex_work；纯数字 8 位内 → pmid；**无法判定返回 None，绝不猜**
  - `work_keys(pairs)` 只遍历 `WORK_TYPES`；归一化后为空串的不产生键（防空串互撞）
  - 属性测试：`normalize(t, normalize(t, x)) == normalize(t, x)` 幂等
  - 单测：各标识符形态；`classify` 对陌生串返回 None
  - _Requirements: 1.4, 1.5, 3.1, 3.3_
  - _Design: Components§1, D5_

- [ ] 2. 源角色与标识符级核验权限（`literature/sources.py`）
  - `ProviderRole` 枚举 `SEARCH / VERIFY / DISCOVER`；
    `PROVENANCE_RANK = {registry:3, publisher:2, aggregator:1, web:0}`
  - `SourceProfile(frozen){name, roles, provenance, final_identifier_types,
    corroboration_identifier_types}`
  - `SOURCE_PROFILES` 按 design D11 的表填 8 个源。**关键**：openalex 的 final 只有
    `openalex_work`、对 `doi/arxiv/pmid` 只作 corroboration；semantic_scholar 同理；
    crossref final=`{doi}`；arxiv final=`{arxiv}`；`legacy` 与 `web_search` 的
    final 为**空集**；`mock` final 为全类型（测试用）
  - `verification_authority(source, id_type) -> "final" | "corroboration" | "none"`
  - `resolve_field_conflict(a_source, b_source, priority)`：先比 `PROVENANCE_RANK`，
    同级才按 `priority` 配置序
  - 属性测试：任意配置序下 `web` 永不胜出；跨层级结果与配置序无关
  - 单测：`(openalex, doi)` → corroboration；`(crossref, doi)` → final；
    `(legacy, *)` → none
  - _Requirements: 11.2, 11.19, 11.20, 11.21, 11.24, 11.25, 11.27_
  - _Design: D11, D9b, Property 32, 33, 35_

- [ ] 3. 核验事件与因果折叠（`literature/verification.py`）
  - `EventKind`：核验尝试 `VERIFIED / CONFLICT / NOT_FOUND / PROVIDER_ERROR`
    + 变迁 `CONFLICT_RESOLVED / QUARANTINED / RELEASED`
  - `VerificationStatus`：`UNVERIFIED / VERIFIED / CONFLICT / QUARANTINED`
  - `VerificationEvent(frozen){event_id, kind, source, provenance, authority,
    identifier_type, identifier, checked_at, target_event_id, details}`；
    `make_event(...)` 算 `event_id = sha1(kind|source|identifier|checked_at|seq)[:12]`
  - `aggregate_status(events)` **按因果引用折叠，不看时间戳**：先校验
    `CONFLICT_RESOLVED.target` 必须指向存在的 `CONFLICT`、`RELEASED.target` 必须指向
    存在的 `QUARANTINED`（否则该变迁无效并计入 `invalid_transitions`）；再按
    `QUARANTINED（存在未被 released 的）→ CONFLICT（存在未被 resolved 的 final）→
    VERIFIED（存在 final）→ UNVERIFIED` 求值
  - **不存** `verification_status` / `resolved` / `quarantined` 任何可变字段
  - 属性测试：追加任一 final `CONFLICT` 后状态必不为 `VERIFIED`；
    仅 `PROVIDER_ERROR` 恒 `UNVERIFIED`；仅 `authority=corroboration` 恒 `UNVERIFIED`；
    **打乱事件顺序或让全部事件共用同一时间戳，折叠结果不变**；
    指向不存在 target 的变迁事件被忽略且被计入 `invalid_transitions`
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.8, 2.10, 11.26_
  - _Design: D3, Components§3, Property 5, 6, 37_

- [ ] 4. `ReferenceRecord` 不可变模型（`literature/models.py`）
  - `frozen=True`；`list` → `tuple`，`dict` → `tuple[tuple[str, str], ...]`
    （有序、可哈希、能 JSON 往返）
  - 字段：`reference_id`、`work_identifiers`、`container_identifiers`、
    `unclassified_identifiers`、著录字段全套（`reference_type`/`container_title`/
    `volume`/`issue`/`pages`/`article_number`/`publisher`/`publisher_place`/
    `edition`/`url`/`accessed_at`/`conference_name`/`conference_place`）、
    `bibkey`/`bibkey_aliases`/`numeric_aliases`/`merged_from`、
    `verification_events`、`legacy_verified`/`verification_method`、
    `discovery_sources`（**复数**）、`created_at`、`field_provenance`、
    `field_conflicts`、`language_hint`
  - 派生 property（**均不作存储字段、不进 JSON**）：`verification_status`
    （调 `aggregate_status`）、`canonical_identifier`（只从 `work_identifiers` 按
    `WORK_PRIORITY` 取）、`abstract_content_hash`（由 `abstract` 实时算）、
    `evidence_available`
  - `missing_fields(style)` **带参数**（字段齐全性取决于目标格式 + `reference_type`）；
    A1 只留接口与按 `reference_type` 的骨架表，四种格式的完整必需字段属最后一期
  - `work_id_map()` / `field_provenance_map()` 返回副本；`with_changes(**kw)`；
    `to_known_ref()`（`id` 取 `numeric_aliases[0]`）
  - `FieldConflict` / `DedupSuggestion` 值类型
  - 单测：frozen 赋值抛 `FrozenInstanceError`；`canonical_identifier` 永不选 container；
    改 `abstract` 后 `abstract_content_hash` 随之变化（无双真相源）
  - _Requirements: 1.2, 1.6, 1.7, 1.9, 1.10, 1.11, 1.14, 2.11, 3.10_
  - _Design: D7, D2, D6, Components§4, Property 39_

- [ ] 5. bibkey 生成（`literature/bibkey.py`）
  - 从 `tools/literature.py` 迁入 `_make_bibkey` → `make_bibkey(entry, taken)`，
    算法与冲突递增策略（`base2`/`base3`）逐字保留
  - `tools/literature.py` 保留 `_make_bibkey` re-export 防外部引用断裂
  - 单测：与迁移前输出逐字一致（拿现有测试用例做基线）
  - _Requirements: 1.7_
  - _Design: D8 模块表_

- [ ] 6. 去重与合并（`literature/dedup.py`）
  - `normalize_title`：NFC → casefold → 去标点 → 压空白；
    `first_author_key(authors)` 取姓氏并归一化
  - `find_duplicate(cand, ledger, *, allow_low)`：高置信=任一 work identifier 归一化
    同值；低置信=标题+年份+首作者姓氏三者全同。
    **`CONTAINER_TYPES` 不得出现在本模块任何位置**——`dedup_keys()` 只遍历
    `WORK_TYPES`，使 ISSN 去重结构上不可达
  - `merge_records(existing, incoming_cand, events, *, priority)`：
    identifiers / events 取并集（events 按 `event_id` 去重、按 `checked_at` 排序）；
    标量字段 existing 空则取 incoming 并登记 `field_provenance`，两侧非空且不同则按
    `resolve_field_conflict` 取胜并记 `FieldConflict`；**`abstract` 绝不清空**；
    bibkey 保留 existing、incoming 进 `bibkey_aliases`；`numeric_aliases` 取并集且
    existing 首个仍为主别名；`reference_id` 保留 `created_at` 较早者、另一个进
    `merged_from`；`legacy_verified` 逻辑或；`verification_status` **不合并**（派生）
  - 属性测试：合并后 `abstract` 非空性单调不减、identifiers 与 events 为超集；
    **ISSN 反例**——两条不同 DOI、相同 ISSN 的候选保持两条独立记录
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_
  - _Design: D5, D6, Components§5, Property 3, 4, 7_

- [ ] 7. `reference_id` 分配（`literature/ledger.py` 内部函数或 `models.py`）
  - `make_reference_id(cand_or_row)`：种子按优先级取
    `f"{type}:{normalized_value}"`（首个可得 work identifier，序同 `WORK_PRIORITY`）
    → `f"title:{normalize_title(title)}|{year}"` → `f"legacy:{normalize_title}|{numeric}"`；
    `"r" + sha1(seed).hexdigest()[:12]`，碰撞追加 `-2`
  - **一次分配永不重算**：入库后从记录读取，不再由当前 `canonical_identifier` 推导
  - 属性测试：同一 DOI 经任意顺序的多次 upsert 得同一 id（跨会话幂等）
  - _Requirements: 1.2, 1.3_
  - _Design: D1, Property 2_

- [ ] 8. `ReferenceLedger`（`literature/ledger.py`）
  - **五套索引**：work key → rid、bibkey → rid、bibkey_alias → rid、
    numeric alias → rid、**merged_from → rid**（漏掉这个则「旧 id 仍可解析」是空话）。
    `upsert` / `replace` 内部整体重建全部索引
  - `ResolutionStatus{RESOLVED, NOT_FOUND, AMBIGUOUS}` +
    `ResolutionResult(frozen){status, reference_id, candidates, matched_via}`；
    `resolve_token(token, *, ext)` 命中多条时返回 `AMBIGUOUS` + 候选集合，
    **绝不任选一条，也不退化为 NOT_FOUND**
  - `upsert(cand, events=()) -> UpsertResult`，action 四态：
    `CREATED | CREATED_WITH_SUGGESTION | MERGED | UNCHANGED`。
    低置信命中时**候选仍创建为独立记录** + 记一条 `DedupSuggestion`
  - `replace(record)`（唯一整条替换入口）、`ingest_known_refs(rows)`（迁移入口，
    任务 11/12 共用）、`next_numeric_alias()`（当前最大值 +1，语义同 `next_ref_id`）
  - **两个引用集合分开**：`resolvable_bibkeys()` / `resolvable_numeric_aliases()`
    （全部可解析，含未核验）与 `citable_ids/bibkeys/numeric_aliases(*, allow_legacy=True)`
    （仅 VERIFIED ∪ legacy_verified）
  - `all_records()` 返回 `tuple`；`suggestions()`；
    `integrity_report()` 汇总歧义别名与无效变迁事件；`to_json()` / `from_json()`
  - 属性测试：任意 upsert 序列后五套索引与 records 一致；对 `all_records()` 返回项
    赋值抛 `FrozenInstanceError`
  - 单测：`resolve_token` 四种入口（bibkey / alias / numeric / merged_from）+
    `AMBIGUOUS` 分支；`resolvable_*` 与 `citable_*` 在含未核验记录时结果不同；
    低置信命中后记录数 +1 且 suggestion 数 +1
  - _Requirements: 1.1, 3.8, 3.9, 4.2, 4.3, 4.4, 19.1_
  - _Design: D0, D5, D7, D9, Components§6, Property 34, 36, 38_

- [ ] 9. HTTP 层分层异常（`providers/retrieval/base.py` + `http_util.py`）
  - `base.py` 新增 `RetrievalNotFound(RetrievalError)` 与
    `RetrievalUnavailable(RetrievalError)`——**都继承 `RetrievalError`**，
    使既有 `except RetrievalError` 调用点行为不变
  - `get_with_retry` 分流：HTTP 404 → `RetrievalNotFound`；
    429 / 5xx / 超时 / 连接失败（重试耗尽后）→ `RetrievalUnavailable`；
    其余 4xx → `RetrievalUnavailable`（权限/参数问题**不是**「不存在」）
  - 背景：现状 404 走 `raise_for_status()` → `HTTPStatusError` → 被 `except Exception`
    抓 → `raise RetrievalError(str(exc))`，抛出的 `RetrievalError` **自身没有
    `.response`**，所以上层任何 `getattr(exc, "response", ...)` 恒得 None，
    真 404 会 100% 被判成 `PROVIDER_ERROR`
  - **必须直接测这一层**（原缺陷就在这里，stub provider 测不到）：monkeypatch
    `httpx.get`，断言 404 → `RetrievalNotFound`、429/5xx/超时 →
    `RetrievalUnavailable`、其余 4xx → `RetrievalUnavailable`，
    且二者都能被 `except RetrievalError` 捕获
  - _Requirements: 2.7_
  - _Design: D4(a), Error Handling 表_

- [ ] 10. 候选 DTO 扩多标识符（`workspace/models.py` + 四个 provider）
  - `ReferenceEntry` 增两个默认空字段 `work_identifiers: dict[str, str]` /
    `container_identifiers: dict[str, str]`（向后兼容，`vars(entry).copy()` 那类反射
    用法不受影响）；加类型别名 `RetrievalCandidate = ReferenceEntry`
  - provider 映射（**不增加任何网络调用**，数据本就在响应里）：
    - OpenAlex：`doi` + `openalex_work`；`primary_location.source.issn_l` → `issn`
    - Semantic Scholar：`paperId` → `semantic_scholar_paper`（**用 paperId，
      不用 CorpusId 代替**，二者是不同标识符体系）；`externalIds` 的
      `DOI`/`ArXiv`/`PubMed`；`fields` 参数已含 `externalIds`，无需改请求
    - arXiv：`arxiv`（atom 若含 doi 则一并取）
    - mock：按样本填 `arxiv`
  - `source_id` **保留不动**（`CitationVerifier` / `MultiSourceProvider._route_order` /
    `facts` 都在用）
  - 单测：各 provider 的 `_to_entry` 对同时有 DOI 与自有 id 的响应，两个标识符都保留
  - _Requirements: 1.4_
  - _Design: D12_

- [ ] 11. `verify_detailed` + `ReferenceVerifier`（`tools/citation.py` + `literature/verifier.py`）
  - 三个 provider 的 `fetch_metadata` 契约收紧：返回 `None` **仅**表示查无此文；
    OpenAlex 的 `except Exception: return None` 与 S2 的
    `except RetrievalError: return None` 改为只吞 `RetrievalNotFound`，
    其余向上抛 `RetrievalUnavailable`；`MultiSourceProvider.fetch_metadata` 改为
    「全部源返回 None → None；存在源抛 `RetrievalUnavailable` 且无源命中 → 抛出」
    （区分「都说没有」与「都没问到」）
  - `VerificationAttempt(frozen){outcome, matched, title_score, year_matches,
    error_kind, note}`；`CitationVerifier.verify_detailed(entry)` 新增；
    `verify()` 改为 `verify_detailed(...).outcome == VERIFIED`；
    `verify_and_mark` 继续基于 `verify()` → **既有调用方与测试零改动**
  - `literature/verifier.py` 的 `ReferenceVerifier` 只做结果翻译 →
    `VerificationEvent`，`authority` 由 `verification_authority(source, id_type)` 决定
  - **A1 只做一次回查**：按 `WORK_PRIORITY` 取第一个可用 work identifier，
    **不遍历多标识符**（否则「网络调用次数不变」不成立）。多标识符交叉核验属 A2
  - 单测：`verify_detailed` 四种 outcome（stub provider 分别抛 `RetrievalNotFound` /
    抛 `RetrievalUnavailable` / 返回不匹配标题 / 返回匹配）；
    核查是否有既有测试断言「网络失败时 `fetch_metadata` 返回 None」，有则单独处理并说明
  - _Requirements: 2.1, 2.5, 2.6, 2.7, 2.9, 4.1_
  - _Design: D4(b)(c), D11, Property 5, 35_

- [ ] 12. 迁移与原子写回（`literature/migration.py`）
  - `migrate_v1_known_refs(rows)`：每条旧 `KnownRef` → **恰好一条** `ReferenceRecord`
    （1:1，**不合并不丢弃**，即使 title 与 bibkey 皆空）。
    `id` → `numeric_aliases`；`bibkey` → `bibkey`；
    `title` → `title` 且 `field_provenance[title]="legacy"`；
    `verified=True` → `legacy_verified=True` + `verification_method="legacy"` +
    一条 `VerificationEvent(kind=VERIFIED, source="legacy")`。
    因 `legacy` 的 `final_identifier_types` 为空集，该事件天然非 final →
    聚合状态仍为 `UNVERIFIED`（迁移不虚高），**迁移代码无需特意设置**
  - **混合 schema 合并**：同时存在 `references` 与 `known_refs` 时，先读 references，
    再把 `known_refs` 中按 bibkey / 数字别名**无对应**的条目走迁移补入。
    **绝不静默忽略 `known_refs`**（旧结构可能含新结构没有的条目）
  - `atomic_write_json(path, data)`：① 目标存在且无备份则复制原始字节到
    `facts.v1.bak.json` ② 写 `<path>.tmp`（同目录保证同卷）③ `flush()` + `os.fsync(fd)`
    ④ `os.replace(tmp, path)` ⑤ 失败清理 tmp、原文件与备份保持完整
  - 迁移中某行字段类型异常 → 尽力构造（缺字段留空）、**不丢弃**、记警告
  - 属性测试：`len(migrate(rows)) == len(rows)` 条数守恒；混合 schema 下
    `len(result) == len(references ∪ known_refs独有)`
  - 单测：原子写回中断模拟（写 tmp 后抛异常 → 原文件与备份完整、无残留 tmp）
  - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5_
  - _Design: D10, Property 29_

- [ ] 13. `SessionFacts` 改造（`session/facts.py`）
  - **自定义 `__init__`**（不再用 dataclass 自动生成，property 无法兼容 dataclass 的
    `__init__`）：签名 `(self, path, known_refs=None, known_numbers=None, claims=None,
    *, ledger=None)`，**保留原位置参数顺序**使位置参数调用也不破。
    `known_refs` 非空则走 `ledger.ingest_known_refs(...)`
  - `known_refs` 只读 property（每次从 `ledger.all_records()` 派生投影）
    + setter 转 `replace_scanned_refs()`（merge 语义，不覆盖已核验，同 P0#18）
  - **`bibkeys()` / `ref_ids()` 保持旧语义** = resolvable（含用户原稿未核验引用）。
    这是红线：换成 citable 会让 `RewriteGate` 把用户原有引用判成非法
  - `add_ref` / `merge_refs` / `next_ref_id` 保留签名，内部委托 Ledger
  - `save()` 写 `{schema_version: 2, references, dedup_suggestions, known_numbers,
    claims}`，**不再写 `known_refs`**，经 `atomic_write_json`；
    `load()` 按 D10 三路分流（v1 迁移 / v2 直读 / 混合合并）
  - 失去 dataclass 的 `__eq__`/`__repr__`（已确认无代码比较 `SessionFacts` 相等性），
    手写 `__repr__` 便于调试
  - 单测：`SessionFacts(path=..., known_refs=[...])` 与位置参数两种构造；
    `facts.known_refs = extract_refs(...)` setter 路径；
    `bibkeys()` 包含未核验引用；旧 v1 facts.json 可无损加载
  - _Requirements: 1.13, 4.2, 4.5, 4.7, 19.2, 19.3, 19.6_
  - _Design: D8, D9, D10, Components§7, Property 36_

- [ ] 14. 接线、配置与回归（`tools/literature.py` + `config.py` + `observability/events.py`）
  - `search_and_verify`：`verifier.verify_detailed(cand)` → `ReferenceVerifier` 转事件
    → `facts.ledger.upsert(cand, events)`。`SearchOutcome` 字段与语义不变
    （`added: list[KnownRef]` 仍为投影）→ `rewrite.py` / `add_section.py` 零改动
  - `config.py` 五项（沿用 `_env_flag` / `_env_float`，非法值降级）：
    `literature_strict_verification=False`、`literature_title_similarity=0.85`、
    `literature_dedup_suggestions_enabled=True`、
    `literature_require_final_verification=False`、
    `literature_source_priority="openalex,semantic_scholar,arxiv"`。
    **不设 `literature_dedup_low_confidence`**（Req 3.5 已禁止低置信自动合并，
    再留开关等于给禁止项开后门）
  - `REFERENCE_LEDGER_UPSERT` 事件：`{action, dedup_basis, dedup_confidence,
    field_conflict_count, verification_status, legacy_verified}`；
    复用既有 Sanitizer，**不落**标题/摘要/完整 DOI 串；`trace_enabled` 默认 False
  - **必须验证**：`literature_require_final_verification=False` 下真机
    `search_and_verify` 的入库结果与现状一致。A1 未接 Crossref，OpenAlex 对 `doi`
    只有 corroboration 权限，若默认要求 final 会让入库结果从「核验通过」静默变成
    「仅佐证」
  - 全套回归，重点盯：`test_week3_rewrite.py`（`SessionFacts(known_refs=...)` 构造、
    `known_refs` setter、`bibkeys()` 计数、`add_ref` 去重、`next_ref_id`、
    md 路径 `ref_ids()`）、`test_week4_add_section.py`（两处构造 +
    `fix_bibliography` 靠 bibkey→title）、`test_planning_console_wiring.py`
    （`merge_refs` 的 P0#18 语义）、provider 层对 `fetch_metadata` 返回 None 的既有断言
  - 隐私测试用 >500 字文本或结构性断言（短文本会整段进 SafeText 的 preview）
  - 清理过程中产生的临时文件
  - _Requirements: 19.6, 19.8, 19.9_
  - _Design: D9b, D10, D11 末段, Property 30_

## Notes

- 每完成一个任务停下来：讲清改了什么、跑全套测试、等确认再继续。
- 跑全套：后台启动
  `python -m pytest tests/ -q --no-header --tb=short 2>&1 | Select-Object -Last 25`，
  轮询到出现 "passed"。**复用终端会拿到旧输出，必须先 stop 再开新的。**
- Wave 1–4（任务 1–8）不碰既有代码路径，每个任务完成后都应确认 476 仍绿——
  这样进 wave 5 出问题时能立刻定位到是哪一步引入的。
- 两条红线（任务 13）：`bibkeys()`/`ref_ids()` 保持 resolvable 语义；
  `SessionFacts(known_refs=...)` 四处调用必须继续可用。
- git 提交由用户自己做，不主动 commit / push。

## 已知简化（A1 有意不做，留档）

1. **按字段的来源策略**：`PROVENANCE_RANK` 是全局层级。DOI 该信 registry，
   但摘要、卷期页往往 publisher 更完整。留到多引文格式那一期，
   `field_provenance`（任务 4/6）已把数据攒好。
2. **多标识符交叉核验**：A1 只单次回查以保住「网络调用次数不变」（任务 11）。
   拿 DOI 与 arXiv id 互相印证属 A2。
3. **`missing_fields(style)` 完整字段表**：A1 只留接口与 `reference_type` 骨架，
   四种引文格式的必需字段属最后一期。
4. **`literature_require_final_verification` 默认 False**：A2 接入 Crossref 后切 True。
5. **`RetrievalCandidate` 转正名**：A1 先做类型别名，A2 起改为主名、
   `ReferenceEntry` 转过渡别名。
6. **无 `unmerge` 能力**：故低置信一律不自动合并（任务 6/8）。若将来要支持人工确认
   合并，需先实现 unmerge。
7. **Correctness Property 编号**：requirements 里是历次增补叠出来的（1–30 后接 31–39），
   语义稳定但数字不表示优先级。若后续再大改，建议换稳定名称
   （如 `P.DEDUP_WORK_ONLY`），A1 不动。
