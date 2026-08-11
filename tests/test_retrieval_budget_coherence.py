"""检索预算参数组的**自洽性**：名义轮数必须真的跑得到。

背景（2026-08 真机校准）：`max_iters` 与时间/verify/provider 上限是一组参数，
单独改一个会让检索在名义轮数之前就 `budget_exhausted` 收尾 —— 结果保留、
不报错，但配额补不齐，属静默降级，很难在单元测试里发现。

实跑一次 zh=5/en=7（7 次 attempt、273.9s）测得单 attempt 成本：
耗时均值 42.9s / 最坏 54.5s，verify 均值 18.3 次 / 最坏 20 次。
旧默认 time=120s 只够约 2 轮，实跑停在 iterations=3、
exhausted_dimensions=['max_verify_calls']，并有 3 条候选取回后未判完即丢。

这些断言钉的是「参数之间的关系」，不是具体数值 —— 数值可随实测更新，
但只要 max_iters 声称 N 轮，另外几个上限就得供得上 N 轮的最坏开销。
"""

from __future__ import annotations

from paper_agent.config import Config

# 单 attempt 最坏开销（2026-08 真机实测，见模块 docstring）
WORST_ATTEMPT_SECONDS = 54.5
WORST_ATTEMPT_VERIFY_CALLS = 20


def test_time_budget_covers_nominal_iterations():
    """时间预算须容得下 max_iters 轮的最坏耗时，否则轮数是空头承诺。"""
    c = Config()
    need = c.literature_retrieval_max_iters * WORST_ATTEMPT_SECONDS
    assert c.literature_retrieval_time_budget_s >= need, (
        f"time_budget={c.literature_retrieval_time_budget_s}s 跑不到 "
        f"max_iters={c.literature_retrieval_max_iters} 轮（最坏需 {need:.0f}s）"
    )


def test_verify_calls_cover_nominal_iterations():
    """verify 上限须容得下 max_iters 轮的最坏调用数。

    这条是旧配置真正先撞到的墙：实跑 exhausted_dimensions=['max_verify_calls']。
    """
    c = Config()
    need = c.literature_retrieval_max_iters * WORST_ATTEMPT_VERIFY_CALLS
    assert c.literature_retrieval_max_verify_calls >= need, (
        f"max_verify_calls={c.literature_retrieval_max_verify_calls} 跑不到 "
        f"max_iters={c.literature_retrieval_max_iters} 轮（最坏需 {need}）"
    )


def test_provider_calls_cover_search_plus_verify():
    """provider 全局上限须 ≥ search + verify，否则子上限形同虚设。"""
    c = Config()
    need = (
        c.literature_retrieval_max_search_calls
        + c.literature_retrieval_max_verify_calls
    )
    assert c.literature_retrieval_max_provider_calls >= need, (
        f"max_provider_calls={c.literature_retrieval_max_provider_calls} "
        f"< search {c.literature_retrieval_max_search_calls} + "
        f"verify {c.literature_retrieval_max_verify_calls} = {need}"
    )
