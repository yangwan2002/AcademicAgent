"""P-2 · CitationUsage：正文实际引用的章节归属。

覆盖三件事：按章归属正确、解析失败留痕（不静默丢）、随 facts.json 往返不失真。
"""

from __future__ import annotations

import os

from paper_agent.literature.citation_usage import CitationUsage, scan_usage
from paper_agent.session.facts import KnownRef, SessionFacts

_MD = """# 1 引言
视觉定位是核心问题[1]，近年进展显著[2]。

# 2 相关工作
早期方法依赖手工特征[2]，后转向深度学习[3]。
"""


def _facts_with_refs(tmp_path, n: int = 3) -> SessionFacts:
    facts = SessionFacts(path=os.path.join(str(tmp_path), "facts.json"))
    for i in range(1, n + 1):
        facts.add_ref(KnownRef(id=str(i), title=f"论文{i}"))
    return facts


def _rid_of(facts: SessionFacts, alias: str) -> str:
    """由数字别名取 reference_id（reference_id 是内部哈希，测试不该硬编码）。"""
    res = facts.ledger.resolve_token(alias, ext=".md")
    assert res.reference_id, f"别名 {alias} 解析不到"
    return res.reference_id


def test_groups_citations_by_section(tmp_path):
    facts = _facts_with_refs(tmp_path)
    usage = facts.refresh_citation_usage(_MD, ".md")

    assert sorted(usage.by_section) == ["1_引言", "2_相关工作"]
    r1, r2, r3 = (_rid_of(facts, a) for a in ("1", "2", "3"))
    assert usage.references_in("1_引言") == (r1, r2)
    assert usage.references_in("2_相关工作") == (r2, r3)


def test_sections_citing_finds_shared_reference(tmp_path):
    """[2] 被两章共引 —— 这正是 B-2 写 bibitem 时要查的关系。"""
    facts = _facts_with_refs(tmp_path)
    usage = facts.refresh_citation_usage(_MD, ".md")

    assert set(usage.sections_citing(_rid_of(facts, "2"))) == {"1_引言", "2_相关工作"}
    assert usage.sections_citing(_rid_of(facts, "1")) == ("1_引言",)
    # 全部被引：保序去重，[2] 只出现一次
    assert len(usage.all_cited_ids()) == 3


def test_unresolvable_token_is_recorded_not_dropped(tmp_path):
    """解析不了的标记必须留痕：静默丢会让「引了不认识的文献」变成「本章没引」。"""
    facts = _facts_with_refs(tmp_path, n=1)
    usage = facts.refresh_citation_usage("# 1 引言\n引用[1]与不存在的[9]。\n", ".md")

    sec = usage.by_section["1_引言"]
    assert sec.reference_ids == (_rid_of(facts, "1"),)
    assert sec.unresolved == (("9", "not_found"),)
    assert usage.unresolved_tokens() == (("1_引言", "9", "not_found"),)


def test_document_without_headings_is_not_reported_as_empty(tmp_path):
    """分不出章节 ≠ 没有引用。后者会让 all_cited_ids 谎报为空。"""
    facts = _facts_with_refs(tmp_path, n=1)
    usage = facts.refresh_citation_usage("没有任何标题的正文，引用[1]。", ".md")

    assert sorted(usage.by_section) == ["_document"]
    assert usage.all_cited_ids() == (_rid_of(facts, "1"),)


def test_survives_facts_roundtrip(tmp_path):
    facts = _facts_with_refs(tmp_path)
    usage = facts.refresh_citation_usage(_MD, ".md")
    facts.save()

    reloaded = SessionFacts.load(facts.path)
    assert reloaded.citation_usage.to_json() == usage.to_json()
    assert reloaded.citation_usage.references_in("1_引言") == usage.references_in("1_引言")


def test_old_envelope_without_section_loads_as_empty(tmp_path):
    """旧 facts.json 没有 citation_usage 段 → 空表，不炸（向后兼容）。"""
    path = os.path.join(str(tmp_path), "facts.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"schema_version": 4, "references": [], "claims": {}}')

    facts = SessionFacts.load(path)
    assert facts.citation_usage.by_section == {}
    assert facts.citation_usage.all_cited_ids() == ()


def test_corrupt_rows_are_skipped_not_fatal():
    """派生数据不该为一条坏记录让 session 打不开（同 pending_flows 先例）。"""
    usage = CitationUsage.from_json([
        "不是 dict",
        {"reference_ids": ["r1"]},                       # 缺 section_id
        {"section_id": "ok", "reference_ids": ["r1"], "unresolved": [["9", "not_found"]]},
        {"section_id": "bad_unresolved", "unresolved": ["形状不对"]},
    ])

    assert sorted(usage.by_section) == ["bad_unresolved", "ok"]
    assert usage.by_section["ok"].unresolved == (("9", "not_found"),)
    assert usage.by_section["bad_unresolved"].unresolved == ()


def test_recompute_replaces_stale_entries(tmp_path):
    """整体重算：上一版引用过、这一版没了的，必须消失而不是残留。"""
    facts = _facts_with_refs(tmp_path)
    facts.refresh_citation_usage(_MD, ".md")
    assert facts.citation_usage.references_in("2_相关工作")

    after = facts.refresh_citation_usage("# 1 引言\n只剩这一章，引用[1]。\n", ".md")
    assert sorted(after.by_section) == ["1_引言"]
    assert after.references_in("2_相关工作") == ()


def test_tex_uses_bibkey_tokens(tmp_path):
    """.tex 走 \\cite{} + bibkey，与 md 的数字别名是两套解析口径。"""
    facts = SessionFacts(path=os.path.join(str(tmp_path), "facts.json"))
    facts.add_ref(KnownRef(id="1", bibkey="zhang2021", title="论文"))

    usage = scan_usage("\\section{引言}\n定位问题~\\cite{zhang2021}。\n", ".tex",
                       facts.ledger)
    sid = next(iter(usage.by_section))
    rid = facts.ledger.resolve_token("zhang2021", ext=".tex").reference_id
    assert usage.by_section[sid].reference_ids == (rid,)
