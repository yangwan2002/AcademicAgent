"""TurnMetrics：Turn 归属计数（backlog B-4）。

**为什么必须有**：`chat.py:_handle_planned_turn` 的理解层异常兜底会**静默** fall
through 到关键词路由，只印一行提示。不计数的话，E2E 跑完看到「20 条请求都处理了」，
却分不清是新栈处理 20 条，还是新栈 15 条 + 旧栈悄悄兜了 5 条。而洞正好在最关键处：
想测的就是新栈行不行，结果新栈失败时旧栈替它把活干了，让新栈**看起来更行**。

一个无法区分归属的实验，结论无效。

与 `trace.json` 的分工（见 backlog P-2a）：
- trace 是**每 run 一棵 span 树**、明确 best-effort（可丢事件、无 durability）；
- 本模块是**跨 Turn 累积的计数**，要能在崩溃后仍反映已发生的事，故每次记录后原子落盘。

纪律：
- 只存**计数与类别名**，不存用户消息 / 正文 / 查询原文（隐私零风险，无需脱敏）。
- 记录与落盘**绝不抛异常**：观测失败不拖垮 REPL（与 FanOutSink 同一原则）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _bump(counter: dict[str, int], key: str) -> None:
    name = str(key or "unknown")
    counter[name] = counter.get(name, 0) + 1


@dataclass
class TurnMetrics:
    """一次 REPL 会话内的 Turn 归属计数。"""

    turns: int = 0
    #: 每个 Turn 实际由哪条路径服务：off / shadow / active / legacy_fallback
    served_by: dict[str, int] = field(default_factory=dict)
    #: active 出了 PlanExecutionReport 的终态分布（done / partially_completed / ...）
    plan_status: dict[str, int] = field(default_factory=dict)
    #: active 未进执行的明确出口（needs_input / no_action / rejected / ...）
    planning_exit: dict[str, int] = field(default_factory=dict)
    #: 新栈**内部**异常（被 Coordinator 捕获，未降级）按异常类别
    active_failed: dict[str, int] = field(default_factory=dict)
    #: **静默降级到旧栈**按异常类别 —— B-4 的核心指标
    fallback_to_legacy: dict[str, int] = field(default_factory=dict)
    #: 用户取消
    cancelled: int = 0
    #: shadow 模式下与旧 Router 的意图一致 / 不一致（D-1 分歧统计的种子）
    shadow_matches_legacy: int = 0
    shadow_differs_from_legacy: int = 0
    shadow_incomplete: int = 0

    # ---- 记录（全部不抛）----

    def record_turn(self) -> None:
        self.turns += 1

    def record_served_by(self, path: str) -> None:
        _bump(self.served_by, path)

    def record_plan_status(self, status: str) -> None:
        _bump(self.plan_status, status)

    def record_planning_exit(self, status: str) -> None:
        _bump(self.planning_exit, status)

    def record_active_failed(self, category: str) -> None:
        _bump(self.active_failed, category)

    def record_fallback(self, category: str) -> None:
        _bump(self.fallback_to_legacy, category)

    def record_cancelled(self) -> None:
        self.cancelled += 1

    def record_shadow(self, *, ok: bool, matches_legacy: bool | None) -> None:
        if not ok:
            self.shadow_incomplete += 1
        elif matches_legacy:
            self.shadow_matches_legacy += 1
        else:
            self.shadow_differs_from_legacy += 1

    # ---- 派生 ----

    @property
    def fallback_count(self) -> int:
        return sum(self.fallback_to_legacy.values())

    @property
    def attribution_is_clean(self) -> bool:
        """本次会话的归属是否干净（无静默降级）→ E2E 结论是否可直接采信。"""
        return self.fallback_count == 0

    # ---- 序列化 / 展示 ----

    def to_dict(self) -> dict:
        return {
            "turns": self.turns,
            "served_by": dict(self.served_by),
            "plan_status": dict(self.plan_status),
            "planning_exit": dict(self.planning_exit),
            "active_failed": dict(self.active_failed),
            "fallback_to_legacy": dict(self.fallback_to_legacy),
            "fallback_count": self.fallback_count,
            "attribution_is_clean": self.attribution_is_clean,
            "cancelled": self.cancelled,
            "shadow": {
                "matches_legacy": self.shadow_matches_legacy,
                "differs_from_legacy": self.shadow_differs_from_legacy,
                "incomplete": self.shadow_incomplete,
            },
        }

    def summary(self) -> str:
        """人类可读摘要。归属不干净时**显式警告**，不让读者自己去推。"""
        lines = [f"Turn 归属统计（共 {self.turns} 个 Turn）"]
        if self.served_by:
            lines.append("  服务方：" + "、".join(
                f"{k}={v}" for k, v in sorted(self.served_by.items())))
        if self.plan_status:
            lines.append("  计划终态：" + "、".join(
                f"{k}={v}" for k, v in sorted(self.plan_status.items())))
        if self.planning_exit:
            lines.append("  未执行出口：" + "、".join(
                f"{k}={v}" for k, v in sorted(self.planning_exit.items())))
        if self.active_failed:
            lines.append("  新栈内部异常：" + "、".join(
                f"{k}={v}" for k, v in sorted(self.active_failed.items())))
        if self.cancelled:
            lines.append(f"  用户取消：{self.cancelled}")
        if self.shadow_matches_legacy or self.shadow_differs_from_legacy or self.shadow_incomplete:
            lines.append(
                f"  shadow 对比：一致={self.shadow_matches_legacy}"
                f"、不同={self.shadow_differs_from_legacy}"
                f"、未完成={self.shadow_incomplete}"
            )
        if self.fallback_count:
            lines.append(
                f"  ⚠ 静默降级到旧栈：{self.fallback_count} 次（"
                + "、".join(f"{k}={v}" for k, v in sorted(self.fallback_to_legacy.items()))
                + "）"
            )
            lines.append(
                "    注意：这些 Turn 实际由**关键词路由**服务，不代表新栈能力；"
                "本次统计不可直接用于评估新栈。"
            )
        else:
            lines.append("  归属干净：无静默降级")
        return "\n".join(lines)

    # ---- 落盘（原子、绝不抛）----

    def save(self, path: str) -> bool:
        """原子写 JSON。返回是否成功；失败只返回 False，绝不抛（观测不拖垮业务）。"""
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except Exception:  # noqa: BLE001 - 观测失败绝不拖垮 REPL
            return False


__all__ = ["TurnMetrics"]
