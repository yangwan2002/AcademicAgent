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
- 排序按 md 数字别名的**数值**升序（`[2]` 在 `[10]` 前）；tex 按正文首次出现顺序。
- 整个参考文献区**每次重建**而非增量追加 —— 增量会在反复改写后留下已删引用的僵尸条目。
- 区块由显式标记界定，重建只替换标记之间的内容，标记之外逐字不动。

## 边界

字段不全时按 `citation_styles` 退化渲染，并如实报告 `incomplete`。
账本里查不到的引用标记进 `unresolved`，**不臆造条目** —— 悬空引用要让用户看见，
而不是被一条编出来的著录掩盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.gates.diff_gate import extract_citation_tokens
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


def _sorted_records(tokens, ledger, ext: str):
    """把正文标记解析成记录，按方言约定排序，保序去重。"""
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

    if not is_tex(ext):
        # 数字别名按数值排序：`[2]` 应在 `[10]` 之前（字符串序会反过来）。
        def _key(item):
            tok = item[0]
            return (0, int(tok)) if tok.isdigit() else (1, tok)
        pairs.sort(key=_key)
    return pairs, unresolved


def render_bibliography(tokens, ledger, ext: str, *, style_name: str = "") -> tuple[str, SyncReport]:
    """渲染参考文献区正文（不含区块标记）。返回 (文本, 报告)。"""
    style = get_style(style_name)
    pairs, unresolved = _sorted_records(tokens, ledger, ext)
    report = SyncReport(unresolved=unresolved)

    lines: list[str] = []
    for _token, record in pairs:
        marker = marker_for(record, ext)
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


def apply_bibliography(text: str, ext: str, ledger, *, style_name: str = ""):
    """纯文本版：返回 (新文本, 报告)，**不写文件**。

    供 rewrite / add_section 在 splice 之后、写入之前调用，使正文与参考文献区
    落在**同一次** `write_working` 里 —— 否则 `:undo` 一步只能回退其中一半，
    正文引用还在而文献区已撤销，又成悬空引用。

    `ledger` 为 None 或正文无引用时原样返回，不报错（不带文献功能的旧路径照旧可用）。
    """
    if ledger is None:
        return text, None
    try:
        tokens = _citation_tokens_in_body(text, ext)
        if not tokens:
            return text, None
        body, report = render_bibliography(tokens, ledger, ext, style_name=style_name)
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
    """
    text = session.read_working()
    ext = session.ext
    tokens = _citation_tokens_in_body(text, ext)

    if not tokens:
        return SyncReport(message="正文没有引用，无需生成参考文献区。")

    body, report = render_bibliography(tokens, ledger, ext, style_name=style_name)

    if is_tex(ext):
        env = ("\\begin{thebibliography}{99}\n" + body + "\n\\end{thebibliography}")
        new_text, changed = _replace_block(text, _TEX_BEGIN, _TEX_END, env)
    else:
        new_text, changed = _replace_block(
            text, _MD_BEGIN, _MD_END, f"{_MD_HEADING}\n\n{body}")

    if changed and body:
        session.write_working(new_text, message="sync_bibliography")
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


__all__ = ["SyncReport", "render_bibliography", "sync_bibliography"]
