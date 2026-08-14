"""Router（B1，Week 2）。

规则优先、LLM 兜底（暂不接 LLM 兜底）。识别 polish 与 convert_format；
其余归 UNCLEAR。同时尝试抽出目标章节 / 目标格式 / 排版预设。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from paper_agent.session.section_index import SectionIndex


class Intent(str, Enum):
    POLISH = "polish"
    REWRITE = "rewrite"
    ADD_SECTION = "add_section"
    ADD_SECTION_BATCH = "add_section_batch"
    CONVERT_FORMAT = "convert_format"
    FORMAT_DOCX = "format_docx"
    #: 兜底：写意图都不认领的请求，交给 ask_loop（ReAct 循环）回答。
    #:
    #: 叫 FALLBACK 而不是 ASK，因为它的定位是**架构位置**（兜底）而不是表层形态
    #: （疑问句）—— 它实际接住两类：被写意图关键词劫持的只读提问，以及原本落
    #: UNCLEAR 的请求。「帮我分析下还缺什么内容」不是疑问句，但归这里。
    #:
    #: 只读不是靠这个名字保证的，是结构保证的：`ask_context.capture()` 出来的
    #: 是 frozen 快照，工具拿不到 session，因此没有 write_working 可调。
    FALLBACK = "fallback"
    UNCLEAR = "unclear"


# ---- 只读提问判定（P-4 第三步）----
#
# 为什么需要它：下面五个关键词表是纯子串匹配，只读提问会命中写意图。实测（本文件
# 的 Router + 一份两章 md）：
#
#   这篇论文缺哪些章节    -> ADD_SECTION_BATCH   （弹访谈）
#   帮我分析下还缺什么内容 -> ADD_SECTION_BATCH   （弹访谈）
#   这段的参考文献够吗    -> FIX_BIBLIOGRAPHY    **静默写 working copy**
#   引言的语言风格是怎样的 -> POLISH             （弹访谈）
#   为什么方法章读起来别扭 -> POLISH             （弹访谈）
#
# 命中原因：`_ADD_BATCH_KW` 含「缺哪些」、`_BIB_KW` 含「参考文献」、`_POLISH_KW`
# 含「语言」「读起来」。
#
# 上表第三行的 `FIX_BIBLIOGRAPHY` 与 `_BIB_KW` **已随意图一起删除**（B-2）——
# 它是当时唯一会静默改文档的一条。删掉之后「参考文献」不再是路由关键词，所以
# 「帮我在相关工作里多引几篇参考文献」这类**祈使句**也不再被劫持（兜底判定只
# 管疑问句，管不到它）。保留这段记录是因为它是本判定的立项依据。
#
# 判定形态：**疑问形态 且 无祈使写作标记**，两个条件都要。
# 偏向漏判是刻意的 —— 代价不对称：漏判只是回到现状（问句被当成写请求，用户看到
# 访谈可以退出），误判会把用户的写请求答成一段话。所以「缺哪些章节，帮我补齐」
# 必须继续走写路径。

#: 疑问形态标记。
_QUESTION_KW = [
    "吗", "呢", "哪些", "哪几", "哪一", "哪个", "哪章", "为什么", "为何",
    "怎么", "怎样", "如何", "是不是", "有没有", "多少", "几个", "几章",
    "什么", "是否", "?", "？",
]

#: 祈使写作标记 —— 出现即视为写请求，**优先于疑问形态**。
#:
#: 只放"明确要求改动文档"的动词。刻意**不含**「帮我」：「帮我分析下还缺什么内容」
#: 是只读请求，分析不改文档。
_IMPERATIVE_WRITE_KW = [
    "润色", "改写", "重写", "补齐", "补全", "补写", "新增", "加一章", "加一段",
    "补一段", "补一句", "写一章", "写个", "改简洁", "改结构", "重新组织",
    "换叙述", "详细写", "展开写", "转成", "转为", "换成", "变成", "转格式",
    "导出为", "帮我改", "给我改", "修一下", "改一下", "调整为", "设置为",
    "polish", "rewrite", "restructure", "add section", "convert",
]


def looks_like_question(message: str) -> bool:
    """这条消息是不是只读提问。

    纯规则、无 LLM：判定行为完全由代码决定，可被测试穷举，不需要靠真机流量摸
    误判率（这也是本项目跳过 shadow 档的理由 —— 规则判定没有未知分布要量）。
    """
    low = (message or "").lower()
    if any(kw in low for kw in _IMPERATIVE_WRITE_KW):
        return False
    return any(kw in low for kw in _QUESTION_KW)


_POLISH_KW = [
    "润色", "改简洁", "通顺", "语法", "语言", "精炼", "别扭", "读起来", "通读",
    "polish", "refine", "smooth", "clarity",
]

_REWRITE_KW = [
    "改写", "重写", "补论证", "补充论证", "换叙述", "重新组织", "改结构",
    "展开", "详细写", "补一段", "加一段", "补一句",
    "rewrite", "restructure", "expand", "elaborate",
]

_ADD_SECTION_KW = [
    "新增", "加一章", "补写", "补一章", "写一章", "写个", "需要一个",
    "add section", "add a section", "write a new section", "new section",
]
_ADD_BATCH_KW = ["补齐", "缺哪些", "缺失章节", "补全章节", "还缺"]

_FORMAT_OP_KW = [
    "字体", "字号", "几号字", "号字", "行距", "首行缩进", "缩进", "对齐", "居中",
    "左对齐", "右对齐", "两端对齐", "样式", "双栏", "单栏", "两栏", "三栏",
    "三线表", "三线", "页码", "页眉", "页脚", "加粗", "斜体", "横向", "纵向",
    "font", "spacing", "indent", "align", "bold", "italic", "column",
]

_CONVERT_KW = ["转成", "变成", "换成", "转格式", "转为", "convert", "导出为"]

# 目标格式关键词 → 规范扩展名
_FORMAT_KW = {
    "docx": "docx", "word": "docx",
    "latex": "tex", ".tex": "tex", "tex": "tex",
    "markdown": "md", ".md": "md",
}
# 排版预设关键词 → preset key
_PRESET_KW = {
    "双栏": "two_column", "two-column": "two_column", "two column": "two_column",
    "2栏": "two_column", "分栏": "two_column",
    "三线表": "three_line_table", "three-line": "three_line_table",
    "three line": "three_line_table",
}


@dataclass
class RouteResult:
    intent: Intent
    section_id: str | None = None
    target_ext: str | None = None
    presets: list[str] = field(default_factory=list)
    title: str | None = None          # add_section 的目标标题（若能推断）
    note: str = ""


# 常见章节名关键词 → 规范标题（供 add_section 预填）
_SECTION_NAME_KW = {
    "引言": "Introduction", "introduction": "Introduction",
    "相关工作": "Related Work", "related work": "Related Work",
    "结论": "Conclusion", "conclusion": "Conclusion",
    "讨论": "Discussion", "discussion": "Discussion",
    "局限": "Limitations", "limitations": "Limitations",
    "背景": "Background", "background": "Background",
}


def _detect_section_title(low: str) -> str | None:
    for kw, title in _SECTION_NAME_KW.items():
        if kw in low:
            return title
    return None


def _match_section(message: str, index: SectionIndex) -> str | None:
    low = message.lower()
    for s in sorted(index.list(), key=lambda x: -len(x.title)):
        if s.title and s.title.lower() in low:
            return s.id
        if s.id and s.id in low:
            return s.id
    return None


def _detect_format(low: str) -> str | None:
    for kw, ext in _FORMAT_KW.items():
        if kw in low:
            return ext
    return None


def _detect_presets(low: str) -> list[str]:
    found: list[str] = []
    for kw, preset in _PRESET_KW.items():
        if kw in low and preset not in found:
            found.append(preset)
    return found


class Router:
    def __init__(self, index: SectionIndex, *, fallback_enabled: bool = False) -> None:
        self.index = index
        self.fallback_enabled = fallback_enabled

    def route(self, message: str) -> RouteResult:
        low = message.lower()

        # 兜底判定前置（P-4 第三步）。必须**先于**下面所有关键词表 —— 它们是纯
        # 子串匹配，会把只读提问吞进写意图（上面那张表）。
        #
        # `fallback_enabled=False` 时整个分支不存在，route() 输出与接入前逐字节
        # 一致，现有全量测试因此不受影响。
        if self.fallback_enabled and looks_like_question(message):
            return RouteResult(intent=Intent.FALLBACK)

        target_ext = _detect_format(low)
        presets = _detect_presets(low)
        has_convert_kw = any(kw in low for kw in _CONVERT_KW)

        # add_section 批处理
        if any(kw in low for kw in _ADD_BATCH_KW):
            return RouteResult(intent=Intent.ADD_SECTION_BATCH)

        # add_section 单章：出现新增类关键词，且目标章节不存在
        if any(kw in low for kw in _ADD_SECTION_KW):
            existing = _match_section(message, self.index)
            if existing is not None:
                return RouteResult(
                    intent=Intent.UNCLEAR,
                    note=f"章节「{existing}」已存在，若要修改请用 rewrite。",
                )
            return RouteResult(
                intent=Intent.ADD_SECTION, title=_detect_section_title(low)
            )

        # convert 信号：出现转换动词，或明确提到某目标格式（预设不单独触发转换）
        if has_convert_kw or target_ext:
            source_ext = self.index.ext
            if target_ext and target_ext == source_ext:
                return RouteResult(
                    intent=Intent.UNCLEAR,
                    note=f"源文件已是 .{source_ext}。同格式排版调整请直接说需求（走 format_docx）。",
                )
            return RouteResult(
                intent=Intent.CONVERT_FORMAT,
                target_ext=target_ext,
                presets=presets,
            )

        # format_docx：排版属性调整（含仅提到预设如“双栏/三线表”）
        if any(kw in low for kw in _FORMAT_OP_KW):
            if self.index.ext == "md":
                return RouteResult(
                    intent=Intent.UNCLEAR,
                    note="markdown 没有排版属性，请先 convert 成 docx 再调整格式。",
                )
            return RouteResult(intent=Intent.FORMAT_DOCX)

        is_polish = any(kw.lower() in low for kw in _POLISH_KW)
        is_rewrite = any(kw.lower() in low for kw in _REWRITE_KW)

        # 保守优先：同时命中润色与改写时走 polish（改动更小）
        if is_polish:
            return RouteResult(
                intent=Intent.POLISH,
                section_id=_match_section(message, self.index),
            )
        if is_rewrite:
            return RouteResult(
                intent=Intent.REWRITE,
                section_id=_match_section(message, self.index),
            )

        return RouteResult(
            intent=Intent.UNCLEAR,
            note="支持：polish（润色）、rewrite（改写）、add_section（新增章节）、"
            "convert_format（转格式）、format_docx（排版）。",
        )


__all__ = ["Intent", "RouteResult", "Router", "looks_like_question"]
