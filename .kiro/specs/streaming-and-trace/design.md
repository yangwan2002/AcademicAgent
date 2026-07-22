# Design Document

## Overview

本功能为学术论文写作 Agent 引入**流式输出**与**轻量运行追踪（Run Trace）**，二者共享同一条事件管道：一次运行产生一串带信封、不可变的结构化 `Event`；单一 `EventBus` 盖章后交 `FanOutSink` fan-out 给两个下游——`ConsoleRenderer`（实时呈现）与 `SimpleTraceRecorder`（构建 span 树，结束时落一个 `trace.json`）。

铁律不变：working copy 是单一真相、源文件在用户 save 前不动、writer→gate→(repair 重试)。流式呈现的永远是**待 gate 校验的候选**，不实时污染 working copy；取消或失败时半截候选安全丢弃。

**本文档只描述 v1（薄脊柱）。** 生产级的持久性/事务/并发硬化（Event_Journal、durability、事务清单、工作区锁等）**已移入文末「Appendix: P1 Reliability Backlog」作为设计存档，v1 不实现**。这样实施者读主文档即等于读 v1 范围。

命名/收口约定：只用 `EventBus`（无 EventEmitter）；渲染器统一叫 `ConsoleRenderer`；取消异常统一 `OperationCancelled`（核心模块 `paper_agent/errors.py`）；用 `LLM_CALL_START/END`；`attempt` 非 `attempt_id`。

## 关键设计决策（v1）

### D0. 统一 EventBus：两入口 + 强制权威信封 + 防御式

- 唯一消费者接口 `EventSink`。`EventBus` 是唯一生产者，**实现 `EventSink`**（provider 用 `emit(Event)`），并提供 `publish(kind, **data)`（业务层用）。二者都经 `_stamp` **强制覆盖**权威信封（不尊重旧值，防串 run）：

```python
def _stamp(self, e) -> Event:
    return build_event(
        kind=e.kind, message=e.message, data=deep_freeze(e.data),   # 仅这些来自调用方
        trace_id=self.ctx.run_id, event_id=new_uuid(), sequence=self.ctx.next_sequence(),
        ts=time.time(), span_id=self.ctx.current_span_id,
        parent_span_id=self.ctx.current_parent_span_id,
        generation_id=self.ctx.generation_id, attempt=self.ctx.attempt,
        llm_call_id=self.ctx.llm_call_id, source_event_id=getattr(e, "event_id", "") or None)
```

- **防御式（观测不拖垮业务）**：`publish/emit` 内部对 `_stamp/deep_freeze/fan-out` 全 try 包裹；任何观测侧异常都**不向业务传播**，记入 `ObservabilityHealth`。
- **返回投递结果（DeliveryResult）**：`publish/emit` 返回 `DeliveryResult{enabled, delivered_count, failed_count}`，供调用方（如 Coordinator 判断 WORKFLOW_END 是否送达）。`NullEventBus` 返回 `enabled=False`。

### D1. 关联层级

`generation_id`（一次 Generator 调用/写作任务，跨其内部所有 gate 重试共享）> `attempt`（重试序号）> `llm_call_id`（一次逻辑 LLM 调用）。`(generation_id, attempt, llm_call_id)` 唯一定位一次 LLM 调用。Provider_Retry 属同一 `llm_call_id`。

### D2. 上下文用 scope，不直接改 bus.ctx

`generation_scope/attempt_scope/llm_call_scope`：进入设 ctx、退出**自动恢复**（禁止业务写 `bus.ctx`；`NullEventBus` 的 scope 返回 `nullcontext()`）。`span()` 用 `SpanScope`。

### D3. 流式 opt-in；候选只进内存

`stream=True`→`provider.stream()`，content 入内存 `Candidate_Buffer` 并 `publish(LLM_DELTA)`；`stream=False`（默认）→`provider.complete()`，与现状逐字节一致。两路径都发 `LLM_CALL_START/END`、`LLM_USAGE`、`GATE_*` → trace 不依赖流式。候选通过 gate 前绝不触碰 working copy。

### D4. 取消：`OperationCancelled` 一路 raise 到 Coordinator（唯一取消模型）

- 检测到取消（provider/generator）→ 抛 `OperationCancelled`；中间层可补事件（如 `LLM_CALL_END(cancelled)`、`aspan.cancel()`）后**继续 `raise`**，**不**吞成 Outcome。
- 所有正在展开的 `SpanScope.__exit__` 识别 `OperationCancelled` → 自动置 `cancelled`（llm_call/generation_attempt/run span 一并 cancelled，与 run_status 一致，杜绝「Run Span=ended 但 run_status=cancelled」矛盾）。
- **Coordinator 是唯一捕获点**：`except OperationCancelled:` → `state.run_status="cancelled"` + 唯一发一次 `RUN_CANCELLED`。Generator/SOP 不发 `RUN_CANCELLED`、不把取消吞成正常返回。

### D5. Trace 最终化：Coordinator 唯一负责，最终状态直接传入

- `WORKFLOW_END` 是**普通结束事件**（给各 sink 标记结束）；**不**由它触发导出。
- **Coordinator 在最外层 `finally` 直接调 `recorder.finalize_best_effort(final_state=...)`**（唯一最终化点，幂等）。最终状态直接传给 recorder，**不依赖** WORKFLOW_END 送达（它可能投递失败）。
- 两层保证：观测失败绝不让论文任务失败；trace 尽力导出。

### D6. Span 归属 + Gate

- v1 span 集：`run / generation_attempt / llm_call / gate / working_copy_update`（router/interview 先作**事件** `ROUTE_DECISION/INTERVIEW_QA`，不单独开 span；复杂化后再加，见 Appendix 备选）。
- **GATE_RESULT 归属 Gate span**（在 `with bus.span("gate")` 内发，此时 llm_call scope 已退出），经 `candidate_id/source_llm_call_id` 关联被检查候选及其来源 LLM 调用。

### D7. 事件敏感等级：LLM_DELTA 是 sensitive_ephemeral

- `SENSITIVE_EPHEMERAL_KINDS = {LLM_DELTA}`（携原始候选文字，供 Renderer 实时显示、Recorder 增量算 hash/preview）。
- **禁止**把此类事件的原文写入 `trace.json`、普通日志、错误报告，或转发给第三方 Exporter（未来接 Langfuse/OTel 时默认不转发原始 LLM_DELTA，防整篇论文外泄）。
- `LLM_CALL_START.prompt`→SafePrompt、`LLM_CALL_END.response`→SafeText 在发布前脱敏；`LLM_THINKING` 无原文。

## Architecture

```text
业务层：Router · Interview · SOP · Generator · FileBackedSession
Provider：ResilientLLMProvider（经 event_sink=bus 传入）
        │ publish(kind,**data)  |  emit(Event)
        ▼
   ┌─────────────────────┐   _stamp 强制盖章 + deep_freeze；防御式；返回 DeliveryResult
   │ EventBus (EventSink) │   +RunContext +generation/attempt/llm_call/span scope
   └──────────┬──────────┘
              ▼
   ┌─────────────────────┐   隔离不静默、不打 payload；置 ObservabilityHealth
   │ FanOutSink           │
   └────┬───────────┬────┘
        ▼           ▼
 ┌──────────────┐  ┌──────────────────────────┐
 │ ConsoleRenderer│ │ SimpleTraceRecorder       │
 │ (流式/状态)    │ │  · SPAN_START/END 建树     │
 └──────────────┘  │  · Sanitizer + 轻量白名单  │
                   │  · 容忍残缺/乱序 Span      │
                   └───────────┬──────────────┘
   Coordinator finally ────────┘ recorder.finalize_best_effort(final_state)
                                      ▼  原子写 runs/<run_id>/trace.json
```

## Components and Interfaces

### 1. Event（events.py，frozen + 深度不可变 + serialize + 统一 EventKind）

```python
@dataclass(frozen=True)
class Event:
    kind: EventKind
    message: str = ""
    data: Mapping = field(default_factory=dict)
    trace_id: str = ""; span_id: str = ""; parent_span_id: str = ""
    ts: float = 0.0; duration_ms: float | None = None
    event_id: str = ""; sequence: int = -1; schema_version: int = 1
    generation_id: str = ""; attempt: int | None = None
    llm_call_id: str = ""; source_event_id: str | None = None
    def __post_init__(self):
        object.__setattr__(self, "data", deep_freeze(self.data))  # dict→MappingProxy,list→tuple,递归
```

- `deep_freeze` 递归；`event.data["x"]=...` 与嵌套 `data["v"].append(...)` 均抛错。`serialize_event(event)->dict` 转普通 JSON。
- 新增 EventKind：`SPAN_START/SPAN_END/ROUTE_DECISION/INTERVIEW_QA/RETRIEVAL_START/RETRIEVAL_END/GENERATION_ATTEMPT_START/GENERATION_ATTEMPT_END/GATE_START/GATE_RESULT/GATE_RETRY/LLM_CALL_START/LLM_CALL_END/RUN_CANCELLED/WORKING_COPY_UPDATE/WORKSPACE_COMMIT/SOURCE_SAVE`；复用既有 `LLM_DELTA/LLM_THINKING/LLM_USAGE/LLM_RETRY/WORKFLOW_START/WORKFLOW_END`。既有 kind 值不动（不做同值别名）。

### 2. RunContext + EventBus + NullEventBus + SpanScope（observability/bus.py）

```python
class EventBus(EventSink):
    def publish(self, kind, message="", **data) -> DeliveryResult: ...   # 防御式 + 返回投递结果
    def emit(self, event) -> DeliveryResult: ...                          # provider 入口
    def span(self, name, **attrs) -> SpanScope: ...
    def generation_scope/attempt_scope/llm_call_scope(...): ...           # 退出自动恢复

class SpanScope:
    def __enter__(self):
        self.parent_id = ctx.current_span_id; self.span_id = new_uuid()
        ctx.push_span(self.span_id)                          # 先 push
        bus._publish_span(SPAN_START, span_id=..., parent_span_id=self.parent_id, name=...)
        return self
    def cancel(self): self._state = "cancelled"
    def __exit__(self, et, e, tb):
        state = "cancelled" if (e and issubclass(et, OperationCancelled)) \
                else "failed" if e else (self._state or "ended")
        try: bus._publish_span(SPAN_END, span_id=..., parent_span_id=..., span_state=state, duration_ms=...)
        finally: ctx.pop_span(self.span_id)                  # 无论 END 成败都弹栈
        return False                                          # 不吞异常（取消继续向上）

class NullEventBus(EventSink):
    def publish(self,*a,**k): return DeliveryResult(enabled=False,delivered_count=0,failed_count=0)
    def emit(self,*a,**k):    return DeliveryResult(enabled=False,delivered_count=0,failed_count=0)
    def as_sink_or_none(self): return None
    def span(self,*a,**k): return NullSpanScope()             # 有 no-op cancel/degrade
    def generation_scope/attempt_scope/llm_call_scope(...): return contextlib.nullcontext()
```

- 时长用 `time.monotonic()` 差；`ts` 仅展示。REPL 单线程，sequence 无锁。

### 3. FanOutSink + ObservabilityHealth（observability/fanout.py）

```python
@dataclass
class ObservabilityHealth:
    pipeline_failed: bool = False          # EventBus 自身故障（stamp/freeze/sequence 等，早于下游）
    trace_collection_failed: bool = False
    renderer_failed: bool = False
    warnings: list[str] = field(default_factory=list)
```

- 转发同一 frozen Event；下游异常 `_report` **只打 sink 类名/EventKind/event_id/异常类型**（不打 payload），并置 `trace_collection_failed`/`renderer_failed`；继续其余下游。零下游接受不转发。EventBus 自身（下游之前）出错置 `pipeline_failed`。

### 4. Sanitizer（observability/sanitize.py）

- `sanitize_text(text)->SafeText`（`@dataclass(frozen=True){hash,preview≤500,length}`）；`redact()` 剔密钥；`sanitize_messages(messages)->SafePrompt`（规范化 `json.dumps(sort_keys=True,ensure_ascii=False,separators=(",",":"))` 后 redact→hash→preview）。覆盖 prompt/response/diff/正文/interview/violations/errors。

### 5. Provider 调用级 EventSink（base/openai_compatible/anthropic/mock/resilient）

- `stream/complete` 加**可选** `event_sink=None`（默认 None → 逐字节不变）；resilient 用它发 `LLM_RETRY`；按调用传入，不长存。调用方 `bus.as_sink_or_none()` 关闭时不传该参数、走原始签名。

### 6. ConsoleRenderer（observability/renderer.py）

- 实现 `EventSink`；职责=把运行状态与流式候选显示到控制台（"streaming" 只是它处理的一类事件）。
- `LLM_DELTA`→候选区（该 llm_call 首增量前打 `[候选·校验中…]`）；`LLM_THINKING`→只显「正在分析…」永不显原文；`GATE_RESULT(fail)`→✗+violations；`GENERATION_ATTEMPT_START`→分隔线分区新候选、`GATE_RETRY`→只显重试原因；`ROUTE_DECISION/INTERVIEW_QA/RETRIEVAL_*/GATE_START`→单行状态；`RUN_CANCELLED`→「已取消」；`GENERATION_ATTEMPT_END(rejected|cancelled|error)`→`[该候选已作废，未写入文档]`。
- `escape_terminal_controls()`：过滤 ANSI/回车覆盖/清屏，仅留普通换行/制表/Unicode（防伪造「已通过」）。

### 7. SimpleTraceRecorder + Span/RunTrace + LocalJsonExporter（tracing/）

- 依 `SPAN_START/SPAN_END` 建 span 树，凭 span 栈定父子；GATE_RESULT 挂 Gate span；统计 provider/gate retry。
- **容忍残缺/乱序 Span（关键，因观测错误不拖垮业务可能丢事件）**：
  - 只有 END 无 START → 建 orphan span + `warning`；
  - 只有 START 无 END → `state=None`（未终结）；
  - parent 不存在 → 暂挂 root + `missing_parent` warning；
  - 重复 END → 忽略第二次 + warning。
  一个事件丢失绝不能让 Recorder 崩溃（否则引发更多观测失败）。
- 文本经 Sanitizer + **轻量白名单**（v1 不做严格注册表）；**不持久化** `SENSITIVE_EPHEMERAL_KINDS` 的原文（LLM_DELTA 只进增量 hash/preview，不写入 trace.json）。
- `finalize_best_effort(final_state)`（幂等）→ `LocalJsonExporter` 原子写 `runs/<run_id>/trace.json`（tmp+os.replace，UTF-8，建目录）。最终状态由 Coordinator 传入。
- **v1 不做**：Event_Journal / durability / fsync / delta-response 一致性校验（→ Appendix）。

### 8. Generator 流式改造（generate/generator.py）

```python
def run_polish(original, goal, llm, gate, *, max_retry=2, bus=None, cancel_token=None, stream=False):
    bus = bus or NullEventBus(); gen_id = uuid4().hex; violations = []
    with bus.generation_scope(gen_id):
        for attempt in range(1, max_retry + 2):
            with bus.attempt_scope(attempt), bus.span("generation_attempt") as aspan:
                attempt_status = "error"
                try:
                    bus.publish(GENERATION_ATTEMPT_START, attempt=attempt)
                    if attempt > 1: bus.publish(GATE_RETRY, attempt=attempt)
                    call_id, candidate_id = uuid4().hex, uuid4().hex
                    with bus.llm_call_scope(call_id), bus.span("llm_call"):
                        try:
                            bus.publish(LLM_CALL_START, model=llm.model_name,
                                        call_mode=("stream" if stream else "complete"),
                                        prompt=sanitize_messages(msgs))
                            candidate, usage, ustatus, n = _generate(llm, msgs, stream, bus, cancel_token)
                            bus.publish(LLM_USAGE, usage=usage, usage_status=ustatus)
                            bus.publish(LLM_CALL_END, status="completed",
                                        response=sanitize_text(candidate), delta_count=n)
                        except OperationCancelled:
                            bus.publish(LLM_CALL_END, status="cancelled"); raise
                        except Exception as exc:
                            bus.publish(LLM_CALL_END, status="failed", error=sanitize_text(str(exc))); raise
                    with bus.span("gate"):
                        bus.publish(GATE_START)
                        result = gate.check(original, candidate)
                        bus.publish(GATE_RESULT, passed=result.passed, violations=result.violations,
                                    candidate_id=candidate_id, source_llm_call_id=call_id)
                    if result.passed:
                        attempt_status = "accepted"
                        return PolishOutcome(ok=True, text=candidate, attempts=attempt)
                    attempt_status = "rejected"; violations = result.violations
                except OperationCancelled:
                    attempt_status = "cancelled"; aspan.cancel(); raise    # 一路 raise 到 Coordinator（D4）
                finally:
                    bus.publish(GENERATION_ATTEMPT_END, status=attempt_status)   # 一定发出
    return PolishOutcome(ok=False, attempts=max_retry+1, last_violations=violations,
                         error_category="validation_exhausted")
```

- 恰一个 `LLM_CALL_END`（completed/cancelled/failed 覆盖首块前/中途/complete/sanitizer 失败）。
- `_generate`：`stream=True` 用 `enumerate` 发 `LLM_DELTA(chunk_index)`、检查取消抛 `OperationCancelled`、Candidate_Buffer 设 `max_candidate_chars/max_delta_count` 上限；`stream=False` 走 `complete()`。thinking 只 `publish(LLM_THINKING)`（无原文、不入 buffer）。provider 按 `as_sink_or_none()` 决定是否传 `event_sink`。
- attempt 状态 `accepted/rejected/cancelled/error`（Gate 拒绝=rejected 非 failed）。**取消一路 raise**（不吞成 Outcome，D4）。rewrite/add_section 同构（各自 `RETRIEVAL_*`）。

### 9. SOP / FileBackedSession 持久化（v1 简单版）

- `write_working(text,*,bus=None)`/`save_to_source(*,bus=None)`（bus 按方法传，不长存）：`.tmp`+`os.replace` **原子写**（working 与 source 均保留权限/编码/换行）→ 发 `WORKING_COPY_UPDATE/WORKSPACE_COMMIT/SOURCE_SAVE`（`with bus.span("working_copy_update")` 包裹 working 写入）。
- **兜底沿用现有 git 快照 undo**（`FileBackedSession.undo`）；v1 **不做**事务清单/回滚-前滚/工作区锁/session_health（→ Appendix）。
- SOP 在 gate 通过后、写入前查一次 `cancel_token`（gate→写窗口，Req 4.7）。

### 10. Run Coordinator（scripts/chat.py）

```python
run_id=uuid4().hex; ctx=RunContext(run_id); 
downstreams=[]; recorder=None
if show_stream: downstreams.append(ConsoleRenderer())
if enable_trace: recorder=SimpleTraceRecorder(run_id); downstreams.append(recorder)
bus = EventBus(FanOutSink(downstreams, health), ctx) if downstreams else NullEventBus()
state = RunState(); bus.publish(WORKFLOW_START); ended=False
try:
    with bus.span("run"):
        state = execute_run(bus=bus, cancel_token=ct, ...)   # provider 按调用传 event_sink=bus
except OperationCancelled:
    state.run_status="cancelled"; bus.publish(RUN_CANCELLED, phase=state.phase)
except Exception as exc:
    state.run_status="failed"; state.error=sanitize_text(str(exc))
finally:
    state.trace_collection_status = ("not_requested" if recorder is None
        else "failed" if (health.pipeline_failed or health.trace_collection_failed) else "completed")
    if not ended: bus.publish(WORKFLOW_END, **state.to_payload()); ended=True
    if recorder is not None: recorder.finalize_best_effort(final_state=state.to_payload())  # 唯一最终化
```

- 每消息一个 run（新 bus）；provider 按调用传 `event_sink=bus`（不改 provider 全局）。`:save` 是**独立 run**。Ctrl-C→`cancel_token.cancel()`。docx 二进制会话路径不受渲染影响（`_build_index` 已处理）。

## Data Models

- **Event**：见 §1（frozen + deep_freeze + 信封 + `llm_call_id` + `source_event_id`）。
- **DeliveryResult**：`{enabled: bool, delivered_count: int, failed_count: int}`；`delivered = delivered_count>0`。
- **SafeText / SafePrompt**：`@dataclass(frozen=True){hash,preview,length}`。
- **ObservabilityHealth**：`{pipeline_failed, trace_collection_failed, renderer_failed, warnings[]}`。
- **Span**：`span_id, parent_span_id, name, generation_id?, attempt?, llm_call_id?, candidate_id?(gate), start_ts, start_monotonic, end_ts?, duration_ms?, state ∈ {None,ended,cancelled,failed}, attributes(脱敏), children[]`。
- **RunTrace**：`trace_id, schema_version, started_at, ended_at, root(Span), attempts, gate_retries, provider_retries, run_status ∈ {completed,failed,cancelled}, quality_status ∈ {normal,degraded}, working_copy_status ∈ {unchanged,updated,rolled_back}, trace_collection_status ∈ {not_requested,completed,failed}, diff_summary(preview), error{category,preview}?`。
- 产物：`runs/<run_id>/trace.json`（`finalize` 时原子写一次）。

### Event Payload Schema（各 kind `data` 键；文本持久化前过 Sanitizer）

| kind | data 关键键 |
|---|---|
| ROUTE_DECISION | intent, section_id?, target_ext?, presets?, title? |
| INTERVIEW_QA | question(preview), answer(preview) |
| RETRIEVAL_START / RETRIEVAL_END | query(preview) / count |
| GENERATION_ATTEMPT_START / GENERATION_ATTEMPT_END | attempt / attempt, status(accepted|rejected|cancelled|error) |
| GATE_RETRY | attempt |
| LLM_CALL_START | model, **call_mode(stream\|complete)**, prompt(SafePrompt) |
| LLM_DELTA | text, chunk_index（**sensitive_ephemeral：不写入 trace.json 原文**）|
| LLM_THINKING | phase?（无原文）|
| LLM_CALL_END | status(completed\|cancelled\|failed), response(SafeText), delta_count, error? |
| LLM_USAGE | prompt_tokens, completion_tokens, latency_ms, usage_status(reported\|estimated\|unavailable) |
| LLM_RETRY | retry_no, delay, error_type, error_preview |
| GATE_START / GATE_RESULT | gate_name / passed, violations(preview[]), candidate_id, source_llm_call_id |
| SPAN_START / SPAN_END | name, attributes / span_state（span_id/parent/duration_ms 只在信封）|
| WORKING_COPY_UPDATE / WORKSPACE_COMMIT / SOURCE_SAVE | path(basename), status |
| RUN_CANCELLED | phase |
| WORKFLOW_END | run_status, quality_status, working_copy_status, trace_collection_status, diff_summary(preview), error? |

## Error Handling

| 失败点 | 处理 |
|---|---|
| EventBus 自身（stamp/freeze/sequence）失败 | 不外抛；`health.pipeline_failed=True`（§3）|
| 下游 sink.emit 抛异常 | FanOutSink 捕获+上报（不含 payload）+置对应 health；继续其余下游 |
| provider.stream 已产出后失败 | 遵循 ResilientLLMProvider 不重发已产出增量；`LLM_CALL_END(failed)`；丢 buffer |
| LLM 调用普通异常 | `LLM_CALL_END(failed,error)` 恰一次（Property 12）；llm_call span=failed |
| 取消（OperationCancelled）| 各 span 记 cancelled；`GENERATION_ATTEMPT_END(cancelled)`（finally）；**一路 raise 到 Coordinator**；working copy 不变 |
| gate 全部拒 | working copy 不变；`run_status=failed, error.category=validation_exhausted` |
| gate 通过后写入前取消 | 放弃写入，working copy 不变（Req 4.7）|
| Working_Copy/Source 写中断 | tmp+os.replace 全有/全无（Property 5）|
| 关闭观测（NullEventBus）| `as_sink_or_none()`→None → provider 走原始签名（Property 6）|
| Trace 收集异常 / 导出失败 | `trace_collection_status`=failed；不影响业务（旁路）；导出失败单独记 stderr |
| Span 事件残缺/乱序 | orphan/unfinished/missing_parent/重复 END 各有降级处理（§7），Recorder 不崩 |

## Correctness Properties

### Property 1: 候选隔离
候选通过 Gate 前 Working_Copy 字节不变。
**Validates: Requirements 2.3, 2.5, 4.2, 4.3**

### Property 2: 单次流式调用增量一致
单次流式调用 content 增量按序拼接 == 该次调用聚合最终文本 == Candidate_Buffer 最终内容。
**Validates: Requirements 10.1, 10.2**

### Property 3: thinking 不入账
thinking 不进 Candidate_Buffer、不参与 Gate、不持久化/不展示原文。
**Validates: Requirements 5.4, 5.5, 5.6, 10.3**

### Property 4: 事件深度不可变且增量唯一
Event（含嵌套 data）不可变；同一段 content 增量至多发射/渲染/记录一次。
**Validates: Requirements 1.4, 15.1, 15.2, 15.3, 15.4**

### Property 5: 写入原子性
Working_Copy / Source 写入全有或全无。
**Validates: Requirements 4.8**

### Property 6: 默认零影响
不注入 bus（NullEventBus）+ `stream=False` + `as_sink_or_none()` 不传 event_sink → 各意图对外行为逐字节不变。
**Validates: Requirements 11.2, 11.5**

### Property 7: 隐私封闭
trace.json 不含论文全文与密钥；持久化文本过 Sanitizer；LLM_DELTA 原文不写入 trace/日志/Exporter（sensitive_ephemeral）。
**Validates: Requirements 8.1, 8.3, 8.4, 8.5**

### Property 8: 统一取消模型
取消经 `OperationCancelled` 一路 raise 至 Coordinator；沿途所有 span 记 `cancelled`，`run_status=cancelled` 与 Run span 状态一致（无 ended/cancelled 矛盾）。
**Validates: Requirements 4.4, 7.10**

### Property 9: Span 终态正确
已结束 Span 恰一终态；取消→cancelled、异常→failed、否则 ended；未结束无终态；SPAN_END 发布失败也弹栈。
**Validates: Requirements 7.10, 7.11, 4.4**

### Property 10: 关联可追溯且信封权威
生成相关事件均带 `(generation_id, attempt, llm_call_id)`；Bus 强制覆盖权威信封，provider 旧值不串 run。
**Validates: Requirements 12.4, 13.1, 13.2, 12.1**

### Property 11: 调用生命周期闭环
每个 `LLM_CALL_START` 恰一 `LLM_CALL_END`；每个 `GENERATION_ATTEMPT_START` 恰一 `GENERATION_ATTEMPT_END`。
**Validates: Requirements 7.5, 4.4**

### Property 12: 结束必达（尽力导出）
无论正常/取消/异常，最外层 finally 发恰一次 WORKFLOW_END，并**直接**调一次 `recorder.finalize_best_effort(final_state)`（幂等）；即使 WORKFLOW_END 投递失败，trace 仍被尝试导出。
**Validates: Requirements 7.1, 7.6, 9.2**

### Property 13: 观测不拖垮业务
EventBus/Sanitizer/Payload/下游任何观测侧失败都不向业务传播；论文任务结果不受影响。
**Validates: Requirements 11.1, 11.2**

## Testing Strategy

- **模式矩阵**：stream off/on × trace off/on 四种全测。
- **默认零影响**：不注入 bus + stream=False 跑既有 65 测试全绿（Property 6）；NullEventBus 下 provider 收到原始调用（无 event_sink）。
- **取消统一**：取消从 provider/generator 一路 raise 到 Coordinator，沿途 span 全 cancelled、run_status=cancelled（Property 8）；无 Run span=ended 矛盾。
- **Span**：嵌套 run→generation_attempt→llm_call→gate 父子正确；attempt 取消/异常仍有 END；SPAN_END 发布异常仍弹栈；**残缺/乱序**（丢 START/END、缺 parent、重复 END）Recorder 不崩。
- **调用闭环**：首块前/中途/complete/sanitizer 失败各恰一个 LLM_CALL_END（Property 11）。
- **不可变**：`data["x"]=...` 与嵌套 `data["v"].append(...)` 均抛错。
- **DeliveryResult**：全关→enabled=False；部分下游失败→delivered_count/failed_count 正确。
- **隐私**：prompt 列表脱敏；trace.json 不含 LLM_DELTA 原文与密钥；ANSI 控制符转义；错误上报不含 payload。
- **最终化**：Router/Interview/Generator/持久化各抛异常 + 取消 + 正常，各恰一个 WORKFLOW_END 且 finalize 被调用一次（Property 12）。
- **health**：pipeline/trace/renderer 失败分别置位，trace_collection_status 正确（含 not_requested）。

## Requirements Traceability（v1）

| 需求 | 设计落点 |
|---|---|
| Req 1 | D0, §2/§3, 架构图 |
| Req 2/4 | D3/D4, §8, §9 原子写, Property 1/5/8/9 |
| Req 3/6 | §8 GATE/ATTEMPT, §6, ROUTE/INTERVIEW/RETRIEVAL 事件 |
| Req 5 | §6 thinking 不显原文, D7 |
| Req 7 | D5/D6, §7 span 树, Property 9/11/12 |
| Req 8 | §4 Sanitizer, D7 敏感等级, §3 不打 payload, Property 7 |
| Req 9 | §7 LocalJsonExporter |
| Req 10 | D3, §8 buffer 一致, Property 2 |
| Req 11 | D0 防御式, §5 默认零影响, Property 6/13 |
| Req 12/13 | §1 信封, D0/D1 llm_call_id, Property 10 |
| Req 15 | D0 增量唯一源, Property 4 |
| Req 16 | §9 简单持久化事件（事务/恢复 → Appendix）|

---

## Appendix: P1 Reliability Backlog（设计存档，v1 不实现）

以下为生产/多进程/断电场景的硬化设计，**v1 不实现**；出现真实需求时按本附录落地。此处保留前期充分论证的成果，避免重复推演。

### A1. Event_Journal 与持久性等级
- append-only `runs/<run_id>/events.ndjson`：每行 `serialize_event` 投影（信封 + 脱敏白名单 payload）；content 增量按 `llm_call_id` 聚合并入 `LLM_CALL_END` 投影（不造 snapshot 假事件、不撞 sequence）。
- `durability = none | normal | strict`（默认 normal）：normal=粗粒度 flush、仅 WORKSPACE_COMMIT/WORKFLOW_END fsync；strict=多边界 fsync。
- 崩溃重建工具：**先按原行序**校验 sequence 单调/无重复（先排序会掩盖倒退/重复），再按 sequence 重放；最后一行不完整→忽略+warning；中间损坏→journal_corrupted；增量缺口→skipped_range；SPAN_START 无 END 判未终结。

### A2. working + facts 事务与崩溃恢复
- 事务清单 `workspace/transactions/<txn>.json`（本身原子写 tmp+fsync+replace），字段：`old/new working/facts backup+tmp+hash`、`version_backend/version_id`、`current_step(prepared→working_replaced→facts_replaced→version_created→committed)`、`created/updated_at`。
- 启动扫描 `status!=committed`：按 `current_step` 区分——早期（未一致）**回滚**；晚期（working+facts 已一致新版）**前滚**补完到 committed（不撤销成功更新）。恢复**幂等**。
- 补偿事件 `WORKING_COPY_ROLLBACK(status)`：回滚后必发，最终态由最后事件确定。
- Facts 严格事务：需更新 Facts 时纳入本事务，失败按旧引用恢复 working+facts。

### A3. 并发与会话健康
- 工作区锁 `workspace/.lock`（启动获取、退出释放、检测陈旧锁）；v1 明确**单进程约束**。
- `session_health ∈ {healthy, recovery_required, read_only}`：`recovery_status=failed` → `recovery_required` → 禁止 polish/rewrite/add_section/save，仅查看/导出诊断/人工恢复。

### A4. 强校验与性能
- Payload **严格**校验注册表（`PAYLOAD_SCHEMAS` + 状态相关校验：CALL_END completed→response+delta_count、failed→error 等；拼错/未知字段报错）。
- `DeltaBatcher`（按 ~20–50ms 或 ~256–1024 字符合并高频 LLM_DELTA，降事件数）。
- delta/response 一致性校验：`call_mode==stream` 时 `DeltaAccumulator.hash == response.hash`，不一致记 `delta_response_mismatch`。

### A5. 状态细化与导出扩展
- 多维状态灾难维度：`workspace_version_status / source_save_status / recovery_status`（配合 A2/A3）。
- `LEGACY_KIND_MAP`（出现旧 EventKind 消费者时的兼容映射，不用同值 Enum 别名）。
- `TraceExporter` 的 Langfuse/OpenTelemetry 实现（默认**不**转发 `SENSITIVE_EPHEMERAL_KINDS` 原文）。

### A6. 备选 Span 细化
- 当 Router/Interview 逻辑复杂化后，为其增加独立 span（v1 先作事件）；增加 `workspace_commit` span 等。

### 对应的 P1 Correctness Properties（随落地启用）
- 序号全序唯一（journal，A1）；事务崩溃可恢复（A2）；恢复失败禁写（A3）；最终状态可还原/回滚补偿（A2）；敏感 payload 严格校验（A4）。
