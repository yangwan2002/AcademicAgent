"""引用方言的**契约闭环**测试:公布出去的标记必须解析得回来。

这类测试专抓**跨模块口径失配** —— 1:1 的单元测试结构上抓不到它:每个模块自己都"对",
合起来却不闭合。真机故障正是如此(1315 个单元测试全绿,第一次真跑必然失败):

    context_builder 公布 [chen2023overview]   ← 自己看没问题
    ledger(md) 只认 [3]                       ← 自己看也没问题
    两者从未被同一个测试连起来               ← 故障就住在这条缝里

所以下面每个测试都同时跨越 ≥2 个模块,断言的是**它们之间的关系**,不是单个模块的行为。
"""
import pytest

from paper_agent.gates.diff_gate import extract_citation_tokens
from paper_agent.generate.context_builder import build_evidence_block
from paper_agent.literature.dialect import (
    citable_tokens,
    is_tex,
    marker_for,
    normalize_ext,
    resolution_order,
    style_for,
)
from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import record_from_dict

BRACKET_EXTS = ["md", "markdown", "docx"]
ALL_EXTS = BRACKET_EXTS + ["tex"]


def _rec(i: int):
    return record_from_dict({
        "reference_id": f"r{i:012x}",
        "numeric_aliases": [str(i)],
        "bibkey": f"author{2000 + i}word",
        "title": f"Paper {i}",
        "year": 2000 + i,
    })


class _Entry:
    """EvidenceBundle 条目的最小形状(bundle 构造代价高,此处只测标记口径)。"""

    def __init__(self, rec):
        self.bibkey = rec.bibkey
        self.numeric_alias = rec.numeric_aliases[0] if rec.numeric_aliases else ""
        self.reference_id = rec.reference_id
        self.title = rec.title
        self.year = rec.year
        self.language_bucket = "en"
        self.spans = ()


def _bundle(records):
    class _B:
        entries = tuple(_Entry(r) for r in records)

    return _B()


# ---------------------------------------------------------------- ext 归一化

@pytest.mark.parametrize("raw,expected", [
    (".MD", "md"), ("md", "md"), (".tex", "tex"), ("TEX", "tex"),
    ("", ""), (None, ""),
])
def test_normalize_ext(raw, expected):
    assert normalize_ext(raw) == expected


@pytest.mark.parametrize("raw", [".tex", "TEX", ".TeX", "tex", "latex"])
def test_is_tex_tolerates_dot_and_case(raw):
    """原先各调用点裸比 `ext == "tex"`,`".tex"` 会静默走错分支。"""
    assert is_tex(raw)


@pytest.mark.parametrize("raw", BRACKET_EXTS + ["", "txt"])
def test_is_not_tex(raw):
    assert not is_tex(raw)


# ------------------------------------------------- 核心闭环:公布 ↔ 解析

@pytest.mark.parametrize("ext", ALL_EXTS)
def test_marker_for_resolves_back_to_same_record(ext):
    """**本文件的核心断言**:`marker_for` 公布的标记,必须被 `resolve_token` 解析回原记录。

    这一条闭环若破,模型照抄公布的标记就会被判「无法解析」,配额永远差满额。
    """
    records = [_rec(i) for i in range(1, 8)]
    ledger = ReferenceLedger(records)
    for rec in records:
        marker = marker_for(rec, ext)
        assert marker, f"ext={ext} 下未能给出标记"
        res = ledger.resolve_token(marker, ext=ext)
        assert res.status.name == "RESOLVED", f"ext={ext} marker={marker} → {res.status}"
        assert res.reference_id == rec.reference_id


@pytest.mark.parametrize("ext", ALL_EXTS)
def test_evidence_block_markers_all_resolve(ext):
    """prompt 数据块 → 解析器 的闭环(跨 context_builder / dialect / ledger 三个模块)。"""
    records = [_rec(i) for i in range(1, 11)]
    ledger = ReferenceLedger(records)
    block = build_evidence_block(_bundle(records), ext)

    # 数据块里标记恒以 [marker] 呈现,故按方括号口径回抽
    markers = extract_citation_tokens(block, "md")
    assert len(markers) == len(records), f"ext={ext} 公布标记数不符:{markers}"
    for m in markers:
        assert ledger.resolve_token(m, ext=ext).status.name == "RESOLVED", \
            f"ext={ext} 公布了解析不了的标记 [{m}]"


def test_md_and_tex_advertise_different_markers():
    """回归:md 与 tex 必须公布**不同**形态 —— 曾经二者都吐 bibkey。"""
    rec = _rec(3)
    assert marker_for(rec, "md") == "3"
    assert marker_for(rec, "tex") == "author2003word"


def test_marker_never_falls_back_across_dialects():
    """缺失时返回空,**不跨方言回退** —— 回退等于公布一个解析不了的假标记。"""
    no_alias = record_from_dict({
        "reference_id": "rdeadbeef0001", "bibkey": "only2020key", "title": "T",
    })
    assert marker_for(no_alias, "md") == ""          # 不退回 bibkey
    assert marker_for(no_alias, "tex") == "only2020key"

    no_bibkey = record_from_dict({
        "reference_id": "rdeadbeef0002", "numeric_aliases": ["7"], "title": "T",
    })
    assert marker_for(no_bibkey, "tex") == ""        # 不退回数字
    assert marker_for(no_bibkey, "md") == "7"


def test_entry_without_resolvable_marker_is_omitted_from_block():
    """公布不了就整条略去,而不是塞个假标记进 prompt。"""
    records = [_rec(1), record_from_dict({
        "reference_id": "rnoalias00001", "bibkey": "k2020a", "title": "No Alias",
    })]
    block = build_evidence_block(_bundle(records), "md")
    assert "No Alias" not in block
    assert "Paper 1" in block


# ------------------------------------------------- 解析顺序 / Gate 口径同源

def test_resolution_order_matches_style():
    assert resolution_order("tex") == ("bibkey", "bibkey_alias")
    for ext in BRACKET_EXTS:
        assert resolution_order(ext) == ("numeric",)


@pytest.mark.parametrize("ext", ALL_EXTS)
def test_style_name_agrees_with_marker_source(ext):
    rec = _rec(5)
    style = style_for(ext)
    if style.name == "bibkey":
        assert marker_for(rec, ext) == rec.bibkey
    else:
        assert marker_for(rec, ext) == rec.numeric_aliases[0]


def test_style_render_roundtrips_through_extractor():
    """`style.render` 产出的文本,必须被 `extract_citation_tokens` 按同一 ext 抽回原 token。"""
    for ext in ALL_EXTS:
        rendered = style_for(ext).render("tok1")
        assert extract_citation_tokens(rendered, ext) == ("tok1",)


class _Facts:
    def __init__(self, bibkeys, ref_ids):
        self._b, self._r = set(bibkeys), set(ref_ids)

    def bibkeys(self):
        return self._b

    def ref_ids(self):
        return self._r


@pytest.mark.parametrize("ext,expected", [
    ("tex", {"k2020a"}), ("md", {"1"}), ("docx", {"1"}),
])
def test_citable_tokens_follows_dialect(ext, expected):
    """Gate 的「已核验集合」与公布口径同源 —— 曾两处各写一遍 `if ext == "tex"`。"""
    assert citable_tokens(_Facts({"k2020a"}, {"1"}), ext) == expected


def test_gate_accepts_exactly_what_was_advertised():
    """端到端口径闭环:公布 → 模型照抄 → Gate 判定,不得出现「未核验」。

    跨 dialect / context_builder / diff_gate / facts 四处。
    """
    from paper_agent.gates.diff_gate import _cite_tokens

    for ext in ALL_EXTS:
        records = [_rec(i) for i in range(1, 4)]
        markers = [marker_for(r, ext) for r in records]
        facts = _Facts(
            {r.bibkey for r in records},
            {r.numeric_aliases[0] for r in records},
        )
        # 模型照抄公布的标记写正文
        body = "结论如下" + "".join(style_for(ext).render(m) for m in markers) + "。"
        unknown = _cite_tokens(body, ext) - citable_tokens(facts, ext)
        assert not unknown, f"ext={ext} 照抄公布标记却被判未核验:{unknown}"
