"""CitationContext：写作编排传给 execute_* 的配额上下文（A2d-5，design D7/D8）。

`execute_rewrite` / `execute_add_section` 的 `citation_context` 参数**默认 None** →
完全走旧路径（无配额零影响，硬回归线）。非 None 时：

- **跳过旧 `search_and_verify`**（Preflight 已是唯一检索入口，Req 13.12）；
- 生成循环每轮跑 Structural Gate **与** CitationQuotaGate，合并违规一次回灌；
- 未通过不可豁免 Gate 不写回 Working Copy。

frozen 值对象，不含可变状态；`deviations` 由编排层累积。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CitationContext:
    bundle: object                        # A2d-2 EvidenceBundle（冻结，全程共用同一实例）
    quota: object                         # A2b QuotaSpec(kind=CITATION)
    effective_constraints: tuple = ()     # A2d-4 effective_constraints
    deviations: tuple = ()                # AcceptedQuotaDeviation（preflight 阶段已接受者）
    port: object | None = None            # LiteratureInteractionPort（final_gate 决策用）
    classifier: object | None = None
    policy: str = "high_only"
    operation_id: str = ""
    task_fingerprint: str = ""


@dataclass(frozen=True)
class QuotaReport:
    """写作结果附带的配额报告（无配额路径为空 → 结果结构不变）。"""

    original_gaps: tuple = ()
    effective_gaps: tuple = ()
    used_reference_ids: tuple[str, ...] = ()
    unused_reference_ids: tuple[str, ...] = ()
    accepted_final_gate: bool = False
    deviations: tuple = ()


__all__ = ["CitationContext", "QuotaReport"]
