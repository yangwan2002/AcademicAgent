"""重试回灌:把 Gate 违规整理成**模型真能照办**的反馈,并识别原地打转。

## 为什么单独一个模块

`rewrite` 与 `add_section` 的重试循环原先各自写了一份逐字相同的违规合并代码,而且都犯了
同一个错:把 `unused_reference_ids`(内部 `reference_id`,形如 `r00000000000c`)直接拼进
回灌文本。md 方言下模型只能引数字别名 ⇒ 照抄回灌即「无法解析」⇒ 配额永远差满额 ⇒
下一轮又收到同一句提示。三轮烧完,每轮都在追一个不可能达成的指令。

`reference_id` 是**内部身份**,方言标记是**对模型的公布形态** —— 两者跨界处必须转换。
账本记账继续用 reference_id(QuotaReport 不受影响),只有进入 prompt 的文本走
`dialect.marker_for`。这与 `build_evidence_block` 公布标记的口径同源。

## 原地打转

违规逐轮**变化**(缺 5 篇 → 缺 2 篇)说明回灌有效、只是没收敛,多给预算是合理的。
违规逐轮**完全相同**说明信息没有流动:模型已经照办了,是约束本身在当前口径下不可满足。
继续烧预算唯一的作用是让失败来得更晚、更难诊断。`is_stagnant` 把这种情况一次识别出来,
失败消息也换成可诊断的说法,而不是含糊的「尝试 3 次未通过」。
"""

from __future__ import annotations

from paper_agent.literature.dialect import marker_for, style_for


def citable_markers(reference_ids, ledger, ext: str) -> list[str]:
    """把内部 `reference_id` 列表转成该 ext 下模型**能写进正文**的标记。

    取不到合法标记的条目直接略去 —— 宁可少提示,也不给一个解析不了的假标记
    （与 `dialect.marker_for` 不跨方言回退同一理由）。
    """
    out: list[str] = []
    for rid in reference_ids or ():
        record = ledger.get(rid) if ledger is not None else None
        if record is None:
            continue
        marker = marker_for(record, ext)
        if marker and marker not in out:
            out.append(marker)
    return out


def merge_violations(structural, quota_result, *, ledger, ext: str) -> list[str]:
    """把结构 Gate 与配额 Gate 的违规合并成一次回灌（顺序确定）。

    `structural` 可为 None（无结构 Gate 结果时）。「可补引用」一项用方言标记表述,
    模型照抄即可解析。
    """
    merged: list[str] = list(getattr(structural, "violations", ()) or ())
    if quota_result is None or quota_result.passed:
        return merged

    merged.extend(quota_result.violations)
    markers = citable_markers(quota_result.unused_reference_ids, ledger, ext)
    if markers:
        # 按该方言的**成品形态**列出（md → `[3]`，tex → `\cite{key}`），模型可逐字照抄。
        # 只给裸 token 会让模型自己猜语法，等于把同一个口径问题推给它。
        style = style_for(ext)
        merged.append(
            "可补引用（证据集中尚未使用，可直接照此形式写入正文）："
            + " ".join(style.render(m) for m in markers)
        )
    return merged


def is_stagnant(previous: list[str] | None, current: list[str]) -> bool:
    """本轮违规与上一轮**完全相同** ⇒ 重试不再产生信息,应当停。

    逐项相等（含顺序）才算相同:违规文本里带有「缺 N」这类计数,顺序与内容任一变化都说明
    模型的输出在动,值得再试一轮。
    """
    return previous is not None and previous == current and bool(current)


def stagnation_message(attempts: int, violations: list[str]) -> str:
    """原地打转时的失败消息:点明约束不可满足,而非笼统地说重试失败。"""
    return (
        f"重试 {attempts} 次后违规完全没有变化，说明当前约束组合无法满足"
        f"（继续重试不会有不同结果，已提前停止）。仍未解决：\n"
        + "\n".join(f"  - {v}" for v in violations)
        + "\n请检查：配额要求是否超过证据集实际可引用的文献数；"
        "或所需文献是否尚未核验入库。"
    )


def exhausted_message(attempts: int, violations: list[str]) -> str:
    """预算耗尽（违规仍在变化，只是没收敛）时的失败消息。"""
    return (
        f"我尝试了 {attempts} 次仍未通过校验。最后一次违反：\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


__all__ = [
    "citable_markers", "merge_violations", "is_stagnant",
    "stagnation_message", "exhausted_message",
]
