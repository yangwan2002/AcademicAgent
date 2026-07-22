# Implementation Plan

## Overview

**先做薄脊柱（P0）**：能"流式看到候选 + 结束落一个 trace.json"，且默认零影响、不破坏既有 65 测试。灾难/并发/持久化硬化列入 **P1 deferred backlog**（设计已存档，按需再做，见 design.md「实施范围」）。P0 完成后即可回去用真实模型验证 agent，再需求驱动地补 P1。

## Task Dependency Graph

同一 wave 内无相互依赖、可并行；后一 wave 依赖前面所有 wave。（仅含 P0）

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2"] },
    { "wave": 2, "tasks": ["3", "5", "6"] },
    { "wave": 3, "tasks": ["4", "7", "8"] },
    { "wave": 4, "tasks": ["9"] },
    { "wave": 5, "tasks": ["10"] }
  ]
}
```

## Tasks

- [ ] 1. Event 信封 + 深度不可变 + serialize + 统一 EventKind（events.py）
  - `frozen=True` + 追加 `event_id/sequence/schema_version/generation_id/attempt/llm_call_id/source_event_id`
  - `deep_freeze`（dict→MappingProxyType、list→tuple、set→frozenset 递归）+ `__post_init__` 冻结 `data`；`serialize_event()` 转普通 JSON
  - 新增 EventKind：`SPAN_START/SPAN_END/ROUTE_DECISION/INTERVIEW_QA/RETRIEVAL_START/RETRIEVAL_END/GENERATION_ATTEMPT_START/GENERATION_ATTEMPT_END/GATE_START/GATE_RESULT/GATE_RETRY/LLM_CALL_START/LLM_CALL_END/RUN_CANCELLED/WORKING_COPY_UPDATE/WORKSPACE_COMMIT/SOURCE_SAVE`（复用既有 LLM_DELTA/LLM_THINKING/LLM_USAGE/LLM_RETRY）；既有 kind 值不动
  - 测试：既有构造可用；`data["x"]=...` 与嵌套 `data["v"].append(...)` 抛错；`serialize_event` 可 json.dumps
  - _Requirements: 12.1, 12.3, 11.4, 1.4_
  - _Design: Components§1, D0, Property 4_

- [ ] 2. Sanitizer + sanitize_messages（observability/sanitize.py）
  - `sanitize_text->SafeText(frozen){hash,preview≤500,length}`；`redact()` 剔密钥；`sanitize_messages->SafePrompt`（规范化 json.dumps(sort_keys) 后 redact→hash→preview）
  - 测试：截断；hash 与字典顺序无关；密钥剔除；SafeText 不可改
  - _Requirements: 8.1, 8.2, 8.4, 8.5_
  - _Design: Components§4, §5(SafeText frozen)_

- [ ] 3. RunContext + EventBus + NullEventBus + SpanScope + OperationCancelled
  - `paper_agent/errors.py`：`OperationCancelled`（核心中立模块）
  - `EventBus(EventSink)` 双入口 `publish/emit`：`_stamp` 强制覆盖权威信封（trace_id/event_id/sequence/ts/span_id/parent/generation_id/attempt/llm_call_id，provider 旧值仅存 source_event_id）+ deep_freeze；**内部异常不外抛**（记 ObservabilityHealth）
  - `generation_scope/attempt_scope/llm_call_scope` 退出自动恢复；`span()`：进入先 push 再发 SPAN_START、退出先发 SPAN_END 再（`finally`）pop；显式 span_id/parent
  - `SpanScope.__exit__`：`OperationCancelled`→cancelled，其他异常→failed，否则 `_state or ended`；`pop_span` 在 finally
  - `NullEventBus`：`span()` 返回 **NullSpanScope**（no-op cancel/degrade）；`as_sink_or_none()`→None；其余 no-op
  - 测试：provider 旧信封被强制覆盖（Property 10）；scope 退出恢复；嵌套 span 父子正确；三终态含取消（Property 9）；SPAN_END 异常仍弹栈；`with NullEventBus.span() as s: s.cancel()` 不报错；publish 内部异常不传播
  - _Requirements: 1.1, 12.1, 12.2, 12.4, 7.9, 7.10, 7.11, 4.4, 11.2, 15.1_
  - _Design: Components§2, D0, D1, D2, Property 9, 10_

- [ ] 4. FanOutSink + ObservabilityHealth（observability/fanout.py）
  - 转发同一 frozen Event；下游异常 `_report` 只打 sink 类名/EventKind/event_id/异常类型（不打 payload）；零下游接受不转发
  - `ObservabilityHealth{trace_collection_failed, renderer_failed, warnings[]}`；捕获下游异常时置位
  - 测试：多下游字段相等；一个抛异常其余仍收到且被上报（不含 payload）；health 置位
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_
  - _Design: Components§3, 第8.5点_

- [ ] 5. Provider 调用级 EventSink 适配（base/openai_compatible/anthropic/mock/resilient）
  - `stream/complete` 加**可选** `event_sink=None`（默认 None → 与现状逐字节一致）；resilient 用它发 `LLM_RETRY`；不长存
  - 调用方 `as_sink_or_none()`：关闭时不传 event_sink，走原始签名
  - 测试：不传时 65 测试不变；传入时 retry 路由正确；跨调用不串
  - _Requirements: 11.1, 11.2, 13.1_
  - _Design: D9, 第1点_

- [ ] 6. ConsoleRenderer 流式（observability/renderer.py）
  - `LLM_DELTA`→候选区（首增量前 `[候选·校验中…]`）；`LLM_THINKING`→只显「正在分析…」永不显原文；`GATE_RESULT(fail)`→✗+violations；`ATTEMPT_START`打分隔线、`GATE_RETRY`只显原因；状态类单行；`RUN_CANCELLED`→已取消；`ATTEMPT_END(rejected|cancelled|error)`→`[该候选已作废]`
  - `escape_terminal_controls()` 过滤 ANSI/回车覆盖/清屏
  - 测试：候选标识；thinking 不现原文；违规呈现；候选作废提示；ANSI 被转义
  - _Requirements: 2.2, 3.4, 3.5, 5.1, 5.3, 6.1, 6.2, 6.3, 6.5, 4.4_
  - _Design: Components§6, 第19点_

- [ ] 7. 简单 TraceRecorder + Span/RunTrace + LocalJsonExporter（薄版）
  - 依 SPAN_START/END 建 span 树；GATE_RESULT 挂 Gate span（candidate_id/source_llm_call_id 关联）；统计 provider/gate retry；关键事件挂对应 span
  - `RunTrace`（trace_id/schema_version/spans/attempts/gate_retries/provider_retries/run_status/error/diff_summary）；文本经 Sanitizer + 轻量白名单（**不做严格注册表**）
  - `finalize_best_effort()`（幂等）→ `LocalJsonExporter` 原子写 `runs/<id>/trace.json`
  - **不做**：Event_Journal / durability / delta-response 一致性校验（→P1）
  - 测试：span 树父子；retry 分别计数；trace.json 不含正文全文/密钥（Property 7）；导出幂等
  - _Requirements: 7.1-7.5, 7.9, 7.10, 7.11, 8.1, 8.3, 9.1, 9.2, 13.3_
  - _Design: Components§7(精简), D4, Property 7_

- [ ] 8. Generator 流式改造（generate/generator.py）
  - `run_polish(..., bus=None, cancel_token=None, stream=False)`；`bus = bus or NullEventBus()`；`generation_scope` 循环外（gen_id 跨重试共享），每轮 `attempt_scope`+`span("generation_attempt")`，内 `llm_call_scope`+`span("llm_call")`+`span("gate")`
  - `GENERATION_ATTEMPT_START`；`LLM_CALL_START(call_mode=stream|complete, prompt=sanitize_messages)`；`LLM_USAGE`（scope 内）；`LLM_CALL_END(status=completed|cancelled|failed, response=sanitize_text)`（**普通异常也发 failed**）；`GATE_RESULT(candidate_id,source_llm_call_id)`；`finally` 保证 `GENERATION_ATTEMPT_END(status=accepted|rejected|cancelled|error)`
  - `stream=True` 用 enumerate 发 `LLM_DELTA(chunk_index)` + Candidate_Buffer 上限；`stream=False`（默认）走 complete，逐字节不变；provider 按 `as_sink_or_none()` 决定是否传 sink；取消用 `OperationCancelled`
  - 测试：默认路径等价（Property 6）；流式拼接==最终文本（Property 2）；thinking 不入 buffer（Property 3）；重试共享 gen_id/attempt 递增/llm_call_id 每次新；首块前/中途/complete/sanitizer 失败各恰一个 CALL_END（Property 12）；Gate 拒绝=rejected 非 failed
  - _Requirements: 2.1, 2.4, 3.1, 3.2, 3.3, 4.1, 5.4, 5.5, 10.1, 10.2, 10.3, 13.2, 15.1, 15.3, 15.4, 11.1_
  - _Design: Components§8, D1, D3, D6, Property 2/3/10/12_

- [ ] 9. Router/Interview/SOP 事件 + Coordinator 装配 + 简单持久化事件（chat.py / session / intents）
  - Router/Interview `with bus.span(...)` 内发 `ROUTE_DECISION/INTERVIEW_QA`；rewrite/add_section 生成循环复用任务 8 逻辑 + `RETRIEVAL_*`
  - `FileBackedSession.write_working/save_to_source` 加**可选 bus**（按方法传，不长存）：原子写 working/source（tmp+os.replace）→ 发 `WORKING_COPY_UPDATE/WORKSPACE_COMMIT/SOURCE_SAVE`；**兜底沿用现有 git 快照 undo（不做事务清单/锁，→P1）**
  - Coordinator（chat.py）：每消息新建 run_id/RunContext/EventBus + FanOutSink（无下游用 NullEventBus）；provider 按调用传 `event_sink`；`with bus.span("run")` 包裹；最外层 `finally` 发恰一次 `WORKFLOW_END` + 直接 `recorder.finalize_best_effort()`；取消唯一发 `RUN_CANCELLED`+`WORKFLOW_END(cancelled)`；`:save` 独立 run；Ctrl-C→cancel_token；docx 二进制路径不受影响
  - 测试：事件挂对应 span；三持久化事件不混类；gate 通过后取消不写；各层异常/取消/正常各恰一个 WORKFLOW_END 且 finalize 被调用（Property 17）；取消后源/Facts/undo 栈不变（Property 1）
  - _Requirements: 1.6, 1.7, 6.1, 6.2, 6.3, 6.4, 16.1, 16.2, 16.3, 16.4, 4.2, 4.3, 4.5, 4.7, 4.8, 2.2, 11.5_
  - _Design: Components§7/§9(精简)/§10, D5(两层保证), D6, Property 1/5/11/17_

- [ ] 10. 集成、模式矩阵、向后兼容
  - 模式矩阵：stream off/on × trace off/on 四种；既有 65 测试全绿（不注入 bus / stream=False，Property 6）
  - 端到端捕获型 sink 断言事件序列（WORKFLOW_START→span(run)→ROUTE→INTERVIEW→attempt(llm_call/gate)→WORKING_COPY_UPDATE→WORKSPACE_COMMIT→span(run) END→WORKFLOW_END）与 trace.json 结构/隐私；`runs/` 入 `.gitignore`
  - 手动验收：stream on 候选滚动+标识；trace on 生成 trace.json
  - _Requirements: 11.1, 11.3, 8.3, 8.4_
  - _Design: Testing Strategy, Correctness Properties(P0 子集)_

## P1 — Deferred Backlog（设计已存档，按需再做）

以下为生产/多进程/断电场景的硬化，v1 不实现；需要时按 design.md 对应小节落地：

- **持久性与崩溃恢复**：Event_Journal（`events.ndjson`）+ `durability=none|normal|strict` + flush/fsync + 崩溃重建工具（design D8、D5-journal、Property 8）
- **事务与并发**：working+facts 事务清单（含 hash/version/时间戳）+ 回滚/前滚 + `recovery_status` + 工作区锁 `workspace/.lock` + `session_health`（design §9 重型、Property 13/15/16）
- **强校验与性能**：Payload **严格**校验注册表（拼错字段报错）+ `DeltaBatcher`（合并高频增量）+ delta/response 一致性校验（design §5/§7、Property 14）
- **状态细化**：多维状态灾难维度（`recovery_status/trace_collection_status`）、`LEGACY_KIND_MAP`（出现旧消费者时）
- **导出扩展**：Langfuse/OpenTelemetry 的 `TraceExporter` 实现（design §7/Req 9）

## Notes

- **薄脊柱目标**：流式可见 + 结束落一个 trace.json；默认零影响（Property 6）；不破坏 65 测试。
- **P0 保留的关键正确性**：候选隔离(1)、流式一致(2)、thinking 不入账(3)、不可变+唯一(4)、默认零影响(6)、span 终态(9)、关联可追溯(10)、调用闭环(12)、结束必达(17，尽力导出)。
- **持久化先用现有 git 快照 undo 兜底**，不引入事务/锁；观测失败绝不拖垮论文业务。
- P0 跑通并用真实模型验证 agent 后，再需求驱动地从 P1 取项。
- `runs/` 加入 `.gitignore`。
