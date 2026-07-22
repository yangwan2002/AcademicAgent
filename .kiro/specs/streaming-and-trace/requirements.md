# Requirements Document

## Introduction

本功能（`streaming-and-trace`）为学术论文写作 Agent 同时引入「流式输出」与「运行追踪（Run Trace）」两项能力。二者共享同一条事件管道：一次生成/一次运行产生一串结构化事件，**流式渲染器**与 **Trace 记录器**只是这串事件的两个下游消费者（fan-out）。

设计的核心约束来自本系统固有的「单一真相是 working copy、源文件在用户 save 前不动、writer→gate→(repair 重试)」模型：流式呈现的永远是**待 gate 校验的候选**，绝不能实时污染 working copy 或任何持久状态；取消或失败时半截文本必须被安全丢弃。

本需求以现有基础为前提，避免重复造轮子：
- `providers/llm/base.py` 已定义 `StreamChunk(kind ∈ content/thinking)`、`CancellationToken`/`CombinedCancellationToken`、`stream()` 协议；`openai_compatible`/`anthropic`/`mock` 均已实现 `stream()`；`complete()` 支持 `on_delta` 回调。
- `providers/llm/resilient.py` 已实现流式重试语义（取消视为正常终态；已产出增量后失败不重试；未产出增量且可重试才重试）。
- `observability/events.py` 已有 `EventKind`/`EventSink`/`Event` 事件底座（`Event` 已含 `trace_id`/`span_id`/`parent_span_id`/`ts`/`duration_ms` 字段与 `SPAN`/`LLM_DELTA`/`LLM_THINKING` 等种类），LLM 层已发事件，但 V2 的 SOP/gate 尚未发自定义事件，也没有 Trace 记录器。

因此本功能主要新增：SOP/gate 层的事件发射、统一的事件分发（fan-out）、流式渲染器（消费者）、Trace 记录器（消费者）、Run_Trace 的 span 树落盘，以及可扩展的 `Trace_Exporter` 接口。

## Glossary

- **Event_Source**: 业务各层（router、interview、retrieval、generator、gate、LLM provider、SOP）通过 `EventSink.emit(event)` 发出结构化 `Event` 的统一出口。
- **Event_Dispatcher**: 实现 `EventSink` 协议的分发器，接收单个 `Event` 后同步 fan-out 给零个或多个已注册的下游 `EventSink`（如 `Streaming_Renderer` 与 `Trace_Recorder`）。
- **Streaming_Renderer**: 消费事件并向用户界面（当前为控制台 REPL）实时呈现的下游 `EventSink`。
- **Trace_Recorder**: 消费事件并构建 `Run_Trace`（span 树）、最终将其落盘的下游 `EventSink`，同时实现 `Trace_Exporter` 的默认本地导出。
- **Run_Trace**: 一次运行（一条用户消息从 router 到最终 commit/失败）的完整追踪记录，组织为 span 树，序列化为 `runs/<run_id>/trace.json`。
- **Span**: `Run_Trace` 中的一个带起止时间、`span_id`、`parent_span_id` 的节点，代表一个可追踪步骤（如 router、interview、一次生成尝试、一次 gate 校验）。
- **Trace_Exporter**: 导出 `Run_Trace` 的接口抽象；本功能仅提供落本地 JSON 的默认实现，为未来 Langfuse/OpenTelemetry 等导出预留扩展点。
- **Candidate_Text**: 一次生成尝试流式产出、尚未通过 gate 校验的候选文本，仅缓存于内存 `Candidate_Buffer`，不写入 working copy。
- **Candidate_Buffer**: 累积单次尝试 content 增量的内存缓冲区；通过 gate 后其内容才交由 SOP 写回 working copy。
- **Working_Copy**: 系统的单一真相文本副本；源文件在用户显式 save 前不改动。
- **Generator**: `generate/generator.py` 中「生成 → gate → 回灌重试」循环的执行体。
- **Gate**: 对候选文本做受控校验（如 `PolishGate`）的组件，返回 pass/fail 与 violations。
- **Run_Id**: 一次运行的唯一标识，用于命名 `runs/<run_id>/` 目录，且等于该运行 `Run_Trace` 的 `trace_id`。
- **Text_Preview**: 敏感文本（prompt、response、diff、论文正文、interview 答案、violations、error 消息）的安全短预览，长度上限 500 字符。
- **Content_Hash**: 敏感文本的哈希摘要（如 SHA-256 十六进制串），用于在不存全文的前提下标识内容。
- **Sanitizer**: 统一的脱敏组件；对任何将被持久化进 Run_Trace 或 Event_Journal 的文本字段（prompt、response、diff、论文正文、interview 答案、violations、error 消息、thinking）施加 Content_Hash + Text_Preview 处理并剔除疑似密钥。
- **Thinking_Content**: 模型推理增量（`StreamChunk.kind == thinking`）；非交付物，默认不展示、不持久化原文。
- **Event_Id**: 单个 Event 的全局唯一标识。
- **Sequence_Number**: 同一 Run 内 Event 的单调递增序号，用于确定事件的全序（供 fan-out、崩溃后重放与 span 重建排序）。
- **Schema_Version**: Event 与 Run_Trace 序列化格式的版本号，用于未来格式演进与向后兼容读取。
- **Attempt_Id**: 一次生成尝试（Gate_Retry 的一轮）的标识，用于把该次尝试的 content/thinking 增量、gate 结果关联到同一次尝试。
- **Provider_Retry**: 由 `ResilientLLMProvider` 发起的、针对底层网络/限流/5xx 等瞬时错误的同一次生成尝试内重试（不改变 Attempt_Id）。
- **Gate_Retry**: 由 Generator 发起的、因 Gate 校验失败而重新生成的新一次尝试（递增 Attempt_Id）。
- **Working_Copy_Update**: 候选通过 Gate 后，Generator/SOP 把候选内容写入 Working_Copy 文件（session 目录内 `current.<ext>`）的动作。
- **Workspace_Commit**: 把当前 Working_Copy 状态提交为一个可回滚版本（git commit / 快照）的动作。
- **Source_Save**: 用户显式 `:save` 时，把 Working_Copy 内容写回用户源文件的动作。
- **Event_Journal**: append-only 的增量事件日志（如 `runs/<run_id>/events.ndjson`），随事件发生即时追加，使进程中途崩溃后仍能重建部分 Run_Trace。
- **Span_State**: 一个 Span 的终态之一：`ended`（正常结束）/ `cancelled`（被取消）/ `failed`（因错误终止）；配合 `started` 与结束时间刻画生命周期。

## Requirements

### Requirement 1: 统一事件管道与 fan-out 分发

**User Story:** 作为系统集成者，我希望一次运行的事件从单一事件源同时分发给实时渲染器与 Trace 记录器，这样流式显示与运行追踪共用同一条数据来源，不会各写一套。

#### Acceptance Criteria

1. THE Event_Dispatcher SHALL 实现 `observability/events.py` 中定义的 `EventSink` 协议。
2. WHEN Event_Dispatcher 的 `emit` 被调用并传入一个 Event, THE Event_Dispatcher SHALL 将该 Event 依次转发给每一个已注册的下游 EventSink。
3. WHERE 已注册的下游 EventSink 数量为零, THE Event_Dispatcher SHALL 接受该 Event 并不产生任何转发。
4. WHEN Event_Dispatcher 向多个下游 EventSink 转发同一个 Event, THE Event_Dispatcher SHALL 向每个下游传递字段值相等的 Event。
5. IF 某个下游 EventSink 的 `emit` 抛出异常, THEN THE Event_Dispatcher SHALL 捕获该异常并继续向其余下游 EventSink 转发该 Event。
6. THE Streaming_Renderer SHALL 实现 `EventSink` 协议并作为 Event_Dispatcher 的一个下游消费者。
7. THE Trace_Recorder SHALL 实现 `EventSink` 协议并作为 Event_Dispatcher 的一个下游消费者。

### Requirement 2: 流式呈现候选而非定稿

**User Story:** 作为论文作者，我希望流式看到的文本被明确标示为「待校验候选」，这样我不会误以为它已经写进我的文档。

#### Acceptance Criteria

1. WHILE 一次生成尝试正在流式产出增量, THE Generator SHALL 将 content 增量累积至内存 Candidate_Buffer 而不写入 Working_Copy。
2. WHEN Streaming_Renderer 开始呈现某次生成尝试的增量, THE Streaming_Renderer SHALL 显示表明该文本为待 gate 校验候选的状态标识。
3. WHILE 一次生成尝试的候选文本尚未通过 Gate 校验, THE Generator SHALL 保持 Working_Copy 内容不变。
4. WHEN 某次生成尝试的候选文本通过 Gate 校验, THE Generator SHALL 将该次尝试对应的 Candidate_Buffer 内容作为该次生成的结果返回给 SOP 层。
5. WHERE 一次运行内任一生成尝试均未通过 Gate 校验, THE Generator SHALL 保持 Working_Copy 内容不变。

### Requirement 3: gate 失败与重试的透明呈现

**User Story:** 作为论文作者，我希望在流式过程中清楚看到 gate 为什么拒绝了某次候选、以及系统正在重新尝试，这样我理解每一次重试的原因。

#### Acceptance Criteria

1. WHEN 一次生成尝试的候选文本经 Gate 校验通过, THE Generator SHALL 发出携带该次尝试序号且标示校验通过的 Event。
2. WHEN 一次生成尝试的候选文本经 Gate 校验失败, THE Generator SHALL 发出携带该次尝试序号与该次 violations 列表的 Event。
3. WHEN Gate 校验失败后 Generator 在剩余重试额度内发起下一次生成尝试, THE Generator SHALL 发出标示进入新一次生成尝试且携带新尝试序号的 Event。
4. WHEN Streaming_Renderer 接收到标示 Gate 校验失败的 Event, THE Streaming_Renderer SHALL 呈现该次校验的 violations 内容。
5. WHILE Generator 正在流式产出某次重试的候选增量, THE Streaming_Renderer SHALL 将该次重试的候选增量与此前失败尝试的候选增量分开呈现。

### Requirement 4: 取消与失败时不污染状态

**User Story:** 作为论文作者，我希望在 Ctrl-C 取消或生成失败时，已经流出来的半截文本不会被拼进我的文档，这样我的文档状态始终干净可信。

#### Acceptance Criteria

1. WHEN 用户在流式过程中触发取消, THE Generator SHALL 停止累积后续增量并丢弃当前 Candidate_Buffer 内容。
2. IF 一次运行因取消而终止, THEN THE Generator SHALL 保持 Working_Copy 内容不变。
3. IF 一次生成尝试在流式产出过程中失败, THEN THE Generator SHALL 丢弃该次尝试的 Candidate_Buffer 内容且不写入 Working_Copy。
4. WHEN 一次运行因取消而终止, THE Streaming_Renderer SHALL 呈现标示本次运行已取消的状态。
5. WHEN 一次运行因取消或失败而终止, THE SOP SHALL 保持源文件、Facts 与撤销栈不变。
6. IF 一次生成尝试在已产出至少一个增量后失败, THEN THE Generator SHALL 遵循 `ResilientLLMProvider.stream` 的既有语义不重发已产出增量。
7. IF 取消发生在候选已通过 Gate 校验之后、Working_Copy_Update 完成之前, THEN THE Generator SHALL 放弃本次 Working_Copy_Update 并保持 Working_Copy 内容不变。
8. WHEN 一次 Working_Copy_Update 已开始写入, THE Generator SHALL 使该次写入以「完全写入」或「完全不写入」之一终结（不留下部分写入的中间态）。

### Requirement 5: thinking 与 content 分离呈现（thinking 默认不展示、不持久化）

**User Story:** 作为论文作者，我希望模型的「思考」内容默认不干扰我、也不被存下来，只有我主动开启时才分区显示，这样候选正文清晰、且冗长/敏感的推理不会泄露或喧宾夺主。

#### Acceptance Criteria

1. WHERE Thinking_Content 的呈现未被显式开启, THE Streaming_Renderer SHALL 不向用户显示 `kind` 为 `thinking` 的增量。
2. WHERE Thinking_Content 的呈现被显式开启, THE Streaming_Renderer SHALL 在与 content 相区分的呈现区域显示 `kind` 为 `thinking` 的增量。
3. WHEN Streaming_Renderer 接收到 `kind` 为 `content` 的 StreamChunk 对应事件, THE Streaming_Renderer SHALL 在候选正文呈现区域显示该 content 增量。
4. THE Generator SHALL 仅将 `kind` 为 `content` 的增量累积进 Candidate_Buffer。
5. THE Generator SHALL 不将 `kind` 为 `thinking` 的增量累积进 Candidate_Buffer。
6. THE Trace_Recorder SHALL 不在 Run_Trace 或 Event_Journal 中持久化 Thinking_Content 的原文。

### Requirement 6: 非 LLM 步骤的进度与状态反馈

**User Story:** 作为论文作者，我希望路由、访谈、文献检索、gate 校验这些非生成步骤也有进度反馈，这样界面不会看起来像卡死了。

#### Acceptance Criteria

1. WHEN Router 完成一次路由决策, THE Router SHALL 发出携带所选意图与抽取到的槽位（section_id/target_ext/presets/title）的 Event。
2. WHEN Interview 向用户提出一个问题并接收到回答, THE Interview SHALL 发出标示该问答步骤的 Event。
3. WHEN 检索步骤开始执行, THE SOP SHALL 发出标示检索开始的 Event。
4. WHEN 检索步骤结束, THE SOP SHALL 发出携带检索结果条目数量的 Event。
5. WHEN 一次 Gate 校验开始执行, THE Generator SHALL 发出标示该 Gate 校验开始的 Event。
6. WHEN 一次 Gate 校验结束, THE Generator SHALL 发出携带该 Gate 校验 pass/fail 结果的 Event。

### Requirement 7: Run_Trace 的 span 树记录

**User Story:** 作为开发者，我希望一次运行被记录为一棵 span 树，涵盖路由、访谈、每次生成尝试、每个 gate、重试次数、最终结果与错误，这样我能事后完整复盘一次运行发生了什么。

#### Acceptance Criteria

1. WHEN 一次运行开始, THE Trace_Recorder SHALL 创建一个以该运行 Run_Id 为 `trace_id` 的根 Span。
2. WHEN Trace_Recorder 接收到 Router 决策 Event, THE Trace_Recorder SHALL 记录一个包含所选意图与抽取到的槽位的 Span。
3. WHEN Trace_Recorder 接收到 Interview 问答 Event, THE Trace_Recorder SHALL 记录包含该问答内容的 Span。
4. WHEN Trace_Recorder 接收到一次生成尝试的相关 Event, THE Trace_Recorder SHALL 记录一个包含 prompt 预览、prompt Content_Hash、model 标识、response 预览、token 用量与延迟的 Span。
5. WHEN Trace_Recorder 接收到一次 Gate 校验结果 Event, THE Trace_Recorder SHALL 在该次生成尝试 Span 下记录该 Gate 的 pass/fail 结果与 violations。
6. WHEN 一次运行结束, THE Trace_Recorder SHALL 在根 Span 记录本次运行的生成尝试总次数。
7. WHEN 一次运行以写回 Working_Copy 成功结束, THE Trace_Recorder SHALL 记录最终 commit 状态与 diff 摘要。
8. WHEN 一次运行因错误终止, THE Trace_Recorder SHALL 记录该错误的类别与错误预览。
9. THE Trace_Recorder SHALL 为每个 Span 记录 `span_id`、`parent_span_id`、开始时间与持续时长。
10. WHEN 一个 Span 结束, THE Trace_Recorder SHALL 为该 Span 记录一个 Span_State（`ended` / `cancelled` / `failed`）与结束时间。
11. WHILE 一个 Span 已开始但尚未终结, THE Trace_Recorder SHALL 使该 Span 处于未终结（无 Span_State）状态。

### Requirement 8: Trace 隐私约束与本地落盘

**User Story:** 作为论文作者，我希望追踪记录默认不保存我的论文全文和密钥，只存哈希与短预览并落在本地，这样我的学术内容不会泄露。

#### Acceptance Criteria

1. THE Trace_Recorder SHALL 通过 Sanitizer 处理所有将被持久化的文本字段（prompt、response、diff、论文正文、interview 答案、violations、error 消息），使其仅以 Content_Hash 与 Text_Preview 形式记录。
2. THE Sanitizer SHALL 将任一 Text_Preview 截断至不超过 500 字符。
3. THE Trace_Recorder SHALL 不在 Run_Trace 或 Event_Journal 中记录论文正文全文。
4. THE Sanitizer SHALL 不在 Run_Trace 或 Event_Journal 中记录 API 密钥。
5. THE Sanitizer SHALL 对 interview 答案、violations 与 error 消息施加与正文一致的脱敏处理（Content_Hash + Text_Preview）。
6. WHEN 一次运行结束, THE Trace_Recorder SHALL 将 Run_Trace 序列化为 JSON 并写入本地路径 `runs/<run_id>/trace.json`。
7. WHERE 目标目录 `runs/<run_id>/` 尚不存在, THE Trace_Recorder SHALL 在写入前创建该目录。
8. THE Trace_Recorder SHALL 使用 UTF-8 编码写入 `trace.json`。

### Requirement 9: 可扩展的 Trace_Exporter 接口

**User Story:** 作为开发者，我希望有一个 Trace_Exporter 接口，未来能把 span 导出到 Langfuse 或 OpenTelemetry，但现在不引入任何外部服务。

#### Acceptance Criteria

1. THE Trace_Exporter SHALL 定义一个接收 Run_Trace 的导出操作抽象。
2. THE 本地 JSON 导出实现 SHALL 实现 Trace_Exporter 接口。
3. THE 本功能 SHALL 仅提供落本地 `trace.json` 的 Trace_Exporter 实现。
4. THE 本功能 SHALL 不引入对 Langfuse 或 OpenTelemetry 等外部服务的运行时依赖。

### Requirement 10: 单次流式调用内的增量一致性

**User Story:** 作为开发者，我希望单次流式调用产出的 content 增量按序拼接后，等于该次调用自身聚合出的最终文本，这样候选文本不因流式而与该次调用的完整结果不一致。

**说明:** 真实 LLM 存在采样随机性，两次**独立**的模型请求（无论流式与否）本就不保证产出相同文本；因此本需求只约束**同一次调用内部**的一致性，不要求跨调用（流式 vs 另一次 `complete()`）文本相等。

#### Acceptance Criteria

1. WHEN 一次流式生成调用在无取消且无错误的条件下完成, THE 流式路径 SHALL 使该次调用按序拼接的所有 `content` 增量等于该次调用聚合返回的最终 `content`。
2. THE Candidate_Buffer 的最终内容 SHALL 等于本次生成尝试所有 `content` 增量按接收顺序拼接的结果。
3. THE Generator SHALL 依据 Candidate_Buffer 的最终内容而非任何 thinking 增量执行 Gate 校验。
4. THE 本功能 SHALL 不要求两次独立模型请求（流式与 `complete()`）产出相等文本。

### Requirement 11: 向后兼容既有测试与 mock 路径

**User Story:** 作为维护者，我希望新增流式与追踪能力不破坏现有 65 个测试与 mock 路径，这样已有端到端流程与测试保持绿色。

#### Acceptance Criteria

1. THE 本功能 SHALL 在既有 65 个测试全部通过的前提下交付。
2. WHERE 未注册任何下游 EventSink（即沿用默认 NullSink 装配）, THE Event_Source SHALL 保持既有意图执行的对外行为不变。
3. THE 本功能 SHALL 复用 `MockLLMProvider` 已原生实现的 `stream()` 使流式路径在 mock 装配下可端到端运行。
4. THE 本功能 SHALL 复用 `observability/events.py` 既有的 `EventKind`、`EventSink`、`Event` 定义而不改变其既有字段语义。
5. WHERE Trace 记录未被启用, THE SOP SHALL 保持既有意图执行的对外行为不变。

### Requirement 12: Event 信封字段（ID / 序号 / Schema 版本 / Attempt）

**User Story:** 作为开发者，我希望每个事件都带唯一 ID、单调序号、Schema 版本与所属尝试标识，这样事件可去重、可排序、可跨版本读取、可关联到具体某次生成尝试。

#### Acceptance Criteria

1. WHEN Event_Source 发出一个 Event, THE Event SHALL 携带一个全局唯一的 Event_Id。
2. WHEN Event_Source 在同一 Run 内发出多个 Event, THE Event_Source SHALL 为它们赋予单调递增的 Sequence_Number。
3. THE Event SHALL 携带一个 Schema_Version。
4. WHEN 一个 Event 属于某次生成尝试（如 content/thinking 增量、gate 结果）, THE Event SHALL 携带该次尝试的 Attempt_Id。
5. WHEN Trace_Recorder 序列化 Run_Trace, THE Run_Trace SHALL 携带一个 Schema_Version。
6. WHEN Trace_Recorder 依据接收到的 Event 重建顺序, THE Trace_Recorder SHALL 依据 Sequence_Number 确定事件全序。

### Requirement 13: 区分 Provider Retry 与 Gate Retry

**User Story:** 作为开发者，我希望「底层网络重试」与「gate 失败后重新生成」在事件与追踪中被区分开，这样我能分清一次运行里到底是网络在抖还是模型在被 gate 打回。

#### Acceptance Criteria

1. WHEN `ResilientLLMProvider` 因瞬时错误在同一次生成尝试内重试, THE 对应 Event SHALL 标示其为 Provider_Retry 且不改变 Attempt_Id。
2. WHEN Generator 因 Gate 校验失败发起新一次生成尝试, THE 对应 Event SHALL 标示其为 Gate_Retry 且携带递增后的 Attempt_Id。
3. WHEN 一次运行结束, THE Trace_Recorder SHALL 分别记录 Provider_Retry 次数与 Gate_Retry 次数。

### Requirement 14: 增量 Event Journal（崩溃可恢复）

**User Story:** 作为开发者，我希望事件在发生时就被增量落盘，这样即使进程中途崩溃，也不会丢失整条 Trace，能重建到崩溃前的状态。

#### Acceptance Criteria

1. WHEN Event_Source 发出一个 Event, THE Trace_Recorder SHALL 在该 Event 发生时将其以 append-only 方式追加写入 Event_Journal（`runs/<run_id>/events.ndjson`）。
2. THE Trace_Recorder SHALL 使 Event_Journal 的每条记录经 Sanitizer 处理（与 Run_Trace 同等隐私约束）。
3. IF 进程在一次运行结束前终止, THEN THE Event_Journal SHALL 保留该运行在终止前已发出的全部 Event。
4. THE Trace_Recorder SHALL 能够仅依据 Event_Journal（按 Sequence_Number 排序）重建该运行的部分 Run_Trace。

### Requirement 15: Event Ownership（增量事件唯一发射源）

**User Story:** 作为开发者，我希望 LLM 增量事件只由唯一一层发射，这样不会出现同一段增量被重复记录或重复渲染。

#### Acceptance Criteria

1. THE 本功能 SHALL 为每一类 LLM 增量事件（content / thinking）指定唯一的发射所有者层。
2. WHEN 一段 content 或 thinking 增量在 provider 栈中流经多层, THE 非所有者层 SHALL 不为该增量重复发射增量事件。
3. WHEN 一次生成尝试完成, THE Streaming_Renderer SHALL 使同一段 content 增量至多被呈现一次。
4. WHEN 一次生成尝试完成, THE Trace_Recorder SHALL 使同一段增量至多被记录一次。

### Requirement 16: 区分 Working Copy Update / Workspace Commit / Source Save

**User Story:** 作为开发者，我希望「写 working 文件」「提交可回滚版本」「保存回源文件」这三个不同层级的持久化在事件与追踪中被分别标识，这样我能准确知道一次运行到底把数据落到了哪一层。

#### Acceptance Criteria

1. WHEN 候选通过 Gate 后 Generator/SOP 写入 Working_Copy, THE 对应 Event SHALL 标示其为 Working_Copy_Update。
2. WHEN SOP 提交一个可回滚版本（git commit / 快照）, THE 对应 Event SHALL 标示其为 Workspace_Commit。
3. WHEN 用户显式 save 使内容写回源文件, THE 对应 Event SHALL 标示其为 Source_Save。
4. THE Trace_Recorder SHALL 在 Run_Trace 中分别记录 Working_Copy_Update、Workspace_Commit 与 Source_Save 三类事件，且不将其混为同一类。
