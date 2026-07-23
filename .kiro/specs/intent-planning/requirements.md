# Requirements Document

## Introduction

本功能（`intent-planning`）解决当前 agent 两个切实的体验缺陷：

1. **一条消息含多个意图时只执行一个**——`Router.route()` 只返回单个 `Intent`，chat.py 是 `if/continue` 链，其余意图被静默丢弃。
2. **说得很清楚却被判 UNCLEAR，或明明说过还被反复追问**——Router 是脆弱的关键词匹配（"加一节"≠"加一章"就判 UNCLEAR），Interview 是固定问答脚本、不读用户消息的自由文本，把「用户没说」与「系统没解析出来」混为一谈。

本功能引入一个**用户请求理解层**：把一条自然语言消息解析为一个**有序的操作计划**（多操作），对每个操作**抽取槽位**，用真实系统状态**校验**，只对**真正缺失**的信息**一次性澄清**，执行前**向用户确认计划**，再复用现有 `coordinator.run` + `execute_*` **按序执行**。

范围原则（右尺寸）：
- **复用**既有的意图执行器（`execute_polish/rewrite/add_section/convert/format`）、Gate、working-copy+git、观测脊柱（EventBus/trace），**不重写**它们。
- **绞杀者迁移**：先以 **Shadow Mode** 与现有 Router 并行、只对比不改写入行为，攒够信心再切换。
- **本 spec 不做**：正式化的 Candidate/Approval 对象、Project/Session 恢复、Evidence Gate / 语义 Reviewer、目录重构、有界 Research Loop——这些进入 P1 backlog，需要时另开 spec。

## Glossary

- **User_Turn**: 用户一次输入（一条消息）及其上下文（项目/会话标识、原文、时间）。原文保存在会话侧；Trace 只存 hash 与脱敏摘要。
- **Rule_Fast_Path**: 对显式命令（`:save`/`:undo`/`:sections` 等）与**无歧义**的明显单意图，用确定性规则直接判定，不调用 LLM。
- **Operation_Analyzer**: 把一条自然语言消息拆分为**有序的操作列表**的组件；只做「切分 + 每段意图 + 原话片段」，不抽取全部细节。
- **Operation**: 计划中的一个可执行步骤，含意图（Intent）、目标（章节/格式等）、目标描述（goal）、约束、依赖关系。
- **Atomic_Requirement**: 从用户消息中识别出的一条原子要求（含 `verbatim`、在原文中的 span、类别）。用于「零静默丢失」的可验证性——每条 Atomic_Requirement 必须被映射到某 Operation/Slot、或进入 Unmapped_Requirement、或进入澄清。
- **Task_Plan**: 一条 User_Turn 解析出的、由若干 Operation 组成的**有序列表**及其状态。v1 **严格保留用户表达顺序**，仅按 artifact 能力前置条件做「模拟排序」建议（用户确认后才重排）；不引入依赖 DAG / 拓扑排序（→ P1）。实际执行为**线性串行**（同一 artifact 不并发编辑，见 Req 8）。
- **Slot_Value**: 一个槽位的取值及其状态与来源：`{value, field_status ∈ present|user_absent|ambiguous|invalid|unavailable, source ∈ user_explicit|normalized|defaulted|user_confirmed, requirement_ids}`。`unavailable` = 因抽取调用失败（provider/parse/schema）暂缺，区别于 `user_absent`。
- **Capability_Precondition**: 某意图可执行所需的 artifact 前置条件（如 format_docx 需当前为 docx；内容编辑需目标章节存在于当前 Working_Copy）。
- **Rule_Analysis**: 规则层对一条消息的分析结果：`{candidates: list[Intent], matched_signals, has_sequence_signal, uncovered_text, unambiguous}`；用于判断能否走 Fast_Path。
- **Intent_Spec**: 某意图的槽位规格：每个 Slot 声明 `required/default_factory/normalizer/validator/reuse_history/must_ask`。默认值由 PlanBuilder 按 Intent_Spec 填入（确定性），**不由 LLM 决定**。
- **Projected_Sections**: 计划静态校验时的「投影章节集合」= 当前 Working_Copy 章节 ∪ 前序 add_section 将创建的章节；用于既支持「先加再改」、又能提前拦「操作不存在的章节」。
- **Session_Baseline**: 计划开始前对 Working_Copy 建立的可回滚基线：`{baseline_id, git_commit?, fallback_snapshot_path?}`；git 可用走 commit，否则走文件副本快照。
- **Plan_Execution_State**: 计划执行中的运行态：`{completed_operations, current_operation_id, baseline}`，持续更新，使取消/异常时仍能产出部分完成报告与回滚选项。
- **Slot**: 某意图执行所需的一个字段（如 polish 的 section/goal；add_section 的 title/purpose/structure/position）。
- **Extraction_Result**: 一次槽位抽取的结果，分**调用状态**（`call_status ∈ success|provider_failed|parse_failed|schema_failed`）与**逐字段状态**（每个 Slot_Value 自带 `field_status`），外加 `unmapped_requirements`、错误类别。调用态与字段态分离，避免把「用户没说」与「解析/校验失败」折叠成同一空结果。
- **Unmapped_Requirement**: 用户明确表达、但系统未能映射到任何 Operation 或 Slot 的要求；必须被显式记录，不得静默丢弃。
- **Plan_Validator**: 用**真实系统状态**校验 Task_Plan 的组件（章节是否存在、docx 操作是否作用于 docx、转换目标是否与当前格式相同、依赖是否成环、是否有 Unmapped_Requirement、是否需要澄清、约束是否冲突）。
- **Clarifier**: 仅针对**真正缺失或有歧义**的字段，一次性向用户追问的组件。
- **Plan_Confirmation**: 执行前把解析出的操作计划摘要展示给用户、请其确认的步骤。
- **Shadow_Mode**: 新理解层与现有 Router 并行运行、记录并对比二者结果，但**不改变**实际执行与写入行为的模式。
- **Silent_Loss**: 用户提出的要求既未被执行、也未被记入 Unmapped_Requirement 或澄清项而消失的情况；目标为零。

## Requirements

### Requirement 1: 规则快路径与 LLM 兜底分流

**User Story:** 作为用户，我希望明确的命令和显然的单意图被立即处理、不必每句都等 LLM，同时复杂或模糊的表达能被更强的理解兜底，这样常见操作快、疑难表达也不掉链子。

#### Acceptance Criteria

1. WHEN 用户输入以命令前缀（如 `:`）开头的显式命令, THE Rule_Fast_Path SHALL 直接按命令处理且不调用 LLM。
2. THE 系统 SHALL 依据 Rule_Analysis 判定能否走 Fast_Path，且仅当**全部**满足时才走：`candidates` 恰一个、`has_sequence_signal` 为假（无「然后/再/并且/同时/先…再…」）、无 `uncovered_text`、目标与动作关系清晰。（不得仅凭「现有 Router 返回了一个 Intent」就判为无歧义——它按优先级截断，会把多意图误判为单意图。）
3. WHERE Rule_Analysis 不满足 Fast_Path 条件（多候选/有序列信号/有未覆盖文本/歧义）, THE 系统 SHALL 调用 Operation_Analyzer。
4. IF Operation_Analyzer（LLM）调用失败, THEN THE 系统 SHALL 按以下规则处理，且**不得**直接执行一个 UNCLEAR 或被优先级截断的规则结果：
   - IF Rule_Analysis 有唯一高置信候选, THEN 降级为单 Operation 并向用户明确提示降级；
   - ELSE 不执行任何写操作，要求用户澄清。

### Requirement 2: 多操作分解

**User Story:** 作为用户，我希望「重写引言并补两篇综述，再把全文转成双栏 Word」这种一句话里的多个要求都被识别并按序完成，而不是只做第一个。

#### Acceptance Criteria

1. WHEN Operation_Analyzer 解析一条包含多个意图的消息, THE Operation_Analyzer SHALL 产出一个包含多个 Operation 的有序列表。
2. THE Operation_Analyzer SHALL 为每个 Operation 记录其对应的用户原话片段（verbatim）。
3. WHEN 一条消息只表达一个意图, THE Operation_Analyzer SHALL 产出恰好包含一个 Operation 的列表。
4. WHERE 消息中存在无法归入任何 Operation 的文本, THE Operation_Analyzer SHALL 将其记入未映射文本而非丢弃。
5. THE Operation_Analyzer SHALL 识别用户消息中的 Atomic_Requirement 列表（各含 verbatim 与原文 span），并使每个 Operation 引用其覆盖的 Atomic_Requirement。
6. THE Task_Plan SHALL **严格保持用户表达的操作顺序**；任何重排只能作为建议提出、经用户确认后生效（见 Req 9），不得静默改变顺序。

### Requirement 3: 按意图的槽位抽取

**User Story:** 作为用户，我希望我在消息里已经说清的细节（改哪一章、目的、结构、位置、目标格式）被系统直接采纳，而不是被忽略后又反问我一遍。

#### Acceptance Criteria

1. WHEN 一个 Operation 的意图确定后, THE 系统 SHALL 对该意图运行对应的槽位抽取，从用户消息中提取该意图支持的字段。
2. THE 槽位抽取 SHALL 覆盖现有五个意图（polish / rewrite / add_section / convert_format / format_docx）各自的字段。
3. WHERE 用户消息中已明确给出某字段, THE 系统 SHALL 采纳该值且不在后续澄清中重复询问该字段。
4. THE 系统 SHALL 为每个已填充字段记录来源，取值限于 `user_explicit | normalized | defaulted | user_confirmed`（如 "Word"→".docx" 属 normalized，非推断）。
5. THE 系统 SHALL NOT 为任何未被用户提供的字段编造具体取值（不得幻觉填充）；模型不确定的内容 SHALL 标为字段状态 `ambiguous` 而非直接采纳。
6. THE 每个 Slot_Value SHALL 记录它所消费的 `requirement_ids`（该字段落实了哪些 Atomic_Requirement）。
7. WHERE 走 Fast_Path（跳过 Operation_Analyzer）, THE PreExtractor SHALL 一并产出该 Operation 的 Atomic_Requirement 列表与其消费关系；IF PreExtractor 整体失败, THEN 系统 SHALL 把完整用户原话记为一条 `unparsed` Atomic_Requirement 并转 Clarifier，避免不可观测的信息丢失。
8. THE 每个已填充（present/defaulted/user_confirmed）Slot SHALL 具备 `execution_binding`（绑定到现有 Plan 字段 / execute_* 参数 / ContextBuilder / Gate 配置）；无绑定者（执行器不消费）SHALL 转 unsupported/unmapped，不得静默丢失。

### Requirement 4: 抽取结果的失败分类与零静默丢失

**User Story:** 作为维护者，我希望「用户没说」「模型没返回」「结果解析失败」「校验不通过」这四种情况被区分开，且任何未能落地的用户要求都被显式记录，这样系统不再把各种失败一律变成空字段、也不再悄悄吞掉用户的要求。

#### Acceptance Criteria

1. THE Extraction_Result SHALL 用 `call_status`（success/provider_failed/parse_failed/schema_failed）表达**整次抽取**的结果，用每个 Slot_Value 的 `field_status`（present/user_absent/ambiguous/invalid）表达**逐字段**的结果。
2. THE 系统 SHALL NOT 将「Provider 失败 / 解析失败 / 校验失败（字段记 `unavailable`）」与「用户未提供（`user_absent`）」折叠为同一种空结果；两者都进 Clarifier，但 `unavailable` 用「自动理解暂时失败，我直接和你确认」的措辞。
3. THE 覆盖判定 SHALL 以 **Slot 消费粒度**为准：一个 Atomic_Requirement 只有被某个具体 Slot_Value 的 `requirement_ids` 消费、或进入 Unmapped_Requirement / 澄清 / 被用户忽略，才算「有去向」；**仅挂在 Operation 上不算被落实**。
4. THE 系统 SHALL 使**已识别的** Atomic_Requirement 的 Silent_Loss 为零：每条要么被某具体 Slot 消费、要么进入 Unmapped_Requirement、要么进入澄清、要么被用户明确忽略。（本条约束「已识别要求不丢失」，不宣称能证明「Analyzer 绝不漏识别」；后者由 Req 12 的 golden 漏失率度量。）
5. WHEN 存在 Unmapped_Requirement, THE 系统 SHALL 在执行前向用户呈现，并要求用户明确选择：补充说明 / 忽略该要求 / 取消计划；仅当全部 Unmapped_Requirement 被用户明确处置后，计划方可进入 ready。

### Requirement 5: 用真实系统状态校验计划

**User Story:** 作为用户，我希望系统在动手前就用文件的真实情况检查我的要求是否可行（章节是否存在、格式是否匹配、操作是否互相矛盾），而不是执行到一半才失败。

校验分两级：**Static_Plan_Validator**（计划构建期，检查 intent/slot/能力组合/约束，**不**假设前序操作尚未执行的产物）与 **Pre_Operation_Validator**（每个 Operation 执行前，基于**最新** Working_Copy 检查章节/格式）。

#### Acceptance Criteria

1. THE Static_Plan_Validator SHALL 校验意图是否受支持、必填槽位是否齐备、意图与目标的能力组合是否合法、以及约束是否互相冲突。
2. THE Static_Plan_Validator SHALL 用 Projected_Sections（当前章节 ∪ 前序 add_section 将创建的章节）校验章节类 Operation：目标在投影集内 → 通过；不在 → 提前标 `section_not_found`。这样既支持「先新增 Discussion 再改写 Discussion」，又能在计划期就拦下「润色不存在的 FooBar」。
3. WHEN Pre_Operation_Validator 在某 Operation 执行前校验, THE Pre_Operation_Validator SHALL 基于最新 Working_Copy 检查其目标章节是否存在、格式前置条件（如 format_docx 需 docx）是否满足。
4. WHEN 一个 convert_format Operation 的目标格式与当前文件格式相同, THE 系统 SHALL 标记为无需转换并给出原因。
5. WHERE 校验发现 Operation 存在互相冲突的约束, THE 系统 SHALL 标记冲突并要求澄清或拒绝执行。
6. IF 任一 Operation 在其执行前校验为不可执行, THEN THE 系统 SHALL 不执行该 Operation，并明确告知用户原因。

### Requirement 6: 只补缺口的澄清

**User Story:** 作为用户，我希望系统只就它确实不知道的信息问我，且尽量一次问清，而不是把我已经说过的又问一遍、或一个字段问一轮。

#### Acceptance Criteria

1. WHEN 执行某 Operation 所需的必填字段在抽取后处于 `user_absent | ambiguous | invalid | unavailable`, THE Clarifier SHALL 就这些字段向用户追问。
2. THE Clarifier SHALL NOT 询问 `field_status == present && source == user_explicit` 的字段（用户已明确提供的）。
3. WHERE 一个 Operation 存在多个缺失字段, THE Clarifier SHALL 在一次澄清交互中一并询问，而非逐字段多轮追问。
4. WHEN 用户在澄清中给出答复, THE 系统 SHALL 把答复回填到对应字段并标记来源为 `user_confirmed`，随后**重跑 Static_Plan_Validator**。
5. THE Clarifier SHALL 最多进行 2 轮澄清；WHEN 达到轮次上限仍有必填字段缺失或产生新冲突, THE 系统 SHALL 把计划保存为 draft 或取消，而非无限追问。
6. WHERE 所有 Operation 的必填字段均已具备, THE 系统 SHALL 跳过澄清直接进入计划确认。

### Requirement 7: 执行前的计划确认

**User Story:** 作为用户，我希望在系统动手改我的论文之前，先看到它打算做哪几步、分别针对什么，这样万一它理解错了我能及时纠正。

#### Acceptance Criteria

1. WHEN Task_Plan 校验通过且无缺失字段, THE 系统 SHALL 在执行前向用户呈现操作计划摘要（各 Operation 的意图与目标）。
2. WHERE 存在多个 Operation, THE 计划摘要 SHALL 按将被执行的顺序列出它们。
3. WHEN 用户确认计划, THE 系统 SHALL 按序执行各 Operation。
4. WHEN 用户否决计划, THE 系统 SHALL 不执行任何写操作。
5. WHEN 用户修改计划（如「第二步不转 Word，改成 Markdown」）, THE 系统 SHALL 将当前计划标记为 rejected 并**生成一个新版本（plan_version 递增）**，对新版本重新抽取/校验/确认，而非原地修改旧计划。
6. THE Task_Plan SHALL 携带 `schema_version / plan_version / analyzer_version / prompt_version` 以支持版本化与可追溯。
7. WHEN 用户在 Unmapped 处置或确认阶段补充说明/修改计划、或澄清答复产生新意图、或发生 needs_replan, THE 系统 SHALL 回到规划循环顶端**重新运行 Analyzer/Builder/Validator** 生成新版本，而非原地修改；该循环 SHALL 以 `plan_revision_limit`（默认 3）为界，超限返回 `revision_limit_exceeded`。
8. WHERE 计划仅含单一且低风险的 Operation, THE 系统 MAY 省略显式确认（由配置 `require_confirmation` 决定，默认 True），但仍须遵守其余需求。

### Requirement 8: 顺序执行与失败/取消隔离

**User Story:** 作为用户，我希望多步计划里前一步失败不会带着错误继续跑后面的步骤，取消时也不会留下改了一半的文档。

#### Acceptance Criteria

1. WHEN 用户确认的 Task_Plan 含一个或多个 Operation, THE 系统 SHALL 在**一次** `coordinator.run` 内按用户顺序**一次执行一个** Operation（每个 Operation 一个子 span），且 SHALL NOT 并发执行任何改动同一 Working_Copy 的 Operation。
2. WHEN 计划开始执行前, THE FileBackedSession SHALL 通过 `create_baseline(label)->Session_Baseline` 建立可回滚基线；git 可用走 commit（记录 commit）、否则走文件副本快照。`restore_baseline(baseline)` 支持回滚。
3. WHEN Pre_Operation_Validator 在执行期发现某 Operation 因前序结果而不可执行, THE 系统 SHALL **停止计划**、置状态 `needs_replan`、基于最新状态生成 `plan_version+1` 供用户重新确认，而**不在**执行循环内部修改已确认计划或重新澄清。
4. IF 某个 Operation 执行失败或被 Gate 拒, THEN THE 系统 SHALL 停止后续 Operation、保留前序已成功的写入、置状态 `partially_completed`，未执行的后续 Operation 记 `skipped_due_to_previous_failure`。
5. WHEN 计划以 partially_completed 结束, THE 系统 SHALL 允许用户选择「保留已完成步骤」或「回滚整个计划到基线」（`rolled_back`）。
6. WHEN 用户在计划执行过程中取消, THE 系统 SHALL 遵循既有取消模型（OperationCancelled 上抛至 Coordinator）；**单个 Operation 的写回是原子的**；未执行的后续 Operation 记 `skipped_due_to_cancellation`；即便取消，Plan_Execution_State SHALL 仍能产出已完成列表与回滚选项。
7. THE 每个 Operation 的写回 SHALL 复用既有 working-copy 原子写与 Gate；本功能不改变 gate 校验语义。
8. WHEN 一个 Operation 的执行器返回业务失败（如 `Result.ok == False`，含 Gate 未过）, THE IntentHandler SHALL 把该 Operation 状态置为 `rejected`（而非仅依赖 Coordinator 的 run_status），且系统 SHALL NOT 将其计入计划成功。
9. THE 计划终态 SHALL ∈ `{done, partially_completed, failed, cancelled, rejected, rolled_back, needs_replan, revision_limit_exceeded}`；`failed` SHALL 保留 `failure_category ∈ {execution_failed, validation_rejected}` 以区分系统失败与质量未达标。
10. THE plan 级回滚 SHALL 明确 `rollback_scope = "working_copy_only"`（Session_Baseline 只覆盖 Working_Copy）；convert 产出的 `output/` 新文件不在回滚范围内，convert 失败时对**本轮新产物**做 best-effort 清理，**不删**执行前已存在的用户文件。
11. WHEN Pre_Operation_Validator 阻断某步, THE 该 Operation 状态 SHALL 记为 `blocked_needs_replan`，其后未执行步骤记 `skipped_due_to_replan`。

### Requirement 9: 能力前置模拟与 Convert 终止约束

**User Story:** 作为用户，我希望系统按我说的顺序做；当我说的顺序在能力上走不通时，它能提醒我并在我同意后重排，而不是静默改序或执行到一半失败。

**说明:** v1 里 `execute_convert` 只产出新文件、不切换会话到输出文件，因此**不支持** convert 之后在新格式上继续编辑/排版。

#### Acceptance Criteria

1. THE 系统 SHALL 按用户表达顺序，用当前 artifact 状态**逐步模拟**每个 Operation 的 Capability_Precondition 是否满足。
2. IF 某 Operation 在其位置上的 Capability_Precondition 不满足（如当前已非 docx 却要 format_docx）, THEN THE 系统 SHALL 提出重排建议或拒绝，并**在用户确认后才重排**，绝不静默改序。
3. WHERE 一个 Task_Plan 中 convert_format 之后**存在任何** Operation, THE 系统 SHALL 判定该计划在 v1 不可执行，并提示用户「convert 需作为最后一步，后续操作请分两轮进行」。（用「其后有无操作」判定，不引入「是否依赖旧格式」这类未定义的依赖判断。）
4. THE convert_format Operation SHALL 作为其所在计划的**终止操作**；「转成双栏 Word」等需求由 convert 的 presets 一次完成。

### Requirement 10: Shadow Mode 灰度迁移

**User Story:** 作为维护者，我希望新理解层先与现有 Router 并行、只观察不接管，等我确认它更好之后再切换，这样迁移风险可控。

理解层有三态：`intent_planning_mode ∈ off | shadow | active`（默认 `off`，即完全走现有 Router）。

#### Acceptance Criteria

1. WHERE mode=shadow, THE 系统 SHALL 对每条自然语言消息**同时**运行现有 Router 与新理解层，并记录二者意图判定与操作拆分的差异供离线评估。
2. WHILE mode=shadow, THE 新理解层 SHALL NOT 向用户发起澄清、SHALL NOT 请求计划确认、SHALL NOT 修改 Working_Copy；实际执行与响应仍完全走现有 Router 路径。
3. IF 新理解层在 shadow 下失败, THEN THE 失败 SHALL NOT 影响旧路径对用户的响应。
4. WHERE mode=active, THE 新理解层 SHALL 接管理解/规划/澄清/确认/执行；WHERE mode=off, THE 系统 SHALL 完全不运行新理解层。

### Requirement 11: 向后兼容

**User Story:** 作为维护者，我希望引入理解层不破坏现有的测试、mock 路径与五个意图的既有对外行为。

#### Acceptance Criteria

1. THE 本功能 SHALL 在既有全部测试通过的前提下交付。
2. WHERE 新理解层未接管（Shadow_Mode 或未启用）, THE 系统 SHALL 保持既有意图执行的对外行为不变。
3. THE 本功能 SHALL 复用现有 `execute_polish/rewrite/add_section/convert/format` 而不改变其对外契约。
4. THE 本功能 SHALL 在 mock provider 装配下可端到端运行（不依赖真实 LLM）。

### Requirement 12: 观测与可评测

**User Story:** 作为维护者，我希望理解层的每一步都能被 trace 记录、并有一套 golden 用例量化它的好坏，这样它跑歪时我能定位、迭代时我有标尺。

#### Acceptance Criteria

1. THE 系统 SHALL 使 Trace 从收到 User_Turn 开始，覆盖操作分析、槽位抽取、计划校验、澄清、以及每个 Operation 的执行。
2. THE 系统 SHALL 复用既有观测脊柱（EventBus / Coordinator / trace.json），且遵守既有隐私约束（脱敏、原文不落 trace）。
3. THE 本功能 SHALL 附带一组 golden 用例，覆盖单意图标准/非标准表达、多意图、上下文引用、模糊表达、冲突约束、不支持任务。
4. THE 评测 SHALL 报告以下指标：意图准确率、操作拆分准确率、目标（章节/格式）抽取准确率、约束抽取召回率、不必要追问率、Silent_Loss 率。
5. THE Silent_Loss 率指标 SHALL 以零为验收目标。

### Requirement 13: 理解层的预算约束

**User Story:** 作为维护者，我希望理解层（尤其 shadow 模式）有明确的调用与规模上限，这样一次复杂请求或灰度对比不会把延迟和成本放大到失控。

#### Acceptance Criteria

1. THE 预算 SHALL 拆分为：`provider_compute_budget`（仅覆盖 provider 调用）、`plan_revision_limit`（默认 3）、调用上限**按修订计**（`max_analyzer_calls_per_revision=1`、`max_extractor_calls_per_revision=max_operations`、`max_total_llm_calls_per_turn`）、`max_operations=5`、`max_input_chars`。
2. THE `provider_compute_budget` SHALL NOT 把用户交互等待时间计入——**不在 Turn 开始激活持续墙钟预算**，而是每次 provider 调用单独激活 deadline 或累计 `provider_elapsed`。
3. WHEN 任一预算被突破, THE 系统 SHALL 停止进一步的理解层调用，向用户说明并给出降级处理（拆分请求或走规则快路径）。
4. WHERE mode=shadow, THE 预算约束 SHALL 同样适用，以免灰度对比放大成本。
