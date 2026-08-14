"""docx 就地编辑：docx 始终是唯一真相，纯文本只是它的**只读投影**。

## 为什么不转 markdown

上一版走的是 pandoc 往返（docx → md 编辑 → md → docx）。那条路有个结构性缺陷：
最终文件由 markdown 重建，排版只能靠 `--reference-doc` 带回**样式级**定义，而
`docx_format.py` 是逐 run 设 `font.size`（run 级直接格式化），带不回来。于是
「用户丢来 docx 要求调格式」会走到最糟的失败形态 —— 排版执行成功、Gate 通过、
`:save` 报已保存，打开文件却发现字号没变。

现在 working copy 就是 docx 副本本身。排版直接改它，正文改动按段落就地写回，
两者不再互斥。

## 投影 ⇄ 写回的契约

意图层（polish / rewrite / add_section / bibliography_sync）全都是
「读全文字符串 → 拼出新全文 → 写回全文」。所以这里提供的是同样的接口：

- `project_text()`：docx → 纯文本，一段一行。标题按 `style.name` 的层级加 `#`
  前缀，使既有的 markdown 章节正则可直接复用（层级来自段落样式，不靠猜文本约定，
  比 md 的 `#` 计数更可靠）。
- `apply_text()`：收整篇新文本，按行 diff 出段落级操作再就地落盘。

## 难点在 run，不在段落

docx 的文本在 XML 里按 run 切碎，run 边界由格式变化、拼写检查甚至编辑历史决定，
与语义无关：`位姿图优化` 可能被切成 `['位姿', '图优', '化…']`，逐 run 查子串永远
匹配不上。所以编辑单元取**段落** —— `p.text` 是 python-docx 拼好的完整文本，
段落粒度不存在跨 run 问题。改写时保留首 run 的字体属性、删掉其余 run。

## 覆盖范围（如实说明）

只覆盖 `doc.paragraphs`，即正文段落（含标题）。**表格单元格、文本框、页眉页脚里
的文字不在其中**，投影看不见，也就改不到。这是有意为之的第一版边界：表格内改写
需要额外的定位语义（第几行第几列），与章节定位不是一回事。`table_text_ignored()`
用于在上层如实告知用户，而不是让他们以为全文都被处理了。
"""

from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass

_HEADING_PREFIX = "#"


class DocxDocumentError(RuntimeError):
    """docx 读写失败（文件损坏、python-docx 缺失等）。"""


def _docx_module():
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise DocxDocumentError(
            "处理 .docx 需要 python-docx：pip install python-docx"
        ) from exc
    return docx


def _heading_level(style_name: str) -> int:
    """`Heading 2` → 2；`Title` → 1；正文样式 → 0。

    只认内置 Heading 样式。中文版 Word 的样式名（`标题 1`）也一并认，否则中文文档
    的章节会被当成正文，章节定位直接失效。
    """
    name = (style_name or "").strip()
    if name in ("Title", "标题"):
        return 1
    for prefix in ("Heading", "标题"):
        if name.startswith(prefix):
            tail = name[len(prefix):].strip()
            if tail.isdigit():
                return max(1, min(9, int(tail)))
    return 0


@dataclass(frozen=True)
class ParagraphSpan:
    """投影文本里一行 ↔ docx 里一个段落。"""

    index: int          # docx paragraphs 下标
    level: int          # 0 = 正文，≥1 = 标题层级
    text: str           # 段落原文（不含 `#` 前缀）


def _paragraphs(doc):
    return list(doc.paragraphs)


def project_lines(doc) -> tuple[list[str], list[ParagraphSpan]]:
    """docx → (投影行, 段落映射)。一段一行，标题加 `#` 前缀。"""
    lines: list[str] = []
    spans: list[ParagraphSpan] = []
    for i, para in enumerate(_paragraphs(doc)):
        level = _heading_level(para.style.name if para.style is not None else "")
        text = para.text
        spans.append(ParagraphSpan(index=i, level=level, text=text))
        lines.append(f"{_HEADING_PREFIX * level} {text}".lstrip() if level else text)
    return lines, spans


def project_text(path: str) -> str:
    """读 docx，返回投影文本。意图层看到的就是这个字符串。"""
    doc = _docx_module().Document(path)
    lines, _ = project_lines(doc)
    return "\n".join(lines)


def strip_heading_prefix(line: str) -> tuple[str, int]:
    """`## 标题` → (`标题`, 2)；正文行 → (原文, 0)。"""
    if not line.startswith(_HEADING_PREFIX):
        return line, 0
    stripped = line.lstrip(_HEADING_PREFIX)
    level = len(line) - len(stripped)
    return stripped.lstrip(), min(9, level)


def _set_paragraph_text(para, new_text: str) -> None:
    """就地替换段落文字，尽量保住段内局部格式。

    ## 为什么不能简单地"清空 + 首 run 写入"

    那样做会把**段内局部格式**抹平：`前缀 + **加粗词** + 后缀` 改一个字，加粗就没了。
    段内加粗/斜体在论文里承载语义（术语强调、定理名），抹掉是实质损失。

    ## 策略：只重写发生变化的中段

    对新旧文本取公共前缀/后缀，落在**未变化区**内的 run 整个保留（连同其格式），
    只有跨越变化区的 run 才被重写。改一个词就只动那个词所在的 run，其余 run 连
    XML 都不碰。全段皆变时退化为"保留首 run 属性"的旧行为。
    """
    runs = list(para.runs)
    if not runs:
        para.add_run(new_text)
        return

    old_text = para.text
    if old_text == new_text:
        return

    # 公共前后缀（按字符）
    head = 0
    limit = min(len(old_text), len(new_text))
    while head < limit and old_text[head] == new_text[head]:
        head += 1
    tail = 0
    while (tail < limit - head
           and old_text[len(old_text) - 1 - tail] == new_text[len(new_text) - 1 - tail]):
        tail += 1
    old_end = len(old_text) - tail          # 旧文本变化区 [head, old_end)

    # 每个 run 在段落文本里的区间
    spans = []
    pos = 0
    for run in runs:
        spans.append((run, pos, pos + len(run.text)))
        pos += len(run.text)

    untouched_prefix = [(r, s, e) for r, s, e in spans if e <= head]
    untouched_suffix = [(r, s, e) for r, s, e in spans if s >= old_end]
    middle = [r for r, s, e in spans if not (e <= head or s >= old_end)]

    if not middle:                          # 纯插入：并到最近的 run 上
        if untouched_prefix:
            middle = [untouched_prefix[-1][0]]
            untouched_prefix = untouched_prefix[:-1]
        else:
            middle = [runs[0]]
            untouched_suffix = untouched_suffix[1:]

    # 变化区的新内容 = 新文本去掉「被保留的前缀 run」和「被保留的后缀 run」所覆盖的部分。
    # 长度必须按**实际保留的 run 文本**算，不能直接用 head/tail —— 跨边界的 run 归入
    # middle 后，它那部分文字仍要由 new_middle 负责写回，否则会丢字。
    kept_head_len = sum(len(r.text) for r, _s, _e in untouched_prefix)
    kept_tail_len = sum(len(r.text) for r, _s, _e in untouched_suffix)
    new_middle = new_text[kept_head_len: len(new_text) - kept_tail_len]

    keep = middle[0]
    font = keep.font
    size, name, bold, italic = font.size, font.name, font.bold, font.italic
    color = font.color.rgb if font.color and font.color.type is not None else None
    for run in middle[1:]:
        run._element.getparent().remove(run._element)
    keep.text = new_middle
    font.size, font.name, font.bold, font.italic = size, name, bold, italic
    if color is not None:
        font.color.rgb = color


def _clone_paragraph(anchor_para, text: str, *, parent, before: bool = False):
    """在 `anchor_para` 前/后插一段，复制它的段落属性（样式/对齐/缩进/字体）。

    复制而非新建：新段落若不继承邻段属性，会以 `Normal` 默认样式出现，在排好版的
    文档里显得格外突兀（字体字号行距全不对）。
    """
    docx = _docx_module()
    element = copy.deepcopy(anchor_para._element)
    if before:
        anchor_para._element.addprevious(element)
    else:
        anchor_para._element.addnext(element)
    para = docx.text.paragraph.Paragraph(element, parent)
    _set_paragraph_text(para, text)
    return para


def _apply_heading_level(para, level: int) -> None:
    """把新插入的段落设成对应层级的标题样式（或正文）。

    必要性：新段落是从邻段复制来的，若邻段是正文而新行是 `## 小节标题`，不设样式
    就会插进去一个"看着像正文的标题" —— 章节索引下次投影时认不出它，用户新增的
    章节等于凭空消失。样式名缺失时静默跳过（模板未定义该样式，不该让写入失败）。
    """
    if level <= 0:
        return
    for name in (f"Heading {level}", f"标题 {level}"):
        try:
            para.style = name
            return
        except KeyError:
            continue


def _delete_paragraph(para) -> None:
    element = para._element
    element.getparent().remove(element)


def _insert_line(line: str, anchor, body, stats, *, before: bool = False):
    """插入一行（含标题样式修正）；返回新段落，供连续插入时当下一个锚点。"""
    text, level = strip_heading_prefix(line)
    para = _clone_paragraph(anchor, text, parent=body, before=before)
    _apply_heading_level(para, level)
    stats["inserted"] += 1
    return para


def _heading_levels_of(lines: list[str]) -> list[int]:
    """投影行 → 标题层级序列。写回前后的结构比对就基于它。"""
    return [lv for lv in (strip_heading_prefix(line)[1] for line in lines) if lv > 0]


def _assert_no_heading_loss(old_lines: list[str], new_lines: list[str]) -> None:
    """写回不得让既有标题消失。丢标题是损坏，新增标题与改名都是意图。

    为什么放在写回层而不是 gate：损坏可能发生在 gate **之后** —— gate 只看单章
    正文，`splice` 拼接造成的跨边界融合它结构上看不到。这里守的是真正落盘的那
    一步，且对所有调用方生效（意图层、docx_crossref、格式化路径）。

    判据是**层级序列的子序列**关系，不是逐项相等：

    - `add_section` 新增标题 → 新序列更长，仍含旧序列 → 放行
    - 改写改标题文字 → 层级不变 → 放行（故只比层级、不比文字）
    - 标题被吞成正文 → 该层级从序列里消失 → 拦下
    - 标题层级被改（`###` → `##`）→ 也算丢失，它同样会让章节树错位

    **已知局限**：若标题行**保住了 `#` 前缀**却吞进了正文文字
    （真机 4.2 那种「### 标题正文首句…」），层级序列不变，此断言看不出来。
    那种融合的成因由 `SectionIndex.splice` 的换行规范化根治；这里只兜「标题
    整个消失」这一类，不假装能覆盖全部形态。
    """
    old_levels = _heading_levels_of(old_lines)
    new_levels = _heading_levels_of(new_lines)

    it = iter(new_levels)
    if all(lv in it for lv in old_levels):     # 子序列成立
        return

    raise DocxDocumentError(
        f"拒绝写入：这次改动会让既有标题消失（写回前 {len(old_levels)} 个标题，"
        f"新文本 {len(new_levels)} 个，且层级序列不再包含原有结构）。\n"
        "常见成因是候选正文丢了首尾换行，导致标题与正文被拼成同一段。\n"
        "文件未被修改。"
    )


def apply_text(path: str, new_text: str) -> dict[str, int]:
    """把整篇新文本写回 docx（就地）。返回 `{replaced, inserted, deleted}` 计数。

    上层给的是全文字符串（既有意图层的契约），这里按**行 diff** 还原成段落操作。
    倒序执行：先处理靠后的操作，前面的段落下标才不会被插入/删除移位。

    写回前校验标题结构（见 `_assert_no_heading_loss`）：丢标题即抛错、不落盘。
    """
    docx = _docx_module()
    doc = docx.Document(path)
    old_lines, _spans = project_lines(doc)
    new_lines = new_text.split("\n")
    _assert_no_heading_loss(old_lines, new_lines)

    stats = {"replaced": 0, "inserted": 0, "deleted": 0}
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    opcodes = [op for op in matcher.get_opcodes() if op[0] != "equal"]

    paragraphs = _paragraphs(doc)
    if not paragraphs:
        # 空文档没有可复制的锚点段落，直接按行追加即可（无排版可保）。
        for line in new_lines:
            text, level = strip_heading_prefix(line)
            para = doc.add_paragraph(text)
            _apply_heading_level(para, level)
            stats["inserted"] += 1
        doc.save(path)
        return stats
    body = paragraphs[0]._parent

    for tag, i1, i2, j1, j2 in reversed(opcodes):
        if tag == "replace":
            common = min(i2 - i1, j2 - j1)
            for k in range(common):
                text, _ = strip_heading_prefix(new_lines[j1 + k])
                _set_paragraph_text(paragraphs[i1 + k], text)
                stats["replaced"] += 1
            # 新行更多 → 余下的插在最后一个被替换的段落之后
            if j2 - j1 > common:
                anchor = paragraphs[i1 + common - 1] if common else paragraphs[i1]
                for k in range(common, j2 - j1):
                    anchor = _insert_line(new_lines[j1 + k], anchor, body, stats)
            # 原段更多 → 多出来的删掉
            for k in range(common, i2 - i1):
                _delete_paragraph(paragraphs[i1 + k])
                stats["deleted"] += 1
        elif tag == "insert":
            if i1 - 1 >= 0:
                anchor = paragraphs[i1 - 1]
                for k in range(j1, j2):
                    anchor = _insert_line(new_lines[k], anchor, body, stats)
            else:
                # 插在文首：以原首段为锚点向**前**插，仍能继承排版属性。
                # （早前版本用 add_paragraph 会落到文末，位置是错的。）
                anchor = paragraphs[0]
                first = True
                for k in range(j1, j2):
                    if first:
                        anchor = _insert_line(new_lines[k], anchor, body, stats,
                                              before=True)
                        first = False
                    else:
                        anchor = _insert_line(new_lines[k], anchor, body, stats)
        elif tag == "delete":
            for k in range(i1, i2):
                _delete_paragraph(paragraphs[k])
                stats["deleted"] += 1

    doc.save(path)
    return stats


def table_text_ignored(path: str) -> int:
    """表格里有多少个非空单元格 —— 投影覆盖不到的部分。

    用于如实告知范围，而不是让用户以为全文都被处理了。
    """
    doc = _docx_module().Document(path)
    count = 0
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    count += 1
    return count


def is_docx_file(path: str) -> bool:
    """按 zip magic 判断（不看扩展名）：扩展名是 docx 的纯文本文件要能识别出来。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"PK"
    except OSError:
        return False


__all__ = [
    "DocxDocumentError", "ParagraphSpan",
    "project_lines", "project_text", "apply_text",
    "strip_heading_prefix", "table_text_ignored", "is_docx_file",
]
