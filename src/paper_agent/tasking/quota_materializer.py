"""引用配额 materialize（A2d-1 / 任务 2，design-a2d D1）。

LLM 只产 `RawQuotaSpec`；本模块把它**确定性**收敛为可执行的 Citation `QuotaSpec`：
`kind=CITATION` / `scope=ACTUALLY_CITED` **由代码注入**（不由 LLM）；`base_year` 由 Turn 级
Clock 注入一次；裸数量按 A2b 的 `min`。成品**必是** executable QuotaSpec，绝不把
`RawQuotaSpec` / `QuotaFinalizeResult` 当成品外传。

`tasking` 可依赖 `literature.quota`（方向正确：tasking 在 literature 上游）。
本模块不读全局 config（base_year 注入）。
"""

from __future__ import annotations

from dataclasses import dataclass

from paper_agent.literature.quota import (
    QuotaKind,
    QuotaScope,
    QuotaSpec,
    RawQuotaSpec,
    finalize_quota,
)


@dataclass(frozen=True)
class QuotaMaterializationResult:
    """materialize 结果：成品 spec + issues + 是否需澄清 + 是否可执行。"""

    spec: QuotaSpec | None = None
    issues: tuple = ()
    needs_clarification: bool = False
    executable: bool = False


def materialize_citation_quota(
    raw: RawQuotaSpec | None,
    *,
    base_year: int,
) -> QuotaMaterializationResult:
    """把 RawQuotaSpec 收敛为可执行 Citation QuotaSpec（确定性）。

    - raw 为 None/空 → 用户没提配额：`spec=None`，不追问（needs_clarification=False）。
    - 否则走 A2b `finalize_quota(kind=CITATION, bare_count_policy="min")`；含
      NEEDS_CLARIFICATION → needs_clarification=True。
    - 成品 spec 恒为 kind=CITATION / scope=ACTUALLY_CITED（断言）。
    """
    if raw is None or (not raw.filters and not raw.constraints):
        return QuotaMaterializationResult(spec=None, needs_clarification=False, executable=False)

    result = finalize_quota(
        raw, kind=QuotaKind.CITATION, base_year=base_year, bare_count_policy="min"
    )
    from paper_agent.literature.quota import QuotaIssueSeverity

    needs_clar = any(
        i.severity is QuotaIssueSeverity.NEEDS_CLARIFICATION for i in result.issues
    )
    spec = result.spec if result.executable else None
    if spec is not None:
        # kind/scope 由代码注入，绝不被 LLM 改写
        assert spec.kind is QuotaKind.CITATION
        assert spec.scope is QuotaScope.ACTUALLY_CITED
    return QuotaMaterializationResult(
        spec=spec,
        issues=result.issues,
        needs_clarification=needs_clar,
        executable=result.executable,
    )


__all__ = ["QuotaMaterializationResult", "materialize_citation_quota"]
