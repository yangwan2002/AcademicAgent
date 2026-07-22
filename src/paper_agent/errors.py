"""核心中立异常模块（避免观测层 ↔ 业务层循环依赖）。

`OperationCancelled` 是**统一取消模型**的唯一信号：provider / generator 检测到
取消即抛它；中间层可在补发事件（如 `LLM_CALL_END(cancelled)`、`span.cancel()`）
后**继续 raise**，绝不把取消吞成正常返回；最终由 Coordinator 唯一捕获，置
`run_status="cancelled"` 并发一次 `RUN_CANCELLED`。

沿途所有 `SpanScope.__exit__` 识别 `OperationCancelled` → 自动把 span 记为
`cancelled`，从而与 `run_status` 保持一致，杜绝「Run Span=ended 但
run_status=cancelled」的矛盾（design D4 / Property 8）。
"""

from __future__ import annotations


class OperationCancelled(Exception):
    """当前运行被用户取消（Ctrl-C / cancel token）。一路 raise 到 Coordinator。"""
