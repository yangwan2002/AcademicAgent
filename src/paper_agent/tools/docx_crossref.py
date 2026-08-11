"""导出后处理：把正文里的纯文本 `[n]` 换成 Word 交叉引用域（REF field）。

## 为什么是「导出后处理」而不是改主流水线

整条流水线以**投影文本**为契约：`project_text` 给模型看它、`apply_text` 按它做行
diff、`FormatGate` 对它算 SHA-256、`CitationQuotaGate` 靠它做确定性引用计数。而
`python-docx` 的 `para.text` **看不见域里的文字**（只拼 `w:r/w:t`），实测带 REF 域的
段落投影出来引用位置是空的。若让工作副本带域，模型会看到「这里没有引用」进而自己补，
配额计数同时归零，`_set_paragraph_text` 那套按 run 重写的保排版逻辑也会破坏域元素。

所以域只在**最终导出**这一步生成，工作副本全程保持纯文本 `[n]`。代价是工作副本里不是
交叉引用（它本来就是中间产物），换来主流水线零改动、零风险。

## 硬约束：`export_crossrefs` 不原地改写

`export_crossrefs` 拒绝 dest == src —— 它是纯函数式的「读一份、写另一份」，原地改写
会在转换中途损坏唯一的那份文件。

这**不等于**「域版不能存回源文件」：`FileBackedSession.save_to_source(crossrefs=True)`
就是把升域后的副本写回源文件，因为 `import_crossrefs`（载入即摊平）已经让带域的 docx
可以安全地重新载入。工作副本始终保持纯文本，升域只作用于写出的那一份。

## 域的形态

正文 `[12, 21]` → 字面 `[` + ``REF Ref_bib_12 \\h`` 域 + `, ` + ``REF Ref_bib_21 \\h`` 域
+ 字面 `]`。**只把数字换成域**，方括号与逗号留作普通文字 —— 与文献表那侧「书签只圈数字」
对应，这样 Word 里更新域不会吃掉括号。

书签名 `Ref_bib_<n>`：字母开头、无空格、远短于 Word 的 40 字符上限。

## 入站方向：用户提交的文档本来就是交叉引用

反过来的情形同样要处理 —— 别人给的 docx 里引用已经是域。此时投影层看不见域内文字，
症状是**静默数据损坏**：模型看到 `匹配是关键 []`（引用位置空着）会自己补一个；
`_set_paragraph_text` 只删 `para.runs`，域元素留在原地漂移，实测两个域会一起堆到段尾，
`[1]...[2]` 变成 `[]...[12]`；配额 Gate 同时恒为 0。

所以 `import_crossrefs` 在**载入时把域摊平成纯文本**（域缓存里就是编号），整条流水线
照常跑纯文本，`export_crossrefs` 再升回域 —— 一次往返。摊平保持**可见文字逐字不变**。

往返要保住原书签名：Word 自己生成的是 `_Ref123456789` 这类名字，若导出时另起
`Ref_bib_<n>`，用户文档里别处引用旧书签的域会断。故摊平时记下 `编号 → 原书签名`
（`ImportReport.bookmark_map`），导出时优先复用。

**不摊平第三方文献管理器的域**：Zotero / EndNote 用 `ADDIN CITATION` 之类的域承载
引用，摊平会切断它与文献库的联系（比看不见引用更糟）。这类域只报告、不动。

## 已知限制

Word 不会在打开时自动刷新域。显示文字来自我们写入的**域缓存**，与转换时的正文一致；
用户改动文献顺序后需 Ctrl+A → F9 重算。这是 Word 的行为，不是本模块的缺陷。
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass, field

from paper_agent.gates.diff_gate import CITATION_BRACKET_RE

#: 文献表条目形如 `[12] Author. Title...`：行首方括号数字。
_BIB_ENTRY_RE = re.compile(r"^\s*\[(\d+)\]")

#: 文献区起止标记（与 `bibliography_sync` 的 md 标记同形；docx 投影里就是普通段落文字）。
_BIB_BEGIN_HINT = "BIBLIOGRAPHY:BEGIN"
_BIB_END_HINT = "BIBLIOGRAPHY:END"

#: 中文文献区标题（无自动同步标记的文档靠它兜底定位）。
_BIB_HEADING_HINTS = ("参考文献", "references", "bibliography")


def _qn(tag: str) -> str:
    from docx.oxml.ns import qn
    return qn(tag)


def _el(tag: str):
    from docx.oxml import OxmlElement
    return OxmlElement(tag)


@dataclass
class CrossrefReport:
    """转换结果。`fields` 为生成的域数量，`bookmarks` 为文献表书签数量。"""

    dest_path: str = ""
    bookmarks: int = 0
    fields: int = 0
    #: 正文引用了但文献表里没有对应条目的编号 —— 不生成域（避免制造断链）。
    dangling: tuple[str, ...] = ()
    #: 文献表有条目但正文未引用的编号（仅报告，不处理）。
    uncited: tuple[str, ...] = ()
    already_converted: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.dest_path) and not self.dangling


@dataclass
class ImportReport:
    """入站摊平结果（域 → 纯文本）。

    `flattened` 为摊平的 REF 域数量；`bookmark_map` 是「编号 → 原书签名」，导出时
    回传给 `export_crossrefs` 以复用用户原有书签。`foreign_fields` 是**没有摊平**的
    第三方引文域（Zotero/EndNote 等）数量 —— 摊平它们会切断与文献库的联系。
    """

    flattened: int = 0
    bookmark_map: dict[str, str] = field(default_factory=dict)
    foreign_fields: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.flattened > 0


def bookmark_name(number: str) -> str:
    """编号 → 书签名。稳定、合法、可逆。"""
    return f"Ref_bib_{number}"


# --------------------------------------------------------------- run 级工具


def _runs_with_offsets(para):
    """返回 [(run, start, end)]，按文档顺序，偏移相对 `para.text`。

    只统计 `w:r/w:t` —— 与 `para.text` 同口径，否则偏移会错位。
    """
    out = []
    pos = 0
    for r in para.runs:
        t = r.text or ""
        out.append((r, pos, pos + len(t)))
        pos += len(t)
    return out


def _copy_rpr(src_run):
    """复制 run 属性（字体/字号/加粗等），供新建 run 继承原格式。"""
    if src_run is None:
        return None
    rpr = src_run._r.find(_qn("w:rPr"))
    if rpr is None:
        return None
    import copy
    return copy.deepcopy(rpr)


def _make_run(text: str, rpr):
    """按给定属性造一个 `w:r`。空文本也造 —— 调用方负责不传空。"""
    r = _el("w:r")
    if rpr is not None:
        r.append(rpr)
    t = _el("w:t")
    t.text = text
    # 前后空格必须显式保留，否则 Word 会吃掉（`[1] Author` 的那个空格）。
    t.set(_qn("xml:space"), "preserve")
    r.append(t)
    return r


def _apply_superscript(rpr):
    """给 rPr 打上上标（`w:vertAlign=superscript`）。rpr 为 None 时新建一个。

    Word 的上标是**字符属性**，不是字号 —— 不要手动缩小 `w:sz`，那样和正文换行
    基线对不齐。`vertAlign` 交给 Word 自己按比例缩放。
    """
    if rpr is None:
        rpr = _el("w:rPr")
    for old in rpr.findall(_qn("w:vertAlign")):
        rpr.remove(old)
    va = _el("w:vertAlign")
    va.set(_qn("w:val"), "superscript")
    rpr.append(va)
    return rpr


def _make_ref_field(number: str, rpr, *, superscript: bool = False,
                    target: str = ""):
    """造一个 `REF <书签> \\h` 域，缓存文字为编号本身。

    `target` 给往返场景：用户原文的书签名（如 Word 的 `_Ref123456789`）要原样复用，
    否则文档里别处引用旧书签的域会断。缺省用我们自己的 `Ref_bib_<n>`。
    """
    fld = _el("w:fldSimple")
    fld.set(_qn("w:instr"), f" REF {target or bookmark_name(number)} \\h ")
    inner = _copy_rpr_from_rpr(rpr)
    if superscript:
        inner = _apply_superscript(inner)
    fld.append(_make_run(number, inner))
    return fld


def _copy_rpr_from_rpr(rpr):
    if rpr is None:
        return None
    import copy
    return copy.deepcopy(rpr)


def _has_ref_field(para) -> bool:
    """该段是否已含 REF 域（幂等判断）。"""
    for fld in para._p.iter(_qn("w:fldSimple")):
        if "REF " in (fld.get(_qn("w:instr")) or ""):
            return True
    for instr in para._p.iter(_qn("w:instrText")):
        if "REF " in (instr.text or ""):
            return True
    return False


# --------------------------------------------------------------- 文献表书签


def _next_bookmark_id(doc) -> int:
    """取一个未被占用的书签 id 起点。id 冲突会让 Word 报文档损坏。"""
    used = set()
    for bm in doc.element.body.iter(_qn("w:bookmarkStart")):
        try:
            used.add(int(bm.get(_qn("w:id"))))
        except (TypeError, ValueError):
            continue
    return (max(used) + 1) if used else 1


def _existing_bookmarks(doc) -> set[str]:
    return {
        bm.get(_qn("w:name")) or ""
        for bm in doc.element.body.iter(_qn("w:bookmarkStart"))
    }


def _bookmark_number_in_entry(para, number: str, bm_id: int) -> bool:
    """在文献表条目段里，用书签圈住 `[n]` 中的**数字**。成功返回 True。

    做法：定位数字在 `para.text` 里的字符区间，按区间把承载它的 run 切成
    前/中/后三段（只切那一个 run，其余 run 连 XML 都不碰），再把
    `bookmarkStart/End` 插在中段两侧。
    """
    text = para.text or ""
    m = _BIB_ENTRY_RE.match(text)
    if not m or m.group(1) != number:
        return False
    start, end = m.span(1)

    target = None
    for run, rs, re_ in _runs_with_offsets(para):
        if rs <= start and end <= re_:
            target = (run, rs)
            break
    if target is None:
        # 数字被拆在多个 run 里（少见）。退化为圈住整个承载区间的首个 run，
        # 宁可书签范围略大，也不放弃 —— REF 显示的是书签内文字，仍是数字。
        for run, rs, re_ in _runs_with_offsets(para):
            if rs <= start < re_:
                target = (run, rs)
                break
    if target is None:
        return False

    run, rs = target
    local_start, local_end = start - rs, end - rs
    full = run.text or ""
    head, mid, tail = full[:local_start], full[local_start:local_end], full[local_end:]
    if not mid:
        return False

    rpr = _copy_rpr(run)
    parent = run._r.getparent()
    anchor = run._r
    idx = list(parent).index(anchor)

    new_nodes = []
    if head:
        new_nodes.append(_make_run(head, _copy_rpr_from_rpr(rpr)))
    bm_start = _el("w:bookmarkStart")
    bm_start.set(_qn("w:id"), str(bm_id))
    bm_start.set(_qn("w:name"), bookmark_name(number))
    new_nodes.append(bm_start)
    new_nodes.append(_make_run(mid, _copy_rpr_from_rpr(rpr)))
    bm_end = _el("w:bookmarkEnd")
    bm_end.set(_qn("w:id"), str(bm_id))
    new_nodes.append(bm_end)
    if tail:
        new_nodes.append(_make_run(tail, _copy_rpr_from_rpr(rpr)))

    for offset, node in enumerate(new_nodes):
        parent.insert(idx + offset, node)
    parent.remove(anchor)
    return True


# --------------------------------------------------------------- 正文域替换


def _convert_body_paragraph(para, known: set[str], stats: dict, *,
                            superscript: bool = False,
                            targets: dict[str, str] | None = None) -> None:
    """把段内所有 `[n]` 的**数字**换成 REF 域；括号与逗号留作文字。

    整段 inline 内容按字符重建，格式取自原 run（承载该字符的那个 run 的 rPr）。
    段内已有的域/书签等非 run 元素**原样保留位置** —— 通过只在纯文本区间做替换
    实现：若段内已含 REF 域则整段跳过（幂等）。

    `superscript=True` 时**整个 `[n]` 连方括号一起上标**（GB/T 7714 顺序编码制的
    常见排法）：方括号与分隔逗号也套上 `vertAlign`，否则会出现「小数字配大括号」。
    `targets` 把编号映射到要复用的原书签名（往返场景）。
    """
    text = para.text or ""
    if not text or "[" not in text:
        return
    matches = [m for m in CITATION_BRACKET_RE.finditer(text)]
    if not matches:
        return

    # 逐个 token 拆出数字的字符区间；非纯数字 token（如 `r0a1b2`）不处理。
    spans: list[tuple[int, int, str]] = []
    for m in matches:
        inner = m.group(1)
        base = m.start(1)
        for part in re.finditer(r"\d+", inner):
            num = part.group(0)
            # token 必须整体是「数字 + 可选逗号分隔的数字」才算引用编号
            if not re.fullmatch(r"[\d\s,]+", inner):
                continue
            if num in known:
                spans.append((base + part.start(), base + part.end(), num))
            else:
                stats.setdefault("dangling", set()).add(num)
    if not spans:
        return

    offsets = _runs_with_offsets(para)
    if not offsets:
        return

    def _rpr_at(pos: int):
        for run, rs, re_ in offsets:
            if rs <= pos < re_:
                return _copy_rpr(run)
        return _copy_rpr(offsets[-1][0])

    # 上标模式下，落在 `[...]` 整体范围内的普通文字（方括号、逗号、空格）也要上标。
    bracket_spans = [(m.start(), m.end()) for m in matches] if superscript else []

    def _in_bracket(pos: int) -> bool:
        return any(s <= pos < e for s, e in bracket_spans)

    def _emit_text(start: int, end: int, out: list) -> None:
        """把 `[start, end)` 这段文字按**原 run 边界**切开逐段发出。

        不能整段合成一个 run —— 段内格式（加粗术语、上标、变体字体）全在 run 上，
        合并会把它们抹平成区间首字符的格式。真机症状：`前缀 **加粗术语** 引用 [1]`
        转换后加粗消失。

        上标模式下还要再按「是否在方括号内」切一刀，让括号跟着数字一起上标。
        """
        for run, rs, re_ in offsets:
            lo, hi = max(start, rs), min(end, re_)
            if lo >= hi:
                continue
            # 在 run 内部按 bracket 归属再分段，避免一个 run 横跨括号内外
            seg_start = lo
            cur = _in_bracket(lo)
            for pos in range(lo + 1, hi + 1):
                nxt = _in_bracket(pos) if pos < hi else not cur
                if nxt != cur:
                    rpr = _copy_rpr(run)
                    if cur:
                        rpr = _apply_superscript(rpr)
                    out.append(_make_run(text[seg_start:pos], rpr))
                    seg_start = pos
                    cur = nxt

    # 重建：文本段 + 域，交替
    nodes: list = []
    cursor = 0
    tmap = targets or {}
    for s, e, num in spans:
        if s > cursor:
            _emit_text(cursor, s, nodes)
        nodes.append(_make_ref_field(
            num, _rpr_at(s), superscript=superscript, target=tmap.get(num, "")))
        stats["fields"] = stats.get("fields", 0) + 1
        cursor = e
    if cursor < len(text):
        _emit_text(cursor, len(text), nodes)

    p = para._p
    for run, _rs, _re in offsets:
        p.remove(run._r)
    # 插在段落属性之后、其余元素之前的原位置：直接追加即可，因为本段原先
    # 只有 run（有域的段已在上层跳过）。
    ppr = p.find(_qn("w:pPr"))
    base_idx = (list(p).index(ppr) + 1) if ppr is not None else 0
    for offset, node in enumerate(nodes):
        p.insert(base_idx + offset, node)


# --------------------------------------------------------------- 区域划分


def _bibliography_range(paragraphs) -> tuple[int, int]:
    """返回文献区的 [起, 止) 段落下标。找不到返回 (len, len)（即空区）。

    三级兜底：自动同步标记 → 「参考文献」标题 → 尾部连续 `[n]` 条目块。
    """
    n = len(paragraphs)
    begin = end = None
    for i, p in enumerate(paragraphs):
        t = p.text or ""
        if _BIB_BEGIN_HINT in t and begin is None:
            begin = i
        if _BIB_END_HINT in t:
            end = i + 1
    if begin is not None:
        return begin, (end if end is not None else n)
    if end is not None:
        # 只有 END 标记（本项目实测的真实形态：BEGIN 在转换中丢过）。
        # 从 END 往前找最后一个非条目段作为起点。
        first = end - 1
        while first - 1 >= 0 and (
            _BIB_ENTRY_RE.match(paragraphs[first - 1].text or "")
            or not (paragraphs[first - 1].text or "").strip()
        ):
            first -= 1
        return first, end

    for i, p in enumerate(paragraphs):
        t = (p.text or "").strip().lower()
        if any(h in t for h in _BIB_HEADING_HINTS) and len(t) <= 20:
            return i, n

    # 尾部连续条目块
    last = None
    for i in range(n - 1, -1, -1):
        if _BIB_ENTRY_RE.match(paragraphs[i].text or ""):
            last = i
            break
    if last is None:
        return n, n
    first = last
    while first - 1 >= 0 and (
        _BIB_ENTRY_RE.match(paragraphs[first - 1].text or "")
        or not (paragraphs[first - 1].text or "").strip()
    ):
        first -= 1
    return first, last + 1


# --------------------------------------------------------------- 入站：域 → 纯文本

#: 第三方文献管理器的域指令关键字 —— 摊平会切断与文献库的联系，只报告不动。
_FOREIGN_FIELD_HINTS = ("ADDIN", "CITATION", "ZOTERO", "MENDELEY", "ENDNOTE")


def _instr_of_fldsimple(fld) -> str:
    return fld.get(_qn("w:instr")) or ""


def _is_foreign(instr: str) -> bool:
    up = instr.upper()
    return any(h in up for h in _FOREIGN_FIELD_HINTS)


def _has_any_field(para) -> bool:
    """该段是否含任何域（含第三方引文域）—— 摊平入口的筛选条件。"""
    for _ in para._p.iter(_qn("w:fldSimple")):
        return True
    for _ in para._p.iter(_qn("w:instrText")):
        return True
    return False


def _bookmark_from_instr(instr: str) -> str:
    """从 ` REF _Ref12345 \\h ` 里取书签名。"""
    m = re.search(r"\bREF\s+(\S+)", instr, re.IGNORECASE)
    return m.group(1) if m else ""


def _field_cached_text(node) -> str:
    """域的缓存文字（用户在 Word 里实际看到的），即域内所有 `w:t` 拼接。"""
    return "".join(t.text or "" for t in node.iter(_qn("w:t")))


def _flatten_fldsimple(para, rep: ImportReport) -> None:
    """把段内 `w:fldSimple` REF 域替换成承载同样文字的普通 run。

    摊平后可见文字**逐字不变** —— 域缓存里就是编号。原书签名记进 `bookmark_map`，
    导出时复用。
    """
    for fld in list(para._p.iter(_qn("w:fldSimple"))):
        instr = _instr_of_fldsimple(fld)
        if _is_foreign(instr):
            rep.foreign_fields += 1
            continue
        if "REF " not in instr.upper():
            continue
        text = _field_cached_text(fld)
        parent = fld.getparent()
        if parent is None:
            continue
        idx = list(parent).index(fld)
        # 继承域内首个 run 的格式（上标等），让摊平后外观也不变
        inner_runs = list(fld.iter(_qn("w:r")))
        rpr = None
        if inner_runs:
            found = inner_runs[0].find(_qn("w:rPr"))
            if found is not None:
                import copy
                rpr = copy.deepcopy(found)
        if text:
            parent.insert(idx, _make_run(text, rpr))
        parent.remove(fld)
        rep.flattened += 1
        num = text.strip().strip("[]")
        bm = _bookmark_from_instr(instr)
        if num.isdigit() and bm:
            rep.bookmark_map.setdefault(num, bm)


def _flatten_fldchar(para, rep: ImportReport) -> None:
    """摊平 `fldChar` 形式的域（begin/separate/end 三段式，Word 原生插入的形态）。

    结构：`run(fldChar begin)` `run(instrText)` `run(fldChar separate)`
    `run(缓存文字)...` `run(fldChar end)`。保留缓存文字所在的 run（它已带正确格式），
    只删掉 begin..separate 与 end 这些控制 run。
    """
    p = para._p
    children = list(p)
    i = 0
    while i < len(children):
        node = children[i]
        if node.tag != _qn("w:r"):
            i += 1
            continue
        begin = None
        for fc in node.iter(_qn("w:fldChar")):
            if (fc.get(_qn("w:fldCharType")) or "") == "begin":
                begin = fc
                break
        if begin is None:
            i += 1
            continue

        # 收集到 end 为止
        j = i
        instr_text = ""
        sep_at = None
        end_at = None
        while j < len(children):
            n = children[j]
            if n.tag != _qn("w:r"):
                j += 1
                continue
            for it in n.iter(_qn("w:instrText")):
                instr_text += it.text or ""
            for fc in n.iter(_qn("w:fldChar")):
                ft = fc.get(_qn("w:fldCharType")) or ""
                if ft == "separate":
                    sep_at = j
                elif ft == "end":
                    end_at = j
            if end_at is not None:
                break
            j += 1

        if end_at is None:
            i += 1
            continue
        if _is_foreign(instr_text):
            rep.foreign_fields += 1
            i = end_at + 1
            continue
        if "REF " not in instr_text.upper():
            i = end_at + 1
            continue

        # 缓存文字在 separate..end 之间；无 separate 则整域无缓存
        cache_lo = (sep_at + 1) if sep_at is not None else end_at
        cached = "".join(
            _field_cached_text(children[k]) for k in range(cache_lo, end_at)
        )
        # 删控制 run：begin..separate 以及 end
        for k in range(i, cache_lo):
            if children[k].getparent() is not None:
                p.remove(children[k])
        if children[end_at].getparent() is not None:
            p.remove(children[end_at])
        rep.flattened += 1
        num = cached.strip().strip("[]")
        bm = _bookmark_from_instr(instr_text)
        if num.isdigit() and bm:
            rep.bookmark_map.setdefault(num, bm)

        children = list(p)
        i = 0 if end_at >= len(children) else i
        continue


def import_crossrefs(path: str) -> ImportReport:
    """**就地**把 docx 里的 REF 交叉引用域摊平成纯文本，返回报告。

    为什么必须做：投影层（`para.text`）看不见域内文字，含域文档进流水线的症状是
    静默数据损坏 —— 模型看到空的 `[]` 会自己补引用；`_set_paragraph_text` 只删
    `para.runs`，域元素留在原地漂移，实测 `[1]...[2]` 会变成 `[]...[12]`；配额
    Gate 同时恒为 0。摊平后整条流水线按纯文本正常工作，`:export` 再升回域。

    只摊平 REF 域。第三方引文域（Zotero/EndNote）仅计数不动 —— 切断它们与文献库
    的联系比看不见引用更糟。
    """
    import docx

    rep = ImportReport()
    doc = docx.Document(path)
    # 不能只看 `_has_ref_field` 的段 —— 第三方引文域（Zotero）不含 `REF`，那样会
    # 整段跳过、漏报，用户得不到「这些引用编辑不到」的警告。
    for para in doc.paragraphs:
        if not _has_any_field(para):
            continue
        _flatten_fldsimple(para, rep)
        _flatten_fldchar(para, rep)

    if rep.foreign_fields:
        rep.warnings.append(
            f"检测到 {rep.foreign_fields} 处第三方文献管理器域"
            "（Zotero/EndNote 等），未摊平：改动它们会切断与文献库的联系。"
            "这些位置的引用对模型不可见，本次不会被编辑。"
        )
    if rep.flattened:
        doc.save(path)
    return rep


# --------------------------------------------------------------- 对外入口


def export_crossrefs(src_path: str, dest_path: str, *,
                     superscript: bool = False,
                     bookmark_map: dict[str, str] | None = None) -> CrossrefReport:
    """把 `src_path` 转成交叉引用版写到 `dest_path`，返回报告。

    `dest_path` 必须不同于 `src_path` —— 原地改写会在转换中途损坏唯一的那份文件。
    `src_path` 全程只读。要把域版存回源文件，走 `save_to_source(crossrefs=True)`：
    它先转到临时文件再原子替换。

    `superscript=True` 把 `[n]` 连方括号一起排成上标（GB/T 7714 顺序编码制）。
    `bookmark_map`（编号 → 原书签名）来自 `import_crossrefs`，用于往返时复用用户
    原有书签，避免文档里别处引用旧书签的域断链。
    """
    import docx

    src_abs, dest_abs = os.path.abspath(src_path), os.path.abspath(dest_path)
    if src_abs == dest_abs:
        raise ValueError(
            "导出目标不能是源文件：带 REF 域的 docx 重新载入后投影层看不见引用，"
            "正文引用会集体消失。请指定另一个路径。"
        )

    rep = CrossrefReport()
    tmp = f"{dest_abs}.tmp.{uuid.uuid4().hex[:8]}"
    shutil.copyfile(src_abs, tmp)
    try:
        doc = docx.Document(tmp)
        paragraphs = list(doc.paragraphs)
        bib_start, bib_end = _bibliography_range(paragraphs)
        if bib_start >= len(paragraphs):
            rep.warnings.append("找不到参考文献区，未生成任何交叉引用。")
            shutil.copyfile(tmp, dest_abs)
            rep.dest_path = dest_abs
            return rep

        if any(_has_ref_field(p) for p in paragraphs):
            rep.already_converted = True
            rep.warnings.append("文档已含 REF 域，跳过转换（幂等）。")
            shutil.copyfile(tmp, dest_abs)
            rep.dest_path = dest_abs
            return rep

        # 正文引用编号必须在**转换前**统计：转换后 `[n]` 已进域，`para.text`
        # 看不见它，`uncited` 会把所有条目都算成未引用。
        cited = _cited_numbers(paragraphs, bib_start, bib_end)

        # 1) 文献表条目加书签
        bmap = dict(bookmark_map or {})
        bm_id = _next_bookmark_id(doc)
        existing = _existing_bookmarks(doc)
        known: set[str] = set()
        for p in paragraphs[bib_start:bib_end]:
            m = _BIB_ENTRY_RE.match(p.text or "")
            if not m:
                continue
            num = m.group(1)
            # 往返场景：用户原书签还在（摊平只动正文域，不删文献表书签）→ 直接复用
            if bmap.get(num) and bmap[num] in existing:
                known.add(num)
                continue
            if bookmark_name(num) in existing:
                known.add(num)
                bmap.pop(num, None)      # 用我们自己的名字
                continue
            if _bookmark_number_in_entry(p, num, bm_id):
                known.add(num)
                existing.add(bookmark_name(num))
                bmap.pop(num, None)
                bm_id += 1
                rep.bookmarks += 1

        # 2) 正文 `[n]` → REF 域（跳过文献区自身）
        stats: dict = {}
        for i, p in enumerate(paragraphs):
            if bib_start <= i < bib_end:
                continue
            if _has_ref_field(p):
                continue
            _convert_body_paragraph(
                p, known, stats, superscript=superscript, targets=bmap)
        rep.fields = stats.get("fields", 0)
        rep.dangling = tuple(sorted(stats.get("dangling", set()), key=_num_key))

        rep.uncited = tuple(sorted(known - cited, key=_num_key))

        doc.save(tmp)
        os.replace(tmp, dest_abs)
        rep.dest_path = dest_abs
        return rep
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _num_key(s: str) -> tuple:
    return (0, int(s)) if s.isdigit() else (1, 0)


def _cited_numbers(paragraphs, bib_start: int, bib_end: int) -> set[str]:
    out: set[str] = set()
    for i, p in enumerate(paragraphs):
        if bib_start <= i < bib_end:
            continue
        for m in CITATION_BRACKET_RE.finditer(p.text or ""):
            inner = m.group(1)
            if re.fullmatch(r"[\d\s,]+", inner):
                out.update(re.findall(r"\d+", inner))
    return out


# --------------------------------------------------------------- 校验（测试/自检用）


def visible_text_with_fields(path: str) -> str:
    """读 docx 的可见文字，**含域缓存**。

    `python-docx` 的 `para.text` 看不见域内文字，故导出结果无法用它校验。这里按
    文档顺序遍历 `w:t`（含 `fldSimple` / `fldChar` 内的），拼出用户在 Word 里
    真正看到的文本。仅供校验，不进主流水线。
    """
    import docx

    doc = docx.Document(path)
    lines = []
    for p in doc.paragraphs:
        lines.append("".join(t.text or "" for t in p._p.iter(_qn("w:t"))))
    return "\n".join(lines)


__all__ = [
    "CrossrefReport",
    "ImportReport",
    "bookmark_name",
    "export_crossrefs",
    "import_crossrefs",
    "visible_text_with_fields",
]
