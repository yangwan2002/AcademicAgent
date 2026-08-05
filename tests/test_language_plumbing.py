"""源语言字段贯通:ReferenceEntry.language → record.language_hint → 语言判定（dogfood 发现）。

此前 OpenAlex 的 `language` 被丢弃、ReferenceEntry 无该字段、candidate_to_record 不读它,
导致真实检索文献一律判 `unknown`、不计任何语言配额（en/zh 配额恒不可满足）。
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.literature.language import DEFAULT_THRESHOLDS, classify_record_language
from paper_agent.literature.migration import candidate_to_record
from paper_agent.workspace.models import ReferenceEntry


def _entry(title, language, *, source="openalex", doi="10.1/x"):
    return ReferenceEntry(
        id="c:1", title=title, authors=["A"], year=2022, source_id=doi, source=source,
        abstract="", language=language, work_identifiers={"doi": doi})


def test_reference_entry_has_language_field():
    e = ReferenceEntry(id="x", title="t", authors=[], year=None, source_id="s")
    assert e.language == ""                          # 默认空,向后兼容


def test_language_flows_into_record_hint():
    rec = candidate_to_record(_entry("Deep Learning for Code", "en"))
    assert rec.language_hint == "en"


def test_english_source_classifies_as_en_bucket():
    """拉丁标题 + 源 language=en → en 桶 high（此前无源信号 → unknown,不计配额）。"""
    rec = candidate_to_record(_entry("Deep Learning for Code Generation", "en"))
    v = classify_record_language(rec, DEFAULT_THRESHOLDS)
    assert v.quota_bucket == "en" and v.confidence == "high"


def test_missing_language_falls_back_to_unknown_for_latin():
    """源未给 language 的拉丁标题仍判 unknown（保守,不硬判 en）——回归对照。"""
    rec = candidate_to_record(_entry("Deep Learning for Code Generation", ""))
    v = classify_record_language(rec, DEFAULT_THRESHOLDS)
    assert v.quota_bucket == "unknown"


def test_chinese_title_classifies_zh_regardless_of_hint():
    """中文标题:字符是硬证据 → zh（即便 hint 缺失或冲突）。"""
    rec = candidate_to_record(_entry("深度学习在代码生成中的应用研究", ""))
    v = classify_record_language(rec, DEFAULT_THRESHOLDS)
    assert v.quota_bucket == "zh"


def test_arxiv_search_sets_english_language():
    """arXiv 题录政策为英文 → search 结果带 language=en,贯通后计入 en 配额。"""
    from paper_agent.providers.retrieval.api import ArxivRetrievalProvider

    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><id>http://arxiv.org/abs/1706.03762v5</id>'
        '<title>Attention Is All You Need</title>'
        '<published>2017-06-12T00:00:00Z</published>'
        '<summary>We propose the Transformer.</summary>'
        '<author><name>Vaswani</name></author></entry></feed>'
    )
    entries = ArxivRetrievalProvider._parse_atom(atom)
    assert entries and entries[0].language == "en"
    rec = candidate_to_record(entries[0])
    assert classify_record_language(rec, DEFAULT_THRESHOLDS).quota_bucket == "en"


def test_openalex_maps_language_field():
    """OpenAlex `_to_entry` 读 work.language（此前被丢弃）。"""
    from paper_agent.providers.retrieval.openalex import OpenAlexRetrievalProvider

    work = {
        "id": "https://openalex.org/W123", "display_name": "A Study",
        "publication_year": 2021, "language": "zh",
        "authorships": [], "abstract_inverted_index": None,
    }
    e = OpenAlexRetrievalProvider._to_entry(work)
    assert e.language == "zh"
