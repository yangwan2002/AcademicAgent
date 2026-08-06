"""CitationQuotaGate：正文引用配额闭环 + 新增引用边界（A2d-3，design-a2d D4/D7）。

在**候选正文生成后、写入 Working_Copy 前**校验（Req 14.1）。两类失败严格分开：

- **配额未达**：可受 `AcceptedQuotaDeviation` 的 `effective_lower_bound` 影响
  （Req 13.11/14.6 同步后口径：按 effective 下界执行，**不是**维度级无条件跳过）。
- **非法引用**：新增的 unresolved / ambiguous / Bundle 外引用 → **硬失败、永不 waiver**。

计数只认 `Bundle ∩ candidate resolved`（Req 14.3），按 `reference_id` 去重、按不同文献数
计数，且**不依据模型自报**（Req 14.2 确定性文本解析）。同时产 effective 与 original 双 gap
供诚实报告。

rewrite 语义：原文已有的引用可原样保留（记 `preserved_legacy_tokens`，不推进新配额、
不因本次改写被自动删除，Req 4.5/4.7）；**新增**引用必须来自当前 Bundle。
"""

from __future__ import annotations

from dataclasses import dataclass

from paper_agent.gates.diff_gate import extract_citation_tokens
from paper_agent.literature.ledger import ResolutionStatus
from paper_agent.literature.quota import count_by_dimension, evaluate_constraints


@dataclass(frozen=True)
class CitationQuotaGateResult:
    passed: bool
    effective_gaps: tuple = ()
    original_gaps: tuple = ()
    used_reference_ids: tuple[str, ...] = ()
    unused_reference_ids: tuple[str, ...] = ()
    new_unresolved_tokens: tuple[str, ...] = ()
    new_ambiguous_tokens: tuple[str, ...] = ()
    new_out_of_bundle_ids: tuple[str, ...] = ()
    preserved_legacy_tokens: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()


class CitationQuotaGate:
    """配额 Gate。不检索、不换 Bundle（Req 13.12：写作循环内绝不检索）。"""

    def check(
        self,
        candidate: str,
        *,
        ext: str,
        bundle,
        ledger,
        quota,
        effective_constraints=None,
        original_text: str = "",
        classifier=None,
        policy: str = "high_only",
    ) -> CitationQuotaGateResult:
        effective = tuple(effective_constraints if effective_constraints is not None
                          else quota.count_constraints)
        cand_tokens = extract_citation_tokens(candidate, ext)
        orig_tokens = set(extract_citation_tokens(original_text, ext)) if original_text else set()
        new_tokens = tuple(t for t in cand_tokens if t not in orig_tokens)

        bundle_ids = set(bundle.reference_ids())

        unresolved: list[str] = []
        ambiguous: list[str] = []
        out_of_bundle: list[str] = []
        preserved: list[str] = []
        counted_ids: list[str] = []

        for token in cand_tokens:
            res = ledger.resolve_token(token, ext=ext)
            is_new = token in new_tokens
            if res.status is ResolutionStatus.RESOLVED:
                rid = res.reference_id
                if rid in bundle_ids:
                    if rid not in counted_ids:
                        counted_ids.append(rid)          # 只 Bundle ∩ candidate 参与配额
                elif is_new:
                    out_of_bundle.append(rid)            # 新增却不在 Bundle → 硬失败
                else:
                    preserved.append(token)              # 原文保留、不在 Bundle → 不计配额
            elif res.status is ResolutionStatus.AMBIGUOUS:
                if is_new:
                    ambiguous.append(token)
                else:
                    preserved.append(token)
            else:  # NOT_FOUND
                if is_new:
                    unresolved.append(token)
                else:
                    preserved.append(token)

        # 计数：施加 filters + language policy，按不同 reference_id
        records = [r for r in (ledger.get(rid) for rid in counted_ids) if r is not None]
        counts = count_by_dimension(records, quota, classifier=classifier, policy=policy)
        effective_gaps = evaluate_constraints(counts, effective)
        original_gaps = evaluate_constraints(counts, quota.count_constraints)

        used = tuple(counted_ids)
        unused = tuple(rid for rid in bundle.reference_ids() if rid not in set(used))

        violations: list[str] = []
        if unresolved:
            violations.append("新增引用无法解析：" + ", ".join(sorted(unresolved)))
        if ambiguous:
            violations.append("新增引用存在歧义：" + ", ".join(sorted(ambiguous)))
        if out_of_bundle:
            violations.append("新增引用不在本次证据集内：" + ", ".join(sorted(out_of_bundle)))
        for g in effective_gaps:
            violations.append(
                f"{g.dimension.value} 未达标：需 {g.required}（{g.operator.value}），"
                f"实际 {g.achieved}，缺 {g.amount}"
            )

        illegal = bool(unresolved or ambiguous or out_of_bundle)
        passed = (not illegal) and not effective_gaps

        return CitationQuotaGateResult(
            passed=passed,
            effective_gaps=effective_gaps,
            original_gaps=original_gaps,
            used_reference_ids=used,
            unused_reference_ids=unused,
            new_unresolved_tokens=tuple(unresolved),
            new_ambiguous_tokens=tuple(ambiguous),
            new_out_of_bundle_ids=tuple(out_of_bundle),
            preserved_legacy_tokens=tuple(dict.fromkeys(preserved)),
            violations=tuple(violations),
        )


__all__ = ["CitationQuotaGate", "CitationQuotaGateResult"]
