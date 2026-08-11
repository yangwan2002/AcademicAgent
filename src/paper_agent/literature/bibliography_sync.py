"""按正文实际引用重建参考文献区（md / docx / tex 通用）。

## 问题

真机跑完一轮 rewrite，正文里有 `[6]`、`[12, 21]`，但文末**没有任何参考文献条目** ——
`tools/bibliography.py` 第 33 行 `if session.ext != "tex": return`，md 直接不做；
而 rewrite / add_section 提交时只写章节正文，从不碰文末。于是引用全是悬空的。
账本里这些文献的元数据是齐的（标题/作者/年份都在），只是没人负责渲染。

## 口径

- **引用是唯一真源**：只列正文真的引用了的文献，不把整个账本倒出来。
  多余条目和缺失条目一样是错。
- 标记形态走 `dialect`（md 数字 / tex bibkey），与 `build_evidence_block` 同源。
- md/docx 走**顺序编码**：正文标记按首次出现顺序重编为 1..N，文献表同序。
  详见 `renumber_sequentially`。tex 不动（编号由 LaTeX 自己排）。
- 整个参考文献区**每次重建**而非增量追加 —— 增量会在反复改写后留下已删引用的僵尸条目。
- 区块由显式标记界定，重建只替换标记之间的内容，标记之外逐字不动。

## 为什么顺序编码必须同时改正文

账本的数字别名是**入库流水号**（`ledger.next_numeric_alias`），不是引用序号。模型
只能从公布的可引用 token 里挑，它写 `[7][3]` 是合规的 —— 但成稿要求 1..N 连续。
真机症状：库里 8 篇被引 4 篇，文献表输出 `[1] [3] [5] [7]`，**跳号**，不能交付。

所以重编号要正文标记、文献表、账本别名**三者一起**改。只改正文会切断
「正文 `[n]` ⇔ 账本记录」的身份对应：正文 `[7]`→`[1]` 而账本里 `1` 仍属另一条，
下一次 `resolve_token("1")` 就解析到**别的文献**。症状是同步跑第二次文献表整体错位，
后续 rewrite / 配额 Gate 一并跟着错。换完账本满足「正文 `[n]` 解析回第 n 条」，故幂等。

## 边界

字段不全时按 `citation_styles` 退化渲染，并如实报告 `incomplete`。
账本里查不到的引用标记进 `unresolved`，**不臆造条目** —— 悬空引用要让用户看见，
而不是被一条编出来的著录掩盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paper_agent.gates.diff_gate import CITATION_BRACKET_RE, extract_citation_tokens
from paper_agent.literature.citation_styles import get_style
from paper_agent.literature.dialect import is_tex, marker_for, style_for

# 显式区块标记：重建只动标记之间，用户在标记外写的内容不受影响。
_MD_BEGIN = "<!-- BIBLIOGRAPHY:BEGIN (自动同步，勿手改区块内内容) -->"
_MD_END = "<!-- BIBLIOGRAPHY:END -->"
_MD_HEADING = "## 参考文献"

_TEX_BEGIN = "% BIBLIOGRAPHY:BEGIN (自动同步)"
_TEX_END = "% BIBLIOGRAPHY:END"


@dataclass
class SyncReport:
    listed: list[str] = field(default_factory=list)       # 已列出的 reference_id
    unresolved: list[str] = field(default_factory=list)   # 正文有标记、账本查不到
    incomplete: list[str] = field(default_factory=list)   # 字段不全（已退化渲染）
    changed: bool = False
    message: str = ""


def _sorted_records(tokens, ledger, ext: str, *, sequential: bool = False):
    """把正文标记解析成记录，按方言约定排序，保序去重。

    `sequential=True`（顺序编码）时**必须保序** —— 条目编号取的是位置序号 1..N，
    再按数字别名重排会让表里的号与正文标记拧反。
    `sequential=False` 保持旧口径：md 按数字别名数值升序（`[2]` 在 `[10]` 前），
    tex 按首次出现顺序。这条路径仍被直接调 `render_bibliography` 的地方用着。
    """
    seen: set[str] = set()
    pairs: list[tuple[str, object]] = []
    unresolved: list[str] = []
    for token in tokens:
        res = ledger.resolve_token(token, ext=ext)
        if res.status.name != "RESOLVED":
            if token not in unresolved:
                unresolved.append(token)
            continue
        rid = res.reference_id
        if rid in seen:
            continue
        record = ledger.get(rid)
        if record is None:
            if token not in unresolved:
                unresolved.append(token)
            continue
        seen.add(rid)
        pairs.append((token, record))

    if not sequential and not is_tex(ext):
        # 数字别名按数值排序：`[2]` 应在 `[10]` 之前（字符串序会反过来）。
        def _key(item):
            tok = item[0]
            return (0, int(tok)) if tok.isdigit() else (1, tok)
        pairs.sort(key=_key)
    return pairs, unresolved


def renumber_sequentially(text: str, ext: str, ledger) -> tuple[str, dict[str, str]]:
    """把正文 `[n]` 与账本数字别名**一起**按首次出现顺序重编为 1..N。

    返回 `(新正文, 旧号→新号)`。只动 md/docx；tex 原样返回（`\\cite{bibkey}` 的编号
    由 LaTeX 排，改它反而错）。

    两边一起改是硬要求：只改正文会让 `resolve_token("1")` 指向另一条记录，同步跑第二次
    文献表就整体错位（见模块 docstring）。改完的账本满足「正文 `[n]` 解析回第 n 条」，
    因此可反复执行。

    五条边界，每条都对应一个真实故障：

    1. **文献区不参与出现顺序** —— 文献区自身含 `[n]`，算进去的话编号由上一轮的表决定
       而非正文，反复改写会漂移。故先切开，只在正文段内定序与改写。
    2. **未被引记录也要让位** —— 被引的排 1..N，未被引的顺延到 N+1.. 。否则「被引的 7→1」
       而未被引者仍占着 `1`，`resolve_token("1")` 立刻 AMBIGUOUS（实测必现）。
    3. **别名整体落盘** —— 先算完整排列再统一 `replace`，不能逐条改：中途 `1` 还被别人
       占着，边改边查必撞号。
    4. **解析不到的标记不动** —— 悬空引用原样留下进 `unresolved` 让用户看见；重编号它
       等于把一个错标记换成另一个错标记，还占掉一个正经序号。
    5. **正文单次 `re.sub`** —— 按映射一次改写，不能逐对 replace：`7→1` 之后再 `1→3`
       会把刚写好的 `1` 二次改写（链式踩踏）。

    同一条记录被多个旧号指向时（账本合并留下多别名），它们映射到**同一个**新号；
    重编后该记录只保留这一个数字别名，旧号不留 —— 留着就是撞号源。
    """
    if is_tex(ext) or ledger is None:
        return text, {}

    i, j = text.find(_MD_BEGIN), text.find(_MD_END)
    if i >= 0 and j > i:
        body, tail = text[:i], text[i:]
    else:
        body, tail = text, ""

    # 旧号 → 新号：按正文首次出现顺序给 reference_id 发号，同一记录共享一个号。
    rid_order: list[str] = []
    mapping: dict[str, str] = {}
    for token in extract_citation_tokens(body, ext):
        res = ledger.resolve_token(token, ext=ext)
        if res.status.name != "RESOLVED":
            continue                      # 边界 4：悬空标记不动
        rid = res.reference_id
        if rid not in rid_order:
            rid_order.append(rid)
        mapping[token] = str(rid_order.index(rid) + 1)

    if not mapping:
        return text, {}

    # 边界 2：未被引记录顺延到 N+1..，腾出 1..N 给被引的。
    cited = set(rid_order)
    uncited = [r.reference_id for r in ledger.all_records() if r.reference_id not in cited]
    new_alias = {rid: str(k) for k, rid in enumerate(rid_order + uncited, start=1)}

    # 边界 3：整排算完再统一落盘。
    for rid, alias in new_alias.items():
        rec = ledger.get(rid)
        if rec is None or rec.numeric_aliases == (alias,):
            continue
        ledger.replace(rec.with_changes(numeric_aliases=(alias,)))

    def _sub(m: re.Match) -> str:
        # 组 1 可能是 `12, 21` 这样的多引用；**逐项**映射（见 docstring 混合组），
        # 映射不到的原样保留，再按规范分隔重组。
        parts = [p.strip() for p in m.group(1).split(",")]
        if not any(p in mapping for p in parts):
            return m.group(0)             # 一个都映射不到：整段不动
        return m.group(0).replace(
            m.group(1), ", ".join(mapping.get(p, p) for p in parts))

    return CITATION_BRACKET_RE.sub(_sub, body) + tail, mapping


def render_bibliography(tokens, ledger, ext: str, *, style_name: str = "",
                        sequential: bool = False) -> tuple[str, SyncReport]:
    """渲染参考文献区正文（不含区块标记）。返回 (文本, 报告)。

    `sequential=True`（md/docx 顺序编码）时条目编号取**在 `tokens` 里的首次出现次序**
    1..N，而不是账本别名 —— 与 `renumber_sequentially` 改写后的正文标记一致。两者都由
    同一份 `tokens` 定序，故必然对齐；`test_body_and_bibliography_markers_agree` 钉住这点。
    """
    style = get_style(style_name)
    pairs, unresolved = _sorted_records(tokens, ledger, ext, sequential=sequential)
    report = SyncReport(unresolved=unresolved)

    lines: list[str] = []
    for idx, (_token, record) in enumerate(pairs, start=1):
        marker = str(idx) if sequential and not is_tex(ext) else marker_for(record, ext)
        if not marker:
            continue
        text = style.render_text(record)
        missing = style.missing(record)
        if missing:
            report.incomplete.append(record.reference_id)
        report.listed.append(record.reference_id)
        if is_tex(ext):
            lines.append(f"\\bibitem{{{marker}}} {text}")
        else:
            lines.append(f"{style_for(ext).render(marker)} {text}")
    return "\n".join(lines), report


def _replace_block(text: str, begin: str, end: str, body: str) -> tuple[str, bool]:
    """替换 begin/end 之间的内容；不存在则在文末追加。返回 (新文本, 是否变化)。"""
    i = text.find(begin)
    j = text.find(end)
    if i >= 0 and j > i:
        new = text[: i + len(begin)] + "\n" + body + "\n" + text[j:]
        return new, new != text
    tail = f"\n\n{begin}\n{body}\n{end}\n"
    return text.rstrip("\n") + tail, True


def _strip_block(text: str, begin: str, end: str) -> str:
    """抽引用前先去掉已有文献区。

    否则文献区自身的 `[2] Author. Title.` 会被当成正文引用 ⇒ 已删掉的引用靠自己的
    旧条目复活，条目永远删不掉（僵尸）。抽取的真源只能是**正文**。
    """
    i = text.find(begin)
    j = text.find(end)
    if i >= 0 and j > i:
        return text[:i] + text[j + len(end):]
    return text


def _citation_tokens_in_body(text: str, ext: str):
    """正文（排除文献区）里的引用标记。"""
    if is_tex(ext):
        stripped = _strip_block(text, _TEX_BEGIN, _TEX_END)
    else:
        stripped = _strip_block(text, _MD_BEGIN, _MD_END)
    return extract_citation_tokens(stripped, ext)


def _write_working(session, text: str, aliases) -> None:
    """写回正文，并把重编号前的别名快照与这次写入配对入 undo 栈。

    `undo_hook` 是较新的可选参数，老 session 替身没有它 —— 故先带着传，
    `TypeError` 时退回不带（撤销的别名回退失效，但正文照常写，不因此中断）。
    """
    if aliases is None:
        session.write_working(text, message="sync_bibliography")
        return
    try:
        session.write_working(
            text, message="sync_bibliography", undo_hook=lambda: aliases)
    except TypeError:
        session.write_working(text, message="sync_bibliography")


def apply_bibliography(text: str, ext: str, ledger, *, style_name: str = ""):
    """纯文本版：返回 (新文本, 报告)，**不写文件**。

    供 rewrite / add_section 在 splice 之后、写入之前调用，使正文与参考文献区
    落在**同一次** `write_working` 里 —— 否则 `:undo` 一步只能回退其中一半，
    正文引用还在而文献区已撤销，又成悬空引用。

    md/docx 走顺序编码：正文标记与账本别名**一起**重排（`renumber_sequentially`），
    之后 `resolve_token("1")` 仍回到正文 `[1]` 指的那条记录，故可反复执行（幂等）。

    `ledger` 为 None 或正文无引用时原样返回，不报错（不带文献功能的旧路径照旧可用）。
    """
    if ledger is None:
        return text, None
    try:
        if not _citation_tokens_in_body(text, ext):
            return text, None
        text, remap = renumber_sequentially(text, ext, ledger)
        tokens = _citation_tokens_in_body(text, ext)
        body, report = render_bibliography(
            tokens, ledger, ext, style_name=style_name, sequential=bool(remap))
        if not body:
            return text, report
        if is_tex(ext):
            block = ("\\begin{thebibliography}{99}\n" + body
                     + "\n\\end{thebibliography}")
            new_text, changed = _replace_block(text, _TEX_BEGIN, _TEX_END, block)
        else:
            new_text, changed = _replace_block(
                text, _MD_BEGIN, _MD_END, f"{_MD_HEADING}\n\n{body}")
        report.changed = changed
        parts = [f"参考文献区已同步 {len(report.listed)} 条"]
        if report.unresolved:
            parts.append(
                f"{len(report.unresolved)} 个标记账本里查不到"
                f"（{', '.join(report.unresolved[:5])}），未列入")
        if report.incomplete:
            parts.append(f"{len(report.incomplete)} 条缺期刊/卷期页，已简化著录")
        report.message = "；".join(parts)
        return new_text, report
    except Exception:  # noqa: BLE001 - 文献区同步失败不该让整次写作白费
        return text, None


def sync_bibliography(session, ledger, *, style_name: str = "") -> SyncReport:
    """按 working copy 里的实际引用重建参考文献区。写入 working copy（不动源文件）。

    tex 侧继续用 `thebibliography` 环境；md/docx 用 `## 参考文献` + HTML 注释界定。
    md/docx 同 `apply_bibliography` 走顺序编码，正文标记与文献表在**同一次**
    `write_working` 里落盘（分两次写会在 undo 时留下半截状态）。
    """
    original = session.read_working()
    ext = session.ext
    if not _citation_tokens_in_body(original, ext):
        return SyncReport(message="正文没有引用，无需生成参考文献区。")

    # 重编号前的别名快照：与正文写入配对入 undo 栈，`:undo` 时两者一起回退
    # （只回正文会让旧号解析到别的文献）。取快照必须在 renumber 之前。
    before_aliases = (
        ledger.numeric_alias_snapshot()
        if hasattr(ledger, "numeric_alias_snapshot") else None
    )

    text, remap = renumber_sequentially(original, ext, ledger)
    tokens = _citation_tokens_in_body(text, ext)

    body, report = render_bibliography(
        tokens, ledger, ext, style_name=style_name, sequential=bool(remap))

    if is_tex(ext):
        env = ("\\begin{thebibliography}{99}\n" + body + "\n\\end{thebibliography}")
        new_text, changed = _replace_block(text, _TEX_BEGIN, _TEX_END, env)
    else:
        new_text, changed = _replace_block(
            text, _MD_BEGIN, _MD_END, f"{_MD_HEADING}\n\n{body}")

    # `changed` 只看文献区块变没变，会漏掉「正文重编号了、但块渲染结果恰好一样」
    # 这一种 —— 那时不写盘就把重编号后的正文丢了。故与原文整体比对。
    changed = changed or new_text != original
    if changed and body:
        _write_working(session, new_text, before_aliases if remap else None)
    report.changed = changed and bool(body)

    parts = [f"参考文献区已同步 {len(report.listed)} 条"]
    if report.unresolved:
        parts.append(
            f"{len(report.unresolved)} 个引用标记在账本里查不到"
            f"（{', '.join(report.unresolved[:5])}），未列入，请核查")
    if report.incomplete:
        parts.append(
            f"{len(report.incomplete)} 条字段不全（缺期刊/卷期页），"
            f"已按现有字段简化著录")
    report.message = "；".join(parts)
    return report


__all__ = [
    "SyncReport", "render_bibliography", "sync_bibliography",
    "apply_bibliography", "renumber_sequentially",
]
