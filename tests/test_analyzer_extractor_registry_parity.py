"""analyzer 支持的 intent 集合必须与抽取器注册表一致。

这两处目前**恰好**相等，但没有任何机制保证它继续相等，而不相等会静默坏掉：

`planning_loop._understand` 遍历 `ar.operations`，对 `build_extractor` 返回
None 的操作 `continue`（不往 extractions 追加）。于是：

    len(extractions) < len(ar.operations)
      → build_plan 的 `if len(raw_operations) != len(extractions)` 命中
      → ValueError("raw_operations 与 extractions 必须一一对应")

即：给 analyzer 放开一个 intent 却忘了注册抽取器，**整轮规划直接崩**，
而不是优雅降级成 unmapped。反向（注册了抽取器但 analyzer 不放行）则是
死代码，抽取器永远拿不到活。

锁这个不变量比修 _understand 更合适：一一对应是 build_plan 的契约（它自己
显式检查），真正该保证的是"每个能进 operations 的 intent 都产出一条 extraction"。
"""

from paper_agent.tasking.extractors import _REGISTRY
from paper_agent.tasking.operation_analyzer import _SUPPORTED


def test_every_supported_intent_has_an_extractor():
    """否则该 intent 一出现就让 build_plan 抛 ValueError（整轮规划崩）。"""
    missing = sorted(i.value for i in _SUPPORTED if i not in _REGISTRY)
    assert not missing, (
        f"analyzer 放行了这些 intent 但没有抽取器：{missing}。\n"
        "后果：_understand 会跳过它们（不产 extraction），导致 extractions 比 "
        "operations 短，build_plan 抛 ValueError。要么注册抽取器，要么从 "
        "_SUPPORTED 移除（移除后会走 unmapped，是优雅降级）。"
    )


def test_no_extractor_is_unreachable():
    """反向：注册了却不放行 = 死代码，说明 _SUPPORTED 漏了或注册表多了。"""
    unreachable = sorted(i.value for i in _REGISTRY if i not in _SUPPORTED)
    assert not unreachable, (
        f"这些 intent 有抽取器但 analyzer 不放行，永远拿不到活：{unreachable}"
    )
