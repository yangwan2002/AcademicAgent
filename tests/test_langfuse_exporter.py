"""Langfuse exporter（trace 导出的第二路）。

锁三件事：
1. **span 树翻译正确** —— 嵌套结构、时间戳、level、metadata 不丢不错位；
2. **本地那一路绝不受影响** —— Langfuse 挂了、包没装、key 不全，
   `trace.json` 照落。这是 `CompositeExporter` 的全部存在理由；
3. **正文原文不上传** —— `delta`/`text` 这类字段剔除后才发。

全程假 SDK：不联网、不需要 key、可进 CI。
"""

from __future__ import annotations

import pytest

from paper_agent.observability.langfuse_exporter import (
    CompositeExporter,
    LangfuseExporter,
)
from paper_agent.observability.recorder import RunTrace, Span, TraceExporter


# --------------------------------------------------------------- 假 SDK

class _FakeNode:
    """假的 Langfuse observation 节点。

    **形状照 SDK v3 抄**（`start_span` / `start_generation` / `end` / `update_trace`），
    不是 v2 的 `client.trace()` + `span()`。第一版按 v2 写，22 个测试全绿但真机
    `AttributeError` —— 假 SDK 与真 SDK 形状不符时，测试验的是自己想象出的 API。
    """

    def __init__(self, kind: str, payload: dict) -> None:
        self.kind = kind
        self.payload = payload
        self.children: list[_FakeNode] = []
        self.ended = False
        self.end_time = None
        self.trace_update: dict | None = None
        self.update_after_end = False

    def start_span(self, **payload):
        node = _FakeNode("span", payload)
        self.children.append(node)
        return node

    def start_generation(self, **payload):
        node = _FakeNode("generation", payload)
        self.children.append(node)
        return node

    def end(self, end_time=None):
        self.ended = True
        self.end_time = end_time
        return self

    def update_trace(self, **payload):
        # 真 SDK 的 update_trace 开头就是
        # `if not self._otel_span.is_recording(): return self`
        # —— span 闭合后调用被**静默丢弃**。这里如实复现，否则测不出时序 bug。
        if self.ended:
            self.update_after_end = True
            return self
        self.trace_update = payload
        return self


class _FakeClient:
    """假 Langfuse 客户端；`roots` 收集顶层 span（v3 里 trace 由首个 span 隐式建）。"""

    def __init__(self) -> None:
        self.roots: list[_FakeNode] = []
        self.flushed = 0
        self.seeds: list[str] = []
        self.auth_ok = True
        self.auth_raises = False

    def auth_check(self) -> bool:
        if self.auth_raises:
            raise RuntimeError("401 Unauthorized")
        return self.auth_ok

    def create_trace_id(self, *, seed=None) -> str:
        self.seeds.append(seed)
        return "otel_" + str(seed)

    def start_span(self, **payload):
        node = _FakeNode("span", payload)
        self.roots.append(node)
        return node

    def start_generation(self, **payload):
        node = _FakeNode("generation", payload)
        self.roots.append(node)
        return node

    def get_trace_url(self, *, trace_id=None) -> str:
        return "https://lf.test/t/" + str(trace_id)

    def flush(self) -> None:
        self.flushed += 1


def _exporter(client):
    return LangfuseExporter(public_key="pk", secret_key="sk", client=client)


def _trace(root: Span | None = None, **kw) -> RunTrace:
    t = RunTrace(trace_id="t_abc", started_at=1000.0, ended_at=1002.0, root=root)
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _span(name: str, **kw) -> Span:
    base = {"span_id": "s_" + name, "name": name, "start_ts": 1000.0,
            "end_ts": 1001.0, "state": "ended"}
    base.update(kw)
    return Span(**base)


# --------------------------------------------------------------- 树翻译

def test_uploads_nested_span_tree_preserving_shape():
    child = _span("openalex_search", attributes={"strategy": "phrase_exact"})
    root = _span("run", children=[_span("retrieval_attempt", children=[child])])
    client = _FakeClient()

    url = _exporter(client).export(_trace(root))

    assert len(client.roots) == 1
    lf = client.roots[0]
    assert lf.payload["name"] == "run"
    # 树形必须保持：run → retrieval_attempt → openalex_search
    attempt = lf.children[0]
    assert attempt.payload["name"] == "retrieval_attempt"
    leaf = attempt.children[0]
    assert leaf.payload["name"] == "openalex_search"
    assert leaf.payload["metadata"]["strategy"] == "phrase_exact", (
        "strategy 丢了就等于白接：F-1 全靠这个字段定案"
    )
    assert url == "https://lf.test/t/otel_t_abc"


def test_llm_spans_become_generations():
    """带 llm_call_id 的 span 映射为 generation —— Langfuse 只对 generation 算成本。"""
    root = _span("run", children=[
        _span("report_relevance_batch", llm_call_id="c_1"),
        _span("openalex_search"),
    ])
    client = _FakeClient()
    _exporter(client).export(_trace(root))

    kinds = {n.payload["name"]: n.kind for n in client.roots[0].children}
    assert kinds == {"report_relevance_batch": "generation", "openalex_search": "span"}


def test_generation_detected_by_name_when_id_absent():
    root = _span("run", children=[_span("structured_call")])
    client = _FakeClient()
    _exporter(client).export(_trace(root))

    assert client.roots[0].children[0].kind == "generation"


@pytest.mark.parametrize(
    ("state", "level"),
    [("ended", "DEFAULT"), ("failed", "ERROR"), ("cancelled", "WARNING"),
     (None, "WARNING")],
)
def test_span_state_maps_to_level(state, level):
    """未终结（None）也报 WARNING：span 没闭合本身是需要看见的信号。"""
    client = _FakeClient()
    _exporter(client).export(_trace(_span("run", state=state)))

    assert client.roots[0].payload["level"] == level


def test_span_is_ended_with_nanosecond_timestamp():
    """v3 的 `end_time` 是 **int 纳秒**；喂 float 秒会错 9 个量级（UI 上时间轴全崩）。"""
    client = _FakeClient()
    _exporter(client).export(_trace(_span("run", end_ts=1001.5)))

    node = client.roots[0]
    assert node.ended, "span 必须显式 end()，否则 Langfuse 侧永远处于未闭合"
    assert node.end_time == 1_001_500_000_000
    assert isinstance(node.end_time, int)


def test_unended_span_still_closed_without_timestamp():
    """本地 span 没闭合（end_ts=None）时也要 end()，否则远端永远挂着。"""
    client = _FakeClient()
    _exporter(client).export(_trace(_span("run", end_ts=None, state=None)))

    node = client.roots[0]
    assert node.ended and node.end_time is None
    assert node.payload["level"] == "WARNING"


def test_trace_metadata_carries_run_status():
    client = _FakeClient()
    _exporter(client).export(
        _trace(_span("run"), run_status="failed", quality_status="degraded", attempts=3)
    )

    meta = client.roots[0].trace_update["metadata"]
    assert meta["run_status"] == "failed"
    assert meta["quality_status"] == "degraded"
    assert meta["attempts"] == 3


def test_flushes_at_run_boundary():
    """SDK 异步批量发送 —— 不 flush 则进程退出时事件可能还没发出去。"""
    client = _FakeClient()
    _exporter(client).export(_trace(_span("run")))
    assert client.flushed == 1


def test_handles_trace_without_root():
    """run 早期就崩时 root 可能是 None，不能因此抛。"""
    client = _FakeClient()
    _exporter(client).export(_trace(None))
    assert len(client.roots) == 1
    assert client.roots[0].children == []


# --------------------------------------------------------------- 隐私

def test_body_text_attributes_are_dropped():
    """正文分片不上传：对调试无价值，却是唯一真正敏感的东西（论文全文）。"""
    root = _span("run", attributes={
        "delta": "本文提出一种空地协同…", "text": "正文", "content": "正文",
        "chunk": "片段", "strategy": "phrase_exact", "results": 9,
    })
    client = _FakeClient()
    _exporter(client).export(_trace(root))

    meta = client.roots[0].payload["metadata"]
    assert meta == {"strategy": "phrase_exact", "results": 9}


# --------------------------------------------------------------- 未配置即不启用

@pytest.mark.parametrize(("pk", "sk"), [("pk", ""), ("", "sk"), ("", "")])
def test_not_configured_without_both_keys(pk, sk, monkeypatch):
    """Langfuse 要 public+secret 一对，缺任一就当没配（静默，不报错）。"""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    client = _FakeClient()
    exporter = LangfuseExporter(public_key=pk, secret_key=sk, client=client)

    assert exporter.configured is False
    assert exporter.export(_trace(_span("run"))) == ""
    assert client.roots == [], "未配置就不该产生任何调用"
    assert client.seeds == [], "连 trace_id 都不该生成"


# --------------------------------------------------------------- 本地那一路不受影响

class _Recording(TraceExporter):
    def __init__(self, result: str = "runs/x/trace.json") -> None:
        self.calls = 0
        self._result = result

    def export(self, trace):
        self.calls += 1
        return self._result


class _Boom(TraceExporter):
    def export(self, trace):
        raise RuntimeError("langfuse down")


def test_composite_local_survives_remote_failure():
    """**本测试是接 Langfuse 的前提**：远端挂掉，trace.json 必须照落。"""
    local = _Recording()
    composite = CompositeExporter(local, _Boom())

    result = composite.export(_trace(_span("run")))

    assert local.calls == 1, "远端失败不能影响本地导出"
    assert result == "runs/x/trace.json", "返回值应是本地路径（主产物）"
    assert composite.failures == [("_Boom", "RuntimeError")], "失败要留痕，不能静悄悄"


def test_composite_runs_local_first():
    """顺序有意义：本地无网络依赖，先跑完它再碰远端。"""
    order: list[str] = []

    class _Tracked(TraceExporter):
        def __init__(self, tag): self.tag = tag
        def export(self, trace):
            order.append(self.tag)
            return ""

    CompositeExporter(_Tracked("local"), _Tracked("remote")).export(_trace(_span("run")))
    assert order == ["local", "remote"]


def test_composite_continues_after_first_failure():
    local, remote = _Boom(), _Recording("uploaded")
    composite = CompositeExporter(local, remote)

    assert composite.export(_trace(_span("run"))) == "uploaded"
    assert remote.calls == 1, "第一路失败不能吞掉后续"


# --------------------------------------------------------------- Coordinator 接线

def test_coordinator_uses_local_only_when_langfuse_off(tmp_path):
    from paper_agent.observability.recorder import LocalJsonExporter

    from paper_agent.coordinator import Coordinator

    exporter = Coordinator(enable_trace=True, runs_dir=str(tmp_path))._build_exporter()
    assert isinstance(exporter, LocalJsonExporter)


def test_coordinator_falls_back_to_local_when_keys_missing(tmp_path, monkeypatch):
    """开了开关但没配 key → 退化单路本地，不报错、不阻断 run。"""
    from paper_agent.observability.recorder import LocalJsonExporter

    from paper_agent.coordinator import Coordinator

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    exporter = Coordinator(
        enable_trace=True, runs_dir=str(tmp_path), enable_langfuse=True
    )._build_exporter()
    assert isinstance(exporter, LocalJsonExporter)


def test_coordinator_composes_when_configured(tmp_path, monkeypatch):
    from paper_agent.coordinator import Coordinator

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")

    exporter = Coordinator(
        enable_trace=True, runs_dir=str(tmp_path), enable_langfuse=True
    )._build_exporter()
    assert isinstance(exporter, CompositeExporter)


def test_trace_json_still_written_when_langfuse_configured(tmp_path, monkeypatch):
    """端到端：配了 Langfuse（但 SDK 未装/连不上）时，trace.json 仍必须落盘。"""
    import json

    from paper_agent.coordinator import Coordinator

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")

    coord = Coordinator(enable_trace=True, runs_dir=str(tmp_path), enable_langfuse=True)
    report = coord.run(lambda bus, tok: "ok")

    assert report.run_status == "completed"
    files = list(tmp_path.rglob("trace.json"))
    assert len(files) == 1, "Langfuse 一路失败时本地 trace.json 仍必须存在"
    assert json.loads(files[0].read_text(encoding="utf-8"))["run_status"] == "completed"


# --------------------------------------------------------------- 真 SDK 契约

"""这一组不联网，只对**真** SDK 做形状校验。

存在理由：上面所有测试都跑在假 SDK 上，而假 SDK 是我们自己写的 —— 它与真 SDK
形状不符时，测试验的是想象出来的 API。第一版正是如此（照 v2 写，22 条全绿，
真机 `AttributeError`）。SDK 升级破坏 API 时，这组会红。
"""

langfuse = pytest.importorskip("langfuse", reason="可选依赖；未装则跳过契约校验")


def _params(fn):
    import inspect

    return set(inspect.signature(fn).parameters)


def test_real_client_has_methods_we_call():
    from langfuse import Langfuse

    for name in ("create_trace_id", "start_span", "start_generation",
                 "get_trace_url", "flush"):
        assert hasattr(Langfuse, name), f"SDK 无 {name}，exporter 会在真机崩"


def test_real_span_has_methods_we_call():
    from langfuse._client.span import LangfuseSpan

    for name in ("start_span", "start_generation", "end", "update_trace"):
        assert hasattr(LangfuseSpan, name), f"span 无 {name}"


def test_real_signatures_accept_our_kwargs():
    from langfuse import Langfuse
    from langfuse._client.span import LangfuseSpan

    assert {"name", "metadata", "level", "trace_context", "status_message"} <= _params(
        Langfuse.start_span)
    assert {"name", "metadata", "level", "status_message"} <= _params(
        LangfuseSpan.start_span)
    assert "end_time" in _params(LangfuseSpan.end)
    assert {"name", "metadata"} <= _params(LangfuseSpan.update_trace)


def test_trace_id_is_deterministic_32_hex():
    """确定性：同一 run 重复导出落到同一条 trace，不会重复建。"""
    from langfuse import Langfuse

    first = Langfuse.create_trace_id(seed="t_abc")
    assert len(first) == 32 and int(first, 16) >= 0, "须是 OTel 的 32 位 hex"
    assert Langfuse.create_trace_id(seed="t_abc") == first


def test_trace_context_accepts_derived_id():
    from langfuse import Langfuse
    from langfuse.types import TraceContext

    TraceContext(trace_id=Langfuse.create_trace_id(seed="t_abc"))


# --------------------------------------------------------------- 认证失败可见性

"""401 必须可见。

上传走 SDK 后台批量线程，认证失败只在 stderr 打一行、`export()` 照常返回 ——
调用方拿不到任何信号，「key 配错」会一路静默到你去 UI 上发现空空如也。
真实踩到过：自检脚本在 401 下打印了「上传成功」。
"""


def test_auth_failure_raises_so_composite_records_it():
    client = _FakeClient()
    client.auth_ok = False
    exporter = _exporter(client)

    with pytest.raises(RuntimeError, match="认证失败"):
        exporter.export(_trace(_span("run")))
    assert client.roots == [], "认证不通过就不该白传"


def test_auth_exception_captured_as_reason():
    client = _FakeClient()
    client.auth_raises = True
    exporter = _exporter(client)

    with pytest.raises(RuntimeError):
        exporter.export(_trace(_span("run")))
    assert "401" in exporter.auth_error


def test_auth_checked_once_and_cached():
    """每个 run 都查一次认证会白费一次网络往返。"""
    client = _FakeClient()
    calls = []
    original = client.auth_check

    def counting():
        calls.append(1)
        return original()

    client.auth_check = counting
    exporter = _exporter(client)
    exporter.export(_trace(_span("run")))
    exporter.export(_trace(_span("run")))

    assert len(calls) == 1, "认证结果应缓存"


def test_composite_records_auth_failure_and_keeps_local():
    """最要紧的一条：认证失败时本地 trace.json 仍然照落。"""
    client = _FakeClient()
    client.auth_ok = False
    local = _Recording()
    composite = CompositeExporter(local, _exporter(client))

    composite.export(_trace(_span("run")))

    assert local.calls == 1, "Langfuse 认证失败不能影响本地导出"
    assert composite.failures and composite.failures[0][0] == "LangfuseExporter"


def test_real_sdk_has_auth_check():
    """契约：`auth_check` 是我们判断认证的唯一同步入口。"""
    from langfuse import Langfuse

    assert hasattr(Langfuse, "auth_check")


# ------------------------------------------------- trace 元数据写入时序

"""`update_trace` 必须在根 span `end()` **之前**调。

真 SDK 里它开头就是 `if not self._otel_span.is_recording(): return self` ——
span 闭合后整个调用被静默丢弃，没有异常、没有返回值差异。第一版把它写在
`_upload_span` 返回之后，结果服务端 trace metadata 只剩 SDK 自己的
resourceAttributes，run_status / local_trace_id 全丢，而 33 个测试全绿。
"""


def test_trace_meta_written_before_root_end():
    client = _FakeClient()
    _exporter(client).export(_trace(_span("run"), run_status="completed"))

    root = client.roots[0]
    assert root.update_after_end is False, "写在 end() 之后 → 真 SDK 会静默丢弃"
    assert root.trace_update is not None, "trace 元数据必须真的写进去"


def test_trace_meta_carries_run_level_fields():
    client = _FakeClient()
    trace = _trace(_span("run"), run_status="completed", quality_status="passed",
                   attempts=2, gate_retries=1)
    _exporter(client).export(trace)

    meta = client.roots[0].trace_update["metadata"]
    assert meta["run_status"] == "completed"
    assert meta["quality_status"] == "passed"
    assert meta["attempts"] == 2
    assert meta["gate_retries"] == 1
    assert meta["local_trace_id"] == trace.trace_id, "要能回连本地 trace.json"


def test_root_still_ended_after_meta_write():
    """写元数据不能把 end() 挤掉。"""
    client = _FakeClient()
    _exporter(client).export(_trace(_span("run", end_ts=1001.5)))

    assert client.roots[0].ended
    assert client.roots[0].end_time == 1_001_500_000_000


def test_only_root_gets_trace_meta():
    """子 span 不该重复写 trace 元数据（会互相覆盖）。"""
    child = _span("child")
    client = _FakeClient()
    _exporter(client).export(_trace(_span("run", children=[child])))

    assert client.roots[0].trace_update is not None
    assert client.roots[0].children[0].trace_update is None


def test_real_update_trace_bails_when_not_recording():
    """契约：真 SDK 确实在 span 未 recording 时提前返回 —— 上面的时序要求源于此。"""
    import inspect

    from langfuse._client.span import LangfuseSpan

    src = inspect.getsource(LangfuseSpan.update_trace)
    assert "is_recording()" in src, "SDK 行为若变，这组时序测试的前提需重新评估"
