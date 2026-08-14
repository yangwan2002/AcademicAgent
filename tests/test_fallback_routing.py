"""只读提问的路由判定（P-4 第三步）。

**这个文件钉的是 P-4 的立项理由**：只读提问会命中写意图的关键词，被当成写请求执行。
其中「这段的参考文献够吗」→ `fix_bibliography` 曾是唯一会**静默写 working copy** 的
（`tools/bibliography.py` 的 `write_working` 前面没有确认）。该意图与 `_BIB_KW` 现已
删除（backlog B-2，能力由 commit 时的 `apply_bibliography` 承担），所以那条现在落
UNCLEAR；本文件保留它是为了钉住「不管开关开没开，都不再有写路径可劫」。

两个方向都要钉，因为判定的代价是不对称的：
- **误判**（写请求被判成提问）→ 用户的写请求被答成一段话，什么也没改。
- **漏判**（提问被判成写请求）→ 回到现状，用户看到访谈可以退出。

所以规则刻意偏保守（疑问形态 **且** 无祈使写作标记），下面 `test_write_requests_*`
那组比劫持那组更重要 —— 它保证偏保守这件事不会在以后被"顺手放宽"掉。

没有 shadow 档：判定是纯规则，行为由代码完全决定、可被这里穷举，不需要靠真机流量
量误判率；项目也尚未上线，没有需要保守观察的线上流量。
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.router import Intent, Router, looks_like_question
from paper_agent.session.section_index import SectionIndex

_DOC = "# 1 引言\n\n内容。\n\n# 2 方法\n\n内容。\n"


def _router(fallback_enabled: bool, ext: str = "md") -> Router:
    return Router(SectionIndex(_DOC, ext), fallback_enabled=fallback_enabled)


#: spec 里实测记录的 5 条劫持。每条都是只读提问，却命中了写意图关键词。
HIJACKED = [
    ("这篇论文缺哪些章节", Intent.ADD_SECTION_BATCH),
    ("帮我分析下还缺什么内容", Intent.ADD_SECTION_BATCH),
    # 这条原来是 FIX_BIBLIOGRAPHY（唯一会静默写文档的劫持）。该意图与 `_BIB_KW`
    # 已删除（B-2），所以关闭兜底时它现在落 UNCLEAR —— 不再有写路径可劫。
    ("这段的参考文献够吗", Intent.UNCLEAR),
    ("引言的语言风格是怎样的", Intent.POLISH),
    ("为什么方法章读起来别扭", Intent.POLISH),
]

#: 真正的写请求。**一条都不能被判成 fallback。**
WRITE_REQUESTS = [
    "缺哪些章节，帮我补齐",      # 疑问词 + 祈使 → 写
    "帮我润色引言",
    "把 3.3 节补写完整",
    "改写方法章",
    "新增一章讨论",
    "转成 docx",
    "参考文献格式帮我修一下",
]


@pytest.mark.parametrize("msg,legacy", HIJACKED)
def test_readonly_questions_no_longer_hijacked(msg, legacy):
    """开关打开后，这 5 条判 fallback；关闭时仍是原来的（错误）行为。"""
    assert _router(fallback_enabled=True).route(msg).intent is Intent.FALLBACK
    assert _router(fallback_enabled=False).route(msg).intent is legacy


@pytest.mark.parametrize("msg", WRITE_REQUESTS)
def test_write_requests_never_become_fallback(msg):
    """祈使写作标记优先于疑问形态 —— 漏判可接受，误判不可。"""
    assert _router(fallback_enabled=True).route(msg).intent is not Intent.FALLBACK
    assert looks_like_question(msg) is False


def test_bibliography_question_does_not_reach_write_path():
    """「参考文献」不再是路由关键词（`_BIB_KW` 随 B-2 删除），两层保护叠起来。

    删除前：`_BIB_KW` 是 `route()` 第一个检查、无条件返回 `FIX_BIBLIOGRAPHY`，
    而那条路径会**静默写 working copy**。当时靠兜底判定前置来挡疑问句那一半。
    删除后：关键词表本身没了，疑问句和祈使句都不会再进写路径。

    这个测试因此从「验证前置顺序」变成「验证没有任何『参考文献』说法能到写路径」，
    覆盖面更宽 —— 包括兜底判定管不到的祈使句。
    """
    for msg in ("这段的参考文献够吗", "参考文献格式对吗"):
        assert _router(fallback_enabled=True).route(msg).intent is Intent.FALLBACK

    # 祈使句：兜底判定不接管（有「修一下」「帮我」这类写作标记），但也不再被劫为
    # fix_bibliography。落 UNCLEAR 会打印功能列表 —— 不改文档，是安全的一侧。
    for msg in ("参考文献格式帮我修一下", "帮我在相关工作里多引几篇参考文献",
                "bibliography 缺条目帮我补"):
        for enabled in (True, False):
            got = _router(fallback_enabled=enabled).route(msg).intent
            assert got is Intent.UNCLEAR, f"{msg} -> {got}"


def test_unclear_questions_become_fallback():
    """原本落 UNCLEAR 的提问（只打印功能列表）现在能被回答。"""
    for msg in ("哪几章还没写", "这篇论文有哪些章节", "引言写得怎么样？"):
        assert _router(fallback_enabled=True).route(msg).intent is Intent.FALLBACK


def test_off_is_byte_identical():
    """`fallback_enabled=False` 时判定分支不存在，输出与接入前一致。

    现有全量测试全部走这条路径，这个断言是"零回归"的可执行形态。
    """
    msgs = [m for m, _ in HIJACKED] + WRITE_REQUESTS + ["哪几章还没写", "转成 latex"]
    off = _router(fallback_enabled=False)
    for msg in msgs:
        assert off.route(msg).intent is not Intent.FALLBACK


def test_router_param_default_is_off():
    """`Router` 形参默认关 —— 库层不猜调用者意图，接管必须显式传入。

    这是 API 契约，不是应用默认档：`Config.agent_loop_mode` 已是 `on`
    （见 `test_fallback_chat_wiring.test_app_default_is_on`）。分开的好处是
    "默认档"只有一个归属地（config），改档不必改库层，也不会让本文件里
    `fallback_enabled=False` 的那些用例随默认值漂移。
    """
    assert Router(SectionIndex(_DOC, "md")).fallback_enabled is False
    assert Router(SectionIndex(_DOC, "md")).route("哪几章还没写").intent is Intent.UNCLEAR


class TestLooksLikeQuestion:
    """判定函数本身的边界。"""

    @pytest.mark.parametrize("msg", [
        "有几个公式", "引言多少字", "哪一章最长", "是不是缺了摘要",
        "有没有哪条文献没被引用", "为何召回率这么低", "这样写行吗？",
        "How many sections?",
    ])
    def test_questions(self, msg):
        assert looks_like_question(msg) is True

    @pytest.mark.parametrize("msg", [
        "润色引言", "补齐缺失章节", "转成 word", "把结论改写得更有力",
    ])
    def test_not_questions(self, msg):
        assert looks_like_question(msg) is False

    def test_empty_and_none_are_safe(self):
        assert looks_like_question("") is False
        assert looks_like_question(None) is False   # type: ignore[arg-type]

    def test_bare_statement_is_not_question(self):
        """没有疑问标记也没有祈使标记 → 不判 fallback，落回原路由。

        保守方向：拿不准就别接管。
        """
        assert looks_like_question("引言这一章") is False

    def test_imperative_wins_over_question_mark(self):
        """「帮我把引言润色一下，可以吗？」有问号，但明确要求改动。"""
        assert looks_like_question("帮我把引言润色一下，可以吗？") is False

    def test_help_me_analyze_is_readonly(self):
        """「帮我」不是祈使写作标记 —— 分析不改文档。"""
        assert looks_like_question("帮我分析下还缺什么内容") is True
