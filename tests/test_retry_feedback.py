"""重试回灌的两条性质:回灌可照办 + 原地打转能识别。

这两件事都属于「跨模块口径」类缺陷,和 tests/test_citation_dialect.py 同源:
Gate 内部用 `reference_id` 记账没错,错在把它当成给模型看的标记 —— 跨界处必须转方言。
"""
from __future__ import annotations

import pytest

from paper_agent.gates.citation_quota_gate import CitationQuotaGate
from paper_agent.gates.diff_gate import extract_citation_tokens
from paper_agent.intents.retry_feedback import (
    citable_markers,
    exhausted_message,
    is_stagnant,
    merge_violations,
    stagnation_message,
)
from paper_agent.literature.dialect import style_for
from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import record_from_dict
from test_citation_pipeline_smoke import _bundle_of, _classifier, _quota, _rec


class _Structural:
    def __init__(self, violations=()):
        self.violations = tuple(violations)
        self.passed = not violations


class _Quota:
    def __init__(self, unused=(), violations=("total 未达标：需 10，实际 2，缺 8",)):
        self.passed = False
        self.violations = tuple(violations)
        self.unused_reference_ids = tuple(unused)


# ------------------------------------------------- 回灌的标记必须能照办

@pytest.mark.parametrize("ext", ["md", "tex", "docx"])
def test_feedback_markers_are_resolvable(ext):
    """**核心断言**:回灌里「可补引用」的每个标记,模型照抄后必须解析得回来。

    原缺陷:直接拼 `reference_id`（r00000000000c），md 下解析恒 NOT_FOUND ⇒
    模型照办也补不上配额 ⇒ 下轮收到同一句提示 ⇒ 烧完预算。
    """
    records = [_rec(i) for i in range(1, 8)]
    ledger = ReferenceLedger(records)
    rids = [r.reference_id for r in records]

    markers = citable_markers(rids, ledger, ext)
    assert len(markers) == len(records), f"ext={ext} 漏掉了可引用文献:{markers}"
    for m in markers:
        assert ledger.resolve_token(m, ext=ext).status.name == "RESOLVED", \
            f"ext={ext} 回灌了解析不了的标记 {m}"


def test_feedback_uses_dialect_not_reference_id():
    """回归:md 下回灌数字别名,tex 下回灌 bibkey —— 都不是 reference_id。"""
    rec = _rec(3)
    ledger = ReferenceLedger([rec])
    rid = rec.reference_id

    assert citable_markers([rid], ledger, "md") == ["3"]
    assert citable_markers([rid], ledger, "tex") == [rec.bibkey]
    assert rid not in citable_markers([rid], ledger, "md")


def test_merged_feedback_is_actionable_end_to_end():
    """整条链:Gate 判不达标 → 回灌 → 模型照抄 → 再判,这次必须通过。

    这是「回灌可照办」的最强形式:同一个 Gate,同一个账本,模型只做「照办」这一件事。
    """
    records = [_rec(i) for i in range(1, 13)]
    ledger = ReferenceLedger(records)
    bundle = _bundle_of(records)
    gate = CitationQuotaGate()

    # 第一轮:只引 2 篇，配额要 10 ⇒ 不达标
    first = "见 [1] 与 [2]。"
    r1 = gate.check(first, ext="md", bundle=bundle, ledger=ledger, quota=_quota(10),
                    original_text="", classifier=_classifier)
    assert not r1.passed

    feedback = merge_violations(_Structural(), r1, ledger=ledger, ext="md")
    hint = [v for v in feedback if "可补引用" in v]
    assert hint, f"没给出可补引用提示:{feedback}"

    # 模型照办:把提示里的标记全部引上
    markers = extract_citation_tokens(hint[0], "md")
    assert markers, "提示里抽不出任何标记（等于没法照办）"
    second = first + "".join(
        f"另有工作{style_for('md').render(m)}。" for m in markers[:8]
    )
    r2 = gate.check(second, ext="md", bundle=bundle, ledger=ledger, quota=_quota(10),
                    original_text="", classifier=_classifier)
    assert r2.passed, f"照办回灌后仍不通过:{r2.violations}"


def test_unresolvable_entry_omitted_from_feedback():
    """无别名的记录在 md 下无法引用 ⇒ 不提它,而不是给个假标记。"""
    ok = _rec(1)
    bad = record_from_dict({
        "reference_id": "rnoalias00001", "bibkey": "k2020a", "title": "No Alias",
    })
    ledger = ReferenceLedger([ok, bad])
    markers = citable_markers([ok.reference_id, bad.reference_id], ledger, "md")
    assert markers == ["1"]


def test_unknown_reference_id_skipped():
    """账本里查不到的 id 直接略去，不炸。"""
    ledger = ReferenceLedger([_rec(1)])
    assert citable_markers(["rnotinledger"], ledger, "md") == ["1"] or True
    assert citable_markers(["rnotinledger"], ledger, "md") == []


def test_structural_violations_preserved():
    """结构违规照原样带上，不被配额部分挤掉。"""
    merged = merge_violations(
        _Structural(("段落数不足",)), _Quota(unused=()), ledger=None, ext="md")
    assert "段落数不足" in merged
    assert any("未达标" in v for v in merged)


def test_passed_quota_contributes_nothing():
    class _Ok:
        passed = True
        violations = ("不该出现",)
        unused_reference_ids = ("r1",)

    assert merge_violations(_Structural(("A",)), _Ok(), ledger=None, ext="md") == ["A"]


# ------------------------------------------------- 原地打转

def test_identical_violations_are_stagnant():
    v = ["total 未达标：需 10，实际 2，缺 8"]
    assert is_stagnant(list(v), list(v))


def test_changing_violations_are_not_stagnant():
    """缺 8 → 缺 3:回灌起了作用，只是没收敛 ⇒ 该继续试。"""
    assert not is_stagnant(
        ["total 未达标：需 10，实际 2，缺 8"],
        ["total 未达标：需 10，实际 7，缺 3"],
    )


def test_first_round_never_stagnant():
    """第一轮没有「上一轮」可比 ⇒ 不能判打转。"""
    assert not is_stagnant(None, ["任何违规"])


def test_empty_violations_not_stagnant():
    assert not is_stagnant([], [])


def test_order_change_is_not_stagnant():
    assert not is_stagnant(["A", "B"], ["B", "A"])


def test_stagnation_message_names_the_cause():
    msg = stagnation_message(2, ["total 未达标：需 10，实际 2，缺 8"])
    assert "无法满足" in msg
    assert "缺 8" in msg
    # 必须区别于普通耗尽消息，否则诊断价值为零
    assert msg != exhausted_message(2, ["total 未达标：需 10，实际 2，缺 8"])
