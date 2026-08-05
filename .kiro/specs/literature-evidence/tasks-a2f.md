# Implementation Plan — A2f：检索意图准备与缺口驱动查询规划

> 承接用户定稿的 design-a2f。5 期,按「值对象/需求同步 → 合成+交互准备 → 语义主题接入 →
> 自适应多 seed Loop → Planner 装配+验收」推进。
>
> **零回归铁律**:无 CitationQuota 的 rewrite/add_section **不进入本能力**,prompt/检索调用/
> 输出逐字节不变;`literature_writing_integration` 默认关;`search_query` 旧槽类型不变。
> 全套既有测试绿(基线取实施开始时实际收集值,不硬编码数字)。

## 分层红线
- `literature/retrieval/*` 不 import `tasking`/`application`/`session`/`config`。
- `tasking/query_synthesizer.py` 可依赖 `literature.retrieval.search_intent`(方向同 quota_materializer)。
- 合成/交互/预算/冻结持久化在 `application` 层;Handler 只调准备服务、消费结果,不 import LLM 实现。
- 阈值/上限/模型/clock 全注入,领域层不读全局 config。

## Task Dependency Graph
```json
{ "waves": [
  { "wave": 1, "tasks": ["1"] },
  { "wave": 2, "tasks": ["2"] },
  { "wave": 3, "tasks": ["3"] },
  { "wave": 4, "tasks": ["4"] },
  { "wave": 5, "tasks": ["5"] }
] }
```

## Tasks

- [x] 1. 契约与 Requirements 同步（A2f-1）
  - `literature/retrieval/search_intent.py`：`SearchIntentOrigin`/`SeedTarget` 枚举、
    `SearchSeed`、`SearchIntent`(frozen)。`__post_init__`/验证工厂保证 topic 非空、
    seed 1–6 个且已规范化(NFC+casefold+空白归一去重保序)。
  - `semantic_fingerprint()`：canonical JSON(schema_version + 清洗后 semantic_topic +
    section_purpose,**不含 seeds**);`strategy_hash()`：semantic_fingerprint + 按确认顺序
    规范化的 seed text/target。两者不得混用。
  - generic-title 集合与 `is_informative_title()`：剔除「引言/背景/相关工作/方法/讨论/
    Introduction/Background/Related Work/Method/Discussion」等纯通用标题。
  - `SearchContext`(只读输入)、`SearchIntentPreparationStatus`/`SearchIntentPreparationOutcome`。
  - **Requirements 同步**（`literature-evidence/requirements.md`）:补 A2f 验收口径——
    禁通用章节名作主题;缺主题必澄清;semantic_topic 变则 TaskFingerprint 变、seed/planner
    查询变则不变;多 seed/planner 计入检索预算;trace 只记 hash/长度/数量/来源;派生主题需确认
    时可跨 turn 恢复同一冻结 intent(不重采样);恢复批次确认复用原 pending batch。
  - 测试：值对象验证(topic 空/seed 0 或 >6/未规范化→拒);两种 hash 分离(topic 变→
    semantic_fingerprint 变、seed 排序变→仅 strategy_hash 变);generic-title 过滤;
    outcome 状态与 issue_code。
  - _Requirements: 8.2, 13.5, 17, 18.2, 18.3（+本期新增口径）_
  - _Design: design-a2f D1/D2、Requirements 对齐前置_

- [x] 2. Synthesizer 与交互准备（A2f-2）
  - `literature/retrieval/query_synthesizer.py`：`SearchIntentSynthesizer` Protocol +
    `DeterministicSearchIntentSynthesizer`(显式 query/确定性投影,无 LLM)+
    `LLMSearchIntentSynthesizer`(structured tool call;schema 只含 topic/section_purpose/
    seeds/target,**无** quota/filter/provider/fingerprint 字段)。
  - 代码侧验证(D3)：topic 3–500、seed 2–300、1–6 个去重保序、剔空/通用标题/纯标点数字/
    无关 provider 指令;target 仅 any/zh/en 且只生成配额实需语言维度;全删→NEEDS_INPUT/ERROR。
  - `application/search_intent_preparation.py`：`prepare_search_intent(operation_view,
    project_context, quota_dimensions, *, synthesizer, interaction_port, budget)`：
    - 显式 query 有效 → deterministic READY,不调 LLM;
    - 已确认 operation 字段确定性投影 → READY,不重复确认;
    - LLM 合成或主要来自 PROJECT/MIXED → 经 `confirm_search_intent` 展示 topic+seed 摘要
      (accept/modify/cancel);仅同义词 seed 不强制确认;
    - 信号不足 → `request_search_topic`,受 `max_search_intent_revisions` 限;
    - provider/parse/schema 失败 → 有确定性信号则 fallback,否则 ERROR;非交互仅在需澄清/确认
      时返回 NEEDS_*,不默认接受。
  - `application/literature_ports.py`：加 `request_search_topic()→SearchTopicDecision`、
    `confirm_search_intent()→SearchIntentDecision`(typed,不复用 confirm_plan);
    `console_literature_port.py` 实现两法(Ctrl-C/EOF/:quit → needs_input,不误接受)。
  - 预算：synthesis LLM 调用经共享 BudgetLedger,**真正发模型调用前**扣 `charge_llm`;
    deterministic 不扣;失败/schema 非法的真实调用仍计一次。
  - 测试(stub LLM/port)：显式 query→READY 且 0 次 synthesizer LLM;派生→topic+2–6 seed;
    过滤规则;ERROR vs NEEDS_INPUT 不混;confirm accept/modify/cancel + 修订上限 + 非交互暂停;
    deterministic 不记 LLM、LLM 每次真实调用(含失败)恰记一次。
  - _Requirements: 8.2, 17, 18.2；本期新增口径_
  - _Design: design-a2f D2/D3/D4/D9_

- [x] 3. 语义主题接入 + 冻结持久化与恢复（A2f-3）
  - RelevanceCriteria 仅消费 `semantic_topic`/`section_purpose`/`operation_semantic_goal`;
    **禁** `" ".join(seeds)` 作 query_intent。
  - `PreflightCheckpoint` 增 `search_intent_id`/`semantic_fingerprint`/`strategy_hash`;
    `facts.py` 版本化持久化 `pending_search_intents`(canonical payload,以 id 引用;旧 envelope
    缺字段→空;损坏条目隔离计数,同 pending_literature_flows 先例)——注意 `to_envelope`/`load` 同步。
  - `literature_flow.py`：按 `checkpoint.state` 恢复,**不无条件从 run() 头重放**——
    NEEDS_SEARCH_TOPIC/NEEDS_SEARCH_INTENT_CONFIRMATION → 载 pending intent 或继续澄清;
    NEEDS_BATCH_CONFIRMATION → 按 batch_id 复用 pending batch,禁重跑 Loop;其余沿用 A2d 恢复。
  - 恢复时重算两 hash 比对,不一致 → `STALE_SEARCH_INTENT`(不调 LLM);modify → 新版本 +revision;
    accept/cancel/flow 完成 → 清理 pending intent。
  - 测试：criteria fingerprint 不因 seed 排序/planner 改写变化,因 topic 变而变;pending intent
    序列化/迁移/清理;hash 不一致→STALE 且不调 LLM;NEEDS_BATCH_CONFIRMATION 恢复复用原 batch_id、
    search 调用不增。
  - _Requirements: 13.5, 18.3；本期新增口径_
  - _Design: design-a2f D4/D5/D6_

- [x] 4. 自适应多 seed Loop + 兼容迁移 + 共享预算（A2f-4）
  - `WritingEvidenceRequest` 增 `search_intent: SearchIntent | None`;`RetrievalRequest` 增
    `search_intent`,`initial_query` 转 legacy(两者冲突→构造失败,不静默忽略);`effective_seeds()`。
  - `batch_id` 改 Loop 启动生成并持久化的 **UUID**(恢复复用);`operation_id`/`semantic_fingerprint`/
    `strategy_hash` 独立字段,**不再用 query hash 充当批次身份**。
  - Loop 自适应 seed 队列(D7)：先算 gap;已满足→不跑 seed;按「非零 gap 对应 target > any >
    已满足维度」排序、同级保用户确认序;每 seed 一次 search、逐 attempt 重算 gap、满足即停;
    `query_fingerprint`(规范 query+source+filters)跨 seed/planner 去重。
  - **修既有缺陷**：`max_iterations`=策略轮次、`max_search_calls`=seed 调用硬上限(trace 分列);
    续搜 `charge_llm` 移到真正调 LLM 处(deterministic 不再虚记)。
  - `run_retrieval_loop` 增可选 `budget_ledger=None`：未传→沿用内部创建(旧调用逐字节不变);
    传入→与 synthesis/planner/judge/search/verify 共用同一 ledger。
  - 测试(stub)：多 seed 首个满足即停、不跑剩余;无进展才续搜;query_fingerprint 跨来源去重;
    UUID batch + 恢复复用;预算触顶保留 Batch、stop_reason/触顶维度正确;旧 initial_query 调用不变;
    deterministic 续搜不记 LLM。
  - _Requirements: 8.2, 13.5；本期新增口径_
  - _Design: design-a2f D6/D7/D9_

- [x] 5. Planner 装配 + 可观测 + 端到端验收（A2f-5）
  - `config.py`：`PAPER_LIT_QUERY_PLANNER=deterministic|llm|hybrid`(默认 deterministic,未知值
    降级+告警)、`literature_search_synthesis`(合成开关)、seed/`max_search_intent_revisions` 上限。
  - `literature_composition.py`：按 config 装配 synthesizer 与 planner;`llm`/`hybrid` 用 writer
    或专用 planner LLM,**不用 relevance reviewer**;失败/非法输出退 Deterministic;Planner 输出
    二次校验(source_name 在 registry allowlist、query 同 seed 清洗去重、拒未知 provider/filter、
    不得改 SearchIntent 语义字段)。
  - `observability/sanitize.py`：统一 digest-only helper(不产 preview);trace 新增
    semantic_fingerprint/strategy_hash/topic 长度/seed_count/各 target 计数/origin/query digest/
    planner model+version/预算快照/accept-modify-cancel;**不记** topic/query/claims/正文原文。
  - `chat.py`：装配串起(默认关,零影响);dogfood runner 打印 seed keywords 与每轮 planner query
    (debug 开关,默认不进普通 trace)。
  - 端到端(全 stub)：CitationQuota op → SearchContext → prepare(READY) → 多 seed Loop →
    strict verify → Bundle → CitationQuotaGate;断言 trace 只含 hash/长度/计数/version。
  - **硬回归**：无配额 rewrite/add_section prompt/检索调用/输出逐字节一致;flag 默认关全链路不变。
  - 全套既有测试绿(基线=实施开始实际值);清理临时文件。
  - _Requirements: 8.2, 18.2, 18.3；本期新增口径_
  - _Design: design-a2f D8/D10、Correctness Properties 1–11_

## Notes
- 每个 Task 后跑相关子集 + 说明变更/验证,再进下一项;Task 5 跑全套。
- 上线策略：代码默认 deterministic;dogfood 显式开 hybrid;真实命中率/成本/错误主题率达阈值前
  不把 hybrid 设为产品默认(design D8)。
- A2f 不接中文源、不做 provider 方言 filter 下推(留 A2g);不声称仅靠 query 优化即可保证中文配额。
- git 提交由用户自行完成。
