"""把 `RunTrace` 导出到 Langfuse（可选的第二路 exporter）。

**为什么可以只写这一个文件**：`SimpleTraceRecorder` 已经把事件流组装成完整的
span 树，`TraceExporter.export(trace)` 拿到的就是那棵树。本模块只做「翻译 + 上传」，
不碰 bus、不碰 recorder、不碰任何业务代码。

**采集能力与本地 JSON 完全相同** —— 数据源是同一个 `RunTrace`。Langfuse 的价值
只在消费端：跨 run 查询、聚合视图、span 树 UI。做 A/B 时不必再 grep 两个 txt
人工对齐。它**不会**多出任何本地 trace.json 看不到的字段。

**本地永远优先**：由 `CompositeExporter` 保证本地那一路先跑、且不受此路影响。
网络挂、key 错、Langfuse 宕机，一律只记 warning，业务与 trace.json 照常。
同 `ambient_publish` 的纪律：观测绝不改变业务可见行为。
"""

from __future__ import annotations

import os

from paper_agent.observability.recorder import RunTrace, Span, TraceExporter

#: 这些 kind 的原文不上传（正文分片对调试无价值，却是唯一真正敏感的东西：
#: 转发即等于把论文全文送到第三方）。最终文本在 working copy 里另有留存。
_DROP_ATTR_KEYS = frozenset({"delta", "text", "content", "chunk"})

#: span 名含这些片段时视为 LLM 调用 → 映射为 Langfuse generation（能算 token/成本）。
_GENERATION_HINTS = ("llm", "generation", "structured_call")


def _clean_attrs(attrs: dict | None) -> dict:
    """剔掉正文类字段，其余原样上传。

    只按 key 名剔除，不做内容嗅探 —— 嗅探会漏且会误伤。真正的兜底在采集端：
    `SENSITIVE_EPHEMERAL_KINDS` 本就不该进 `RunTrace`。
    """
    if not attrs:
        return {}
    return {k: v for k, v in attrs.items() if k not in _DROP_ATTR_KEYS}


def _is_generation(span: Span) -> bool:
    if span.llm_call_id or span.generation_id:
        return True
    name = (span.name or "").lower()
    return any(h in name for h in _GENERATION_HINTS)


def _level(state: str | None) -> str:
    """span 终态 → Langfuse level。未终结（None）按 WARNING：span 没闭合本身是信号。"""
    return {"ended": "DEFAULT", "cancelled": "WARNING", "failed": "ERROR"}.get(
        state or "", "WARNING"
    )


class LangfuseExporter(TraceExporter):
    """把 `RunTrace` 的 span 树递归上传为 Langfuse trace。

    `export` 返回 trace 的 URL（拿不到则返回空串）—— 与 `LocalJsonExporter`
    返回落盘路径同一口径：都是「产物在哪」。
    """

    def __init__(
        self,
        *,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
        client=None,
    ) -> None:
        self._public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        self._secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY", "")
        # 自托管填 http://localhost:3000；默认云版。
        self._host = host or os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
        self._client = client  # 注入用（测试走假 SDK，不联网）
        self._auth_checked: bool | None = None
        #: 认证失败原因（供 smoke / 诊断读取）；认证正常时为空。
        self.auth_error = ""

    @property
    def configured(self) -> bool:
        """缺任一把 key 就算没配 —— Langfuse 要 public + secret 一对。"""
        return bool(self._public_key and self._secret_key)

    def _get_client(self):
        if self._client is not None:
            return self._client
        from langfuse import Langfuse  # 延迟导入：未装包时也能导入本模块

        self._client = Langfuse(
            public_key=self._public_key,
            secret_key=self._secret_key,
            host=self._host,
        )
        return self._client

    def _auth_ok(self, client) -> bool:
        """首次导出前查一次认证，结果缓存。

        **为什么必须显式查**：上传走 SDK 的后台批量线程，401 只会在 stderr 打一行
        `Failed to export span batch code: 401`，`export()` 照常返回 —— 调用方
        拿不到任何失败信号。不查的话「key 配错」会一路静默，直到你去 UI 上发现空空
        如也。这是第一版真实踩到的坑：自检脚本在 401 下打印了「上传成功」。
        """
        if self._auth_checked is not None:
            return self._auth_checked
        check = getattr(client, "auth_check", None)
        if not callable(check):
            self._auth_checked = True  # 老 SDK 没这方法：不阻断，照原样上传
            return True
        try:
            self._auth_checked = bool(check())
        except Exception as exc:  # noqa: BLE001 - 认证失败是预期分支，不是崩溃
            self._auth_checked = False
            self.auth_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return self._auth_checked

    def export(self, trace: RunTrace) -> str:
        if not self.configured:
            return ""
        client = self._get_client()
        if not self._auth_ok(client):
            # 认证不通过就别白传了：上传注定 401，只会刷 stderr。抛出去让
            # CompositeExporter 记进 failures —— 这样「配错了」是可见的。
            raise RuntimeError(f"Langfuse 认证失败，未上传。{self.auth_error}".strip())

        # SDK v3 是 OTel 风格：**没有** `client.trace()`，trace 由第一个 span 隐式
        # 创建。trace_id 必须是 OTel 的 32 位 hex，故用 `create_trace_id(seed=)`
        # 从我们的 run_id 确定性派生 —— 同一个 run 重复导出会落到同一条 trace。
        trace_id = client.create_trace_id(seed=trace.trace_id)

        root = trace.root
        if root is None:
            # run 早期就崩：没有 span 树可上传。造一个空壳 span 承载失败元数据，
            # 否则这次 run 在 Langfuse 里完全不存在，反而看不见「崩了」。
            root = Span(span_id=trace.trace_id, name="run", start_ts=trace.started_at,
                        end_ts=trace.ended_at, state="failed")

        # trace 级元数据：UI 里可直接按 run_status 筛选，不必点进 span。
        meta = {
            "run_status": trace.run_status,
            "quality_status": trace.quality_status,
            "working_copy_status": trace.working_copy_status,
            "attempts": trace.attempts,
            "gate_retries": trace.gate_retries,
            "provider_retries": trace.provider_retries,
            "schema_version": trace.schema_version,
            "local_trace_id": trace.trace_id,  # 回连本地 runs/<id>/trace.json
        }
        if trace.warnings:
            meta["warnings"] = list(trace.warnings)
        if trace.error:
            meta["error"] = trace.error

        # **必须在 end() 之前写**：`update_trace` 开头就是
        # `if not self._otel_span.is_recording(): return self` —— span 闭合后
        # 整个调用被静默丢弃。第一版写在 _upload_span 返回之后，服务端 trace
        # metadata 只剩 SDK 自己的 resourceAttributes，run_status 全丢。
        def _write_trace_meta(node) -> None:
            update_trace = getattr(node, "update_trace", None)
            if callable(update_trace):
                update_trace(name=root.name or "run", metadata=meta)

        self._upload_span(client, root, trace_id=trace_id, on_open=_write_trace_meta)

        # run 边界 flush：SDK 异步批量发送，进程可能先退出。
        flush = getattr(client, "flush", None)
        if callable(flush):
            flush()

        get_url = getattr(client, "get_trace_url", None)
        return (get_url(trace_id=trace_id) or "") if callable(get_url) else ""

    def _upload_span(self, parent, span: Span, *, trace_id: str | None = None,
                     on_open=None):
        """递归上传一棵 span 树，返回创建出的根节点。

        深度等于业务嵌套深度（实测个位数），不必改写成显式栈。

        `on_open`：节点建好、**尚未 end** 时回调一次。给 trace 级元数据用 ——
        SDK 的 `update_trace` 在 span 闭合后会静默失效。
        """
        metadata = _clean_attrs(span.attributes)
        for key in ("attempt", "candidate_id", "llm_call_id", "generation_id"):
            value = getattr(span, key, None)
            if value not in (None, ""):
                metadata[key] = value

        kwargs = {
            "name": span.name or "span",
            "metadata": metadata,
            "level": _level(span.state),
        }
        if span.state == "failed":
            kwargs["status_message"] = "span ended in failed state"
        if trace_id is not None:
            # 顶层 span 要显式绑 trace_context，子 span 自动继承父的。
            from langfuse.types import TraceContext

            kwargs["trace_context"] = TraceContext(trace_id=trace_id)

        factory = parent.start_generation if _is_generation(span) else parent.start_span
        node = factory(**kwargs)
        if on_open is not None:
            on_open(node)

        for child in span.children:
            self._upload_span(node, child)

        # 显式闭合并回填真实结束时间（SDK 要 int 纳秒，不吃 float 秒）。
        end = getattr(node, "end", None)
        if callable(end):
            end(end_time=_to_ns(span.end_ts)) if span.end_ts else end()
        return node


def _to_ns(ts: float | None) -> int | None:
    """epoch 秒 → int 纳秒。v3 的 `end_time` 是纳秒整数，喂 float 秒会错 9 个量级。"""
    return None if not ts else int(ts * 1_000_000_000)


class CompositeExporter(TraceExporter):
    """按序跑多个 exporter；**任一路失败不影响其余**。

    顺序有意义：本地 JSON 放第一位，它是可靠的那一路（无网络依赖）。
    返回值取第一个非空 —— 调用方问的是「主产物在哪」，那始终是本地路径。
    """

    def __init__(self, *exporters: TraceExporter) -> None:
        self._exporters = [e for e in exporters if e is not None]
        #: 失败记录（exporter 类名 → 异常类名），供 health/诊断读取。
        self.failures: list[tuple[str, str]] = []

    def export(self, trace: RunTrace) -> str:
        first = ""
        for exporter in self._exporters:
            try:
                result = exporter.export(trace)
            except Exception as exc:  # noqa: BLE001 - 一路失败不能拖垮其余
                self.failures.append((type(exporter).__name__, type(exc).__name__))
                continue
            if not first and result:
                first = result
        return first


__all__ = ["CompositeExporter", "LangfuseExporter"]
