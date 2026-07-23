# Implementation Plan

## Overview

按「**数据模型 → Shadow（只读不接管）→ Active**」推进。核心纪律：**复用**现有 `execute_*`/Gate/working-copy+git/观测脊柱，**不重写**；`intent_planning_mode` 默认 `off`；**当前完整测试套件全部通过**、mock 端到端；Shadow 阶段绝不改写入行为，攒够信心（Active 门槛见 Task 24）再开 Active。

任务用扁平编号（1..24，含拆分后的 PreExtractor 4 个、PlanExecutor 5 个），wave 分组见下。每完成一个任务停下汇报 + 全套测试全绿。

## Task Dependency Graph

同一 wave 内无相互依赖、可并行；后一 wave 依赖前面所有 wave。

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2", "3"] },
    { "wave": 2, "tasks": ["4", "5", "6", "7", "8"] },
    { "wave": 3, "tasks": ["9"] },
    { "wave": 4, "tasks": ["10", "11", "12"] },
    { "wave": 5, "tasks": ["13"] },
    { "wave": 6, "tasks": ["14", "15"] },
    { "wave": 7, "tasks": ["16"] },
    { "wave": 8, "tasks": ["17"] },
    { "wave": 9, "tasks": ["18", "19"] },
    { "wave": 10, "tasks": ["20"] },
    { "wave": 11, "tasks": ["21"] },
    { "wave": 12, "tasks": ["22"] },
    { "wave": 13, "tasks": ["23"] },
    { "wave": 14, "tasks": ["24"] }
  ]
}
```

关键依赖：`1→2/4/5/6/9/13`；`2(offset)→6/9`；`4(IntentSpec)→9/13`；`6(Analyzer)→9/13`；`9(框架)→10/11/12`；`10-12→13`；`13→14`；`3+5+6+9-13+14→16(Shadow)`；`13+14→17`；`17→18`；`4+现有 execute_*→19`；`19→20`；`20→21→22→23`（20–23 都改 `runtime/plan_executor.py`，**串行**避免冲突）；全部→`24`。Task 23 的纯 Validator 逻辑可与 19 并行先写，接入 PlanExecutor 须在 20 之后。

## Tasks

- [ ] 1. 数据模型（tasking/contracts.py）
  - **值对象 frozen**：`AtomicRequirement / SlotValue / RawOperation / Operation / SlotSpec / IntentSpec / Rule_Analysis / PlanRevisionRequest / ExecutionOutcome / SessionBaseline`
  - **可变运行态**：`Plan_Execution_State{completed_operations, current_operation_id, baseline}`（执行中持续更新，含 `mark_cancelled()` / `to_cancelled_report()`）——**不 frozen**
  - **生成后不可变**：`PlanExecutionReport`（含 plan_status/failure_category/rollback_scope/operations）；`Task_Plan`（含 version 字段、status 枚举）
  - **跨模块结果模型（统一在此定义，避免各任务各自发明）**：`AnalyzeResult{call_status,operations,requirements,unmapped_text,error_category}`、`ValidationOutcome{result,issues,missing_fields,reorder_suggestions}`、`ValidationIssue`、`ReorderSuggestion`、`ConfirmedPlan`、`PlanningExit`、`PlanningSnapshot{working_hash,file_type,sections,facts_summary}`、`SectionSummary`
  - 测试：值对象改属性抛错；Plan_Execution_State 可更新；枚举取值集合正确（field_status 含 unavailable、op status 含 blocked_needs_replan/skipped_*）；跨模块模型字段齐全
  - _Requirements: 3.1, 4.1, 8.9_
  - _Design: Data Models, D3, D7_

- [ ] 2. offset 定位工具（tasking/spans.py）
  - `locate_verbatim(text, verbatim)->(start,end)|ambiguous_span`：程序定位，找不到/多匹配→ambiguous_span（**不让 LLM 算 offset**）
  - 测试：中文/空格/重复文本；多匹配/未命中→ambiguous_span
  - _Requirements: 4.3_
  - _Design: D4_

- [ ] 3. 观测事件 + Recorder + Trace JSON（observability/events.py, recorder.py）
  - 新增业务 EventKind：`OPERATION_ANALYSIS/PRE_EXTRACT/PLAN_VALIDATION/CLARIFICATION_QA/UNMAPPED_RESOLUTION/PLAN_CONFIRMED/PLAN_REJECTED/PLAN_CANCELLED/OPERATION_RESULT/PLAN_RESULT`；**复用** WORKFLOW_START/END=Turn、span("operation:...")=Operation
  - **扩展 `SimpleTraceRecorder._handle()`** 让新事件经 `_record_event()` 进 trace.json；过 Sanitizer
  - 测试：新事件确实出现在 trace.json；OPERATION_RESULT 能看出 succeeded/rejected/failed、PLAN_RESULT 能看出 done/partial/needs_replan、skipped 步骤可见；**原始用户文本不落盘**
  - _Requirements: 12.1, 12.2_
  - _Design: D11, Component§10_

- [ ] 4. IntentSpec 注册表（tasking/intent_spec.py）
  - 五意图 `IntentSpec/SlotSpec{required,default_factory,normalizer,validator,reuse_history,must_ask,execution_binding}`，按 design 附录表（含 rewrite want_search/search_limit、add_section structure_kind/desc/subsections、format_docx `list[FormatOp]`）
  - **结构化绑定**：`class BindingKind(str,Enum){PLAN_FIELD,EXECUTE_ARG,CONTEXT_FIELD,GATE_POLICY}`；`ExecutionBinding{kind, target}`
  - 测试：五意图 slot/默认/must_ask 与附录一致；每个 slot 有结构化 ExecutionBinding（完整「Handler 真的消费」校验推迟到 Task 24）
  - _Requirements: 3.4, 3.8_
  - _Design: D10, Appendix, Property 10_

- [ ] 5. RuleAnalysis（tasking/rule_analysis.py）
  - 产 `Rule_Analysis{candidates, matched_signals, has_sequence_signal, uncovered_text, unambiguous}`；序列信号词表（然后/再/并且/同时/先…再…）
  - 测试：单意图无歧义→unambiguous；"先润色再转Word"→多候选+序列信号+非 unambiguous
  - _Requirements: 1.2, 1.3_
  - _Design: D5, Component§2, Property 4_

- [ ] 6. Operation_Analyzer（tasking/operation_analyzer.py）
  - LLM 按用户序切分 `[RawOperation]` + canonical `AtomicRequirement`（verbatim，offset 用 Task2）+ unmapped_text；非法 intent→unmapped_text
  - **失败态保留四种**（与 PreExtractor 一致，不混为一谈）：`call_status ∈ success|provider_failed|parse_failed|schema_failed`
  - 发布 `OPERATION_ANALYSIS` 事件（由 TurnService 在调用边界发，纯函数不依赖 bus）
  - 测试：多意图有序拆分；requirement 齐全；非法 intent 入 unmapped；四种失败态可区分不塌陷
  - _Requirements: 2.1, 2.2, 2.4, 2.5, 2.6_
  - _Design: Component§3, D0, D4_

- [ ] 7. Session Baseline（session/file_session.py）
  - `create_baseline(label)->SessionBaseline` / `restore_baseline` / `release_baseline`；git 走 commit（返回 SHA）、否则快照
  - **快照位置** `<workspace_root>/.baselines/<session_id>/<baseline_id>/`（**在 git 仓库外，或加 git 排除**，防 `git add -A` 误提交）
  - 测试：git/非 git 两路径；快照**不被 git commit**；release 幂等；restore 后仍可继续写；**只恢复 Working_Copy、源文件不受影响**；docx 二进制安全
  - _Requirements: 8.2_
  - _Design: Component§9, D7_

- [ ] 8. Config 接入（config.py）
  - `PAPER_INTENT_PLANNING_MODE=off|shadow|active`（默认 off）、`PAPER_PLANNING_MAX_OPERATIONS`、`PAPER_PLANNING_MAX_REVISIONS`、`PAPER_PLANNING_MAX_INPUT_CHARS`、`PAPER_PLANNING_TIMEOUT`
  - **总量预算**（因 3 次修订 → 最多 3 Analyzer + 3×N PreExtractor，不能只靠单次 timeout）：`PAPER_PLANNING_MAX_LLM_CALLS`、`PAPER_PLANNING_PROVIDER_TIME_BUDGET`、`PAPER_SHADOW_TIMEOUT`
  - 非法值降级默认
  - 测试：默认 off；各 env 解析；非法值安全降级；总量预算生效
  - _Requirements: 10.3, 13.1_
  - _Design: D9_

- [ ] 9. PreExtractor 公共框架（tasking/extractors/base.py）
  - `structured_call` + schema 校验 + `unavailable` 字段态 + requirement 消费（consumed_requirement_ids）+ Fast_Path newly_discovered_requirements + 整体失败兜底（整句 unparsed requirement）
  - 测试：user_absent≠unavailable；schema 失败标 call_status；Fast_Path 产 requirement；整体失败兜底不静默
  - _Requirements: 3.1, 3.2, 3.5, 3.7, 4.1, 4.2_
  - _Design: Component§4, D3, D4_

- [ ] 10. PreExtractor：polish + convert（tasking/extractors/）
  - 两个简单意图先验证框架协议；复用现有 interview 槽位；不确定→ambiguous；不填默认值
  - 测试：字段来源/状态正确；requirement 消费到具体 slot
  - _Requirements: 3.1, 3.5, 3.6_
  - _Design: Component§4, Appendix_

- [ ] 11. PreExtractor：rewrite + add_section（tasking/extractors/）
  - 复杂槽位：搜索（want_search/search_query/search_limit）、结构（structure_kind/desc/subsections）
  - 测试：结构化 structure 可转现有 AddSectionPlan；search_query 非空→want_search
  - _Requirements: 3.1, 3.5, 3.6_
  - _Design: Component§4, Appendix, D6_

- [ ] 12. PreExtractor：format_docx（tasking/extractors/）
  - 输出**受限** `list[FormatOp]`，只允许现有 `parse_format_ops()` 支持的类型
  - 测试：非法操作类型被拒/标 invalid；合法 op 可转现有格式操作
  - _Requirements: 3.1, 3.5_
  - _Design: Component§4, Appendix_

- [ ] 13. PlanBuilder + build_revision（tasking/plan_builder.py）
  - 合并→有序 Operation（保序）；按 IntentSpec 填默认值 + normalizer；能力前置模拟收集 reorder_suggestions（不自动改序）；requirement 去重/校验/未消费入 unmapped；execution_binding 校验（无绑定→unmapped）
  - `build_revision(previous_plan, revision_request, remaining_operation_ids)->TaskPlan`（Task 17 的 Planning Loop 调用；**只重规划未执行步骤**）
  - 测试：保序；默认值来自 spec 非 LLM；Slot 消费粒度；无绑定转 unmapped；build_revision 不丢原请求、不含已完成 op
  - _Requirements: 2.6, 3.4, 3.8, 4.3, 4.4, 7.7_
  - _Design: Component§5, D0, D2, D4, D10, Property 2/10_

- [ ] 14. Static_Plan_Validator（tasking/plan_validator.py）
  - Projected_Sections（当前∪前序 add_section 将创建）校验；intent 支持/必填/能力组合/约束冲突；convert 同格式、convert 后有**任何** op→不可执行
  - 测试：先加再改不误拒；改不存在章节计划期即拦；convert 后有 op→不可执行；冲突标记
  - _Requirements: 5.1, 5.2, 9.3, 9.4_
  - _Design: Component§6, D6, D1, Property 6/8_

- [ ] 15. Golden 数据 + 理解指标（tests/eval/intent_planning/）【9A】
  - 20–30 条 golden + 人工标注 Atomic_Requirement；跑分**理解层可测指标**：意图准确率、操作拆分、目标抽取、约束召回、相对标注的 requirement 漏失率、运行时 Silent_Loss=0
  - （**不必要追问率/澄清轮数**推迟到 Task 18，需 Clarifier）
  - 测试：跑分器可运行；基线数字记录
  - _Requirements: 12.3, 12.5_
  - _Design: Testing Strategy, D4, Property 2_

- [ ] 16. Shadow Mode（application/turn_service.py，只读不接管）
  - `mode ∈ off|shadow|active`；shadow 用**独立 shadow_coordinator** 跑 analyze→pre_extract→build→static_validate（**只记录**）；旧路径**另起** run，共同 turn_id 关联；shadow 失败不影响旧路径
  - **执行前只读快照**：在 legacy 执行**之前**捕获 `PlanningSnapshot{working_hash,file_type,sections,facts_summary}`；shadow 基于该快照分析，**不重新读取已被 legacy 改动的 Working_Copy**（保证与旧 Router 当时决策公平比较）
  - **不显著阻塞**：v1 采「捕获快照 → legacy 先执行 → 之后跑 shadow」或严格短超时 + 记 `shadow_added_latency_ms`；预算按修订计、provider 每调用单独 deadline（不在 Turn 起点激活持续墙钟）
  - 测试：off 不跑新层；shadow 下 working copy 字节不变、**当前完整测试套件全部通过**、旧路径不受 shadow 失败/超时影响；shadow 用快照而非改动后文件；差异被记录
  - _Requirements: 1.1, 10.1, 10.2, 10.3, 11.1, 11.2, 11.4, 13.1, 13.2, 13.3, 13.4_
  - _Design: D9, Component§1, Property 9_

- [ ] 17. Clarifier + resolve_unmapped + Confirmation + 有界规划循环（tasking/clarifier.py, planning_loop.py）
  - Clarifier 只问 (required/must_ask)&field_status∈{user_absent,ambiguous,invalid,unavailable}、≤2 轮、回填标 user_confirmed 后重跑静态校验、超限 draft/取消；unavailable 用「自动理解失败」措辞
  - resolve_unmapped 强制补充/忽略/取消；Confirmation 摘要+确认/否决/修改；`require_confirmation` 默认 True
  - `run_planning_loop(...)->ConfirmedPlan|PlanningExit`（max_plan_revisions=3）：补充/修改/needs_replan 用 PlanRevisionRequest 调 build_revision 回顶重建、不丢原请求、不原地改
  - 测试：已给字段不问；多缺口一次问清；≤2 轮转 draft/取消；unmapped 未处置不 ready；修改→新版本重建不丢原请求；否决/未确认不写
  - _Requirements: 4.5, 6.1, 6.2, 6.3, 6.5, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8_
  - _Design: Component§7, D2, D3, D5, Property 4/5_

- [ ] 18. 澄清相关指标（tests/eval/intent_planning/）【9B】
  - 不必要追问率、澄清轮数、user_explicit 字段重复询问率；**计划确认不计入澄清追问**
  - 测试：指标可算；简单任务零澄清追问
  - _Requirements: 12.4_
  - _Design: Testing Strategy, Property 4_

- [ ] 19. IntentHandlers（runtime/handlers/，五个）
  - 薄适配现有 execute_*：`ok→succeeded / not-ok→rejected`（不只看 run_status）；记 changed_working_copy/generated_outputs
  - **重点：抽取的槽位必须真正产生执行效果**（防「提取了但没执行」）：
    - `polish.constraints`：现有 `PolishPlan` 无独立 constraints 字段 → Handler 须绑定到 ContextBuilder constraint block 或 Gate Policy，不能只存在 TaskPlan 里
    - `format_docx.operations`：现有 `execute_format()` 收原始消息，而 TaskPlan 存 `list[FormatOp]` → Handler 须把 FormatOp 序列化成现有 parser 可识别的规范指令，**或**给执行器加**可选** `ops` 参数（须兼容旧调用）；明确二选一
  - 测试：Gate 拒→rejected；ok→succeeded；每个 IntentSpec 可执行 Slot **确被 Handler 消费**；**验证最终执行效果**（constraints 真的影响生成/校验、FormatOp 真的落到文档），不只验证「Handler 读了字段」
  - _Requirements: 8.8, 3.8_
  - _Design: Component§8, D8, Property 10_

- [ ] 20. PlanExecutor + 状态聚合（runtime/plan_executor.py）
  - `execute_plan(plan,state,bus,ct)->ExecutionOutcome`：create_baseline→维护 Plan_Execution_State→有序遍历→每 op 一个 span→Handler→OperationResult；失败/rejected 停后续（skipped_due_to_previous_failure）+partially_completed；按状态聚合表出 plan_status/failure_category
  - 测试：全成功→done；前成功后失败→partially_completed + skipped_*；Gate 拒 failure_category=validation_rejected；单 op 原子
  - _Requirements: 8.1, 8.3, 8.4, 8.7_
  - _Design: Component§8, D7, 状态聚合表, Property 7_

- [ ] 21. Cancellation + PLAN_CANCELLED/PLAN_RESULT/OPERATION_RESULT（runtime/plan_executor.py）
  - `except OperationCancelled: state.mark_cancelled(); bus.publish(PLAN_CANCELLED,...); raise`（**finalize 前进 trace**）；每 op 发 OPERATION_RESULT、计划终了发 PLAN_RESULT
  - 测试：取消进 trace + 保留已完成列表 + rollback_available；未执行记 skipped_due_to_cancellation
  - _Requirements: 8.5, 8.6, 12.1_
  - _Design: D2, D7, D11_

- [ ] 22. Baseline 保留/回滚/释放处置（runtime/plan_executor.py + turn_service.py）
  - 部分失败当前 Turn 结束前问保留/回滚；按处置表 restore/release；convert 失败 best-effort 清**本轮新产物**（先记 output_existed_before/hash_before，只删本轮创建、执行前不存在的文件）
  - **取消场景的默认策略**（本轮不做 Session Resume，须在当前进程内处置）：零写入取消→release baseline；部分写入取消→暂存 baseline 立即询问保留/回滚；无法获得回答→默认保留 working、不删 baseline；进程退出前未处置→提示或按配置自动回滚
  - 测试：done 自动 release；rolled_back restore+release；partial+keep release；零写入取消 release；部分写入取消暂存+询问；不删既有用户输出
  - _Requirements: 8.4, 8.10_
  - _Design: D7, Property 7_

- [ ] 23. Pre_Operation_Validator + needs_replan（runtime/plan_executor.py）
  - 每 op 执行前基于最新 working copy 查章节/格式；失效→停+`blocked_needs_replan`+ExecutionOutcome(needs_replan, remaining_operations)，**不在循环内重澄清**；由 Task 17 Loop 消费
  - 测试：先加再改执行期通过；执行期失效→needs_replan（不重澄清）+ 后续 skipped_due_to_replan；needs_replan 只重规划未执行、不重跑已成功
  - _Requirements: 5.3, 8.3, 8.11, 9.1, 9.2, 7.7_
  - _Design: Component§6, D6, D2, Property 6/11_

- [ ] 24. Active 端到端 + 模式矩阵 + 冻结验收（application/turn_service.py）
  - TurnService 接 active：一个 Turn 一个 coordinator.run 包住整段旅程（rule_analyze 在 run 内）
  - **专用 Fixture**：含 Introduction/Method/Conclusion 的 tex；用例一「新增 Discussion 章节讨论结果意义与局限，**放 Conclusion 前**」（依赖 Conclusion 存在）正确拆解/抽槽/不重复追问/确认/执行/落 trace
  - 用例二（写完整、含章节使澄清可跳过）：「**先保守润色 Introduction、保留数字和引用，再转换为 docx**」——多操作按序 + convert 终止校验 + 直通不触发澄清
  - 模式矩阵 off/shadow/active × mock
  - **Active 门槛**：Shadow 回放样本 ≥50–100、Shadow 无写入副作用、Trace 完整率=100%、provider/解析失败均正确降级、当前完整测试套件全绿、所有 Slot 有执行绑定且被 Handler 消费、取消/回滚测试全过、**无未解决的 release-blocking P0/P1 defect**（不含明确延期的 P1 功能）
  - Golden 指标：单意图意图准确率≥95%、多操作拆分≥90%、约束召回≥90%、简单任务**零澄清追问**（确认不计入）、Silent_Loss=0
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 12.1_
  - _Design: 全量 Property, Testing Strategy_

## Notes

- **迁移纪律**：Wave 1–7（数据模型/理解链路/观测/baseline/**Shadow**）**不改写入行为**；Active（Wave 8–10）才接管。
- **职责边界**：`build_revision`（Task13）产计划；`run_planning_loop`（Task17）消费 ExecutionOutcome 决定是否重规划；`execute_plan`（Task20/23）只**返回** needs_replan、**不自己重建计划**；needs_replan 只重规划未执行步骤、绝不重跑已成功。
- **复用不重写**：Handler 薄包 execute_*；Baseline 扩 Session；观测复用脊柱。
- **事件发布归属**（纯函数不依赖 bus，由 TurnService/PlanExecutor 在调用边界发布）：Task6→`OPERATION_ANALYSIS`；Task9–12→`PRE_EXTRACT`；Task14→`PLAN_VALIDATION`；Task17→`CLARIFICATION_QA/UNMAPPED_RESOLUTION/PLAN_CONFIRMED/PLAN_REJECTED`；Task19–23→`OPERATION_RESULT/PLAN_RESULT/PLAN_CANCELLED`。
- **诚实边界**：Silent_Loss=0 指「已识别 requirement 有去向」；漏识别率由 Golden 度量。
- **P1（不做）**：依赖 DAG/拓扑、convert 后继续编辑、Candidate/Approval 正式化、Project/Resume、Evidence/语义 Gate、Research Loop、目录重构。
