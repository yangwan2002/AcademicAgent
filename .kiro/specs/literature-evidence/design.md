# Design Document — A1：文献模型、核验事件、去重与迁移

## Overview

A1 是 spec A 的**数据层地基**，零 LLM 调用、零用户交互变化、网络调用次数不变。目标：把
「文献」从两个各自为政的贫血结构（`ReferenceEntry` 检索用、`KnownRef` 持久化用）
收敛成**一个有身份、有核验审计、可去重、可迁移**的持久化模型。

A1 交付：
1. `reference_id` 稳定主键 + 按格式分类的引用别名（tex bibkey / md 数字别名）
2. `work_identifiers` / `container_identifiers` 分离（修 ISSN/ISBN 误合并），
   **含检索候选 DTO 的多标识符扩展**
3. 只追加的 `VerificationEvent` 序列 + `VerificationStatus` 折叠派生
4. `verify_detailed()`：把 `NOT_FOUND` 与 `PROVIDER_ERROR` 的语义从 provider 一路保到记录
5. 标识符级去重与合并（含字段级来源与冲突裁决）；低置信只出建议、不自动合并
6. `ReferenceLedger` 与 project facts 的 schema v2 持久化（原子写回）
7. 旧 `known_refs` 无损迁移（含混合 schema 合并）+ `legacy_verified` 标记

A1 **不做**：Evidence_Span 生成、Language_Verdict 判定、Relevance、Retrieval_Batch、
Retrieval_Loop、LiteraturePreflight、CitationQuotaGate、引文格式渲染、
`search_literature` 意图、Crossref / web_search 的 provider 实现。

**验收铁律**：既有 476 个测试全绿，且 `.tex` / `.md` 写作链路行为逐字节不变。

## 关键设计决策

### D0. 候选 DTO 与持久化实体分离（已与 Requirements 对齐）

Req 1.1 / 19.1 已改为「`ReferenceRecord` 是唯一可写、唯一持久化的文献实体，
`RetrievalCandidate` 为非持久化 DTO，`KnownRef` 为只读兼容投影」。约束不是**类型数量**，
而是**只有一个可写真相源**。

| 角色 | 类型 | 位置 | 谁产生 |
|---|---|---|---|
| 检索候选（无身份，非持久化） | `RetrievalCandidate`（= 现 `ReferenceEntry`，保留原名原位并扩字段，见 D12） | `workspace/models.py` | 4 个 retrieval provider |
| 持久化实体（唯一可写） | `ReferenceRecord`（新增，**不可变**，见 D7） | `literature/models.py` | 仅 `ReferenceLedger` |
| 只读兼容投影 | `KnownRef` | `session/facts.py` | 从 Ledger 派生，A2 移除 |

候选之所以不能与记录同类：候选**尚无身份**（无 `reference_id`、无核验事件、无引用别名），
把这些字段塞进同一个类会让每个 provider 返回一堆空值，且极易误把候选当已入库记录用。

实现上 `RetrievalCandidate` 先做成 `ReferenceEntry` 的类型别名（四个 provider 零改名），
A2 起改为主名、`ReferenceEntry` 转为过渡别名。**唯一可写真相源**由
「只有 `Ledger.upsert()` / `replace()` 能产出 `ReferenceRecord`」+ 记录 frozen（D7）
共同保证，见 Property 34。

### D1. `reference_id` 用内容派生哈希，一次分配永不重算

否决的方案：递增整数（跨会话冲突，且与 md 的 `[1]` 混淆）；uuid4（不可复现，
迁移与测试无法断言）；每次由当前 `canonical_identifier` 计算（合并后主键漂移，
违反 Property 2）。

采用：**首次入库时**按优先级取种子，算 `"r" + sha1(seed).hexdigest()[:12]`，
写入后**永不重算**：

```
seed = f"{type}:{normalized_value}"              # 首个可得 work identifier，序：doi > arxiv > pmid > openalex_work > semantic_scholar_paper
     | f"title:{normalize_title(title)}|{year}"  # 无 work identifier
     | f"legacy:{normalize_title(title)}|{numeric_alias}"  # 迁移旧 KnownRef
```

同一 DOI 在任何会话、任何机器上算出同一 id → 跨会话合并天然幂等，迁移可复现。
哈希碰撞追加 `-2` 后缀。合并时保留 `created_at` 较早者的 id（相同则字典序小者），
另一个进 `merged_from`，使旧 id 仍可解析。

### D2. 引用别名按文档格式分类；数字别名对新文献同样分配

现有代码里 `KnownRef.id` **不只是**旧数据标识：`context_builder._fmt_refs` 是
`token = r.bibkey if ext == "tex" else r.id`，`RewriteGate` / `AddSectionGate` 在
非 tex 时用 `facts.ref_ids()`，而 `search_and_verify` 给**新检索**文献分配的正是
`id=facts.next_ref_id()`。所以若数字别名只给迁移记录，`.md` 写作链路会直接断，
且既有测试会红。

```python
bibkey: str                          # 当前 tex 引用键
bibkey_aliases: tuple[str, ...]      # 历史 bibkey（合并/重分配产生），仍可解析
numeric_aliases: tuple[str, ...]     # md/docx 的 [n]；首个为当前主别名
```

- 新建记录（含检索新入库）→ `ledger.next_numeric_alias()` 分配一个
- 迁移旧 `KnownRef.id` → 进 `numeric_aliases`
- `to_known_ref()` 取 `numeric_aliases[0]` 作 `KnownRef.id`

字段名不用 `citation_aliases`：`ReferenceEntry` 已有同名字段（导入源文档的引用标记），
避免语义撞车。

### D3. 核验状态由**只追加事件流**折叠派生

原设计里 `VerificationRecord` 是 frozen dataclass 却带 `resolved: bool` 并说
「未来改成 True」——frozen 改不了，自相矛盾。`quarantined: bool` 同样是可变状态且
无审计来源。改为全事件化：

```python
class EventKind(str, Enum):
    VERIFIED / CONFLICT / NOT_FOUND / PROVIDER_ERROR      # 核验尝试
    CONFLICT_RESOLVED / QUARANTINED / RELEASED            # 状态变迁

@dataclass(frozen=True)
class VerificationEvent:
    event_id: str                    # sha1(kind|source|identifier|checked_at|seq)[:12]
    kind: EventKind
    source: str = ""
    provenance: str = "aggregator"   # D11
    authority: str = "corroboration" # final | corroboration —— 按标识符类型判定，D11
    identifier_type: str = ""
    identifier: str = ""
    checked_at: str = ""             # ISO8601
    target_event_id: str = ""        # 变迁事件指向被作用的事件
    details: str = ""
```

`aggregate_status(events)` **按因果引用折叠，不看时间戳先后**：

```
1. 校验变迁事件的 target：
     CONFLICT_RESOLVED.target 必须是一个存在的 CONFLICT 事件
     RELEASED.target          必须是一个存在的 QUARANTINED 事件
   不满足 → 该变迁事件无效（忽略并计入 invalid_transitions 供报告）
2. resolved_ids   = {有效 CONFLICT_RESOLVED 的 target}
   released_ids   = {有效 RELEASED 的 target}
3. QUARANTINED  if 存在 QUARANTINED 事件且其 event_id ∉ released_ids
   CONFLICT      elif 存在 final CONFLICT 事件且其 event_id ∉ resolved_ids
   VERIFIED      elif 存在 final VERIFIED 事件
   UNVERIFIED    else
```

**为什么不按时序**：原设计对隔离状态用「QUARANTINED / RELEASED 按时序后者胜」，
这在跨源合并时会碎：两个源的事件时间戳可能相同或乱序，一条来源不明的 RELEASED
就能悄悄解除隔离。改为因果引用后，「解除」必须明确指向它解除的那一次隔离，
折叠结果与时间戳完全无关（Property 37）。同一次隔离被多条 RELEASED 指向是幂等的。

`final` 指该事件由**对这个标识符类型具备最终核验权限**的源产生（见 D11）。

由此**免费**得到五个不变量，无需额外逻辑：

- Req 2.10「新冲突必须降级」：CONFLICT 分支先于 VERIFIED 求值，追加冲突事件即降级。
  代码里写不出「因 verified 更强而保留」的路径。
- Req 2.6「provider_error 不是文献状态」：`PROVIDER_ERROR` 不出现在任何分支，
  只有它的事件集必落 `UNVERIFIED`（Req 2.8）。
- Req 3.6「合并后重新派生」：合并只需并集事件，状态自动正确。
- Req 2.2「变迁可审计」：每次消解/隔离都是一条带 `target_event_id` 的事件，
  能回答「谁在什么时候解除了这个冲突」。
- Req 11.26「仅有佐证不算核验」：`authority="corroboration"` 的事件不进入 VERIFIED
  分支，所以 aggregator 单独回查到 DOI 无法把状态推到 VERIFIED（D11、Property 35）。

`ReferenceRecord` **不存** `verification_status`、不存 `resolved`、不存 `quarantined`。

### D4. `verify_detailed()`：把失败语义从 provider 一路保到事件

现状让 `NOT_FOUND` / `PROVIDER_ERROR` 的区分**不可能**建立，三层都在吞语义：

| 位置 | 现有行为 | 后果 |
|---|---|---|
| `CitationVerifier.verify()` | `except RetrievalError: return False` | 网络失败与不存在同为 False |
| `OpenAlexRetrievalProvider.fetch_metadata` | `except Exception: return None` | 吞掉一切异常 |
| `SemanticScholar...fetch_metadata` | `except RetrievalError: return None` | 同上 |
| `MultiSourceProvider.fetch_metadata` | 单源失败 `continue`，全失败 `return None` | 全源宕机 = 查无此文 |

两层修改，均**不增加网络调用**：

**(a) HTTP 层引入分层异常**（这一步不做，上层拿不到 404）：

现状 `get_with_retry` 里 404 走 `resp.raise_for_status()` → 抛 `httpx.HTTPStatusError`
→ 被 `except Exception` 捕获（名字不含 Timeout/Connect/Read）→
`raise RetrievalError(str(exc)) from exc`。抛出的 `RetrievalError` **自身没有
`.response` 属性**，所以任何 `getattr(exc, "response", ...)` 的写法恒得 `None`，
真正的「查无此文」会 100% 被判成 `PROVIDER_ERROR`。原始异常只在 `__cause__` 里，
靠它判断过于脆弱。

```python
# providers/retrieval/base.py
class RetrievalError(Exception): ...            # 保留，作为基类
class RetrievalNotFound(RetrievalError): ...    # 目标标识符不存在（HTTP 404 / 空结果）
class RetrievalUnavailable(RetrievalError): ... # 超时 / 限流 / 连接失败 / 重试耗尽
```

`get_with_retry` 分流：`404` → `RetrievalNotFound`；`429` / `5xx` / 超时 / 连接错误
（重试耗尽后）→ `RetrievalUnavailable`；其余 4xx → `RetrievalUnavailable`
（配置或权限问题，不是「不存在」）。因为二者都继承 `RetrievalError`，
所有既有 `except RetrievalError` 的调用点行为不变。

**(b) provider 契约明确化**（`base.py` docstring + 三个实现）：

```
HTTP 404          → RetrievalNotFound → fetch_metadata 返回 None → NOT_FOUND 事件
超时/限流/连接失败 → RetrievalUnavailable → 向上抛              → PROVIDER_ERROR 事件
```

即 `fetch_metadata` 返回 `None` **仅**表示查无此文；调用失败一律抛
`RetrievalUnavailable`。OpenAlex 的 `except Exception: return None` 与 S2 的
`except RetrievalError: return None` 都改为只吞 `RetrievalNotFound`。
`MultiSourceProvider.fetch_metadata` 改为：全部源都返回 None → None；
存在源抛 `RetrievalUnavailable` 且无源命中 → 抛 `RetrievalUnavailable`
（区分「都说没有」与「都没问到」）。

**测试要求**：必须直接测 `get_with_retry`（用 monkeypatch 伪造 httpx 响应/异常），
断言 404 → `RetrievalNotFound`、超时 → `RetrievalUnavailable`。
只用 stub provider 测不到这一层，而这一层正是原缺陷所在。

**(c) `CitationVerifier` 增量扩接口**，旧方法基于新方法实现，行为不变：

```python
@dataclass(frozen=True)
class VerificationAttempt:
    outcome: EventKind          # VERIFIED | CONFLICT | NOT_FOUND | PROVIDER_ERROR
    matched: ReferenceEntry | None = None
    title_score: float = 0.0
    year_matches: bool | None = None
    error_kind: str = ""        # timeout | http | parse | unavailable
    note: str = ""

def verify_detailed(self, entry) -> VerificationAttempt      # 新增
def verify(self, entry) -> bool:                              # 保持原语义
    return self.verify_detailed(entry).outcome == EventKind.VERIFIED
```

`verify_and_mark` 继续基于 `verify()` → **既有调用方与测试零改动**。

`ReferenceVerifier` 只把 `VerificationAttempt` 翻译成 `VerificationEvent`。
**A1 只做一次回查**：沿用现有 `search_and_verify` 的单次核验调用，
按 `WORK_PRIORITY` 取**第一个**可用 work identifier 回查，
**不**逐个遍历多标识符——否则「网络调用次数不变」不成立。
多标识符交叉核验（拿 DOI 与 arXiv id 分别回查互相印证）属 A2。

**风险**：(a) / (b) 改变了 HTTP 层与 provider 的异常行为。因新异常均继承
`RetrievalError`，既有 `except RetrievalError` 调用点不受影响；但若有测试断言
「网络失败时 `fetch_metadata` 返回 None」，需在 tasks 里单独处理并说明。

### D5. 去重只用 work identifiers；低置信只出建议，默认不合并

```python
WORK_TYPES      = {"doi", "arxiv", "openalex_work", "semantic_scholar_paper", "pmid"}
CONTAINER_TYPES = {"issn", "eissn", "isbn"}
```

`CONTAINER_TYPES` 在 `dedup.py` 里**不出现**——`dedup_keys()` 只遍历 `WORK_TYPES`，
从结构上不可能用 ISSN 去重（不是运行时判断跳过）。

两级匹配，但**只有高置信会合并**，低置信一律不合并：

| 级别 | 条件 | 处置 | `upsert` 返回 |
|---|---|---|---|
| high | 任一 work identifier 归一化同值 | 合并进已有记录 | `MERGED` / `UNCHANGED` |
| low | 归一化标题 + 年份 + 首作者姓氏 三者全同 | **仍创建独立记录** + 记 `DedupSuggestion` | `CREATED_WITH_SUGGESTION` |
| 无 | 都不命中 | 创建独立记录 | `CREATED` |

原设计返回 `SUGGESTED` 是有歧义的——说不清候选到底有没有入库。正确语义是
**低置信命中不阻止入库**：候选照常成为独立 `ReferenceRecord`，同时留一条建议记录。
所以枚举为 `CREATED | CREATED_WITH_SUGGESTION | MERGED | UNCHANGED`。

原设计还把低置信自动合并默认打开并声称「可复核可撤销」，这是**过度承诺**：合并后字段
已按优先级覆盖、事件已并集，仅靠摘要式 snapshot 无法还原，而系统并没有 unmerge 实现。
且中文姓名、团体作者（「中国科学院…」）、常见标题（「基于深度学习的图像分类研究」）
都容易三项全同。既然 Req 3.5 已禁止低置信自动合并，就**不该再留自动合并开关**：

```python
literature_dedup_suggestions_enabled: bool = True   # 是否产出去重建议（不影响是否合并）
```

这同时更符合「A1 默认行为不变」：现有 `add_ref` 只按 bibkey / id 去重，**从来没有**
标题模糊去重。

`DedupSuggestion{left_id, right_id, basis, confidence, evidence}` 由 Ledger 累积，
A1 只呈现与持久化，处置流程属 A2。

### D6. 字段级来源：`field_provenance`

Req 11.19 要求按 `Metadata_Provenance` 裁决字段冲突，但整条记录只有一个
`discovery_source` 时无法回答「`year` 这个值是谁给的」——多源合并后裁决无从下手。

```python
field_provenance: tuple[tuple[str, str], ...]   # (field_name, source_name)
```

- 创建记录时：所有非空字段登记为该候选的 `source`
- 合并时：某字段被 incoming 取代 → 更新该字段的 source；未取代 → 保持
- 裁决时：比较 `PROVENANCE_RANK[profile(existing_field_source).provenance]` 与
  incoming 的层级（D11）

存 tuple of pairs 而非 dict：配合 D13 的不可变要求，且字段数有限、查找开销可忽略。
`field_provenance_map()` 返回副本供只读使用。

### D7. `ReferenceRecord` 不可变，Ledger 是唯一修改入口

原设计用可变 dataclass + `dict` / `list` 字段，且 `Ledger.get()` / `all_records()`
直接返回记录引用。调用方改一下 `work_identifiers` 或 `bibkey`，Ledger 的
work-key / bibkey / numeric-alias 三套索引就静默失效——数据层地基不能有这个洞。

```python
@dataclass(frozen=True)
class ReferenceRecord:
    work_identifiers: tuple[tuple[str, str], ...] = ()      # 有序 pair，可哈希
    container_identifiers: tuple[tuple[str, str], ...] = ()
    authors: tuple[str, ...] = ()
    verification_events: tuple[VerificationEvent, ...] = ()
    ...
    def work_id_map(self) -> dict[str, str]      # 返回副本
    def with_changes(self, **kw) -> ReferenceRecord   # dataclasses.replace 包装
```

- 全字段 immutable：`list` → `tuple`，`dict` → `tuple[tuple[str, str], ...]`
- `Ledger.get()` / `all_records()` 返回不可变对象 / `tuple`，天然安全
- 修改只经 `Ledger.upsert()` / `replace()`，二者内部**必定重建索引**
- 不用 `MappingProxyType` 作字段类型：frozen dataclass 需可哈希且能 JSON 往返，
  tuple-of-pairs 两者都满足

`aggregate_status` 是纯函数 + 记录不可变 → `verification_status` 这个 property
可以安全地做 `functools.cached_property`（A1 先不加，避免过早优化）。

### D8. `SessionFacts` 需要自定义 `__init__`，property 不够

`facts.known_refs` 有五处消费方（`context_builder._fmt_refs`、`RewriteGate`、
`AddSectionGate`、`fix_bibliography`、`extractor`）。更关键的是**四处**代码用关键字参数
构造：`SessionFacts(path=..., known_refs=[KnownRef(...)])`
（`test_week3_rewrite` 两处、`test_week4_add_section` 两处）。

只把字段换成 property + setter **不够**——`@dataclass` 生成的 `__init__` 不再接受
`known_refs` 参数，这四处会直接 `TypeError`，476 全绿不成立。

采用**自定义 `__init__`**（不再用 dataclass 自动生成）：

```python
class SessionFacts:
    def __init__(
        self,
        path: str,
        known_refs: list[KnownRef] | None = None,   # 过渡入参，A2 移除
        known_numbers: list[KnownNumber] | None = None,
        claims: dict | None = None,
        *,
        ledger: ReferenceLedger | None = None,
    ) -> None:
        self.path = path
        self.ledger = ledger if ledger is not None else ReferenceLedger()
        if known_refs:
            self.ledger.ingest_known_refs(known_refs)   # 走迁移路径，含 legacy 标记
        ...

    @property
    def known_refs(self) -> list[KnownRef]:      # 只读投影，每次派生
        return [r.to_known_ref() for r in self.ledger.all_records()]

    @known_refs.setter
    def known_refs(self, scanned):               # 过渡 API，A2 移除
        self.replace_scanned_refs(scanned)       # merge 语义，不覆盖已核验
```

保留**位置参数顺序**（`path, known_refs, known_numbers, claims`）与原 dataclass 一致，
使任何位置参数调用也不破。`ledger` 作 keyword-only 新入参。
setter 走 merge 语义，与 P0#18 的修复一致（`facts.known_refs = extract_refs(...)` 那处
依赖它）。`bibkeys()` / `ref_ids()` / `add_ref()` / `merge_refs()` / `next_ref_id()`
全部保留签名，内部委托 Ledger。

代价是失去 dataclass 的 `__eq__` / `__repr__`。已确认无生产代码或测试比较
`SessionFacts` 相等性；仍按需手写 `__repr__` 便于调试。

### D9. 两个引用集合：原有未核验引用不能变成非法

原设计把 `facts.bibkeys()` 直接改成 `ledger.citable_bibkeys()`（只含 VERIFIED），
这会**改变行为**：现状 `bibkeys()` 返回**全部** bibkey，不过滤 `verified`，
而 `extractor.extract_refs()` 从用户原稿扫出的引用一律 `verified=False`。
一改，用户原稿里本来就有的引用在 `RewriteGate` 里立刻变成「引用了未核验的文献」，
既违反「A1 行为逐字节不变」，也违反 Req 4.7「保留用户原有引用」。

两个集合分开（Req 4.2）：

```python
# Resolvable：全部可解析引用标记，含用户原有未核验引用
def resolvable_bibkeys(self) -> set[str]
def resolvable_numeric_aliases(self) -> set[str]

# Generatable：Agent 新增引用时允许用的
def citable_bibkeys(self, *, allow_legacy: bool = True) -> set[str]
def citable_numeric_aliases(self, *, allow_legacy: bool = True) -> set[str]
```

A1 的接线策略：

- `facts.bibkeys()` / `facts.ref_ids()` **保持旧语义**（= resolvable），
  于是 `RewriteGate` / `AddSectionGate` 行为逐字节不变，476 不受影响
- 新增 `citable_*` 接口，A1 只建不用
- A3 再让 Gate 做「原文 vs 候选」的差集比较：原有未核验引用允许原样保留，
  **新增**引用必须属 Generatable（Req 4.5、Property 36）

`legacy_verified` 与 `verification_status` **正交**：

```python
def citable_ids(self, *, allow_legacy: bool = True) -> set[str]
# True （兼容，默认）：VERIFIED ∪ legacy_verified
# False（严格）      ：仅 VERIFIED
```

严格模式由 `literature_strict_verification`（默认 False）开启。Property 29「不虚高」
仍成立：legacy 记录的 `verification_status` 确实是 `UNVERIFIED`，放宽的只是
**可引用性**这个独立判定，报告里标注核验方式为 legacy。
`legacy_verified` 是创建时决定、之后不变的迁移事实，不是状态变迁，
作为 frozen 字段与 D3 的事件化不冲突。

### D9b. 配置项：默认值必须让行为不变

沿用 `config.py` 现有 `_env_flag` / `_env_float` 模式（非法值降级为默认）：

```python
literature_strict_verification: bool = False      # PAPER_LIT_STRICT_VERIFY
literature_title_similarity: float = 0.85         # PAPER_LIT_TITLE_SIM
literature_dedup_suggestions_enabled: bool = True # PAPER_LIT_DEDUP_SUGGEST（不影响是否合并）
literature_require_final_verification: bool = False  # PAPER_LIT_REQUIRE_FINAL_VERIFY
literature_source_priority: str = "openalex,semantic_scholar,arxiv"  # PAPER_LIT_SOURCE_PRIORITY
```

- 没有 `literature_dedup_low_confidence` 这个开关：Req 3.5 已禁止低置信自动合并，
  再留一个「自动合并开关」等于给禁止项开后门。只保留「是否产出建议」。
- `literature_require_final_verification` 默认 `False` 的理由见 D11 末段
  （A1 未接 Crossref，立刻要求 final 会静默改变真机入库结果）。
- `literature_source_priority` 语义收窄为「**同 provenance 层级内**的排序」，
  跨层级由 `PROVENANCE_RANK` 决定，配置无法把 `web` 提到 `registry` 之上（D11）。

**关于字段级来源策略的已知简化**：`PROVENANCE_RANK` 目前是全局层级，但
「registry 一律优先」对所有字段并不都合理——DOI 该信 registry，而摘要、卷期页往往
publisher 更完整。A1 先用全局层级（够用且可测），按字段的来源策略
（`FIELD_PROVENANCE_POLICY: field → 层级序`）留到多引文格式那一期，
届时 `field_provenance`（D6）已把所需数据攒好。

### D10. 迁移：混合 schema 要合并，写回要原子

facts.json 加 `schema_version`。加载分流：

| 情形 | 处理 |
|---|---|
| 无 `schema_version`，有 `known_refs` | v1，全量迁移 |
| `schema_version == 2`，只有 `references` | 直接读 |
| **同时有 `references` 与 `known_refs`** | **合并**：先读 references，再把 `known_refs` 中按 bibkey / 数字别名**无对应**的条目走迁移路径补入 |

第三种是部分迁移状态（如迁移写回中断、或用户手工编辑）。原设计「以 references 为准、
忽略 known_refs」会丢掉 known_refs 独有的条目，违反无损原则。

迁移规则（每条旧 `KnownRef` → 恰好一条 `ReferenceRecord`，**1:1 不合并不丢弃**，
即使 title 与 bibkey 皆空）：

| 旧字段 | 新位置 |
|---|---|
| `id` | `numeric_aliases`（供 md 的 `[1]`，D2） |
| `bibkey` | `bibkey` |
| `title` | `title`，`field_provenance[title]="legacy"` |
| `verified=True` | `legacy_verified=True` + `verification_method="legacy"` + 一条 `VerificationEvent(kind=VERIFIED, source="legacy", authoritative=False)` |
| `verified=False` | 无事件 |

`authoritative=False` 使该事件**不产生** `VERIFIED` 聚合状态（迁移不虚高，Property 29）；
可引用性由 D9 的 legacy 通道保证。而 `"legacy"` 在 `SOURCE_PROFILES` 里 `roles` 为空集
（D11），所以 `authoritative=False` 是**派生**的，不需要迁移代码特意设置。

**原子写回**（`facts.save()` 与迁移写回共用）：

```
1. 若目标存在且尚无备份 → 复制原始字节到 facts.v1.bak.json
2. 写 facts.json.tmp（同目录，保证同一文件系统）
3. flush() + os.fsync(fd)
4. os.replace(tmp, target)     # 同卷原子替换，Windows/POSIX 均可
5. 失败时清理 tmp，原文件保持完整
```

迁移的**计算**在 `load()` 内同步完成（纯内存，无网络）；**写回**发生在下一次
`save()`，因此只读会话不会改动用户文件。

### D11. 源角色与来源层级：`literature/sources.py`

```python
class ProviderRole(str, Enum): SEARCH / VERIFY / DISCOVER

PROVENANCE_RANK = {"registry": 3, "publisher": 2, "aggregator": 1, "web": 0}

@dataclass(frozen=True)
class SourceProfile:
    name: str
    roles: frozenset[ProviderRole]
    provenance: str
    final_identifier_types: frozenset[str]         # 可最终核验（native + registry）
    corroboration_identifier_types: frozenset[str] # 只能佐证
```

**核验权限必须细化到标识符类型**（否则 OpenAlex 只要回查到 DOI 就能单独把状态推成
VERIFIED，与 Req 11.17「不得仅以 aggregator 作 DOI 最终裁决」正面冲突）：

| 源 | roles | provenance | 可最终核验 | 仅佐证 |
|---|---|---|---|---|
| `crossref` | VERIFY | registry | `doi` | — |
| `pubmed` | SEARCH, VERIFY | registry | `pmid`, `doi` | — |
| `arxiv` | SEARCH, VERIFY | publisher | `arxiv` | `doi` |
| `openalex` | SEARCH, VERIFY | aggregator | `openalex_work` | `doi`, `arxiv`, `pmid` |
| `semantic_scholar` | SEARCH, VERIFY | aggregator | `semantic_scholar_paper` | `doi`, `arxiv`, `pmid` |
| `web_search` | DISCOVER | web | — | — |
| `legacy` | — | web | — | — |
| `mock` | SEARCH, VERIFY | aggregator | 全部（测试用） | — |

```python
def verification_authority(source: str, id_type: str) -> str
    # "final" | "corroboration" | "none"
def resolve_field_conflict(a_source, b_source, priority) -> str   # 层级 → 同级配置序
```

四个结构性后果：

- `authority` 由 `(source, identifier_type)` 一对决定，不再是单一布尔 → aggregator
  回查 DOI 只产生佐证事件，`aggregate_status` 的 VERIFIED 分支只看 `final` 事件，
  Req 11.17 从**结构上**成立而非靠调用点自觉（Property 35）。
- `legacy` / `web_search` 的 `final_identifier_types` 为空 → D10 的迁移与 A2 的兜底
  都不需特殊处理，其事件天然是非 final。
- `web` 的 rank 为 0 且 `resolve_field_conflict` 恒不选它 → Req 11.21 结构性成立。
- **A1 的实际影响**：唯一已接入的真实源里，OpenAlex 只能最终核验 `openalex_work`。
  这意味着 A1 阶段（未接 Crossref）用 DOI 核验只会得到佐证事件。为避免这直接改变
  现有 `search_and_verify` 的入库结果，`mock` 保留全类型 final，且
  `literature_require_final_verification` 默认 `False`（见 D9）——默认下佐证事件
  仍按 `final` 记入，与现状一致；A2 接入 Crossref 后再切 `True`。
  这一点必须在 tasks 里显式验证，否则会静默改变真机行为。

`crossref` / `web_search` 在 A1 只有 profile 声明、无 provider 实现；A2 接入时
不需改动 A1 的任何数据结构或裁决逻辑。

### D12. 候选 DTO 扩多标识符（不能推迟到 A2）

现有 provider 把多标识符压成一个 `source_id`，A1 的「多标识符去重」因此拿不到数据：

| provider | 现状 | 丢失的 |
|---|---|---|
| OpenAlex | `source_id = doi or openalex_id` | 有 DOI 时丢 OpenAlex Work ID |
| Semantic Scholar | `source_id = ext["DOI"] or paperId` | 丢 paperId / ArXiv / PMID |
| arXiv | `source_id = arxiv_id` | 无 DOI 抓取 |

`ReferenceEntry` 增两个默认空字段（向后兼容，`vars(entry).copy()` 那类反射用法不受影响）：

```python
work_identifiers: dict[str, str] = field(default_factory=dict)
container_identifiers: dict[str, str] = field(default_factory=dict)
```

四个 provider 的映射机械更新，**不增加网络调用**（数据本来就在响应里）：

- OpenAlex：`doi` + `openalex_work`；`primary_location.source.issn_l` → `issn`
- S2：`paperId` → `semantic_scholar_paper`（**用 paperId，不用 CorpusId 代替**，
  二者是不同标识符体系）；`externalIds` 的 `DOI` / `ArXiv` / `PubMed`；
  `fields` 参数已含 `externalIds`，无需改请求
- arXiv：`arxiv`（+ 若 atom 含 `doi` 则一并取）
- mock：按样本填 `arxiv`

`source_id` **保留不动**（`CitationVerifier` / `MultiSourceProvider._route_order` /
`facts` 都在用）。入库时 `work_identifiers` 为空则从 `source_id` 经
`identifiers.classify()` 反推；**归类失败的串进 `unclassified_identifiers`**，
绝不塞进数字别名或任何参与去重的字段（否则一个陌生 DOI 串会变成 md 的 `[10.1145/...]`）。

## Architecture

```text
                        ┌──────────────────────────────┐
retrieval provider ───▶ │ ReferenceEntry (= Candidate) │  候选，无身份
(openalex/arxiv/s2/mock)│ + work/container_identifiers │  D12
                        └───────────┬──────────────────┘
                                    │
                     ┌──────────────▼───────────────┐
                     │ CitationVerifier             │
                     │  .verify_detailed()          │  D4：保住失败语义
                     │  → VerificationAttempt       │
                     └──────────────┬───────────────┘
                                    │
                     ┌──────────────▼───────────────┐
                     │ ReferenceVerifier            │  只遍历 work_identifiers
                     │  → VerificationEvent         │  D3
                     └──────────────┬───────────────┘
                                    │
                     ┌──────────────▼───────────────┐
                     │ ReferenceLedger.upsert()     │  唯一修改入口，必重建索引
                     │  1. dedup.find_duplicate     │  只看 work_identifiers
                     │  2. high→merge / low→suggest │  D5
                     │  3. bibkey + numeric alias   │  D2
                     │  4. reference_id（一次）       │  D1
                     │  5. field_provenance 更新     │  D6
                     └──────────────┬───────────────┘
                                    │
              ┌─────────────────────▼──────────────────────┐
              │ ReferenceRecord（frozen，唯一持久化模型）      │
              │  verification_status = 事件流折叠（不存字段）  │
              └─────────────────────┬──────────────────────┘
                                    │
        ┌───────────────────────────┼────────────────────────────┐
   facts.json v2              KnownRef 只读投影            citable_ids()
   （备份+tmp+原子替换）      (context_builder /            (RewriteGate /
                              fix_bibliography)             AddSectionGate)
```

## Components and Interfaces

### 1. `literature/identifiers.py`

```python
WORK_TYPES / CONTAINER_TYPES: frozenset[str]
WORK_PRIORITY: tuple[str, ...]      # doi > arxiv > pmid > openalex_work > s2

def normalize(id_type: str, raw: str) -> str
def classify(raw: str) -> str | None     # 10.x→doi、YYMM.NNNNN→arxiv、W…→openalex_work
def work_keys(pairs) -> set[tuple[str, str]]      # 只含 WORK_TYPES
```

`classify` 是兼容旧 `source_id`（裸串）与迁移的关键。
**幂等性**：`normalize(t, normalize(t, x)) == normalize(t, x)`，作属性测试。

### 2. `literature/sources.py`
见 D11。

### 3. `literature/verification.py`

```python
class EventKind(str, Enum): ...          # D3，7 个取值
@dataclass(frozen=True)
class VerificationEvent: ...             # D3
def aggregate_status(events) -> VerificationStatus       # 纯函数，单遍折叠
def make_event(kind, *, source, ...) -> VerificationEvent  # 自动算 event_id
```

### 4. `literature/models.py`

```python
@dataclass(frozen=True)
class ReferenceRecord:
    reference_id: str
    work_identifiers: tuple[tuple[str, str], ...] = ()
    container_identifiers: tuple[tuple[str, str], ...] = ()
    unclassified_identifiers: tuple[str, ...] = ()      # 无法归类的标识符串，不参与去重
    title: str = ""; authors: tuple[str, ...] = (); year: int | None = None
    publication_date: str = ""
    abstract: str = ""                                  # hash 为派生 property，不存字段
    language_hint: str = ""                             # provider 原始值，判定属 A2
    reference_type: str = ""
    # 著录字段（Req 1.7）——A1 只落库，渲染属最后一期
    container_title / volume / issue / pages / article_number / publisher /
    publisher_place / edition / url / accessed_at / conference_name / conference_place: str
    # 身份与别名（D2）
    bibkey: str = ""; bibkey_aliases: tuple[str, ...] = ()
    numeric_aliases: tuple[str, ...] = (); merged_from: tuple[str, ...] = ()
    # 核验（D3）
    verification_events: tuple[VerificationEvent, ...] = ()
    legacy_verified: bool = False; verification_method: str = ""
    # 溯源（D6）
    discovery_sources: tuple[str, ...] = ()            # 可多个：多源合并不丢来源
    created_at: str = ""
    field_provenance: tuple[tuple[str, str], ...] = ()
    field_conflicts: tuple[FieldConflict, ...] = ()

    @property verification_status -> VerificationStatus    # D3，事件流折叠
    @property canonical_identifier -> tuple[str,str]|None  # 只从 work_identifiers 取
    @property abstract_content_hash -> str                 # 由 abstract 实时算，非存储字段
    @property evidence_available -> bool
    def missing_fields(style: str) -> tuple[str, ...]      # 见下
    def work_id_map() / field_provenance_map() -> dict     # 副本
    def to_known_ref() -> KnownRef                          # 只读投影
    def with_changes(**kw) -> ReferenceRecord
```

两处从「字段/无参 property」改为「派生/带参」的理由：

- `abstract_content_hash` 是 `abstract` 的纯派生值。存成字段就是双真相源——改了摘要忘更新
  哈希，A3 的 Evidence_Span 失效检测会漏判。改成 property 实时算（A1 数据量小，
  必要时再加 `cached_property`）。
- `bibliographic_complete` **不可能**是无参 property：字段是否齐全取决于目标引文格式
  与 `reference_type`（GB/T 7714 的期刊论文与 APA 的网页所需字段完全不同）。改为
  `missing_fields(style)`，A1 只留接口与按 `reference_type` 的必需字段表骨架，
  完整实现属最后一期。`evidence_available` 与格式无关，保留无参 property。

### 5. `literature/dedup.py`

```python
@dataclass(frozen=True)
class DedupMatch:      reference_id: str; basis: str; confidence: str
@dataclass(frozen=True)
class DedupSuggestion: left_id: str; right_id: str; basis: str; evidence: str

def normalize_title(t) -> str            # NFC → casefold → 去标点 → 压空白
def first_author_key(authors) -> str     # 取姓氏并归一化
def find_duplicate(cand, ledger, *, allow_low) -> DedupMatch | None
def merge_records(existing, incoming_cand, events, *, priority) -> MergeResult
```

`merge_records` 规则：

| 内容 | 规则 |
|---|---|
| work / container identifiers | 并集；同类型冲突值按 provenance 裁决 + 记 `FieldConflict` |
| verification_events | 并集，按 `event_id` 去重，按 `checked_at` 排序 |
| verification_status | 不合并——由并集折叠派生（D3） |
| 标量非空字段 | existing 空 → 取 incoming 并记 provenance；两侧非空且不同 → 按 `PROVENANCE_RANK` 取胜（同级按配置序），更新 `field_provenance` + 记 `FieldConflict` |
| abstract | 同上；**绝不清空**已有摘要 |
| bibkey | 保留 existing；incoming 的进 `bibkey_aliases` |
| numeric_aliases | 并集，保留 existing 的首个为主别名 |
| reference_id | 保留 `created_at` 较早者；另一个进 `merged_from` |
| legacy_verified | 逻辑或 |

### 6. `literature/ledger.py`

```python
```python
class ResolutionStatus(str, Enum): RESOLVED / NOT_FOUND / AMBIGUOUS

@dataclass(frozen=True)
class ResolutionResult:
    status: ResolutionStatus
    reference_id: str = ""                    # 仅 RESOLVED 时有值
    candidates: tuple[str, ...] = ()          # 仅 AMBIGUOUS 时有值
    matched_via: str = ""                     # bibkey | bibkey_alias | numeric | merged_from

class ReferenceLedger:
    def get(rid) -> ReferenceRecord | None
    def resolve_token(token, *, ext) -> ResolutionResult
    def upsert(cand, events=()) -> UpsertResult
        # action: CREATED | CREATED_WITH_SUGGESTION | MERGED | UNCHANGED  （D5）
    def replace(record) -> None                       # 唯一的整条替换入口
    def ingest_known_refs(rows) -> int                # 迁移入口，D8/D10 共用
    def next_numeric_alias() -> str
    def resolvable_bibkeys() / resolvable_numeric_aliases() -> set[str]     # D9
    def citable_ids / citable_bibkeys / citable_numeric_aliases(*, allow_legacy=True)
    def all_records() -> tuple[ReferenceRecord, ...]
    def suggestions() -> tuple[DedupSuggestion, ...]
    def integrity_report() -> LedgerIntegrityReport   # 歧义别名、无效变迁事件
    def to_json() -> list[dict]  /  from_json(rows) -> ReferenceLedger
```

**`resolve_token` 必须能表达歧义**。原设计返回 `str | None` 无法区分「没找到」与
「命中多条」——而迁移遗留的重复 bibkey / 数字别名 / 被合并的旧 id 完全可能撞车。
返回 `str | None` 时代码只能任选一条或谎报找不到，两者都会让引用悄悄指错文献。
调用方（A3 的 CitationQuotaGate、bibliography 渲染）必须显式处理 `AMBIGUOUS`。

**五套索引**（原设计漏了 `merged_from`，导致「旧 id 仍可解析」这个承诺落空）：

| 索引 | 键 | 用途 |
|---|---|---|
| work key → rid | `(type, normalized_value)`，仅 `WORK_TYPES` | 去重 |
| bibkey → rid | 当前 bibkey | tex 引用解析 |
| bibkey_alias → rid | 历史 bibkey | 旧 tex 引用解析 |
| numeric alias → rid | md/docx 的 `[n]` | md 引用解析 |
| merged_from → rid | 被合并掉的旧 `reference_id` | 旧引用与旧 trace 解析 |

任一索引的某个键映射到多个 rid 时，该键记入歧义集合，`resolve_token` 对它返回
`AMBIGUOUS` 并列出候选；`integrity_report()` 汇总供用户处置。
`upsert` / `replace` 内部整体重建全部索引（A1 数据量小，正确优先）。

### 7. `session/facts.py` 改造
见 D8。`save()` 走 D10 的原子写；`load()` 走 D10 的三路分流。

### 8. `tools/literature.py` 改造（最小）
`search_and_verify` 内：`verifier.verify_detailed(cand)` → `ReferenceVerifier` 转事件
→ `facts.ledger.upsert(cand, events)`。`SearchOutcome` 字段与语义不变
（`added: list[KnownRef]` 仍为投影）→ `rewrite.py` / `add_section.py` 零改动。

## Data Models

模型定义见 Components 第 3/4 节。持久化形态（facts.json v2）：

```json
{
  "schema_version": 2,
  "references": [
    {
      "reference_id": "ra1b2c3d4e5f6",
      "work_identifiers": [["doi", "10.1145/3292500.3330701"], ["openalex_work", "W2741809807"]],
      "container_identifiers": [["issn", "15577317"]],
      "title": "...", "authors": ["..."], "year": 2019,
      "bibkey": "smith2019deep", "bibkey_aliases": [], "numeric_aliases": ["3"],
      "verification_events": [
        {"event_id": "e0f1a2b3c4d5", "kind": "verified", "source": "openalex",
         "provenance": "aggregator", "authoritative": true,
         "identifier_type": "doi", "identifier": "10.1145/...",
         "checked_at": "2026-07-27T10:00:00Z", "target_event_id": "", "details": ""}
      ],
      "field_provenance": [["title", "openalex"], ["year", "crossref"]],
      "legacy_verified": false, "verification_method": "",
      "created_at": "2026-07-27T10:00:00Z"
    }
  ],
  "dedup_suggestions": [...],
  "known_numbers": [...],
  "claims": {...}
}
```

`verification_status` **不在 JSON 里**（派生字段不持久化，避免与事件流形成双真相源）。
标识符存 pair 数组而非 object，与 D7 的 tuple-of-pairs 内存表示一一对应，且保序。

## Correctness Properties

A1 覆盖 requirements 30+ 条 Property 中的 10 条；其余属 A2/A3。

### Property 2: 主键稳定性与别名可解析
`reference_id` 一次分配永不重算；bibkey 重分配、合并、格式切换均不改变它；
被替换的 bibkey 进 `bibkey_aliases`、被合并的 id 进 `merged_from`、旧数字标识进
`numeric_aliases`，三者均可经 `resolve_token` 解析。新建记录（含检索新入库）
必分配数字别名，`.md` 引用链路不断。
**Validates: Requirements 1.2, 1.3, 1.7, 3.8, 3.9**

### Property 3: 去重只用作品级标识符
`dedup_keys()` 只遍历 `WORK_TYPES`，`CONTAINER_TYPES` 不出现在去重路径中（D5）——
结构上不可达。回归测试含 ISSN 反例。
**Validates: Requirements 1.4, 3.2, 3.3**

### Property 4: 标识符唯一性与合并完备
`upsert` 后不存在两条 work identifier 归一化同值的记录；`merge_records` 对 identifiers
与 `verification_events` 取并集，status 由并集折叠派生。
**Validates: Requirements 3.1, 3.2, 3.6**

### Property 5: 核验状态可追溯且不虚高
`aggregate_status` 只在存在 `authoritative` 且 `kind=VERIFIED` 的事件时返回 `VERIFIED`；
`PROVIDER_ERROR` 不出现在任何返回 `VERIFIED`/`CONFLICT` 的分支，仅含它的事件集恒得
`UNVERIFIED`（D3）；`NOT_FOUND` 与 `PROVIDER_ERROR` 经 `verify_detailed` 全程可区分
（D4）；回查只用 work identifiers。
**Validates: Requirements 2.1, 2.5, 2.6, 2.7, 2.8, 2.9**

### Property 6: 冲突必降级且变迁可审计
`verification_status` 是事件流折叠而非存储字段，`CONFLICT` 分支先于 `VERIFIED` 求值 →
追加冲突事件后不可能仍返回 `VERIFIED`。消解与隔离均为带 `target_event_id` 的追加事件，
不存在可变布尔字段（D3）。
**Validates: Requirements 2.2, 2.10**

### Property 7: 元数据不无故丢失
`merge_records` 对标量字段只在 existing 为空时取 incoming，两侧非空时按
`PROVENANCE_RANK` 取胜并记 `FieldConflict`；`abstract` 绝不被清空；事件与标识符单调递增；
低置信匹配不触发自动合并（D5）。
**Validates: Requirements 1.13, 3.5, 3.6, 3.7, 3.10**

### Property 35: 核验权限细化到标识符类型
`verification_authority(source, id_type)` 决定事件的 `authority`；
`aggregate_status` 的 VERIFIED 分支只接受 `authority="final"` 的事件 →
aggregator（OpenAlex / S2）回查 DOI 只产生佐证事件，无法单独把状态推成 VERIFIED
（D11）。`legacy` / `web_search` 的 `final_identifier_types` 为空。
**Validates: Requirements 2.9, 11.17, 11.24, 11.25, 11.26, 11.27**

### Property 36: 原有未核验引用不被误判
`facts.bibkeys()` / `ref_ids()` 在 A1 保持 resolvable 语义（含用户原稿的未核验引用），
`RewriteGate` / `AddSectionGate` 行为逐字节不变；`citable_*` 为新增接口，A1 只建不用
（D9）。
**Validates: Requirements 4.2, 4.5, 4.7**

### Property 37: 状态变迁按因果折叠
`aggregate_status` 只按 `target_event_id` 折叠：`conflict_resolved` 只能指向
`conflict`、`released` 只能指向 `quarantined`；折叠结果与事件时间戳先后及是否相同
无关；target 不存在或类型不匹配的事件被忽略并计入 `invalid_transitions`（D3）。
**Validates: Requirements 2.2, 2.3, 2.4, 2.10**

### Property 38: 引用解析歧义显式化
`resolve_token` 返回 `ResolutionResult`，命中多条时为 `AMBIGUOUS` + 候选集合，
绝不任选一条也不退化为 `NOT_FOUND`；五套索引含 `merged_from`，使被合并的旧
`reference_id` 仍可解析；无法归类的标识符串进 `unclassified_identifiers`，
不进入别名或去重字段（D7 索引表、D12）。
**Validates: Requirements 3.8, 3.9, 3.10**

### Property 39: 无派生字段双真相源
`verification_status`、`abstract_content_hash`、`canonical_identifier` 均为派生
property，不作为存储字段、不进 JSON；`missing_fields(style)` 带参数，
不存在与目标格式无关的 `bibliographic_complete` 存储值。
**Validates: Requirements 1.11, 2.5**

### Property 34: 唯一可写真相源 + 记录不可变 + 索引一致
`ReferenceRecord` 为 frozen 且全字段 immutable；只有 `Ledger.upsert()` / `replace()`
能产出记录，`RetrievalCandidate` 永不持久化，`KnownRef` 只读 →
系统中只有一个可写文献真相源；`Ledger.get()` / `all_records()` 返回的对象无法被外部
修改；一切修改内部必重建五套索引 → 不存在「记录已改、索引未更」的状态（D0/D7）。
**Validates: Requirements 1.1, 1.2, 1.3, 19.1**

### Property 29: 迁移无损且不虚高
迁移 1:1（含 title/bibkey 皆空的行），条数守恒；混合 schema 下 `known_refs` 的独有条目
一并迁移而非忽略（D10）；写回经备份 + tmp + `os.replace` 原子替换，中途失败不产生半截
文件；旧 `verified=True` 产生的事件 `authoritative=False` → 聚合状态仍为 `UNVERIFIED`，
同时置 `legacy_verified` 与 `verification_method="legacy"`。
**Validates: Requirements 19.1, 19.2, 19.3, 19.4, 19.5**

### Property 32: 来源层级裁决
每条事件带 `provenance`；每个字段的当前来源可由 `field_provenance` 查出（D6）；
`resolve_field_conflict` 结果恒等于 `registry > publisher > aggregator > web` 层级序，
同级才落配置序；`web` 恒不胜出（rank=0），配置无法把低层级提到高层级之上。
**Validates: Requirements 1.8, 3.7, 11.19, 11.20, 11.21, 11.23**

### Property 33: 角色能力不越权
`is_authoritative` 从 `SourceProfile.roles` 派生而非手写集合；不含 `VERIFY` 的源
（`web_search`、`legacy`）永不产生使 `aggregate_status` 返回 `VERIFIED` 的事件。
**Validates: Requirements 11.2, 11.22**

### Property 30: 默认零影响
四个新配置项默认值使行为与现状一致（含低置信去重默认关闭）；`bibkeys()` 默认兼容模式；
网络调用次数不变；`trace_enabled` 默认 False。全套 476 测试绿。
**Validates: Requirements 19.6, 19.8, 19.9**

## Error Handling

| 失败点 | 处理 |
|---|---|
| facts.json 损坏 / 非法 JSON | 与现状一致：`load` 抛出由上层处理；不静默清空、不写回 |
| 迁移中某行字段类型异常 | 该行尽力构造（缺字段留空），**不丢弃**，记警告（守条数守恒） |
| 原子写回中途失败 | 清理 tmp，原文件与备份保持完整；下次 `save` 重试 |
| `classify()` 无法识别 `source_id` | 不猜类型；存入 `unclassified_identifiers`，**不进**别名与去重字段 |
| HTTP 404 | `RetrievalNotFound` → `fetch_metadata` 返回 None → `NOT_FOUND` 事件 |
| HTTP 429 / 5xx / 超时 / 连接失败 | 重试耗尽后 `RetrievalUnavailable` → `PROVIDER_ERROR` 事件 |
| 其余 4xx（权限/参数错） | `RetrievalUnavailable`（不是「不存在」），`PROVIDER_ERROR` 事件 |
| 变迁事件 target 不存在或类型不匹配 | 忽略该事件，计入 `invalid_transitions`，由 `integrity_report()` 报告 |
| 引用标记命中多条记录 | `resolve_token` 返回 `AMBIGUOUS` + 候选集合；调用方必须显式处理 |
| 归一化后 work identifier 为空串 | 视为无该标识符，不入 dedup key（防空串互相命中） |
| dedup 高置信命中多条已有记录 | Ledger 已有重复（迁移遗留）→ 合并全部命中项 |
| `fetch_metadata` 抛 `RetrievalError` | 产 `PROVIDER_ERROR` 事件，状态维持 `UNVERIFIED`，不中断入库 |
| `fetch_metadata` 返回 `None` | 产 `NOT_FOUND` 事件（D4 已保证这只表示查无此文） |
| 全部源抛错且无源命中 | `MultiSourceProvider` 抛 `RetrievalError`，不伪装成「查无此文」 |
| bibkey 冲突 | 沿用现有 `_make_bibkey` 的递增策略 |
| 数字别名冲突 | `next_numeric_alias()` 取当前最大值 +1，与 `next_ref_id()` 语义一致 |

## Testing Strategy

**属性测试**（A1 核心，多为纯函数）：

1. `normalize` 幂等：`normalize(t, normalize(t, x)) == normalize(t, x)`
2. `aggregate_status`：追加任一 final `CONFLICT` 事件后，状态**必不为** `VERIFIED`
3. `aggregate_status`：仅含 `PROVIDER_ERROR` 的事件集 → 恒 `UNVERIFIED`；
   仅含非 final 事件（web / legacy）→ 恒 `UNVERIFIED`
4. `aggregate_status` 因果折叠：`CONFLICT_RESOLVED` 指向唯一冲突事件后状态可回到
   `VERIFIED`；`RELEASED` 必须指向具体 `QUARANTINED` 才生效；
   **打乱事件顺序或让全部事件共用同一时间戳，折叠结果不变**；
   指向不存在 target 的变迁事件被忽略
5. `aggregate_status`：仅含 `authority="corroboration"` 事件的记录恒 `UNVERIFIED`
   （aggregator 回查 DOI 不足以核验）
6. `resolve_field_conflict`：任意配置序下 `web` 永不胜出；跨层级结果与配置序无关
7. `merge_records`：合并后 `abstract` 非空性单调不减、identifiers 与 events 为超集
8. `reference_id` 稳定性：同一 DOI 经任意顺序的多次 upsert 得同一 id
9. 记录不可变：对 `all_records()` 返回项赋值抛 `FrozenInstanceError`；
   任意 upsert 序列后**五套**索引与 records 一致
9. 迁移条数守恒：`len(migrate(rows)) == len(rows)`；混合 schema 下
   `len(result) == len(references ∪ known_refs独有)`
10. **ISSN 反例**（Property 3 回归）：两条不同 DOI、相同 ISSN 的候选 →
    Ledger 保持两条独立记录

**HTTP 层直接测试**（不能只用 stub provider——原缺陷正在这一层）：
monkeypatch `httpx.get`，断言 404 → `RetrievalNotFound`、
429/5xx/超时 → `RetrievalUnavailable`、其余 4xx → `RetrievalUnavailable`；
并断言二者都可被 `except RetrievalError` 捕获（既有调用点不破）。

**单元测试**：`classify` 各形态与归类失败进 `unclassified_identifiers`；
低置信匹配返回 `CREATED_WITH_SUGGESTION` 且候选**确实**成为独立记录；
`resolve_token` 对 bibkey / bibkey_alias / numeric / merged_from 四入口 +
`AMBIGUOUS` 分支；`resolvable_*` 与 `citable_*` 的差异（含用户原稿未核验引用）；
`verification_authority` 对 (openalex, doi) 返回 corroboration、
对 (crossref, doi) 返回 final；`verify_detailed` 四种 outcome；
`SessionFacts(path=..., known_refs=[...])` 与位置参数两种构造方式；
原子写回的中断模拟（写 tmp 后抛异常 → 原文件与备份完整）。

**回归**：全套 476 必须绿。重点盯：
`test_week3_rewrite.py`（`SessionFacts(known_refs=...)` 构造、`known_refs` setter、
`bibkeys()` 计数、`add_ref` 去重、`next_ref_id`、md 路径的 `ref_ids()`）、
`test_week4_add_section.py`（`SessionFacts(known_refs=...)` 两处、
`fix_bibliography` 靠 bibkey→title）、`test_planning_console_wiring.py`
（`merge_refs` 的 P0#18 语义）、provider 层对 `fetch_metadata` 返回 None 的既有断言
（D4 改了异常行为）。

**隐私**：写「原文不落 trace」测试时用 >500 字文本或结构性断言
（短文本会整段进 SafeText 的 preview）。

## 实施顺序（建议的 tasks 切分）

1. `identifiers.py` + `sources.py`（含标识符类型级核验权限）+ `verification.py`
   （含因果折叠）——纯函数，零依赖，独立测完
2. `models.py`（frozen `ReferenceRecord` + 派生 property + `to_known_ref`）
3. `dedup.py` + `bibkey.py`
4. `ledger.py`（upsert 四态 / 五套索引 / `ResolutionResult` / resolvable+citable /
   suggestions / integrity_report）
5. **D12**：`RetrievalCandidate` 扩两字段 + 四个 provider 映射（机械改动，独立可测）
6. **D4(a)**：HTTP 层分层异常 `RetrievalNotFound` / `RetrievalUnavailable`
   + 直接测 `get_with_retry`
7. **D4(b)(c)**：三个 provider 的 `fetch_metadata` 契约 + `verify_detailed`
   + `ReferenceVerifier`（单次回查）
8. **D8**：`facts.py` 自定义 `__init__`（兼容 `known_refs=` 入参）+ schema v2 读写
   + 原子写 + `migration.py`
9. `tools/literature.py` 接线 + `config.py` 五配置项 + `REFERENCE_LEDGER_UPSERT` 事件
10. 全套回归 + 属性测试

1–5 步不改任何既有代码路径的行为，可边写边跑全套确认零回归。三处风险点：

- 第 6/7 步改 HTTP 与 provider 的异常行为。新异常都继承 `RetrievalError`，
  既有 `except RetrievalError` 不受影响；但要查有无测试断言「网络失败返回 None」。
- 第 8 步 `facts.py` 是五处消费方的共同依赖，且要保住 `SessionFacts(known_refs=...)`
  的四处调用。
- 第 9 步要验证 `literature_require_final_verification=False` 下真机
  `search_and_verify` 的入库结果与现状一致（D11 末段）。

## 待办与已知简化（写入 tasks，不阻塞 A1）

1. **按字段的来源策略**：全局 `PROVENANCE_RANK` 对 DOI 合理，对摘要/卷期页则
   publisher 往往更优。留到多引文格式那一期，`field_provenance` 已备好数据（D9b）。
2. **多标识符交叉核验**：A1 只做单次回查以保住「网络调用次数不变」；拿 DOI 与
   arXiv id 互相印证属 A2（D4）。
3. **`missing_fields(style)` 的完整字段表**：A1 只留接口与 `reference_type` 骨架，
   四种引文格式的必需字段表属最后一期。
4. **Correctness Property 编号**：requirements 里的编号是历次增补叠出来的
   （1–30 后接 31–39），语义稳定但数字不表示优先级。若后续还要大改，
   建议改用稳定名称（如 `P.DEDUP_WORK_ONLY`）而非数字，A1 不动。
5. **`RetrievalCandidate` 转正名**：A1 先做类型别名，A2 起改为主名、
   `ReferenceEntry` 转过渡别名（D0）。
6. **`unmerge` 能力**：当前无拆分已合并记录的实现，所以低置信一律不自动合并（D5）。
   若将来要支持人工确认合并，需先实现 unmerge。
