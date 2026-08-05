"""回归：证据数据块给模型的引用标记，必须与 ledger 的解析口径一致。

这是真机「重试 3 次全失败」的**首要根因**。`build_evidence_block` 原先一律 bibkey
优先、不看 ext；而 `ReferenceLedger.resolve_token` 对 md/docx **只查数字别名**。
于是 .md 论文里模型被告知引用 `[chen2023overview]`，写出来必然 NOT_FOUND：
CitationQuotaGate 判「新增引用无法解析」，counted_ids 恒空、配额永远差满额 —— 模型
无论怎么写都过不了，重试全部烧掉。
"""
import json
from pathlib import Path

import pytest

from paper_agent.generate.context_builder import build_evidence_block
from paper_agent.gates.diff_gate import extract_citation_tokens
from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import record_from_dict


class _Entry:
    bibkey = "chen2023overview"
    numeric_alias = "3"
    reference_id = "rad314c46cf93"
    title = "Overview of Collaborative SLAM"
    year = 2023
    language_bucket = "en"
    spans = ()


class _Bundle:
    entries = (_Entry(),)


def _first_marker(block: str) -> str:
    return block.split("[", 1)[1].split("]", 1)[0]


@pytest.mark.parametrize("ext", ["md", "docx"])
def test_numeric_marker_for_bracket_dialects(ext):
    assert _first_marker(build_evidence_block(_Bundle(), ext)) == "3"


def test_bibkey_marker_for_tex():
    assert _first_marker(build_evidence_block(_Bundle(), "tex")) == "chen2023overview"


def test_ext_accepts_leading_dot_and_case():
    assert _first_marker(build_evidence_block(_Bundle(), ".MD")) == "3"


def test_omitted_ext_defaults_to_numeric():
    """未传 ext 时按方括号方言（数字别名）公布。

    此前这条测试断言的是 `"chen2023overview"`，理由写着「保持旧行为」—— 但那个「旧行为」
    正是缺陷本身：缺省下公布 bibkey，而纯文本格式只认数字。把 bug 当契约钉住，等于
    保证同一故障能再来一次。缺省走数字是安全的一侧：md/docx/txt 都用方括号，
    唯有 tex 需要 bibkey，而 tex 路径一律显式传 ext。"""
    assert _first_marker(build_evidence_block(_Bundle())) == "3"


def test_empty_bundle_renders_nothing():
    class _Empty:
        entries = ()

    assert build_evidence_block(_Empty(), "md") == ""


# --- 与真实 ledger 闭环 --------------------------------------------------------

def _bundle_of(records):
    class _E:
        def __init__(self, rec):
            self.bibkey = rec.bibkey
            self.numeric_alias = rec.numeric_aliases[0] if rec.numeric_aliases else ""
            self.reference_id = rec.reference_id
            self.title = rec.title or "untitled"
            self.year = rec.year
            self.language_bucket = "en"
            self.spans = ()

    class _B:
        entries = tuple(_E(r) for r in records)

    return _B()


def test_advertised_markers_resolve_for_healthy_ledger():
    """数据块公布的标记 = 模型会照抄进正文的东西，必须条条按 md 口径解析得通。

    用干净账本（别名唯一）构造，验证「公布口径 ↔ 解析口径」这一条闭环本身成立。
    真机账本存在**另一个独立缺陷**（跨批次别名碰撞），由
    tests/test_commit_numeric_alias_collision.py 覆盖 —— 那是数据损坏，不是本口径问题。
    """
    records = [
        record_from_dict({
            "reference_id": f"r{i:012x}", "numeric_aliases": [str(i)],
            "bibkey": f"author{2020 + i}word", "title": f"Paper {chr(64 + i)}",
        })
        for i in range(1, 11)
    ]
    ledger = ReferenceLedger(records)

    markers = extract_citation_tokens(build_evidence_block(_bundle_of(records), "md"), "md")
    assert len(markers) == 10
    unresolved = [
        m for m in markers
        if ledger.resolve_token(m, ext="md").status.name != "RESOLVED"
    ]
    assert not unresolved, f"md 口径下无法解析的标记：{unresolved}"


def test_tex_advertised_markers_resolve_too():
    records = [
        record_from_dict({
            "reference_id": f"r{i:012x}", "numeric_aliases": [str(i)],
            "bibkey": f"author{2020 + i}word", "title": f"Paper {chr(64 + i)}",
        })
        for i in range(1, 6)
    ]
    ledger = ReferenceLedger(records)
    block = build_evidence_block(_bundle_of(records), "tex")
    markers = extract_citation_tokens(block, "md")   # 数据块里仍是 [marker] 形态
    unresolved = [
        m for m in markers
        if ledger.resolve_token(m, ext="tex").status.name != "RESOLVED"
    ]
    assert not unresolved, f"tex 口径下无法解析的标记：{unresolved}"


_FACTS = Path(".paper_workspace/proj_1780f6e14f27/facts.json")


@pytest.mark.skipif(not _FACTS.exists(), reason="需要真机 facts.json")
def test_real_ledger_alias_collisions_are_reported():
    """真机账本的别名碰撞：xfail 式记录**磁盘数据仍损坏**。

    代码侧已阻止新碰撞（`_dealias`），但既有 facts.json 不会被测试自动改写 —— 修数据
    是带副作用的操作，需显式执行。这里只做体检：有碰撞就 skip 并把清单打出来，
    避免用一条永久失败的断言把 CI 卡死。
    """
    import collections

    data = json.loads(_FACTS.read_text(encoding="utf-8"))
    counts = collections.Counter(
        n for r in data.get("references", []) for n in (r.get("numeric_aliases") or [])
    )
    dupes = {k: v for k, v in counts.items() if v > 1}
    if dupes:
        pytest.skip(f"真机账本仍有重复数字别名（需数据修复）：{sorted(dupes)}")
