"""Router（B1，Week 2）。

规则优先、LLM 兜底（暂不接 LLM 兜底）。识别 polish 与 convert_format；
其余归 UNCLEAR。同时尝试抽出目标章节 / 目标格式 / 排版预设。
"""

from __future__ import annotations

import re
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
    FIX_BIBLIOGRAPHY = "fix_bibliography"
    UNCLEAR = "unclear"


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
_BIB_KW = ["bibliography", "参考文献", "文献列表", "缺条目", "bibitem"]

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
    def __init__(self, index: SectionIndex) -> None:
        self.index = index

    def route(self, message: str) -> RouteResult:
        low = message.lower()

        # fix_bibliography：显式提到 bibliography/参考文献
        if any(kw in low for kw in _BIB_KW):
            return RouteResult(intent=Intent.FIX_BIBLIOGRAPHY)

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
            "convert_format（转格式）、format_docx（排版）、fix_bibliography（修引用）。",
        )


__all__ = ["Intent", "RouteResult", "Router"]
