"""著录字段贯通链:provider → 候选 → 账本记录 → 渲染。

真机现象:账本 25 条，`container_title`/`volume`/`pages` 覆盖率 **0/25**，
完整格式（GB/T 7714 的「刊名, 年, 卷(期): 页」）永远渲染不出来。

根因是一条**断链**，且注释写反了因果:crossref.py 原注释称"这些字段在候选→Record
转换时映射"，但转换函数只能读候选**已有**的字段 —— provider 侧不取，转换处就无从
映射。链上任一环缺失，末端就永远是退化形态。所以这组测试**逐环**钉,而不只测末端:
只测末端的话，某一环退化时另一环的兜底可能掩盖它。
"""
from __future__ import annotations

import pytest

from paper_agent.literature.citation_styles import get_style
from paper_agent.literature.migration import candidate_to_record
from paper_agent.providers.retrieval.crossref import CrossrefVerifyProvider
from paper_agent.providers.retrieval.openalex import OpenAlexRetrievalProvider
from paper_agent.workspace.models import ReferenceEntry

_OA_WORK = {
    "id": "https://openalex.org/W123",
    "display_name": "Cooperative Monocular-Based SLAM",
    "publication_year": 2018,
    "doi": "https://doi.org/10.3390/s18051351",
    "type": "journal-article",
    "authorships": [
        {"author": {"display_name": "Juan-Carlos Trujillo"}},
        {"author": {"display_name": "Rodrigo Munguía"}},
    ],
    "primary_location": {"source": {
        "display_name": "Sensors", "issn_l": "1424-8220",
        "host_organization_name": "MDPI",
    }},
    "biblio": {"volume": "18", "issue": "5", "first_page": "1351", "last_page": ""},
}

_CR_MSG = {
    "DOI": "10.1109/access.2020.3030190",
    "title": ["Collaborative Multi-Robot Search and Rescue"],
    "author": [{"given": "Jorge", "family": "Peña Queralta"}],
    "issued": {"date-parts": [[2020]]},
    "type": "journal-article",
    "container-title": ["IEEE Access"],
    "volume": "8", "page": "191617-191643",
    "publisher": "IEEE", "ISSN": ["2169-3536"],
    "URL": "https://doi.org/10.1109/access.2020.3030190",
}


# ---------------- 第 1 环:provider 抽字段 ----------------

def test_openalex_extracts_venue_fields():
    """期刊名在 primary_location.source，卷期页在 biblio —— 数据本就在响应里。"""
    e = OpenAlexRetrievalProvider._to_entry(_OA_WORK)
    assert e.container_title == "Sensors"
    assert e.volume == "18" and e.issue == "5"
    assert e.pages == "1351"           # 只有 first_page ⇒ 不拼出 "1351-"
    assert e.reference_type == "journal-article"
    assert e.publisher == "MDPI"


def test_openalex_page_range_joined():
    work = dict(_OA_WORK, biblio={"first_page": "29", "last_page": "49"})
    assert OpenAlexRetrievalProvider._to_entry(work).pages == "29-49"


def test_openalex_missing_biblio_is_empty_not_crash():
    """字段缺失必须是空串,不能抛 —— 检索链断了比缺字段严重得多。"""
    e = OpenAlexRetrievalProvider._to_entry(
        {"display_name": "T", "publication_year": 2020})
    assert e.container_title == "" and e.volume == "" and e.pages == ""


def test_crossref_extracts_venue_fields():
    c = CrossrefVerifyProvider._to_candidate(_CR_MSG)
    assert c.container_title == "IEEE Access"
    assert c.volume == "8" and c.pages == "191617-191643"
    assert c.publisher == "IEEE"


def test_crossref_unescapes_html_entities():
    """Crossref 刊名带 HTML 实体 ⇒ 不解码就把错刊名印进参考文献表。"""
    msg = dict(_CR_MSG,
               container_title=None,
               **{"container-title": ["IEEE Robotics &amp; Automation Magazine"]})
    c = CrossrefVerifyProvider._to_candidate(msg)
    assert c.container_title == "IEEE Robotics & Automation Magazine"
    assert "&amp;" not in c.container_title


# ---------------- 第 2 环:候选 → 记录 ----------------

def test_candidate_to_record_carries_venue_fields():
    """这一环是原先的断点:provider 取到了，但转换不带 ⇒ 存不进账本。"""
    c = OpenAlexRetrievalProvider._to_entry(_OA_WORK)
    r = candidate_to_record(c)
    assert r.container_title == "Sensors"
    assert r.volume == "18" and r.issue == "5" and r.pages == "1351"
    assert r.reference_type == "journal-article"


def test_candidate_without_new_fields_still_converts():
    """不带这些字段的旧候选对象仍可转换（getattr 兜底），不破坏既有构造路径。"""

    class _Old:
        title = "Legacy"
        authors = ["A"]
        year = 2019
        source = "mock"

    r = candidate_to_record(_Old())
    assert r.title == "Legacy" and r.container_title == ""


def test_plain_reference_entry_defaults_are_empty():
    e = ReferenceEntry(id="x", title="T", authors=[], year=2020, source_id="s")
    assert e.container_title == "" and e.volume == "" and e.reference_type == ""


# ---------------- 第 3 环:渲染完整格式 ----------------

def test_full_gbt_rendering_end_to_end():
    """全链贯通的验收:OpenAlex 响应 → 完整国标条目。"""
    r = candidate_to_record(OpenAlexRetrievalProvider._to_entry(_OA_WORK))
    text = get_style("gbt7714").render_text(r)
    assert "[J]" in text
    assert "Sensors" in text
    assert "2018" in text
    assert "18(5)" in text
    assert "1351" in text
    assert not get_style("gbt7714").missing(r)


def test_crossref_full_rendering_end_to_end():
    r = candidate_to_record(CrossrefVerifyProvider._to_candidate(_CR_MSG))
    text = get_style("gbt7714").render_text(r)
    assert "IEEE Access" in text and "191617-191643" in text
    assert "8" in text


def test_no_volume_still_renders_without_empty_skeleton():
    """无卷号时不能留 `, (): ` 空骨架。"""
    work = dict(_OA_WORK, biblio={"first_page": "73"})
    r = candidate_to_record(OpenAlexRetrievalProvider._to_entry(work))
    text = get_style("gbt7714").render_text(r)
    assert "()" not in text
    assert ", :" not in text
    assert "Sensors" in text


@pytest.mark.parametrize("field_name", [
    "reference_type", "container_title", "volume", "issue", "pages",
    "article_number", "publisher", "conference_name", "url",
])
def test_every_declared_field_survives_conversion(field_name):
    """逐字段钉:任一字段在转换中掉队，这里就红。

    参数化是刻意的 —— 将来加第 10 个字段时，忘了在转换里带上就会被这条抓住。
    """
    c = ReferenceEntry(
        id="x", title="T", authors=["A"], year=2020, source_id="10.1/x",
        **{field_name: "probe-value"},
    )
    assert getattr(candidate_to_record(c), field_name) == "probe-value"
