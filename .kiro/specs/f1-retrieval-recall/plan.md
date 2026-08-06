# F-1 · 检索召回质量：短语优先查询（plan）

## 结论先行：F-3a 的主假设被实测否证

backlog F-3a 把病因归为「查询词粒度问题：`UAV` / `image matching` 这类**宽词**召回应用领域大热门」，
建议方向是「seeds 更长更专（`cross-view` + `viewpoint change` + `scale difference` 组合而非单 `UAV`）」。

**实测（直连 OpenAlex，零 LLM，2026-08-05）表明这个方向是错的**，而且合成器**早就已经在做**它建议的事。
`last_run.txt` 里记录的真实 seeds 是：

```
cross-view image matching UAV ground vehicle
large viewpoint scale variation image matching
air-ground visual localization cross-view
aerial terrestrial image matching scale difference
```

已经是「更长更专的组合」，不是单 `UAV`。把它再加长反而更差：

| 查询 | total | 前 10 条切题数 |
|---|---|---|
| `cross-view image matching UAV ground vehicle`（现状 seed） | 9423 | 2/10 |
| 加长到 `... large viewpoint change scale difference air-ground`（F-3a 建议） | 7370 | **1/10** |
| `"cross-view image matching"`（同样的词，**加引号**） | 215 | **10/10** |

**真正的病因是查询语法，不是查询词。** OpenAlex 的 `search=` 参数做的是 OR-ish 的词袋相关性排序，
词给得越多，匹配面越大、越向高被引大热门倾斜。所以 `air-ground` 会招来 Ground-Water Hydraulics
（F-1 原文以为是「被拆词」——方向对，但结论应落在语法上），`UAV` 会招来树皮甲虫和作物表型。
词序相邻信息在词袋里被丢掉了；**加引号把它要回来**。

这也解释了 F-3a 观察到的「OpenAlex 排序偏好被引量而非主题匹配度」：不是排序器偏心，
而是宽词袋下**候选池本身**就是该领域的高被引集合，排序器只能在这个池子里挑。

## 证据的可复现性与边界

跨 4 个主题验证，规则是「引住前 3 个词，其余留自由词」：

| 主题 | 裸词切题 | 短语优先切题 |
|---|---|---|
| 跨视角图像匹配 | 2/10 | **7/10** |
| 大模型代码生成 | 5/10 | **10/10** |
| 联邦学习隐私 | 10/10 | 9/10 |
| 知识图谱问答 | 7/10 | **10/10** |

**没有一例显著变差**；裸词已经够准时（联邦学习）短语优先基本持平。

**必须诚实标注的两个局限**：
1. 「切题」是**确定性关键词代理**（标题含 cross-view / image matching / geo-localization 之类），
   **不是 reviewer 的判定**。它衡量的是召回池的主题纯度，与真实 relevant/weakly 分布相关但不等同。
   真验收只能靠 dogfood 复跑（见「验收」）。
2. 引号会**打穿到 0**：`"air-ground visual localization"` → 0 命中，`"cross-view image matching UAV ground vehicle"`（整句引住）→ 0。
   所以**必须有回退**，否则把召回问题换成召回归零。中文 seed 同理：
   `"空地协同" 跨视角 图像匹配` → 2 条，裸词 202 条 —— CJK 不适用此规则。

## 改什么

### 1. `providers/retrieval/openalex.py` —— 短语优先 + 保底回退（核心）

改动只在 provider 内部，因为 `"..."` 是 **OpenAlex/Elasticsearch 的语法**，不是通用检索语义。
让 loop / planner / seeds 保持 provider 无关（现在也是）。

`search(query, limit)` 变成两级阶梯：

- **rung 0（新）**：`"<前3词>" <其余词>`。仅当同时满足：
  - query 不含 `"`（用户/planner 显式给了短语就尊重，不二次加工）
  - `len(tokens) > 3`（保证有自由词余量 → 天然避开「整句引住」那个归零陷阱）
  - 前 3 词不含 CJK（中文分词场景引号有害，实测）
- **rung 1（现状）**：原样 `search=query`。
- 采纳规则：rung 0 拿到 `>= min(limit, 5)` 条就用它，否则**丢弃并回退 rung 1**。
  ⇒ 召回下界与今天完全相同，最坏情况只多一次 HTTP。

成本：一次额外 GET ≈ 1.6s（实测）。对照单次 attempt 的 72s（relevance LLM 占绝大头），
在噪声范围内。`provider_time_budget_s=120` 不受实质影响。

### 2. 可观测：新 EventKind `PROVIDER_SEARCH`

用刚接好的 `ambient_publish` 发一条：`strategy`（`phrase_first` / `bare` / `bare_fallback`）、
`results`、`query_len`、`phrase_len`。这样「短语优先到底有没有被用上、回退了几次」
在 `trace.json` 里直接可读 —— 不然又是一个「改了但看不见有没有生效」。
这是上一轮埋点的第一笔实际回报。

### 3. 开关：`literature_openalex_phrase_first: bool = True`

按 provider 构造参数注入（`factory.py` 读 config 传进去），**不在 provider 内部读全局 config**，
与现有 provider 保持一致的纯度。默认 **开**：回退机制保证召回不劣化，而收益是实测的。
注意这会给 backlog D-3（flag 矩阵膨胀）再加一个 flag —— 我认为值得，因为无声改检索行为更糟；
若你不同意可以默认关，但那样它基本不会被用上。

### 4. 测试 `tests/test_openalex_phrase_first.py`（新）

全部用**假 HTTP 层**，不联网、不进 CI 的网络依赖：

- 3 词以上 → 首个请求带引号短语
- rung 0 结果不足 → 第二次请求是裸词，且**返回的是裸词结果**（召回下界不倒退）
- query 已含 `"` → 只发一次请求，不加工
- `len(tokens) <= 3` → 只发一次请求（不触发整句引住的归零）
- 前 3 词含 CJK → 只发一次请求
- flag 关 → 行为与今天逐字节相同（单请求、裸词）
- `PROVIDER_SEARCH` 事件的 strategy 取值正确
- 按项目纪律：每条测试都会**注入原 bug 验证它真的变红**

## 不做什么（划清边界）

- **不动 seeds 合成 / planner prompt**。实测表明词本身没问题，改 prompt 是在治错的病。
- **不加 `title_and_abstract.search` filter**。实测 total 只剩 15 条，太窄，且与 `search=`
  的排序语义不同，风险大于收益。
- **不动 OpenAlex 排序参数 / 年份加权**。F-3a 提到的「偏好被引量」是宽词袋的**症状**；
  先修因，修完再看还剩多少。
- **不碰 relevance 判定**。F-2 已确认判定层没问题（GPT-5 把噪声全判对了），是输入脏。

## 验收

1. 单测全绿 + 全量套件不回归（当前基线 1525 passed）。
2. **dogfood 真机复跑**同一主题同一配额，与 `baseline_2026-08-01_total5_en5.txt` /
   `candidates.jsonl` 对比：候选池的 `unrelated` 比例应显著下降（基线：20 候选里 7 个 unrelated、
   4 篇偏题）。这才是 F-1 是否真的松动的判据 —— 关键词代理只是先导指标。
3. 复跑要花真钱和真时间（基线 123s / 34 verify calls），**跑之前我会先确认**。

## 风险

- 关键词代理与 reviewer 判定不完全对齐，dogfood 复跑可能显示收益小于代理预测。
  缓解：埋点已能给出 accept_new/reject 的真实分布，一次复跑即可判定。
- 极窄主题下 rung 0 可能命中 <5 条而恒回退，等于白花一次 HTTP。
  缓解：`PROVIDER_SEARCH` 的 strategy 分布可量化这件事，必要时再调阈值。
