"""回归：跨批次提交不得产生重复数字别名。

真机 facts.json（proj_1780f6e14f27，19 条文献、两个 committed batch）里别名
1/10/11/12/13 各指向两条不同文献。成因：`attempt.py` 在 Attempt 期按
`ledger.next_numeric_alias()` 预分配 `numeric_base`，**两批在彼此提交前各自算基线就会拿到
同一段号**；`ledger.add_record` 是忠实追加、不校验别名唯一性，冲突直接落盘。

后果：`resolve_token(ext="md")` 对这些号返回 AMBIGUOUS ⇒ md 论文里相关引用一律被判
「无法解析」，CitationQuotaGate 永远差满额，rewrite 重试到预算耗尽也不可能通过。
"""
import json

from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import record_from_dict
from paper_agent.literature.retrieval.transaction import _dealias


def _rec(rid: str, alias: str, *, bibkey: str = "", title: str = "T"):
    return record_from_dict({
        "reference_id": rid,
        "numeric_aliases": [alias],
        "bibkey": bibkey or rid,
        "title": title,
    })


def test_colliding_alias_is_reassigned():
    led = ReferenceLedger([_rec("r001", "1"), _rec("r002", "2")])
    fixed = _dealias(_rec("r003", "2"), led)
    assert fixed.numeric_aliases == ("3",)


def test_non_colliding_alias_is_left_alone():
    led = ReferenceLedger([_rec("r001", "1")])
    fixed = _dealias(_rec("r002", "2"), led)
    assert fixed.numeric_aliases == ("2",)


def test_dealias_only_touches_numeric_aliases():
    led = ReferenceLedger([_rec("r001", "1", bibkey="keep2020a")])
    incoming = _rec("r002", "1", bibkey="other2021b", title="Untouched Title")
    fixed = _dealias(incoming, led)
    assert fixed.numeric_aliases == ("2",)
    assert fixed.reference_id == "r002"
    assert fixed.bibkey == "other2021b"
    assert fixed.title == "Untouched Title"


def test_record_without_alias_is_returned_unchanged():
    led = ReferenceLedger([_rec("r001", "1")])
    incoming = record_from_dict({"reference_id": "r002", "bibkey": "b", "title": "T"})
    assert _dealias(incoming, led).numeric_aliases == ()


def test_second_batch_all_resolvable_after_dealias():
    """两批各自从 base=1 编号（真机情形）：合并后每个别名都必须唯一可解析。"""
    batch1 = [_rec(f"ra{i}", str(i)) for i in range(1, 6)]
    batch2 = [_rec(f"rb{i}", str(i)) for i in range(1, 6)]   # 同段号，冲突
    led = ReferenceLedger(batch1)
    for r in batch2:
        led.add_record(_dealias(r, led))

    aliases = led.resolvable_numeric_aliases()
    assert len(aliases) == 10, aliases
    unresolved = [
        a for a in aliases if led.resolve_token(a, ext="md").status.name != "RESOLVED"
    ]
    assert not unresolved, unresolved


def test_collision_without_dealias_is_ambiguous():
    """基线：不修的话就是 AMBIGUOUS —— 说明这条测试确实覆盖了真机故障。"""
    led = ReferenceLedger([_rec("r001", "1")])
    led.add_record(_rec("r002", "1"))
    assert led.resolve_token("1", ext="md").status.name == "AMBIGUOUS"
