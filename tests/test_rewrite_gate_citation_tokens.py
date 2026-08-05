"""真机回归：带引用配额的 rewrite「重试 3 次全失败」的三个根因。

真机复现输入（引言章、四个 ### 子标题、要求引用 10 篇英 8 中 2）在修复前必然失败，
且失败原因还打印不出来。三个缺陷各自独立、任一未修都会让整条链路不可用：

1. `_BRACKET_REF` 要求 token 以数字开头，而 ledger 的 reference_id 是 ``"r"+sha1[:12]``
   ⇒ md 正文里的真实引用**一个都抽不到**。连带两处误判：
   - CitationQuotaGate 的 counted_ids 恒空 → total 未达标恒成立；
   - id 里的数字被 `_data_numbers` 当成「来源不明的新数字」。
2. RewriteGate 段落上限 `po+5` 假设等量改写，与「扩写成正文 + 引用 10 篇」直接矛盾。
3. `chat.py` 只打印 message 首行，违规详情（第 2 行起）被丢掉。
"""
from paper_agent.gates.diff_gate import (
    RewriteGate,
    _count_paragraphs,
    _data_numbers,
    extract_citation_tokens,
)


class _Facts:
    def __init__(self, ids=()):
        self._ids = set(ids)

    def ref_ids(self):
        return self._ids

    def bibkeys(self):
        return set()

    def number_values(self):
        return set()


# --- 1. 引用 token 抽取 --------------------------------------------------------

def test_bracket_ref_extracts_ledger_style_reference_ids():
    """reference_id 是 `r`+hex，必须能被抽到（修复前返回空元组）。"""
    text = "互补 [rad314c46cf93]，另见 [r476150f8c87e]。"
    assert extract_citation_tokens(text, "md") == ("rad314c46cf93", "r476150f8c87e")


def test_bracket_ref_still_extracts_numeric_ids():
    assert extract_citation_tokens("见 [12] 与 [3]。", "md") == ("12", "3")


def test_bracket_ref_ignores_markdown_links_and_checkboxes():
    """`[文字](url)` 不是引用；`[ ]` 也不是。"""
    assert extract_citation_tokens("见 [文档](http://x) 和 [ ] 项。", "md") == ()


def test_tex_citation_extraction_unchanged():
    assert extract_citation_tokens(r"见 \cite{key1,key2}。", "tex") == ("key1", "key2")


def test_reference_id_digits_are_not_counted_as_data_numbers():
    """id 内的数字不得被当作「编造的新数字」。"""
    assert _data_numbers("互补 [rad314c46cf93]。") == set()


# --- 2. 扩写请求的段落上限 ----------------------------------------------------

# 仿真机结构：四个 ### 子标题，各带一段概述 ⇒ 8 段，Gate 默认窗口 [6, 13]。
# 子标题里的编号是原文数字（在 allowed 集合内），正文不再额外引入数字。
_ORIG = "\n\n".join(f"### （{i}）小节标题\n\n本小节的概述内容。" for i in range(1, 5))


def _expanded(n_para: int, ids) -> str:
    """构造 n 段扩写正文。正文里**不写任何数字** —— 否则会触发「编造新数字」这条
    规则，测的就不是段落上限了。"""
    ids = list(ids)
    blocks = []
    for i in range(n_para):
        cite = f" [{ids[i % len(ids)]}]" if ids else ""
        blocks.append(f"扩写后的论述段落，铺开说明其中一个要点{cite}。")
    return "\n\n".join(blocks)


def test_expand_request_rejected_without_budget():
    """基线：默认窗口下扩写必被段落数拦死（这就是真机的第 2 个根因）。"""
    ids = [f"r{i:012x}" for i in range(10)]
    cand = _expanded(16, ids)
    r = RewriteGate("md", _Facts(ids), 2).check(_ORIG, cand, set())
    assert not r.passed
    assert any("段落数" in v for v in r.violations)


def test_expand_budget_allows_expansion():
    ids = [f"r{i:012x}" for i in range(10)]
    cand = _expanded(16, ids)
    budget = _count_paragraphs(_ORIG) + 10
    r = RewriteGate("md", _Facts(ids), 2, expand_budget=budget).check(_ORIG, cand, set())
    assert r.passed, r.violations


def test_expand_budget_does_not_relax_lower_bound():
    """放宽只作用于上限：「越改越少」仍要拦（防把章节删空）。"""
    r = RewriteGate("md", _Facts(), 2, expand_budget=50).check(_ORIG, "只剩一段。", set())
    assert not r.passed
    assert any("段落数" in v for v in r.violations)


def test_known_ref_check_still_catches_unknown_citation():
    """放宽段落数不能顺带放过未核验引用。"""
    cand = _expanded(9, ["rdeadbeef0000"])
    r = RewriteGate("md", _Facts({"r000000000000"}), 2, expand_budget=99).check(
        _ORIG, cand, set())
    assert not r.passed
    assert any("未核验" in v for v in r.violations)


# --- 3. _expand_budget 的推导 -------------------------------------------------

def test_expand_budget_derives_from_direction_and_quota():
    from paper_agent.intents.rewrite import _expand_budget
    from paper_agent.literature.quota import (
        CountConstraint, CountDimension, CountOperator, QuotaKind, QuotaScope, QuotaSpec,
    )

    class _Plan:
        direction = "把每个子标题下的概述扩写成正式引言正文"
        details = ""

    quota = QuotaSpec(
        kind=QuotaKind.CITATION, scope=QuotaScope.ACTUALLY_CITED,
        count_constraints=(CountConstraint(
            dimension=CountDimension.TOTAL, operator=CountOperator.MIN, value=10),),
    )

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.quota = quota
    assert _expand_budget(_Plan(), ctx, _ORIG) >= 10


def test_expand_budget_zero_for_plain_rewrite():
    """没有扩写意图、没有配额 ⇒ 不放宽（保持原有严格度）。"""
    from paper_agent.intents.rewrite import _expand_budget

    class _Plan:
        direction = "把被动语态改成主动语态"
        details = ""

    assert _expand_budget(_Plan(), None, _ORIG) == 0
