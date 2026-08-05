"""回填已有账本的著录字段（`:refs`）。

provider 修好只对**新检索**生效；已存在的记录是修复前抓的，刊名/卷期页仍为空。
不回填，老会话永远渲染不出完整格式（真机那 25 条正是如此）。

设计上刻意**只按 DOI 精确查、不做模糊匹配**:著录字段错配比缺失更糟 ——
把 A 的刊名安到 B 上，读者按刊名去查会查不到，且这种错极难自查。
"""
from __future__ import annotations

from paper_agent.literature.backfill import backfill_ledger, backfill_record
from paper_agent.literature.citation_styles import get_style
from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import record_from_dict
from paper_agent.workspace.models import ReferenceEntry


class _Status:
    def __init__(self, name):
        self.name = name


class _Result:
    def __init__(self, name, candidate=None):
        self.status = _Status(name)
        self.candidate = candidate


class _FakeCrossref:
    """按 DOI 返回预置结果；记录被问过的 DOI，用于断言「不做模糊匹配」。"""

    def __init__(self, table, *, raise_on=()):
        self.table = table
        self.raise_on = set(raise_on)
        self.asked = []

    def fetch_metadata(self, id_type, identifier):
        self.asked.append((id_type, identifier))
        if identifier in self.raise_on:
            raise RuntimeError("网络炸了")
        found = self.table.get(identifier)
        if found is None:
            return _Result("NOT_FOUND_AT_SOURCE")
        return _Result("FOUND", found)


def _cand(**kw):
    base = dict(id="x", title="T", authors=["A"], year=2020, source_id="10.1/x",
                container_title="IEEE Access", volume="8", issue="3",
                pages="1-10", reference_type="journal-article", publisher="IEEE")
    base.update(kw)
    return ReferenceEntry(**base)


def _rec(doi="10.1/x", **kw):
    d = {
        "reference_id": "r000000000001",
        "title": "Existing Title",
        "authors": ["Author A"],
        "year": 2020,
        "numeric_aliases": ["1"],
        "work_identifiers": [["doi", doi]] if doi else [],
    }
    d.update(kw)
    return record_from_dict(d)


# ---------------- 单条 ----------------

def test_fills_empty_fields():
    rec = _rec()
    assert rec.container_title == ""
    new, changed = backfill_record(rec, _FakeCrossref({"10.1/x": _cand()}))
    assert changed
    assert new.container_title == "IEEE Access"
    assert new.volume == "8" and new.pages == "1-10"


def test_does_not_overwrite_existing_values():
    """用户手工订正过的值不该被回填冲掉。"""
    rec = _rec(container_title="我订正过的刊名", volume="99")
    new, _ = backfill_record(rec, _FakeCrossref({"10.1/x": _cand()}))
    assert new.container_title == "我订正过的刊名"
    assert new.volume == "99"
    assert new.pages == "1-10"        # 空字段照样补


def test_no_doi_means_no_guessing():
    """无 DOI ⇒ 不查、不猜。错配比缺失更糟。"""
    provider = _FakeCrossref({"10.1/x": _cand()})
    rec = _rec(doi="", work_identifiers=[])
    new, changed = backfill_record(rec, provider)
    assert not changed
    assert new.container_title == ""
    assert provider.asked == []        # 一次都没问 ⇒ 确实没走模糊匹配


def test_not_found_leaves_record_untouched():
    rec = _rec(doi="10.9/unknown")
    new, changed = backfill_record(rec, _FakeCrossref({}))
    assert not changed and new.container_title == ""


def test_provider_exception_does_not_propagate():
    """单条网络失败不该毁掉整批回填。"""
    rec = _rec()
    new, changed = backfill_record(rec, _FakeCrossref({}, raise_on=("10.1/x",)))
    assert not changed and new is rec


def test_only_doi_lookup_used():
    provider = _FakeCrossref({"10.1/x": _cand()})
    backfill_record(_rec(), provider)
    assert provider.asked == [("doi", "10.1/x")]


# ---------------- 整册 ----------------

def test_ledger_backfill_reports_accurately():
    ledger = ReferenceLedger([
        _rec(reference_id="r000000000001", doi="10.1/x"),
        _rec(reference_id="r000000000002", doi="10.9/missing"),
        _rec(reference_id="r000000000003", doi="", work_identifiers=[]),
    ])
    report = backfill_ledger(ledger, _FakeCrossref({"10.1/x": _cand()}))
    assert report.filled == ["r000000000001"]
    assert report.failed == ["r000000000002"]
    assert report.no_doi == ["r000000000003"]
    assert "1 条补上著录字段" in report.message
    assert "不猜测" in report.message


def test_complete_records_skipped():
    """已完整的不重复查 —— 省掉无谓的网络往返。"""
    complete = _rec(reference_id="r000000000009", container_title="Sensors",
                    volume="18", issue="5", pages="1351",
                    reference_type="journal-article")
    ledger = ReferenceLedger([complete])
    provider = _FakeCrossref({"10.1/x": _cand()})
    report = backfill_ledger(ledger, provider)
    assert report.already_complete == ["r000000000009"]
    assert provider.asked == []


def test_backfill_enables_full_rendering():
    """验收:回填前渲染退化，回填后是完整国标形态。"""
    style = get_style("gbt7714")
    ledger = ReferenceLedger([_rec()])
    before = ledger.all_records()[0]
    assert style.missing(before)                    # 回填前缺字段

    backfill_ledger(ledger, _FakeCrossref({"10.1/x": _cand()}))
    after = ledger.all_records()[0]
    assert not style.missing(after)
    text = style.render_text(after)
    assert "[J]" in text and "IEEE Access" in text and "8(3): 1-10" in text


def test_ledger_updated_in_place():
    ledger = ReferenceLedger([_rec()])
    backfill_ledger(ledger, _FakeCrossref({"10.1/x": _cand()}))
    assert ledger.get("r000000000001").container_title == "IEEE Access"


def test_empty_ledger_is_noop():
    report = backfill_ledger(ReferenceLedger([]), _FakeCrossref({}))
    assert report.filled == [] and "0 条" in report.message
