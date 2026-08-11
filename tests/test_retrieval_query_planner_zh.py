"""补 `language.zh` 缺口时的查询构造（2026-08 真机校准）。

旧实现给英文查询拼「中文」二字，真机实测召回中文文献 0~1 条 —— 检索引擎把「中文」
当普通检索词，匹到的是标题含 Chinese 的英文论文。三个主题对照（OpenAlex，per-page=20）：

    主题                英文+「中文」        中文原文
    跨视角图像地理定位   中文 1 / 切题 1      中文 10 / 切题 3
    大语言模型代码生成   中文 0 / 切题 0      中文 13 / 切题 7
    知识图谱问答        中文 1 / 切题 1      中文 15 / 切题 12

中文原文来自 `criteria.section_purpose`：合成器会把 `semantic_topic` 归一成英文
（"跨视角图像地理定位" → "cross-view geo-localization"），Planner 原先只拿到 topic，
手里没有任何中文词。
"""

from __future__ import annotations

from paper_agent.literature.retrieval.query_planner import (
    DeterministicQueryPlanner,
    PlannerRequest,
)

ZH = "跨视角图像地理定位"
EN = "cross-view geo-localization"


def _plan(**kw) -> tuple[str, ...]:
    req = PlannerRequest(
        initial_query=EN,
        tried_queries=(EN,),          # 非空 → 走扩展分支而非 initial
        gap_dimensions=("language.zh",),
        **kw,
    )
    return DeterministicQueryPlanner().propose(req).queries


def test_zh_gap_uses_purpose_when_topic_is_english():
    """topic 被归一成英文时，应改用 purpose 里的中文原文。"""
    assert _plan(topic=EN, purpose=ZH) == (ZH,)


def test_zh_gap_never_appends_the_word_zhongwen():
    """回归：不得再产出 `<英文> 中文` 这种实测无效的查询。"""
    for q in _plan(topic=EN, purpose=ZH):
        assert not q.endswith("中文"), f"又拼回了「中文」二字：{q!r}"


def test_zh_gap_prefers_topic_when_it_already_has_han():
    """topic 本身是中文就直接用，不必绕道 purpose。"""
    assert _plan(topic=ZH, purpose="引言背景") == (ZH,)


def test_zh_gap_falls_back_when_no_chinese_available():
    """中英文都没有汉字可用时保持旧行为，不臆造中文。"""
    assert _plan(topic=EN, purpose="introduction background") == (f"{EN} 中文",)


def test_en_gap_unaffected():
    """language.en 缺口的行为不受本次改动影响。"""
    req = PlannerRequest(
        initial_query=EN,
        topic=EN,
        purpose=ZH,
        tried_queries=(EN,),
        gap_dimensions=("language.en",),
    )
    assert DeterministicQueryPlanner().propose(req).queries == (f"{EN} review",)


# ------------------------------------------------- 接线：raw_topic 必须真的到达 Planner

def test_loop_passes_raw_topic_to_planner():
    """`SearchIntent.raw_topic` 要经 Loop 传进 `PlannerRequest.purpose`。

    这条盯的是接线而非算法：字段加了、`_zh_query` 也改了，但只要 Loop 不传，
    补 zh 缺口时 Planner 手里依然没有中文词 —— 正是本次修复前的状态。
    """
    from paper_agent.literature.relevance import RelevanceCriteria
    from paper_agent.literature.retrieval.budget import RetrievalLimits
    from paper_agent.literature.retrieval.loop import RetrievalRequest, run_retrieval_loop
    from paper_agent.literature.retrieval.search_intent import SearchIntent, SearchSeed

    from tests.test_retrieval_loop import _clock, _deps, _quota   # 复用既有搭建

    seen: list[str] = []

    class _Spy:
        def propose(self, request):
            seen.append(request.purpose)
            from paper_agent.literature.retrieval.query_planner import SearchPlan
            return SearchPlan(queries=(), strategy_reason="spy")

    intent = SearchIntent(semantic_topic=EN, raw_topic=ZH, seeds=(SearchSeed(text=EN),))
    req = RetrievalRequest(
        quota=_quota(total_min=99),
        criteria=RelevanceCriteria(topic=EN, section_purpose="引言背景"),
        search_intent=intent,
    )

    run_retrieval_loop(req, deps=_deps({EN: []}),
                       limits=RetrievalLimits(max_iterations=2),
                       planner=_Spy(), clock=_clock())

    assert seen, "planner 没被调用，这条测试没验到东西"
    # 拿到的是中文原文，而不是 criteria.section_purpose（"引言背景"）
    assert seen[0] == ZH
