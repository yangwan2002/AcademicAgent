"""TurnMetrics：Planning 结果与只读交接计数。

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
    """跨 REPL 会话累积的自然语言 Turn 计数。"""

    turns: int = 0
    #: PlanExecutionReport 的终态分布（done / partially_completed / ...）
    plan_status: dict[str, int] = field(default_factory=dict)
    #: 未进执行的明确出口（needs_input / no_action / rejected / ...）
    planning_exit: dict[str, int] = field(default_factory=dict)
    #: Planning 内部异常（被 Coordinator 捕获）按异常类别
    planning_failed: dict[str, int] = field(default_factory=dict)
    #: OperationAnalyzer 成功但没有支持的写操作，交给 AskLoop 的次数
    readonly_fallbacks: int = 0
    #: 用户取消
    cancelled: int = 0

    # ---- 记录（全部不抛）----

    def record_turn(self) -> None:
        self.turns += 1

    def record_plan_status(self, status: str) -> None:
        _bump(self.plan_status, status)

    def record_planning_exit(self, status: str) -> None:
        _bump(self.planning_exit, status)

    def record_planning_failed(self, category: str) -> None:
        _bump(self.planning_failed, category)

    def record_readonly_fallback(self) -> None:
        self.readonly_fallbacks += 1

    def record_cancelled(self) -> None:
        self.cancelled += 1

    # ---- 序列化 / 展示 ----

    def to_dict(self) -> dict:
        return {
            "turns": self.turns,
            "plan_status": dict(self.plan_status),
            "planning_exit": dict(self.planning_exit),
            "planning_failed": dict(self.planning_failed),
            "readonly_fallbacks": self.readonly_fallbacks,
            "cancelled": self.cancelled,
        }

    def summary(self) -> str:
        """人类可读摘要。"""
        lines = [f"Planning Turn 统计（共 {self.turns} 个 Turn）"]
        if self.plan_status:
            lines.append("  计划终态：" + "、".join(
                f"{k}={v}" for k, v in sorted(self.plan_status.items())))
        if self.planning_exit:
            lines.append("  未执行出口：" + "、".join(
                f"{k}={v}" for k, v in sorted(self.planning_exit.items())))
        if self.planning_failed:
            lines.append("  Planning 内部异常：" + "、".join(
                f"{k}={v}" for k, v in sorted(self.planning_failed.items())))
        if self.readonly_fallbacks:
            lines.append(f"  转只读 AskLoop：{self.readonly_fallbacks}")
        if self.cancelled:
            lines.append(f"  用户取消：{self.cancelled}")
        return "\n".join(lines)

    # ---- 读回（绝不抛）----

    @classmethod
    def load(cls, path: str) -> "TurnMetrics":
        """从 JSON 读回累积计数；文件不存在/损坏 → 全零实例（绝不抛）。

        **为什么必须有**：本模块的定位是「跨 Turn 累积、崩溃后仍反映已发生的事」，
        但此前只有 `save` 没有 `load` ⇒ 每次启动 REPL 从零开始，
        `runs/turn_metrics.json` 只剩最后一次会话的最后状态（实测 `turns: 1`）。
        累积计数不跨会话，就无法回答「新栈这周降级了几次」——B-4 要的正是这个。

        损坏文件按**全零**处理而非报错：一份读不懂的计数不该阻止 REPL 启动，
        且下一次 `save` 会以正确结构覆盖它。
        """
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                return cls()
        except Exception:  # noqa: BLE001 - 不存在/损坏/无权限一律退回全零
            return cls()

        def _counter(key: str) -> dict[str, int]:
            value = raw.get(key)
            if not isinstance(value, dict):
                return {}
            out: dict[str, int] = {}
            for k, v in value.items():
                if isinstance(v, bool) or not isinstance(v, int):
                    continue     # 只接受整数计数，异常值丢弃而非崩
                out[str(k)] = v
            return out

        def _int(value) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        return cls(
            turns=_int(raw.get("turns")),
            plan_status=_counter("plan_status"),
            planning_exit=_counter("planning_exit"),
            planning_failed=_counter("planning_failed"),
            readonly_fallbacks=_int(raw.get("readonly_fallbacks")),
            cancelled=_int(raw.get("cancelled")),
        )

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
