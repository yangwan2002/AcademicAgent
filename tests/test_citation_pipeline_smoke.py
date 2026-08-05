"""引用链路端到端冒烟(**不打真实 LLM**)。

模拟一个**听话的模型**:它严格照抄 prompt 数据块里公布的标记。这样的模型必须能过 Gate,
否则链路本身不可满足 —— 这正是真机「rewrite 重试三次全失败」的实质:模型无论怎么写都过不了。

覆盖的一整条链:

    真实账本记录 → build_evidence_block(公布标记) → 听话模型照抄
                 → extract_citation_tokens(抽回) → ledger.resolve_token(解析)
                 → CitationQuotaGate(配额判定)

上一轮的四个缺陷,**每一个都会被这里拦住**:
  1. 标记口径不匹配   → 照抄的标记解析不了 ⇒ test_obedient_model_passes_quota_gate 红
  2. 正则不认 r+hex   → 抽不到 token ⇒ counted_ids 空 ⇒ 同上红
  3. 段落上限过窄     → test_expanded_body_within_budget 红
  4. 别名跨批次碰撞   → resolve 得 AMBIGUOUS ⇒ test_two_batch_ledger_pipeline 红

与 tests/test_citation_dialect.py 的分工:那边测「两个模块之间的口径关系」,
这边测「整条链在真实数据形状下能否走通」。
"""
from __future__ import annotations

import pytest

from paper_agent.gates.citation_quota_gate import CitationQuotaGate
from paper_agent.gates.diff_gate import RewriteGate, extract_citation_tokens
from paper_agent.generate.context_builder import build_evidence_block
from paper_agent.literature.dialect import style_for
from paper_agent.literature.language import DEFAULT_THRESHOLDS, classify_record_language
from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.models import ReferenceRecord
from paper_agent.literature.quota import (
    CountConstraint,
    CountDimension,
    CountOperator,
    QuotaKind,
    QuotaScope,
    QuotaSpec,
)
from paper_agent.literature.relevance import (
    RelevanceAssessment,
    RelevanceCriteria,
    RelevanceStore,
    relevance_content_hash,
)
from paper_agent.literature.retrieval.transaction import _dealias
from paper_agent.literature.verification import EventKind, make_event

_CRIT = RelevanceCriteria(topic="collaborative slam")
_JUDGE = "judge-v1"


def _rec(i: int, *, numeric: str | None = None) -> ReferenceRecord:
    ev = make_event(EventKind.VERIFIED, source="crossref", identifier_type="doi",
                    identifier=f"10.1/r{i}", authority="final",
                    checked_at="2026-01-01T00:00:00Z")
    return ReferenceRecord(
        reference_id=f"r{i:012x}",
        title=f"Paper Number {i}",
        abstract=f"This work studies topic {i} for collaborative slam systems.",
        year=2015 + (i % 10),
        language_hint="en",
        work_identifiers=(("doi", f"10.1/r{i}"),),
        numeric_aliases=(numeric if numeric is not None else str(i),),
        bibkey=f"author{2015 + (i % 10)}word{i}",
        verification_events=(ev,),
    )


def _classifier(rec):
    return classify_record_language(rec, DEFAULT_THRESHOLDS)


def _store(records):
    st = RelevanceStore()
    for r in records:
        st.put(RelevanceAssessment(
            reference_id=r.reference_id,
            criteria_fingerprint=_CRIT.fingerprint(),
            content_hash=relevance_content_hash(r),
            judge_key=_JUDGE,
            relevance="high",
            rationale="on topic",
        ))
    return st


def _bundle_of(records):
    """EvidenceBundle 的最小替身:只带标记口径所需字段。"""
    class _E:
        def __init__(self, rec):
            self.bibkey = rec.bibkey
            self.numeric_alias = rec.numeric_aliases[0] if rec.numeric_aliases else ""
            self.reference_id = rec.reference_id
            self.title = rec.title
            self.year = rec.year
            self.language_bucket = "en"
            self.spans = ()

    class _B:
        entries = tuple(_E(r) for r in records)

        def reference_ids(self):
            return {e.reference_id for e in self.entries}

    return _B()


def _quota(n: int) -> QuotaSpec:
    cons = (CountConstraint(CountDimension.TOTAL, CountOperator.MIN, n),)
    return QuotaSpec(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED, count_constraints=cons)


def _obedient_model(block: str, ext: str, *, n: int) -> str:
    """听话的模型:只引用数据块里公布的标记,按该 ext 的语法写。

    这是**能力下界** —— 一个完全服从指令的模型。它过不了,说明链路不可满足。
    """
    markers = extract_citation_tokens(block, "md")[:n]
    style = style_for(ext)
    paras = [
        f"研究进展表明相关方法持续演进{style.render(m)}。这一方向已有系统性讨论。"
        for m in markers
    ]
    return "\n\n".join(paras)


# ------------------------------------------------------------------ 主链路

@pytest.mark.parametrize("ext", ["md", "tex", "docx"])
def test_obedient_model_passes_quota_gate(ext):
    """照抄公布标记的模型必须过配额 Gate。红 ⇒ 链路不可满足(真机故障复现)。"""
    records = [_rec(i) for i in range(1, 13)]
    ledger = ReferenceLedger(records)
    bundle = _bundle_of(records)
    quota = _quota(10)

    block = build_evidence_block(bundle, ext)
    candidate = _obedient_model(block, ext, n=10)

    res = CitationQuotaGate().check(
        candidate, ext=ext, bundle=bundle, ledger=ledger, quota=quota,
        original_text="原有概述。", classifier=_classifier,
    )
    assert res.passed, f"ext={ext} 听话模型仍被拒:{res.violations}"


@pytest.mark.parametrize("ext", ["md", "tex"])
def test_every_advertised_marker_is_counted(ext):
    """公布 12 条、引用 12 条 ⇒ counted 必须是 12,不能有标记被静默丢弃。"""
    records = [_rec(i) for i in range(1, 13)]
    ledger = ReferenceLedger(records)
    bundle = _bundle_of(records)

    block = build_evidence_block(bundle, ext)
    candidate = _obedient_model(block, ext, n=12)

    res = CitationQuotaGate().check(
        candidate, ext=ext, bundle=bundle, ledger=ledger, quota=_quota(12),
        original_text="原有概述。", classifier=_classifier,
    )
    assert res.passed, res.violations
    assert len(res.used_reference_ids) == 12, f"used={sorted(res.used_reference_ids)}"
    # 没有任何公布出去的标记落进「解析不了 / 有歧义 / 不在 bundle 内」三类
    assert not res.new_unresolved_tokens, res.new_unresolved_tokens
    assert not res.new_ambiguous_tokens, res.new_ambiguous_tokens
    assert not res.new_out_of_bundle_ids, res.new_out_of_bundle_ids


def test_reference_id_style_tokens_are_extracted():
    """账本 id 形如 `r`+hex —— 抽取器必须认它(旧正则要求首字符为数字,一个都抽不到)。"""
    body = "见 [r1a2b3c4d5e6f] 与 [rdeadbeef0001]。"
    assert extract_citation_tokens(body, "md") == ("r1a2b3c4d5e6f", "rdeadbeef0001")


def test_markdown_link_not_mistaken_for_citation():
    """`[文字](url)` 不是引用 —— 放宽首字符后仍须排除链接。"""
    assert extract_citation_tokens("见 [文档](http://x) 和 [3]。", "md") == ("3",)


# ------------------------------------------------------------ 结构 Gate 联动

def test_expanded_body_within_budget():
    """「概述扩写成正文 + 引 10 篇」必须能过段落上限(缺陷 3)。"""
    records = [_rec(i) for i in range(1, 11)]
    original = "本节概述协同 SLAM 的研究进展。"      # 1 段
    candidate = _obedient_model(build_evidence_block(_bundle_of(records), "md"), "md", n=10)
    assert candidate.count("\n\n") + 1 == 10

    class _F:
        def bibkeys(self):
            return {r.bibkey for r in records}

        def ref_ids(self):
            return {a for r in records for a in r.numeric_aliases}

        def number_values(self):
            return set()

    gate = RewriteGate("md", _F(), target_level=2, expand_budget=10)
    res = gate.check(original, candidate)
    para_violations = [v for v in res.violations if "段落" in v]
    assert not para_violations, para_violations


def test_shrinking_still_rejected():
    """放宽上限不等于放弃下限:越改越少仍须拦(避免修复顺带把护栏拆了)。"""
    class _F:
        def bibkeys(self):
            return set()

        def ref_ids(self):
            return set()

        def number_values(self):
            return set()

    original = "\n\n".join(f"第 {i} 段内容。" for i in range(1, 11))
    gate = RewriteGate("md", _F(), target_level=2, expand_budget=20)
    res = gate.check(original, "只剩一段。")
    assert any("段落" in v for v in res.violations), res.violations


# ------------------------------------------------------ 跨批次账本(缺陷 4)

def test_two_batch_ledger_pipeline():
    """两批文献各自从 base=1 编号(真机情形)→ 提交去碰撞 → 整条链仍走得通。"""
    batch1 = [_rec(i, numeric=str(i)) for i in range(1, 8)]
    batch2 = [_rec(100 + i, numeric=str(i)) for i in range(1, 8)]   # 同段号

    ledger = ReferenceLedger(batch1)
    for rec in batch2:
        ledger.add_record(_dealias(rec, ledger))

    records = list(ledger.all_records())
    assert len(records) == 14
    bundle = _bundle_of(records)

    # 账本已去碰撞,但 bundle 替身用的是记录自身的别名 —— 需取账本里的最终值
    block = build_evidence_block(bundle, "md")
    markers = extract_citation_tokens(block, "md")
    unresolved = [
        m for m in markers if ledger.resolve_token(m, ext="md").status.name != "RESOLVED"
    ]
    assert not unresolved, f"跨批次后仍有解析不了的标记:{unresolved}"
    assert len(set(markers)) == len(markers), "公布了重复标记"
