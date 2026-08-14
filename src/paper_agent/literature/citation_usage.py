"""正文实际引用的持久化映射：哪一章引了哪篇文献（backlog P-2 的唯一真缺口）。

Ledger 存的是「我知道哪些文献」（known / resolvable / citable）；**正文实际引用**此前
只靠每次 regex 扫（`diff_gate.extract_citation_tokens`），不持久化、不按章节归属。
`ReferenceEntry.citation_aliases` 语义是「导入源文档使用的引用标记」，不是这个。

这不是新记忆子系统 —— 它是一张 `(reference_id, section_id)` 表，落在既有 facts.json
envelope 里，靠既有 `SectionIndex` 与 `Ledger.resolve_token` 计算。backlog P-2b 的结论
是记忆走文件路线、不建向量库；这里遵循它。

两条设计约束，都是从项目其他模块贯彻的纪律来的：

1. **解析不了的 token 必须留痕，不能静默丢。** `resolve_token` 返回 NOT_FOUND 或
   AMBIGUOUS 时记进 `unresolved`，理由同 `relevance.py` / `verification.py` 的
   「失败不伪装成结论」：把「引了一篇我不认识的文献」悄悄记成「本章没引」，
   会让上层的引用配额判定和 bibliography 生成都建立在假数据上。
2. **AMBIGUOUS 绝不任选。** `Ledger.resolve_token` 命中多条时明确不猜，这里照它办。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.literature.ledger import ResolutionStatus


@dataclass(frozen=True)
class SectionCitations:
    """单章的引用归属。

    `tokens` 保留正文里的原始标记（`[3]` / bibkey），因为同一 reference_id 可能被
    不同标记引到（数字别名 + bibkey），排障时需要知道正文里到底写的是哪个。
    """

    section_id: str
    reference_ids: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()
    #: 扫到但解析不了的标记 → 原因（`not_found` / `ambiguous`）。见模块 docstring 约束 1。
    unresolved: tuple[tuple[str, str], ...] = ()


@dataclass
class CitationUsage:
    """全文的「章节 → 文献」映射。整体重算、整体替换，不做增量。

    重算而非增量的理由：正文是可被任意意图改写的，增量维护要求每个写入路径都记得
    更新这张表 —— 那正是 `citation_aliases` 语义漂移的成因。全量重算是 O(全文)，
    对单篇论文的规模（万字级）可以忽略，换来的是「表与正文必然一致」。
    """

    by_section: dict[str, SectionCitations] = field(default_factory=dict)

    # ---- 查询 ----
    def sections_citing(self, reference_id: str) -> tuple[str, ...]:
        """哪些章节引了这篇（B-2 写 bibitem 时按此查表，不必再扫全文）。"""
        return tuple(
            sid for sid, sc in self.by_section.items()
            if reference_id in sc.reference_ids
        )

    def references_in(self, section_id: str) -> tuple[str, ...]:
        sc = self.by_section.get(section_id)
        return sc.reference_ids if sc else ()

    def all_cited_ids(self) -> tuple[str, ...]:
        """正文实际引用到的全部 reference_id（保序去重，供配额与 bibliography 用）。"""
        out: list[str] = []
        seen: set[str] = set()
        for sc in self.by_section.values():
            for rid in sc.reference_ids:
                if rid not in seen:
                    seen.add(rid)
                    out.append(rid)
        return tuple(out)

    def unresolved_tokens(self) -> tuple[tuple[str, str, str], ...]:
        """全部解析失败项 `(section_id, token, reason)`，供上层报给用户。"""
        return tuple(
            (sid, tok, why)
            for sid, sc in self.by_section.items()
            for tok, why in sc.unresolved
        )

    # ---- 持久化（facts.json envelope 的一段）----
    def to_json(self) -> list[dict]:
        return [
            {
                "section_id": sc.section_id,
                "reference_ids": list(sc.reference_ids),
                "tokens": list(sc.tokens),
                "unresolved": [list(u) for u in sc.unresolved],
            }
            for sc in self.by_section.values()
        ]

    @classmethod
    def from_json(cls, rows) -> "CitationUsage":
        """宽容加载：损坏行跳过而非抛。

        同 `SessionFacts.load` 对 `pending_literature_flows` 的处理 —— 这张表是可以
        从正文重算的**派生数据**，为一条坏记录让整个 session 打不开是不划算的。
        """
        usage = cls()
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            sid = row.get("section_id")
            if not sid:
                continue
            unresolved = tuple(
                (str(u[0]), str(u[1]))
                for u in (row.get("unresolved") or ())
                if isinstance(u, (list, tuple)) and len(u) >= 2
            )
            usage.by_section[sid] = SectionCitations(
                section_id=sid,
                reference_ids=tuple(row.get("reference_ids") or ()),
                tokens=tuple(row.get("tokens") or ()),
                unresolved=unresolved,
            )
        return usage


def scan_usage(text: str, ext: str, ledger) -> CitationUsage:
    """扫全文，算出「哪章引了哪篇」。确定性、零 LLM、零网络。

    不支持章节的格式（`SectionIndex.supports_sections` 为假）会把整篇归到一个
    伪章节 `_document`，而不是返回空表 —— 「这个格式我分不出章节」和「这篇没有引用」
    是两件事，后者会让 `all_cited_ids()` 谎报为空。
    """
    from paper_agent.session.section_index import SectionIndex
    from paper_agent.gates.diff_gate import extract_citation_tokens

    index = SectionIndex(text, ext)
    if index.supports_sections and index.list():
        regions = [(s.id, index.body_of(s)) for s in index.list()]
    else:
        regions = [("_document", text)]

    usage = CitationUsage()
    for sid, body in regions:
        rids: list[str] = []
        toks: list[str] = []
        unresolved: list[tuple[str, str]] = []
        for tok in extract_citation_tokens(body, ext):
            res = ledger.resolve_token(tok, ext=ext)
            if res.status is ResolutionStatus.RESOLVED:
                toks.append(tok)
                if res.reference_id not in rids:
                    rids.append(res.reference_id)
            else:
                # AMBIGUOUS 不任选（见模块 docstring 约束 2），NOT_FOUND 同样留痕。
                unresolved.append((tok, res.status.value))
        if rids or unresolved:
            usage.by_section[sid] = SectionCitations(
                section_id=sid,
                reference_ids=tuple(rids),
                tokens=tuple(toks),
                unresolved=tuple(unresolved),
            )
    return usage
