# Implementation Plan — A2a：语言判定 + 任务相关性

## Overview

A2 的第一个子阶段，建设三块**纯语义地基**：语言判定（Req 5）、任务相关性
（Req 7）和不可信外部输入防护（Req 17）。A2a 是检索源文本第一次进入 LLM 提示的
位置，因此安全清洗不能推迟到 A2c。

**离线可测、不碰网络、不改写作链路、不改 facts.json schema。** 相关性接口用 stub LLM
验证协议和失败语义；真实模型的语义准确率不在本阶段宣称完成，留到 A2c 的 Shadow 评测。

**验收铁律**：既有 506 个测试全绿；语言判定保持确定性、可属性测试；任何 Reviewer
调用失败都不得伪装成“文献无关”或“文献相关”。

范围约束：

- 本阶段的产品口径明确为**中文 / 英文**，因此
  `quota_bucket ∈ {zh, en, other, unknown}`，只有 zh/en 可以计入对应配额。
- `language_code ∈ {zh, en, ja, ko, other, unknown}`；日/韩/德/法等落入
  `quota_bucket=other`，必须单列报告，不能混入英文配额。
- 仅凭 Latin 脚本只能证明“非中文”，不能证明“英文”；缺少可靠语言信号时不得把
  德文、法文等猜成英文。
- `RelevanceStore` 只做**内存 + 序列化**，不落盘；接入 project facts 属 A2c。

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2"] },
    { "wave": 3, "tasks": ["3", "4"] },
    { "wave": 4, "tasks": ["5"] },
    { "wave": 5, "tasks": ["6"] }
  ]
}
```

- Task 1 改动既有结构化调用基础设施，必须先完成并立即跑全套回归。
- Task 2 建立外部文本的唯一清洗/包裹入口。
- Task 3、4 都依赖 Task 2；Task 4 还依赖 Task 1，二者完成依赖后可并行。
- Task 6 是最终集成验收，必须等待 Task 5 完成，不得与配置任务并行。

## Tasks

- [ ] 1. `structured_call` 抽到中立模块（`providers/llm/structured.py`）
  - 新建 `providers/llm/structured.py`，把以下内容从
    `tasking/extractors/base.py` 与 `tasking/contracts.py` **原语义迁入**：
    - `CallStatus`；
    - `StructuredCallResult`、`_extract_json_object`、`_forced_tool_name`、
      `_looks_unsupported`、`_UNSUPPORTED_HINTS`、`structured_call`。
  - `tasking/contracts.py` 删除原 `CallStatus` 定义，改为从中立模块导入并继续 re-export，
    保证所有 `from ...tasking.contracts import CallStatus` 调用点不变。
  - `tasking/extractors/base.py` 从中立模块导入并继续 re-export
    `StructuredCallResult` / `structured_call`；不得复制第二份实现。
  - 新模块不得 import `tasking`，只依赖 `providers.llm.base` 与标准库。
  - 新增兼容性断言：
    `tasking.contracts.CallStatus is providers.llm.structured.CallStatus`。
  - 测试必须覆盖：
    - tool call arguments 为 dict → success；
    - arguments 非 dict → schema_failed；
    - 无 tool call、正文为合法 JSON → success；
    - 无 tool call、正文非 JSON → parse_failed；
    - `forced_tool_name` 不受支持时只降级重试一次；
    - provider 超时/限流等普通失败不得触发无强制参数的第二次请求。
  - 完成立即运行全套 506，确认 planning loop、analyzer、extractor、turn service 零回归。
  - _Requirements: A2a 基础设施，支撑 Req 7 的结构化 Reviewer 调用_
  - _Design: design-a2a.md D6_

- [ ] 2. 不可信外部输入防护（`literature/untrusted.py`）
  - `clean_external_text(text, *, max_chars)` 使用唯一、固定顺序：
    HTML entity 解码 → 剥离 HTML/XML 标签 → 去控制字符（保留 `\n`/`\t`）→
    规范化空白 → NFC 规范化 → 按 Unicode 码位截断。
  - HTML entity 应解码而不是简单删除，避免把学术符号或文本无声抹掉。
  - `wrap_as_data(label, text)` 使用稳定定界符，把文本放入明确的“待分析数据”区，
    并声明数据中的指令性语句不得作为系统指令执行。
  - 包裹后的外部文本只能进入 user/data 内容，不得拼进 system 指令或 tool schema。
  - 测试覆盖 HTML/entity、控制字符、NFC、空白、超长截断和空输入。
  - Prompt Injection 测试只证明**结构性隔离**：含“ignore previous instructions”或
    “输出 relevant”的原文仍完整位于数据块内，未进入 system/schema。Stub 测试不得
    被表述为“已经证明真实模型不会受注入影响”。
  - _Requirements: 17.2, 17.3, 17.6, 17.7_
  - _Design: design-a2a.md D5_

- [ ] 3. 语言判定（`literature/language.py`）
  - 定义 frozen `LanguageThresholds`，所有阈值经参数注入；`language.py` 不得 import
    `config.py`，保持纯函数和可重复测试。
  - `script_counts(text)` 分书写系统计数并明确覆盖的 Unicode 范围：Han 基本区与扩展区、
    Hiragana、Katakana、Hangul（含 Jamo）、Latin、Cyrillic。数字和标点不进入分母。
  - `script_signal(text, thresholds)` 产出 `han_dominant | latin_dominant | ja | ko |
    cyrillic | ambiguous`。Han 只是书写系统信号，不直接等同中文；出现显著假名或
    Hangul 时必须优先判 ja/ko。
  - 增加 `normalize_language_hint()`，显式处理 `zh/zh-CN/zho/chi/cmn`、
    `en/eng`、`ja/jpn`、`ko/kor` 等常见代码；`und`、空串和未知值不得猜测。
  - `classify_language(title, abstract="", language_hint="", *, thresholds)` 必须先复用
    Task 2 的清洗规则，再分别计算 title、abstract、source 三个信号，禁止把标题和摘要
    直接拼成一个比例。融合规则至少满足：
    - Han 主导、无假名/Hangul，source=zh → `language_code=zh`、high；
    - Han 主导、source 缺失 → zh/medium，严格策略下不计入；
    - Han 与 source=ja/ko/en 等冲突 → low 或 unknown，不得盲目信 script；
    - Latin 主导、source=en → en/high；
    - Latin 主导、source=de/fr 等 → other/high 或 medium；
    - Latin 主导、source 缺失 → `language_code=unknown`、`quota_bucket=unknown`，
      **不得判 en**；
    - 假名/Hangul → ja/ko，并投影到 `quota_bucket=other`；
    - Cyrillic → other；完全无依据 → unknown/none；
    - 标题与摘要冲突时保留冲突 reason，不得用长摘要的字符量直接压过标题。
  - `LanguageVerdict` 至少包含：
    `language_code`、`quota_bucket(zh|en|other|unknown)`、
    `confidence(high|medium|low|none)`、`basis`、各字段 script ratios、`reason_codes`。
  - `counts_toward(verdict, target_bucket, policy)`：
    - `high_only` **只计 high**；
    - `include_low_with_warning` 可计 high/medium/low，但调用方必须报告低置信数量；
    - other/unknown/none 永不计入中文或英文配额；
    - 不新增“high_only 实际包含 medium”这类名实不符行为。
  - 不给 `ReferenceRecord` 增加依赖全局配置的 property。提供
    `classify_record_language(record, thresholds)` 或 `LanguageClassifier`；A2b 必须显式
    传入运行时阈值，避免同一记录因隐式全局状态得到不同结果。
  - 增加小型离线 Golden Set，至少覆盖：中文、英文、中英双语、中文标题+英文摘要、
    日文汉字+假名、纯汉字日文标题、韩文、德/法拉丁文本、Cyrillic、source 冲突和空文本。
    阈值默认值只有通过该集合后才能固定。
  - 属性/单元测试验证纯函数确定性、各脚本分区、融合规则及两种计数 policy。
  - _Requirements: 5.1–5.8_
  - _Design: design-a2a.md D1, Property A2a-1/2/3；Requirements 与 Design 必须统一为
    zh/en/other/unknown，不能继续用 non_zh 表示本阶段的英文配额_

- [ ] 4. 任务相关性（`literature/relevance.py`）
  - 定义 frozen `RelevanceCriteria`：`topic`、`research_question`、`section_purpose`、
    `query_intent`、`include_conditions`、`exclude_conditions`。Fingerprint 基于稳定字段的
    canonical JSON（UTF-8、NFC、sort_keys、固定 separators）计算，不使用管道拼串。
  - 定义 `JudgeVersion{provider, model_id, prompt_version, schema_version}`，用于缓存失效；
    仅有 `prompt_version` 不足以处理模型或 schema 变更。
  - `RelevanceAssessment` 至少包含：`reference_id`、`task_fingerprint`、
    `reference_content_hash`、`judge_version`、`status(SUCCESS|ERROR)`、
    可空 `verdict(relevant|weakly_relevant|unrelated)`、可空 `confidence`、
    `basis(title_and_abstract|title_only)`、有界的 `matched_aspects` / `mismatch_aspects`、
    枚举 `reason_code`、有界 `rationale`、可空 `error_kind`、`assessed_at`。
  - `reference_content_hash` 必须基于**真正送入 Reviewer 的清洗、NFC、截断后文本**计算，
    不能对原始文本计算 hash 后评审另一份文本。
  - `RelevanceStore` 的完整键为：
    `(reference_id, task_fingerprint, reference_content_hash, judge_version)`；任一成分改变均
    不得复用旧结论。提供 `get/put/to_json/from_json`，本阶段不接 facts。
  - `RelevanceJudge` 只消费构造函数传入的 reviewer LLM 与 JudgeVersion；“优先 reviewer、
    否则回退 writer”的选择由应用组合层负责，Judge 内部不得读取全局 config。
  - `.assess(candidate, criteria)`：先调用 Task 2 清洗/包裹，再调用 Task 1 的
    `structured_call`。工具 schema 必须约束 verdict/confidence/reason_code 枚举、数组项
    类型、最大项数和 rationale 最大长度；解析后仍执行程序侧 schema 校验。
  - rubric 同时写入 system 纪律和字段 description，明确：
    - relevant：直接服务同一研究问题、对象、方法或章节目的；
    - weakly_relevant：仅提供相邻主题或通用背景；
    - unrelated：研究问题、对象、方法和章节用途均不匹配；
    - 禁止只因关键词重合判 relevant；必须执行 include/exclude 条件。
  - 无摘要时 `basis=title_only`，最终 verdict **最高为 weakly_relevant**且 confidence=low；
    若模型返回 relevant，程序统一降为 weakly_relevant/low，不保留 relevant/low 的第二口径。
  - `structured_call` 非 SUCCESS 或程序侧 schema 校验失败时，返回
    `status=ERROR`、`verdict=None` 和明确 `error_kind`；不得映射成 unrelated。
  - Stub 单元测试覆盖成功三分类、title_only 降级、provider/parse/schema 错误、非法枚举、
    过长输出裁剪/拒绝、缓存随 content/judge version 失效及序列化往返。
  - Prompt Injection 单元测试只验证外部文本位于隔离数据块、未进入 system/schema；
    “真实模型判定不受影响”留给 A2c 的对抗评测，不由 Stub 结果证明。
  - _Requirements: 7.1–7.7, 17.2–17.4_
  - _Design: design-a2a.md D2, D3, D4, Property A2a-4/5/6_

- [ ] 5. 配置接线（`config.py` + 应用组合层）
  - 增加语言阈值配置并标注“初始值，需由 Golden Set 校准”：
    `PAPER_LIT_LANG_HAN_ZH`、`PAPER_LIT_LANG_LATIN`、`PAPER_LIT_LANG_KANA_MIN`、
    `PAPER_LIT_LANG_HANGUL_MIN`。
  - 增加并校验 `PAPER_LIT_LANG_POLICY ∈ {high_only, include_low_with_warning}`；
    默认 `high_only`，非法值降级并产生可观测警告，不得静默进入宽松模式。
  - `config.py` 只解析原始配置；由应用组合层构造 `LanguageThresholds` 并注入分类器。
    `literature/language.py` 不得 import config。
  - 应用组合层负责选择 Reviewer：有独立 reviewer 配置则使用 reviewer，否则显式回退
    writer，并把最终 provider/model 写入 JudgeVersion。
  - _Requirements: 5.6, 5.7_
  - _Design: design-a2a.md D1, D3_

- [ ] 6. 全套回归、结构验收与交付检查
  - 全套 506 绿；新增 A2a 单元、属性和 Golden Set 测试全部通过。
  - 确认 `literature` 包不 import `tasking`，并确认
    `providers.llm.structured` 也不 import `tasking`。
  - 确认 language 模块不 import config，Reviewer 选择只发生在组合层。
  - 确认 Requirements、Design、Tasks 对语言配额桶的口径一致：当前为
    `zh/en/other/unknown`，只有 zh/en 参与配额；若 Requirements 仍写“外文/non_zh”，
    必须先同步，且不得宣称 A2a 完成。
  - 输出测试摘要并清理临时文件；不修改 Working Copy 和 facts.json schema。
  - _Requirements: A2a 验收铁律_
  - _Design: design-a2a.md Testing Strategy_

## Notes

- 每完成一个 Task，先说明变更和验证结果，再进入下一项；Task 1 后必须立即跑全套。
- 全套测试以本轮开始时实际收集到的既有测试为基线；“506”是当前快照，不作为永不变化的
  固定数量。新增 A2a 测试后总数应上升，验收标准是既有测试无回归且新增测试全绿。
- Prompt Injection 的结构测试只证明消息边界和权限边界正确，不等价于证明任意真实模型
  都不会受攻击；真实模型对抗评测属于 A2c Shadow 阶段。
- A2a 保证接口、失败语义、确定性分类和缓存失效正确，不对真实 Reviewer 的语义准确率
  做无数据承诺。A2c 必须测量错误接纳率、错误拒绝率、title_only 误接纳率，并按中文、
  英文及 other 子集分别报告。
- git 提交由用户自行完成。

## 已定决策

- 语言范围为中文/英文：`language_code` 表达 zh/en/ja/ko/other/unknown，
  `quota_bucket` 表达 zh/en/other/unknown；只有 zh/en 计入对应配额。
- 仅凭 Latin 脚本不判英文；它最多证明非中文。英文配额必须有 en 来源信号或未来引入并
  通过评测的语言识别器。
- `high_only` 只计 high；medium/low 只有在 `include_low_with_warning` 下才可计入并报告。
- 相关性错误与语义 verdict 分离：错误时 `status=ERROR, verdict=None`。
- RelevanceStore 内存 + 序列化，不落盘；缓存键包含 content hash 与完整 JudgeVersion。
