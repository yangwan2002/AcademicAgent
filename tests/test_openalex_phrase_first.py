"""OpenAlex 短语优先查询（F-1 检索召回质量）。

**背景**：`docs/backlog.md` F-3a 把召回噪声归因于「查询词粒度」，建议 seeds 更长更专。
直连 OpenAlex 实测否证了这一点 —— 合成器早已在给长组合 seed，再加长反而更差
（前 10 条切题 2/10 → 1/10）；把同样的词**加引号**则 10/10。病因是 `search=` 的
OR-ish 词袋语法丢掉了词序相邻性，不是词本身。

本测试锁住阶梯规则与**召回下界**：每级过窄时必须继续退，末级恒为裸词，
绝不把「召回不准」换成「召回归零」（`"air-ground visual localization"` 实测 0 命中）。

**为何是阶梯而非单一策略**：收窄到什么程度合适取决于词组学界是否真在用，静态
判断不了。首版用 `len(tokens) > 3` 预判，而 planner 真实输出几乎全是 3 词，
真机 dogfood 的 strategy 分布是 `{'bare': 2}` —— 功能上线却从未执行。这批测试
因此覆盖 2 词与 3 词查询必须进入阶梯。

全部走假 HTTP 层：不联网、不花钱、可进 CI。
"""

from __future__ import annotations

import pytest

from paper_agent.providers.retrieval.openalex import (
    OpenAlexRetrievalProvider,
    build_phrase_ladder,
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


# --------------------------------------------------------------- 阶梯构造（纯函数）

def test_ladder_is_exact_then_head_then_bare_for_long_query():
    """长查询三级：整句引住 → 引前 3 词 → 裸词。

    实测这条查询：裸词 9512 条 2/10 切题、引前 3 词 84 条 7/10、整句引住 0 条。
    第一级归零正是 `phrase_head` 存在的理由。
    """
    q = "cross-view image matching UAV ground vehicle"
    assert build_phrase_ladder(q) == [
        ("phrase_exact", '"cross-view image matching UAV ground vehicle"'),
        ("phrase_head", '"cross-view image matching" UAV ground vehicle'),
        ("bare", q),
    ]


@pytest.mark.parametrize("q", ["image matching", "cross-view image matching"])
def test_short_query_enters_ladder_with_exact_then_bare(q):
    """2~3 词必须进阶梯 —— 首版按词数挡掉，导致真机上短语优先从未生效。

    `"Cross-view image matching"` 实测 220 条 10/10 切题，是全表最好结果；
    按词数挡掉它等于放弃主要收益。词数 <= 3 时没有自由词余量，故无 `phrase_head`。
    """
    assert build_phrase_ladder(q) == [("phrase_exact", f'"{q}"'), ("bare", q)]


def test_single_word_query_skips_quoting():
    """单词整句引住与裸词同义，只会白花一次 GET。"""
    assert build_phrase_ladder("transformer") == [("bare", "transformer")]


def test_no_rewrite_when_query_already_has_quotes():
    """用户/planner 显式给了短语 → 尊重原样，不二次加工。"""
    q = '"cross-view image matching" UAV'
    assert build_phrase_ladder(q) == [("bare", q)]


def test_no_rewrite_for_cjk():
    """中文 seed 加引号实测有害：`"空地协同" 跨视角 图像匹配` 2 条 vs 裸词 202 条。"""
    q = "空地协同 跨视角 图像匹配 定位"
    assert build_phrase_ladder(q) == [("bare", q)]


# --------------------------------------------------------------- 阶梯行为

def test_exact_phrase_adopted_when_enough(http):
    """第一级够数就停 —— 与改造前同样一次 GET，无额外成本。"""
    fake = http([8])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    entries = provider.search("cross-view image matching UAV ground vehicle", limit=10)

    assert len(fake.queries) == 1, "命中足够时不该继续退级"
    assert fake.queries[0] == '"cross-view image matching UAV ground vehicle"'
    assert len(entries) == 8


def test_short_query_adopts_exact_phrase(http):
    """3 词查询必须真的发出引号请求 —— 首版在此处退化为裸词。"""
    fake = http([9])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    entries = provider.search("Cross-view image matching", limit=10)

    assert fake.queries == ['"Cross-view image matching"']
    assert len(entries) == 9


def test_falls_through_exact_then_head_then_bare(http):
    """整句归零 → 引前 3 词仍不足 → 落到裸词，返回的必须是**裸词**结果。

    这是召回下界保证：最坏情况等于改造前 + 两次多余 HTTP。
    """
    fake = http([0, 2, 7])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    entries = provider.search("air-ground visual localization cross-view", limit=10)

    assert fake.queries == [
        '"air-ground visual localization cross-view"',
        '"air-ground visual localization" cross-view',
        "air-ground visual localization cross-view",
    ]
    assert len(entries) == 7, "返回的必须是末级裸词结果，不是被丢弃的窄结果"


def test_head_phrase_adopted_when_exact_too_narrow(http):
    """整句归零但引前 3 词够数 → 停在中间级，不必退到裸词。

    对应实测那条：整句 0 命中、引前 3 词 84 条 7/10 切题、裸词只 2/10。
    """
    fake = http([0, 8])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    entries = provider.search("cross-view image matching UAV ground vehicle", limit=10)

    assert len(fake.queries) == 2, "中间级够数就不该再发裸词"
    assert fake.queries[1] == '"cross-view image matching" UAV ground vehicle'
    assert len(entries) == 8


def test_bare_last_rung_adopted_even_when_empty(http):
    """末级无条件采纳：裸词 0 命中就是 0 命中，与改造前一致，不额外重试。"""
    fake = http([0, 0, 0])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    entries = provider.search("cross-view image matching UAV ground vehicle", limit=10)

    assert len(fake.queries) == 3, "末级返回后不该再发请求"
    assert entries == []


def test_threshold_respects_small_limit(http):
    """limit 小于阈值时按 limit 判定，否则小 limit 永远无法采纳短语。"""
    fake = http([2])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    entries = provider.search("cross-view image matching UAV ground", limit=2)

    assert len(fake.queries) == 1, "limit=2 且拿到 2 条 → 已满，不该退级"
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

    # 三次 search 共发 5 次 HTTP：整句命中 → 整句落空+引前3词命中 → 单词直接裸词。
    fake = http([9, 0, 6, 1])
    provider = OpenAlexRetrievalProvider(mailto="t@e.com")
    with use_bus(bus):
        provider.search("cross-view image matching UAV ground vehicle", limit=10)
        provider.search("air-ground visual localization cross-view", limit=10)
        provider.search("transformer", limit=10)

    events = [e for e in seen if e.kind is EventKind.PROVIDER_SEARCH]
    assert _strategies(events) == ["phrase_exact", "phrase_head", "bare"]
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
    assert "phrase_exact" in blob, "strategy 落盘丢失 → 无法归因短语优先是否生效"


# --------------------------------------------------------------- 核验路径不受影响

def test_verify_adapter_disables_phrase_first():
    """书目反查给的是完整标题，短语优先只会白花 HTTP（基线 34 次 verify）。"""
    from paper_agent.providers.retrieval.verify_adapters import OpenAlexVerifyProvider

    inner = OpenAlexVerifyProvider()._inner
    assert isinstance(inner, OpenAlexRetrievalProvider)
    assert inner._phrase_first is False
