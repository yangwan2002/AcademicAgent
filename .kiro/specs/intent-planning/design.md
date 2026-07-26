# Design Document

## Overview

本功能在现有 agent 前面加一层**用户请求理解层**：把一条自然语言消息解析为**有序操作列表（Task_Plan）**，抽槽、校验、只补缺口地澄清、执行前确认，再在**一次** `coordinator.run` 内**线性串行**执行各 Operation（每个 Operation 一个子 span），复用现有 `execute_*`。

三段式理解（不用巨型 prompt）：Operation_Analyzer（切分 + Atomic_Requirement，按用户序，不画边）→ PreExtractor（按意图抽槽，调用态/字段态分离）→ PlanBuilder + Static_Plan_Validator（保留用户序 + 投影校验）。

铁律与复用：不重写 `execute_*`/Gate/working-copy+git/观测脊柱；`intent_planning_mode ∈ off|shadow|active`（默认 off）；本 spec 不做 Candidate/Approval 正式化、Project/Resume、Evidence/语义 Gate、依赖 DAG/拓扑、目录重构（→ P1）。

## 关键设计决策

### D0. 严格保留用户顺序 + 能力前置模拟；v1 不用 DAG/拓扑

- Analyzer 只输出「按用户序的 Operation + Atomic_Requirement + unmapped_text」，不画依赖边。
- 执行序**默认等于用户序**；按当前 artifact 状态逐步模拟 Capability_Precondition，不满足 → 提重排建议或拒绝，**用户确认后才重排**，绝不静默改序。
- 不引入固定意图偏序（反例：DOCX「先排版再转 md」正确序 FORMAT→CONVERT，固定偏序会错成 CONVERT→FORMAT）。v1 用有序列表即可。

### D1. Convert 是计划的终止操作

- 现状 `execute_convert` 只在 `output/` 生成**新文件**，不改 working copy、不重绑 `FileBackedSession`；故不支持 convert 后在新格式上继续编辑。
- 因此 convert_format 必须是其计划的**终止操作**；「转双栏 Word」由 convert 的 `presets` 一次完成。**#11 更明确**：convert_format 之后**存在任何 Operation** 即判该计划不可执行、提示分两轮（不引入「是否依赖旧格式」这种未定义的依赖判断）。不引入 `switch_working`/重绑/转换回滚。

### D2. 一个 Turn 一个 Coordinator Run，包住**整个** Turn；Operation 为子 span

- `coordinator.run` 每次新建 `run_id/EventBus/RunContext/recorder/trace`，故必须**一个 Turn 一个 run**，且该 run 包住**整段自然语言 Turn**（analyze→extract→build→validate→clarify→confirm→execute），才能兑现「Trace 从 TURN_START 覆盖全旅程」。

execute_turn 内部是一个**有界规划循环**（补充/修改/needs_replan 都回到循环顶重建计划，绝不原地改已确认计划）。TurnService 在 run 外持有 `Plan_Execution_State`，以便取消/异常后仍能生成最终报告：

```python
def execute_turn(state, bus, cancel_token) -> PlanExecutionReport:
    revision = PlanRevisionRequest(previous_plan=None, revision_text=text, revision_reason="initial")
    remaining_operations = None
    for _ in range(max_plan_revisions):                # 默认 3
        # rule_analyze 也在 run 内（#11：完整 Trace）
        raw_ops, canonical_reqs = analyze_or_fastpath(revision)     # 失败按 D3；修订=旧计划+修订文本，不丢原请求（#2）
        extractions = [pre_extract(op, canonical_reqs) for op in raw_ops]
        plan = build_plan(raw_ops, extractions, revision.previous_plan, remaining_operations)
        static_validate(plan)                          # 投影校验（D6）
        plan = clarify(plan, ask, max_rounds=2)        # 只补缺口；返回值不抛
        res = resolve_unmapped(plan, ask)              # 补/忽略/取消
        if res.has_new_user_text:                      # 补充说明：走修订，不丢原计划
            revision = PlanRevisionRequest(plan, res.new_text, "supplement"); continue
        decision = confirm(plan, ask)
        if decision.kind == "modify":
            revision = PlanRevisionRequest(plan, decision.modification, "user_modify"); continue
        if decision.kind == "reject":
            return PlanExecutionReport(status="rejected")

        outcome = execute_plan(plan, state, bus, cancel_token)      # 见 §8；返回 ExecutionOutcome
        if outcome.plan_status == "needs_replan":                  # #1：真正回到循环
            revision = PlanRevisionRequest(plan, outcome.replan_reason, "needs_replan")
            remaining_operations = outcome.remaining_operations    # 只重规划未执行的，已成功的不重跑
            continue
        return outcome.report
    return PlanExecutionReport(status="revision_limit_exceeded")

# TurnService（run 外持 state）：
state = PlanExecutionState()
run_report = coordinator.run(lambda bus, ct: execute_turn(state, bus, ct))
# UI 报告可用 state.to_cancelled_report()；但取消进 Trace 由 execute_plan 在 finalize 前发布（见下）
```

- clarify/confirm/needs_replan 的分支均为**返回值/循环控制**，不抛异常（否则被 coordinator 记 failed）；`max_plan_revisions=3` 防无限。
- **#1**：`needs_replan` 由 `execute_plan` 返回 `ExecutionOutcome{report, plan_status, remaining_operations, replan_reason}`，循环据此**只重规划未执行的 Operation**，绝不重跑已成功的。
- **#2**：修订统一用 `PlanRevisionRequest{previous_plan, revision_text, revision_reason}`——Analyzer 收到「旧计划摘要 + 修订文本 + 未执行操作」，产出**对旧计划的修订**，不把「第二步改成 md」当成全新请求而丢掉第一步。Unmapped 的「补充说明」同机制。
- **#3 取消进 Trace**：coordinator 会在返回前 `finalize_best_effort()`，故取消状态必须在**上抛前、在 run 内**发布，才能进这份 trace：

```python
# execute_plan 内：
try:
    ...
except OperationCancelled:
    state.mark_cancelled()
    bus.publish(EventKind.PLAN_CANCELLED, plan_status=state.plan_status,
                completed_operation_ids=state.completed_ids, rollback_available=state.rollback_available)
    raise   # 继续上抛，让 Coordinator 统一 finalize
```

### D3. 三段式各自可降级；调用态/字段态分离；失败不塌陷

- `SlotValue{value, field_status ∈ present|user_absent|ambiguous|invalid|unavailable, source ∈ user_explicit|normalized|defaulted|user_confirmed, requirement_ids}`。`unavailable` 表示**因抽取调用失败**（provider/parse/schema）而暂缺，区别于用户真的没说（user_absent）。
- `ExtractionResult{call_status ∈ success|provider_failed|parse_failed|schema_failed, slots, consumed_requirement_ids, newly_discovered_requirements, unmapped_requirements, error_category}`。
- 「user_absent」与「provider/parse/schema 失败(→unavailable)」永不折叠；不确定标 `ambiguous`、不伪造。Clarifier 对 `user_absent/ambiguous/invalid/unavailable` 都追问；对 `unavailable` 用「自动理解暂时失败，我直接和你确认以下信息」的措辞合并一次问清。
- **Analyzer 失败不无条件回退 Router**：若 Rule_Analysis 有唯一高置信候选 → 降级单 Operation 并明确提示；否则不写、要求澄清。绝不执行 UNCLEAR/优先级截断结果（Req 1.4）。

### D4. 可验证零静默丢失（Slot 消费粒度）

- `AtomicRequirement{requirement_id, verbatim, source_start, source_end, category, ambiguous_span?}`；**LLM 只给 verbatim，程序在原文定位算 offset**，找不到/多匹配 → `ambiguous_span`（不让 LLM 算字符位）。
- **Requirement 唯一真相源**：
  - Analyzer 路径 → **Analyzer 产 canonical requirements**；PreExtractor 只回 `consumed_requirement_ids`，**不再创建重复 requirement**。
  - Fast_Path（无 Analyzer）→ **PreExtractor 负责产 canonical requirements**（`newly_discovered_requirements`）+ 消费关系。
- `pre_extract(operation, canonical_requirements: list[AtomicRequirement] | None) -> ExtractionResult`。
- PlanBuilder 校验：`SlotValue.requirement_ids` 全引用真实 requirement；同一 requirement 不因重复抽取生成多个 id；未消费者进 unmapped。
- 覆盖判定按 **Slot 消费**：每个 AtomicRequirement 必须被某具体 `SlotValue.requirement_ids` 消费、或 unmapped/clarification/ignored；**仅挂 Operation 不算**。
- **#7 Fast_Path 抽取整体失败兜底**：若 PreExtractor 整体失败（无 requirement/无 slot），把**完整用户原话**记为一条 `AtomicRequirement{requirement_id, verbatim=user_text, category="unparsed"}` → 进 Clarifier 并说明「自动理解失败」，避免 provider 失败时出现不可观测的信息丢失。
- 诚实边界：运行时只保证「已识别的 requirement 有去向」；漏识别率由 golden eval 度量。

### D5. Fast_Path 判定 + 三段式入口统一

- 规则层升级为 `rule_analyze(text) -> Rule_Analysis{candidates, matched_signals, has_sequence_signal, uncovered_text, unambiguous}`。
- **仅当** 恰一候选 + 无序列信号 + 无未覆盖文本 + 目标动作清晰 → Fast_Path（跳过 Analyzer），**但仍走** PreExtractor→PlanBuilder→Static_Validator→Clarifier→Confirm→执行（只省多操作拆分 LLM，不省抽槽与校验）。
- 不得仅凭「现有 Router 返回一个 Intent」判无歧义（它按优先级截断，会把「先润色再转 Word」误判为单 convert）。

### D6. 校验分两级 + 投影章节

- **Static_Plan_Validator**（计划期）：intent 支持性、必填槽位、能力组合、约束冲突；章节用 **Projected_Sections**（当前 ∪ 前序 add_section 将创建）校验——支持「先加再改」、又提前拦「改不存在的 FooBar」。
- **Pre_Operation_Validator**（每步执行前）：基于最新 Working_Copy 查章节/格式。**失败不在循环内重澄清**，而是停计划 → `needs_replan` → 基于最新状态生成 `plan_version+1` → 重新确认再执行（D7）。

### D7. 失败/取消语义 + 运行态 + git 基线

- 单 Operation 写回原子（复用既有原子写）——「不留半成品」仅对单 Operation 成立。
- 计划开始前建 `Session_Baseline`（git commit 或文件/二进制快照）。执行中维护 `Plan_Execution_State{completed_operations, current_operation_id, baseline}`（在 coordinator 返回值之外持有），使取消/异常时仍能产出部分完成报告 + 回滚选项。
- **回滚边界**：`Session_Baseline` 只覆盖 **Working_Copy**；`execute_convert` 产出的 `output/` 新文件**不在** session git 内。故 plan 级回滚 `rollback_scope = "working_copy_only"`；convert 失败时对**本轮新生成**的临时输出做 best-effort 清理，**不删**执行前已存在的用户文件。
- `OperationResult` 记 `changed_working_copy: bool` 与 `generated_outputs: list[str]`，供报告与清理定位。
- **Baseline 生命周期 + 处置时机（#8）**：本 spec 不做 Session Resume/`:rollback-plan`，故部分失败时**在当前 Turn 结束前立即询问**「保留已完成 / 回滚整个计划的 Working_Copy」。处置策略：

| 计划终态 | Baseline 处置 |
|---|---|
| done | 自动 release |
| rolled_back | restore → release |
| partially_completed + 用户选保留 | release |
| partially_completed + 用户选回滚 | restore → release |
| cancelled（Ctrl-C 不便追问）| 保留为 `last_recoverable_baseline`，默认恢复或待用户决定 |
| 进程退出前仍未决定 | 保留 fallback 快照（下次启动可清理）；不静默丢弃 |
- 计划终态：`done | partially_completed | failed | cancelled | rejected | rolled_back | needs_replan`；op 状态含 `blocked_needs_replan`；未执行的后续 op 记 `skipped_due_to_previous_failure | skipped_due_to_cancellation | skipped_due_to_replan`。

### D8. IntentHandler 翻译业务失败

- 现有 `execute_*` 用 `Result(ok=False)`（含 Gate 未过）表达业务失败、不抛异常。Handler：`ok→succeeded`，否则 `rejected`；**不**仅依赖 `run_status`（否则 Gate 失败被记成 run completed / plan success）。

### D9. Shadow 三态拓扑（无嵌套 Coordinator）+ 预算

- 三个**互斥入口**，避免 shadow 下「新层 run 里再调旧路径的 run」造成嵌套 Coordinator：

```python
def handle_turn(text):
    if mode == "off":
        return legacy_handle_turn(text)                     # 完全旧路径
    if mode == "shadow":
        shadow_coordinator.run(lambda bus, ct: analyze_only(text, bus, ct))  # 独立只读 Planner Trace
        return legacy_handle_turn(text)                     # 旧路径完全不变；经 turn_id 关联两条 trace
    if mode == "active":
        state = PlanExecutionState()
        return coordinator.run(lambda bus, ct: execute_turn(state, bus, ct))
```

- shadow 用**独立** `shadow_coordinator` 跑 `analyze_only`（只到 static_validate、只记录），旧执行路径**另起**自己的 run——两者不嵌套；shadow 失败不影响旧路径；靠共同 `turn_id` 关联。
- **#10 shadow 不显著阻塞**：理想顺序是「快照只读文档 → 先跑 legacy → 之后/后台跑 shadow」。v1 若不引后台线程，则 shadow 用**严格短超时**、超时立即放弃、阻塞 legacy 不超过固定上限，并把 shadow 额外延迟计入指标。
- **#9 预算拆分**（人机交互等待不计入 provider 预算）：**不在 Turn 开始激活一个持续墙钟预算**；而是每次 provider 调用单独激活 deadline context（或累计 `provider_elapsed_seconds += call_elapsed`）。调用上限按修订计：`max_analyzer_calls_per_revision=1`、`max_extractor_calls_per_revision=max_operations`、`max_total_llm_calls_per_turn`（因最多 3 次修订，全 Turn 不可能只 1 次 Analyzer / 5 次 Extractor）。`plan_revision_limit=3`、`max_operations=5 / max_input_chars`。shadow 同样受限。

### D10. IntentSpec 驱动槽位；默认值由系统填；Slot 必须绑定到执行器

- 每意图一个 `IntentSpec{slots: {name: SlotSpec{required, default_factory, normalizer, validator, reuse_history, must_ask, execution_binding}}}`。
- **默认值由 PlanBuilder 按 IntentSpec 填（确定性），不由 LLM PreExtractor 决定**；normalizer/validator 供规范化与校验。
- Clarifier 追问条件：`(required or must_ask) and field_status ∈ {user_absent, ambiguous, invalid, unavailable}`（**#4**：unavailable 一并追问；但**仅** required/must_ask 字段追问，避免可选字段因抽取服务失败而全部反问）。
- **#5 execution_binding**：每个 Slot 声明它落到哪：现有 Plan 字段 / `execute_*` 参数 / ContextBuilder / Gate 配置。校验：任何 `present/defaulted/user_confirmed` 的 Slot 若无 execution_binding（执行器根本不消费它）→ 必须转 `unsupported/unmapped`，防「理解层看似理解了、执行器却完全没用」。

### D11. 观测事件精简（复用生命周期）

- 复用 `WORKFLOW_START/END`=Turn、`bus.span("operation:<id>")`=Operation；**不新增** TURN_START/END、OPERATION_START/END。
- 只加业务事件：`OPERATION_ANALYSIS / PRE_EXTRACT / PLAN_VALIDATION / CLARIFICATION_QA / UNMAPPED_RESOLUTION / PLAN_CONFIRMED / PLAN_REJECTED`。

## Architecture

```text
User_Turn ──> TurnService
                │  :cmd → 直接处理（不进 LLM）
                │  自然语言 → rule_analyze → Rule_Analysis
                ▼
        coordinator.run(execute_turn)          # 一个 Turn 一个 run；包住整段旅程
          ├─ fast_path? 恰一候选&无序列信号&无未覆盖&清晰
          │     是 → RawOperation×1（跳过 Analyzer）
          │     否 → Operation_Analyzer（[{intent,verbatim,req_ids}] + AtomicReq[] + unmapped）
          │            失败 → 唯一高置信?降级单op+提示 : 不写+要求澄清
          ├─ PreExtractor(intent)  → ExtractionResult(call_status + SlotValue[field_status,source,req_ids] + requirements)
          ├─ PlanBuilder           → Task_Plan（保留用户序；默认值由 IntentSpec 填）
          ├─ Static_Plan_Validator → 投影章节/能力组合/约束（不查“现在是否存在”，查投影集）
          ├─ Clarifier(≤2 轮)      → 只问 user_absent/ambiguous/invalid；回填后重跑静态校验
          ├─ resolve_unmapped      → 补/忽略/取消（全处置后才 ready）
          ├─ PlanConfirmation      → 确认/否决/修改(→plan_version+1)；默认 require_confirmation=True
          └─ execute_plan:
                create_baseline() ; state=Plan_Execution_State()
                for op in plan.operations:            # 有序、串行
                  with bus.span(f"operation:{op.id}"):
                    Pre_Operation_Validator(最新 working copy)
                       └ 失效 → 停 → needs_replan(plan_version+1)
                    IntentHandler.execute → OperationResult(ok→succeeded/rejected)
                    失败/rejected → 停后续(skipped_*) → partially_completed
                → PlanExecutionReport（含 completed + 回滚选项）
   Convert = 终止操作
   mode=shadow：只跑到 Static_Validator 并记录差异，不进 clarify/confirm/execute
```

## Components and Interfaces

### 1. TurnService（application/turn_service.py）
- `:`-命令直接处理；自然语言构造 `execute_turn` 交一次 `coordinator.run`（D2）。按 `intent_planning_mode` 走 off/shadow/active。

### 2. rule_analyze（tasking/rule_analysis.py）
- 在现有关键词表基础上，返回 `Rule_Analysis`：命中的候选意图集、各自信号、是否有序列连接词、未覆盖文本、是否无歧义（D5）。供 Fast_Path 判定与 Analyzer 失败降级。

### 3. Operation_Analyzer（tasking/operation_analyzer.py）
```python
def analyze(text, supported_intents, file_type, sections, recent_summary) -> AnalyzeResult
# operations:[RawOperation{intent, verbatim, requirement_ids}], requirements:[AtomicRequirement], unmapped_text, status
```
- 只按用户序切分、标 intent、标 AtomicRequirement 的 verbatim（**offset 由程序定位**，D4）；非法 intent/无法归类 → unmapped_text。失败 → status=provider_failed（TurnService 按 D3 降级）。

### 4. PreExtractor（tasking/extractors/*.py，按 IntentSpec）
- 五个抽取器返回 `ExtractionResult`（D3）；Fast_Path 下同时产出 requirements + 消费关系（D4）。字段来源/状态如 D3；默认值不在此填（D10）。

### 5. PlanBuilder（tasking/plan_builder.py）
- 合并 → 有序 `Operation`（保留用户序）；按 IntentSpec 填默认值（`defaulted`）；跑 normalizer；能力前置模拟收集重排建议（不自动改序）；汇总 unmapped（含 Slot 未消费的 AtomicRequirement）。

### 6. Static_Plan_Validator / Pre_Operation_Validator（tasking/plan_validator.py）
- Static：Projected_Sections 校验 + intent/slot/能力组合/约束（D6）。
- Pre_Operation：最新 working copy 查章节/格式；失败 → needs_replan（不重澄清，D6/D7）。

### 7. Clarifier + resolve_unmapped + PlanConfirmation（tasking/clarifier.py, TurnService）
- Clarifier 只问缺口、≤2 轮、回填标 `user_confirmed` 后重跑静态校验、超限转 draft/取消。
- resolve_unmapped：逐条强制「补充/忽略(ignored)/取消」，全处置后 ready。
- Confirm：呈现执行序摘要；确认/否决/修改（修改→新 plan_version）。

### 8. PlanExecutor + IntentHandler（runtime/）
```python
class IntentHandler(Protocol):
    intent: Intent
    def execute(self, op, ctx, bus, cancel_token) -> OperationResult: ...
```
- `execute_plan` 内：`create_baseline` → 维护 `Plan_Execution_State` → 有序遍历 → 每 op 一个 span → Pre_Operation_Validator → Handler 调 `execute_*` → `OperationResult(status)`（D8）。失败/rejected 停后续、partially_completed；needs_replan/取消按 D7。

### 9. Session 扩展（session/file_session.py，不改 execute_* 契约）
```python
@dataclass
class SessionBaseline: baseline_id: str; git_commit: str | None; fallback_snapshot_path: str | None
class FileBackedSession:
    def create_baseline(self, label: str) -> SessionBaseline: ...   # git commit 或文件/二进制快照副本
    def restore_baseline(self, b: SessionBaseline) -> bool: ...      # git reset 或原子替换恢复
    def release_baseline(self, b: SessionBaseline) -> None: ...      # 成功/保留后清理 fallback 快照
```
- git 不可用降级：文本/DOCX 均存二进制基线副本，回滚用原子替换（D7、Req 8.2）。
- 生命周期：create → (restore | release)；`release` 清理 fallback 快照避免工作区堆积。回滚只覆盖 Working_Copy（`rollback_scope="working_copy_only"`）；convert 的 `output/` 产物由 OperationResult.generated_outputs 记录、失败时 best-effort 清理本轮新产物。

### 10. 观测扩展（复用 events.py / bus / recorder）
- 复用 WORKFLOW_START/END=Turn、span("operation:...")=Operation；只加业务事件（D11）。文本经既有 Sanitizer，原文不落 trace。

## Data Models

- **AtomicRequirement**：`{requirement_id, verbatim, source_start, source_end, category, ambiguous_span?}`（offset 程序定位）。
- **RawOperation**：`{intent, verbatim, requirement_ids}`。
- **SlotValue**：`{value, field_status, source, requirement_ids, verbatim?}`。
- **ExtractionResult**：`{call_status, slots:dict[str,SlotValue], consumed_requirement_ids, newly_discovered_requirements:[AtomicRequirement], unmapped_requirements, error_category?}`（requirement 唯一真相源见 D4）。
- **SlotSpec / IntentSpec**：`SlotSpec{name, required, default_factory?, normalizer?, validator?, reuse_history, must_ask, execution_binding}`；`IntentSpec{intent, slots:dict[str,SlotSpec]}`（D10）。
- **PlanRevisionRequest**：`{previous_plan, revision_text, revision_reason ∈ initial|supplement|user_modify|needs_replan}`（D2/#2）。
- **ExecutionOutcome**：`{report: PlanExecutionReport, plan_status, remaining_operations, replan_reason}`（D2/#1）。
- **Rule_Analysis**：`{candidates, matched_signals, has_sequence_signal, uncovered_text, unambiguous}`。
- **Operation**：`{operation_id, intent, target, goal, slots, constraints, requirement_ids, status}`。
- **Task_Plan**：`{plan_id, source_turn_id, schema_version, plan_version, analyzer_version, prompt_version, operations(有序), unmapped_requirements, missing_fields, reorder_suggestions, status}`。无 DAG（v1）。
- **SessionBaseline / Plan_Execution_State**：见 §9 / D7；`Plan_Execution_State.to_cancelled_report()` 供取消后出报告。
- **OperationResult**：`{operation_id, status ∈ succeeded|rejected|failed|cancelled|blocked_needs_replan|skipped_due_to_previous_failure|skipped_due_to_cancellation|skipped_due_to_replan, changed_working_copy: bool, generated_outputs: list[str], message}`。
- **PlanExecutionReport**：`{plan_status, failure_category?, operations:[OperationResult], rollback_scope="working_copy_only", rollback_available, completed_operation_ids}`。`failure_category ∈ execution_failed(系统/provider/工具) | validation_rejected(Gate 拒质量未达标)`——顶层都可归 failed，但保留类别以便评测区分系统错误与质量问题。
- 不引入 CandidatePatch / CommitManifest / ValidationReport（→ P1）；回滚复用 working-copy + git/快照。

### Plan 状态聚合规则

| 执行结果 | Plan 状态 |
|---|---|
| 全部成功 | done |
| 首项系统/工具失败且无写入 | failed（failure_category=execution_failed）|
| 首项 Gate 拒 | failed（failure_category=validation_rejected）|
| 前面成功、后面失败/拒绝 | partially_completed |
| 执行前取消 | cancelled |
| 部分完成后取消 | partially_completed |
| 状态变化导致无法继续（Pre-Op 失效）| needs_replan |
| 用户选择整体回滚 | rolled_back |
| 规划修订超上限 | revision_limit_exceeded |

## Correctness Properties

### Property 1: 顺序保真且线性串行
执行序默认=用户序；重排必经用户确认、绝不静默；一次一个、不并发改同一 Working_Copy。
**Validates: Requirements 2.6, 8.1, 9.1, 9.2**

### Property 2: 可验证零静默丢失（Slot 消费粒度）
每个已识别 AtomicRequirement 必被某具体 Slot 消费、或 unmapped/clarification/ignored；仅挂 Operation 不算；漏识别率由 golden 度量。
**Validates: Requirements 4.3, 4.4, 3.6**

### Property 3: 失败类型不塌陷
call_status 与 field_status 分离可区分，不折叠；不为未提供字段伪造取值；Analyzer 失败不执行 UNCLEAR/截断结果。
**Validates: Requirements 4.1, 4.2, 3.5, 1.4**

### Property 4: Fast_Path 保守且完整
仅「恰一候选+无序列信号+无未覆盖+清晰」走 Fast_Path，且仍经抽槽与校验；不凭「Router 返回一个」判无歧义。
**Validates: Requirements 1.2, 1.3, 3.7**

### Property 5: 只补缺口且有界
Clarifier 只问 user_absent/ambiguous/invalid，不问 user_explicit；一次问清；≤2 轮转 draft/取消。
**Validates: Requirements 6.1, 6.2, 6.3, 6.5**

### Property 6: 计划期投影校验 + 执行期不重澄清
Static 用 Projected_Sections（支持先加再改、拦不存在章节）；Pre-Op 失效 → needs_replan（不在循环内改已确认计划）。
**Validates: Requirements 5.2, 5.3**

### Property 7: 失败/取消隔离 + 单操作原子 + 可回滚可续报
单 op 原子；某步失败/rejected → 停后续、partially_completed、后续记 skipped_*；取消经 Plan_Execution_State 仍产出已完成列表 + 回滚；Gate 未过=rejected 不计成功；git/快照基线回滚。
**Validates: Requirements 8.4, 8.6, 8.8, 8.2**

### Property 8: Convert 终止约束
convert_format 为终止操作；其后有依赖旧格式 op → 判不可执行 + 提示分两轮。
**Validates: Requirements 9.3, 9.4**

### Property 9: 默认零影响（off/shadow）
off 不跑新层；shadow 只记录、不发澄清/确认、不改 Working_Copy、失败不影响旧路径、不显著阻塞（严格短超时）；行为与既有 Router 路径逐字节一致，既有测试全绿。
**Validates: Requirements 10.2, 10.3, 11.1, 11.2**

### Property 10: Slot 必绑定执行器
任何 present/defaulted/user_confirmed 的 Slot 必有 execution_binding（落到 Plan 字段/execute_* 参数/ContextBuilder/Gate），否则转 unsupported/unmapped；不存在「已理解但执行器不消费」的静默丢失。
**Validates: Requirements 3.6, 4.4**

### Property 11: needs_replan 回规划循环且不重跑已成功
needs_replan 经 ExecutionOutcome 回到有界规划循环，只对未执行 Operation 重规划；已成功 Operation 不被重复执行。
**Validates: Requirements 7.7, 8.3**

## Error Handling

| 失败点 | 处理 |
|---|---|
| Analyzer LLM 失败 | 唯一高置信→降级单 op+提示；否则不写+要求澄清（Req 1.4，绝不跑 UNCLEAR/截断）|
| PreExtractor provider/解析失败 | 字段记失败态/进澄清；不伪造（D3）|
| Analyzer 非法 intent | 丢 unmapped_text（Req 2.4）|
| 顺序能力不可行 | 提重排建议/拒绝，用户确认才改序（Req 9.2）|
| convert 非终止 | 判不可执行 + 提示分两轮（Req 9.3/9.4）|
| 章节不存在（计划期）| Projected_Sections 提前标 section_not_found（Req 5.2）|
| 章节/格式失效（执行期）| Pre-Op → needs_replan，不重澄清（Req 8.3、D6）|
| 必填槽位缺失 | Clarifier ≤2 轮（Req 6）|
| Unmapped | 强制补充/忽略/取消，处置后才 ready（Req 4.5）|
| 用户修改计划 | 旧 rejected，新 plan_version（Req 7.5）|
| Operation 业务失败/Gate 拒 | Handler→rejected，停后续，partially_completed（Req 8.8、D8）|
| 取消 | 上抛；单 op 原子；Plan_Execution_State 保留已完成 + 回滚（Req 8.6、D7）|
| git 不可用 | 文件/二进制快照基线 + 原子替换回滚（Req 8.2、§9）|
| 预算突破 | 停理解层调用 + 降级说明（Req 13.2）|
| 观测侧失败 | 复用既有防御式脊柱，不拖垮业务（Req 12.2）|

## Testing Strategy

- **Analyzer golden 集**（起步 20–30 条）：指标含意图/拆分/目标/约束召回、不必要追问率、相对人工标注的 requirement 漏失率、运行时 Silent_Loss=0。
- **Fast_Path 判定**：「先润色再转 Word」不得走 Fast_Path；单一无歧义才走且仍抽槽校验（Property 4）。
- **Analyzer 失败降级**：唯一高置信→单 op；否则要求澄清、不跑 UNCLEAR（Property 3）。
- **requirement 消费**：约束未落 slot → 不算已处理，进 unmapped（Property 2）；offset 由程序定位、多匹配标 ambiguous_span。
- **投影校验 / 执行期**：先加再改计划期不误拒；改不存在章节计划期即拦；执行期失效→needs_replan（Property 6）。
- **失败/取消/回滚**：partially_completed + skipped_*；取消保留已完成列表；git 及 git 不可用两条路径回滚可用；Gate 拒=rejected（Property 7）。
- **Convert 终止**（Property 8）。**三态 Shadow / 向后兼容**：off/shadow 不改 UX/写入、失败不影响旧路径、既有测试全绿、mock 端到端（Property 9）。

## Appendix: IntentSpec 配置表（五意图必填/默认/是否追问）

默认值由 PlanBuilder 按此表填入（`defaulted`），不由 LLM 决定；`must_ask=是` 表示缺失时必须澄清。

| Intent | Slot | required | default | must_ask |
|---|---|---|---|---|
| polish | section_id | 是 | 无 | 是 |
| polish | goal | 否 | 保守润色 | 否 |
| polish | constraints | 否 | [] | 否 |
| rewrite | section_id | 是 | 无 | 是 |
| rewrite | direction | 是 | 无 | 是 |
| rewrite | details | 否 | "" | 否 |
| rewrite | search_query | 否 | "" | 否 |
| rewrite | want_search | 否 | (search_query 非空 → True) | 否 |
| rewrite | search_limit | 否 | 5 | 否 |
| add_section | title | 是 | 无 | 是 |
| add_section | purpose | 是 | 无 | 是 |
| add_section | structure_kind | 是 | 无（desc\|list\|mimic）| 是 |
| add_section | structure_desc | 否 | "" | 否 |
| add_section | subsections | 否 | [] | 否 |
| add_section | position | 否 | end | 否 |
| add_section | search_query | 否 | "" | 否 |
| add_section | want_search | 否 | (search_query 非空 → True) | 否 |
| add_section | search_limit | 否 | 5 | 否 |
| convert_format | target_ext | 是 | 无 | 是 |
| convert_format | presets | 否 | [] | 否 |
| format_docx | operations | 是 | 无（`list[FormatOp]`，仅 parse_format_ops 支持的类型）| 是 |
| format_docx | scope | 否 | document | 否 |

> **#6**：`add_section.structure` 是结构化联合（`structure_kind + structure_desc + subsections`），非模糊字符串，便于 Handler 转成现有 `AddSectionPlan`；`format_docx.operations` 是**受限** `list[FormatOp]`，PreExtractor 只允许输出现有 `parse_format_ops()` 支持的操作类型；`rewrite`/`add_section` 的 `want_search/search_limit` 与现有 Plan 对齐。

## Appendix: P1 Backlog（本 spec 不做）

- 依赖 DAG + 拓扑排序；convert→切换 working copy→继续编辑（switch_working/重绑/转换回滚）。
- Candidate/Approval 正式化 + Base_Hash 冲突检查（尽量压 git）。
- Project/Session 恢复（:status/:resume/:candidates/:rollback）。
- Evidence Gate + Claim–Evidence Link + 独立语义 Reviewer；有界 Research Loop 形式化；Mutation Eval。
- 目录重构（application/tasking/runtime/engines/... 渐进搬迁）。
