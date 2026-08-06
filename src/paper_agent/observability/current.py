"""Run 作用域的「当前 bus」（ambient context）。

**为什么需要它**：`providers/llm/structured.py::structured_call` 是全项目结构化输出的
唯一收敛点（relevance / relevance_batch / query_planner / extractors 全经它）。要给
这些 LLM 调用埋 span，只有两条路：

1. 把 `bus` 显式穿进 `structured_call` —— 需要同时改
   `RelevanceJudge` / `BatchRelevanceJudge` / `LLMQueryPlanner` / `BaseExtractor`
   的构造或方法签名，**以及它们上游每一层**（retrieval Loop → Orchestrator →
   Handler → PlanExecutor）。链条上任一跳漏传，观测就静默失效 —— 正是
   `docs/backlog.md` 第七节「能力写对了，接线送不到」那一类 bug 的温床。
2. 由 Coordinator 在 run 边界登记一个 run 作用域的 ambient bus，`structured_call`
   就地取用。**一跳，无中间层**。

选 2。

**这不违反「bus 按方法传、不长存」的纪律**：那条纪律防的是 bus 被对象长期持有导致
跨 run 串台。`ContextVar` 由 `use_bus()` 在 run 退出时 `reset`（`finally`，异常也走），
生命周期严格等于一次 run —— 比长存引用更强的保证，不是更弱。

**边界（刻意不做）**：
- 不用它替代任何**已有**的显式 `bus` 参数。已显式传到的地方继续显式传：
  显式优于隐式，此处只补显式传不到的地方。
- 业务层不读它。只有观测代码读 —— 业务逻辑绝不因「当前有没有 bus」而改变行为。
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar

#: 当前 run 的 bus；run 外为 None（此时埋点全部退化为 no-op）。
_CURRENT_BUS: ContextVar[object | None] = ContextVar("paper_agent_current_bus", default=None)


def current_bus():
    """当前 run 的 bus，run 外返回 None。读取绝不抛。"""
    try:
        return _CURRENT_BUS.get()
    except Exception:  # noqa: BLE001 - 观测查询绝不拖垮业务
        return None


@contextlib.contextmanager
def use_bus(bus):
    """在此作用域内把 `bus` 登记为当前 bus；退出必定还原（含异常/取消路径）。"""
    token = _CURRENT_BUS.set(bus)
    try:
        yield bus
    finally:
        try:
            _CURRENT_BUS.reset(token)
        except Exception:  # noqa: BLE001 - reset 失败不能反过来炸业务
            pass


def ambient_publish(kind_name: str, **data) -> None:
    """用当前 ambient bus 发一个事件（按 `EventKind` 成员名寻址）。

    按名字取 kind 而非导入枚举：本模块须对「事件种类还没定义」保持宽容
    （`plan_executor._publish` 同一策略）。无 bus / 无该种类 → 静默 no-op。
    """
    bus = current_bus()
    if bus is None:
        return
    try:
        from paper_agent.observability.events import EventKind
    except Exception:  # noqa: BLE001
        return
    kind = getattr(EventKind, kind_name, None)
    if kind is None:
        return
    try:
        bus.publish(kind, **data)
    except Exception:  # noqa: BLE001 - 观测失败不拖垮业务
        pass


@contextlib.contextmanager
def ambient_span(name: str, **attrs):
    """用当前 ambient bus 开一个 span；无 bus 时是零成本 no-op。

    观测**永不**改变业务可见行为：拿不到 bus、`span()` 不存在、或 span 自身抛异常，
    一律安静退化为空上下文。
    """
    bus = current_bus()
    span = getattr(bus, "span", None) if bus is not None else None
    if span is None:
        yield None
        return
    try:
        scope = span(name, **attrs)
    except Exception:  # noqa: BLE001
        yield None
        return
    # `SpanScope` 自身已保证 publish 不外抛、SPAN_END 失败也弹栈，故这里直接进入：
    # 业务异常照常穿过（span 会记 failed 终态），不额外包裹。
    with scope as entered:
        yield entered


__all__ = ["ambient_publish", "ambient_span", "current_bus", "use_bus"]
