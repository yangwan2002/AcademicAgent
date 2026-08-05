"""md 多引用 `[12, 21]` 的解析（真机缺陷回归）。

真机现象:改写引言后，系统逐个弹窗问「出现新数字 12 / 6 / 21，是真实来源的吗」——
12 和 21 其实是引用编号。根因是 `_BRACKET_REF` 不允许方括号内有空白:
`[12, 21]` 整体匹配不上 ⇒ ① `_data_numbers` 剔不掉它，两个编号被当成编造数字，
② `extract_citation_tokens` 一个 token 都抽不到，这条引用不算引用、配额白算。

tex 侧无此问题（`_CITE` 显式 split(",")），所以这是 **md 方言少做了拆分**，
不是「md 特有的怪毛病」——同一套口径两个方言实现不一致，就会有一侧漏。
"""
from __future__ import annotations

import pytest

from paper_agent.gates.diff_gate import data_numbers, extract_citation_tokens


# ---------------- 新数字误判 ----------------

@pytest.mark.parametrize("text", [
    "协同感知[12, 21]。",          # 真机原样：逗号 + 空格
    "协同感知[12,21]。",           # 无空格
    "协同感知[12 , 21]。",         # 逗号两侧都有空格
    "协同感知[12,  21]。",         # 多空格
    "见[1, 2, 3, 4]。",           # 四条
    "见 [6]；",                   # 单条（原本就正常，防回归）
])
def test_citation_numbers_not_treated_as_data(text):
    """引用编号绝不能进 data_numbers —— 否则用户被逐个弹窗问确认。"""
    assert data_numbers(text) == set(), f"{text} 里的引用编号被误判成数据数字"


def test_real_numbers_still_detected():
    """对照实验:真实数据数字必须照旧抓到，修复不能把检测能力一起削掉。"""
    assert data_numbers("准确率 0.87，见[12, 21]") == {"0.87"}
    assert data_numbers("悬停在 12 m 高度，见[6]") == {"12"}
    assert data_numbers("提升 3.2%") == {"3.2"}


# ---------------- 引用 token 抽取 ----------------

@pytest.mark.parametrize("text,expected", [
    ("协同感知[12, 21]。", ("12", "21")),
    ("协同感知[12,21]。", ("12", "21")),
    ("见[1, 2, 3]。", ("1", "2", "3")),
    ("见 [6]；", ("6",)),
    ("[r7a338d84a937, rb245a256eaa0]", ("r7a338d84a937", "rb245a256eaa0")),
])
def test_multi_citation_split_into_tokens(text, expected):
    """`[12, 21]` 是**两条**引用。不拆会让配额只算一条，且 token 恒解析不到。"""
    assert extract_citation_tokens(text, "md") == expected


def test_md_matches_tex_semantics():
    """同一批引用，md 与 tex 必须抽出同一组 token —— 两方言口径一致。"""
    md = extract_citation_tokens("见[a, b, c]", "md")
    tex = extract_citation_tokens(r"见\cite{a, b, c}", "tex")
    assert md == tex == ("a", "b", "c")


# ---------------- 不该被吞的东西 ----------------

@pytest.mark.parametrize("text", [
    "任务 [ ] 未完成",             # 空框：首字符须字母/数字，本就不匹配
    "- [x] 已完成的待办",          # markdown 任务列表（真正的任务框语法）
    "* [ ] 未完成的待办",
    "  - [X] 缩进的待办",
    "链接[显示文字](http://example.com)",   # md 链接
])
def test_non_citations_not_captured(text):
    """放宽正则不能把任务列表、md 链接吞成引用。

    任务框只在**列表项开头**剔除。散在正文里的 `[x]` 不剔 —— 那位置上它与一个
    单字母 bibkey 无法区分，当作 token 交给账本判「解析不到」比静默丢弃更诚实。
    """
    assert extract_citation_tokens(text, "md") == ()


def test_bracket_with_spaces_only_is_not_citation():
    assert extract_citation_tokens("[   ]", "md") == ()
