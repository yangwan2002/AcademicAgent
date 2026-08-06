"""OpenAlex 短语优先查询（F-1 检索召回质量）。

**背景**：`docs/backlog.md` F-3a 把召回噪声归因于「查询词粒度」，建议 seeds 更长更专。
直连 OpenAlex 实测否证了这一点 —— 合成器早已在给长组合 seed，再加长反而更差
（前 10 条切题 2/10 → 1/10）；把同样的词**加引号**则 10/10。病因是 `search=` 的
OR-ish 词袋语法丢掉了词序相邻性，不是词本身。

本测试锁住改写规则与**召回下界**：短语过窄时必须回退裸词，绝不把「召回不准」
换成「召回归零」（`"air-ground visual localization"` 实测 0 命中）。

全部走假 HTTP 层：不联网、不花钱、可进 CI。
"""

from __future__ import annotations

import pytest

from paper_agent.providers.retrieval.openalex import (
    OpenAlexRetrievalProvider,
    build_phrase_first_query,
)


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _work(i: int) -> dict:
    return {
        "id": f"https://openalex.org/W{i}",
        "display_name": f"Title {i}",
        "publication_year": 2020,
        "authorships": [],
    }


class _FakeHTTP:
    """记录每次 GET 的 `search` 参数，按调用序返回预置结果数。"""

    def __init__(self, counts: list[int]) -> None:
        self._counts = list(counts)
        self.queries: list[str] = []

    def __call__(self, url, params=None, timeout=None, **kw):
        params = params or {}
        self.queries.append(params.get("search", ""))
        n = self._counts.pop(0) if self._counts else 0
        return _Resp({"results": [_work(i) for i in range(n)]})


@pytest.fixture
def http(monkeypatch):
    """替换 provider 模块里的 `get_with_retry`；默认每次都返回够用的结果数。"""

    def _install(counts: list[int]) -> _FakeHTTP:
        fake = _FakeHTTP(counts)
        monkeypatch.setattr(
            "paper_agent.providers.retrieval.openalex.get_with_retry", fake
        )
        return fake

    return _install


# --------------------------------------------------------------- 改写规则（纯函数）

def test_quotes_first_three_words_leaves_rest_free():
    assert (
        build_phrase_first_query("cross-view image matching UAV ground vehicle")
        == '"cross-view image matching" UAV ground vehicle'
    )


def test_no_rewrite_when_query_already_has_quotes():
    """用户/planner 显式给了短语 → 尊重原样，不二次加工。"""
    assert build_phrase_first_query('"cross-view image matching" UAV') == ""


@pytest.mark.parametrize("q", ["transformer", "image matching", "cross-view image matching"])
def test_no_rewrite_when_too_few_words(q):
    """词数 <= 3 没有自由词余量；整句引住实测会打穿到 0 命中，故不做。"""
    assert build_phrase_first_query(q) == ""


def test_no_rewrite_for_cjk_head():
    """中文 seed 加引号实测有害：`"空地协同" 跨视角 图像匹配` 2 条 vs 裸词 202 条。"""
    assert build_phrase_first_query("空地协同 跨视角 图像匹配 定位") == ""


# --------------------------------------------------------------- 阶梯行为

def test_phrase_query_is_tried_first_and_adopted_when_enough(http):
    fake = http([8])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    entries = provider.search("cross-view image matching UAV ground vehicle", limit=10)

    assert len(fake.queries) == 1, "命中足够时不该再发裸词请求"
    assert fake.queries[0] == '"cross-view image matching" UAV ground vehicle'
    assert len(entries) == 8


def test_falls_back_to_bare_query_when_phrase_too_narrow(http):
    """短语过窄（0 命中）→ 必须回退裸词，且返回的是**裸词**结果。

    这是本次改造的召回下界保证：改造后最坏情况等于改造前 + 一次多余 HTTP。
    """
    fake = http([0, 7])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    entries = provider.search("air-ground visual localization cross-view", limit=10)

    assert len(fake.queries) == 2
    assert fake.queries[0].startswith('"air-ground visual localization"')
    assert fake.queries[1] == "air-ground visual localization cross-view", (
        "第二次必须是原始裸词"
    )
    assert len(entries) == 7, "返回的必须是回退后的裸词结果，不是被丢弃的短语结果"


def test_threshold_respects_small_limit(http):
    """limit 小于阈值时按 limit 判定，否则小 limit 永远无法采纳短语。"""
    fake = http([2])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    entries = provider.search("cross-view image matching UAV ground", limit=2)

    assert len(fake.queries) == 1, "limit=2 且拿到 2 条 → 已满，不该回退"
    assert len(entries) == 2


def test_flag_off_is_byte_identical_to_legacy(http):
    fake = http([3])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com", phrase_first=False)
    provider.search("cross-view image matching UAV ground vehicle", limit=10)

    assert fake.queries == ["cross-view image matching UAV ground vehicle"]


def test_non_rewritable_query_makes_single_request(http):
    fake = http([1])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    provider.search("transformer", limit=10)

    assert fake.queries == ["transformer"], "不可改写 → 单请求，且不因命中少而重试"


# --------------------------------------------------------------- 可观测

def _strategies(events):
    return [e.data.get("strategy") for e in events]


def test_publishes_strategy_without_leaking_query_text(http, monkeypatch):
    """`PROVIDER_SEARCH` 让「短语优先有没有生效」在 trace 里可读，且不泄查询原文。"""
    from paper_agent.observability.bus import EventBus, RunContext
    from paper_agent.observability.current import use_bus
    from paper_agent.observability.events import EventKind
    from paper_agent.observability.health import ObservabilityHealth

    seen: list = []

    class _Sink:
        def emit(self, event):
            seen.append(event)

    bus = EventBus(_Sink(), RunContext("run_test"), ObservabilityHealth())

    # 三次 search 共发 4 次 HTTP：短语命中 → 短语落空+回退 → 不可改写单发。
    fake = http([9, 0, 6, 1])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    with use_bus(bus):
        provider.search("cross-view image matching UAV ground vehicle", limit=10)
        provider.search("air-ground visual localization cross-view", limit=10)
        provider.search("transformer", limit=10)

    events = [e for e in seen if e.kind is EventKind.PROVIDER_SEARCH]
    assert _strategies(events) == ["phrase_first", "bare_fallback", "bare"]
    assert [e.data["results"] for e in events] == [9, 6, 1]

    for e in events:
        blob = repr(e.data)
        assert "cross-view" not in blob and "transformer" not in blob, (
            "隐私口径同 RETRIEVAL_*：只出长度与计数，不出查询原文"
        )
    assert fake.queries  # 确认真走了假 HTTP


def test_search_works_without_ambient_bus(http):
    """无 bus 时埋点必须静默 no-op —— 观测绝不改变业务可见行为。"""
    http([9])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    assert len(provider.search("cross-view image matching UAV ground", limit=10)) == 9


def test_provider_search_reaches_trace_json(http, tmp_path):
    """`PROVIDER_SEARCH` 必须真的落进 trace.json —— recorder 有白名单，
    「发了事件」≠「trace 里有」。首次接线就漏了白名单，这条测试是为了别再漏。"""
    import json

    from paper_agent.observability.bus import EventBus, RunContext
    from paper_agent.observability.current import use_bus
    from paper_agent.observability.health import ObservabilityHealth
    from paper_agent.observability.recorder import SimpleTraceRecorder

    seen: list = []

    class _Sink:
        def emit(self, event):
            seen.append(event)

    bus = EventBus(_Sink(), RunContext("run_trace"), ObservabilityHealth())

    http([9])
    with use_bus(bus):
        with bus.span("retrieval_attempt"):
            OpenAlexRetrievalProvider(mailto="t@e.com").search(
                "cross-view image matching UAV ground vehicle", limit=10
            )

    recorder = SimpleTraceRecorder("r_phrase")
    for event in seen:
        recorder.emit(event)

    blob = json.dumps(recorder.trace.root.to_dict(), ensure_ascii=False)
    assert "provider_search" in blob, "事件被 recorder 白名单挡掉了（首次接线就漏在这）"
    assert "phrase_first" in blob, "strategy 落盘丢失 → 无法归因短语优先是否生效"


# --------------------------------------------------------------- 核验路径不受影响

def test_verify_adapter_disables_phrase_first():
    """书目反查给的是完整标题，短语优先只会白花 HTTP（基线 34 次 verify）。"""
    from paper_agent.providers.retrieval.verify_adapters import OpenAlexVerifyProvider

    inner = OpenAlexVerifyProvider()._inner
    assert isinstance(inner, OpenAlexRetrievalProvider)
    assert inner._phrase_first is False
