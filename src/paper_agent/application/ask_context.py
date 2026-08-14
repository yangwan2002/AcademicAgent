"""只读问答的上下文快照与工具集（P-4 第一步）。

**只读是结构性保证，不是约定。** `AskContext` 是一份 frozen 快照，捕获时就把需要
的文本取出来；它**不持有 session**，所以工具闭包里根本没有 `write_working` 可调。
这比"约定工具不写"强：想写也没有对象可写。

工具集刻意从小起步（`list_sections` / `read_section` / `doc_stats`），先真机问一轮，
再按**实测缺口**补，而不是先猜一堆工具再写测试把猜的形状钉死。第二步 24 问真机
评测后补了 `edit_history`：历史类 3 问全部答不上来，而信息其实一直在 session 的
git commit 里（`polish <id>` 等），缺的只是读取通路。

`read_section` 超限时**显式报告截断**（`[已截断：仅前 N 字，全长 M 字]`），不静默截。
静默截断会让模型以为自己看完了整章，然后据此下断言 —— 降级必须可见。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.session.section_index import SectionIndex
from paper_agent.tools.doc_stats import is_non_body_heading, stats_from_text

#: `read_section` 单次返回的字符上限。超出则截断并显式告知。
READ_SECTION_MAX_CHARS = 4000

#: 捕获的编辑历史条数上限。够答"刚才改了什么""改过几次"，又不会撑爆上下文。
HISTORY_MAX_ENTRIES = 30

#: 二进制/无解析器格式（docx 等）在 chat 侧构建为空索引，这里如实反映为"无章节"。
_NO_SECTIONS_NOTE = "当前文档没有可识别的章节（可能是 docx 等未提供章节解析的格式）。"


@dataclass(frozen=True)
class SectionInfo:
    """一个章节的**摘要**信息：不含正文。

    带 `chars` 是为了让"哪章最短/最长""哪章还没写"这类问题只用 `list_sections`
    一个工具就能答，不必把正文拉进上下文。
    """

    id: str
    title: str
    level: int
    chars: int
    #: 参考文献/致谢/附录。这类章节不参与"正文有几章""哪章最长"的口径 ——
    #: 实测问"哪一章最长"被答成"参考文献 621 字"，模型还得自己找补。
    non_body: bool = False

    def as_line(self) -> str:
        indent = "  " * max(0, self.level - 1)
        tag = "（非正文）" if self.non_body else ""
        return f"{indent}[{self.id}] {self.title}{tag} — {self.chars} 字"


@dataclass(frozen=True)
class AskContext:
    """只读快照。捕获后与 session 完全脱钩。"""

    ext: str
    sections: tuple[SectionInfo, ...] = ()
    stats: dict = field(default_factory=dict)
    total_chars: int = 0
    #: section_id → 正文。捕获时一次取出，工具从这里读，不回头碰文件。
    bodies: dict = field(default_factory=dict)
    #: 本会话编辑历史（倒序）。捕获时取出，与 bodies 同口径 —— 工具拿不到 session。
    history: tuple = ()
    #: 历史不可用的原因（git 未启用等）。非空时 `edit_history` 如实说明。
    history_note: str = ""
    #: section_id → (标题串, ...)：哪一章引了哪几篇（P-2 CitationUsage 的渲染结果）。
    #: 捕获时算好并**摊平成字符串**，与 bodies 同口径 —— 工具拿不到 facts/ledger。
    citations: dict = field(default_factory=dict)
    #: 扫到但解析不了的引用标记 `(section_id, token, reason)`。空 ≠ 没问题（见 note）。
    citation_unresolved: tuple = ()
    #: 引用归属不可用的原因（未传 facts 等）。区分"没引用"与"查不到"。
    citation_note: str = ""

    # ---- 章节口径 ----

    @property
    def body_sections(self) -> tuple[SectionInfo, ...]:
        """正文章节（不含参考文献/致谢/附录）。"""
        return tuple(s for s in self.sections if not s.non_body)

    # ---- 事实摘要：预加载进 system 的那一小段 ----

    def facts_digest(self) -> str:
        """给 LLM 的文档事实摘要。刻意只有结构与计数，不含正文。

        计数部分**分开报**而不合并：正文引用与文献表条目是两个数，相加没有意义
        （曾把 10 次出现 + 8 条条目报成"引用 18"）；行内 `$x$` 是变量符号，
        与块级公式分列，不然 `$I_a$` 这种会被算成公式。
        """
        lines = [f"文档格式：{self.ext or '未知'}；全文 {self.total_chars} 字。"]
        if self.sections:
            body = self.body_sections
            extra = len(self.sections) - len(body)
            head = f"共 {len(body)} 个正文章节"
            if extra:
                head += f"，另有 {extra} 个非正文章节（参考文献/致谢/附录，已标注）"
            lines.append(head + "：")
            lines.extend("  " + s.as_line() for s in self.sections)
        else:
            lines.append(_NO_SECTIONS_NOTE)
        if self.stats:
            lines.append(_stats_line(self.stats))
        return "\n".join(lines)


def _stats_line(stats: dict) -> str:
    """把计数渲染成一行。各项口径写明，避免模型把两个数相加或混淆。"""
    parts = [
        f"正文引用 {stats.get('unique_citations', 0)} 个不同编号",
        f"文献表 {stats.get('bib_entries', 0)} 条",
        f"块级公式 {stats.get('display_formulas', 0)}",
        f"行内数学符号 {stats.get('inline_math', 0)}（如 $x$，不是公式）",
        f"表 {stats.get('tables', 0)}",
        f"图 {stats.get('figures', 0)}",
    ]
    return "计数：" + "、".join(parts)


def capture(session, facts=None) -> AskContext:
    """从 session 取一份只读快照。**这是唯一接触 session 的地方。**

    只调 `read_working()`（读）。返回后 session 不再被引用，工具拿不到它。

    `facts` 可选：给了就顺带算出「哪章引了哪篇」（P-2 CitationUsage）。同样只读 ——
    这里**不写回** `facts.citation_usage`、不 save，因为 `:ask` 必须是零副作用的。
    """
    ext = getattr(session, "ext", "") or ""
    try:
        text = session.read_working()
    except OSError:
        # 读不到就给空快照：问答降级为"没有内容可读"，而不是让整个循环炸掉。
        return AskContext(ext=ext)

    index = SectionIndex(text, ext) if ext else SectionIndex(text, "md")
    infos: list[SectionInfo] = []
    bodies: dict[str, str] = {}
    # 非正文章节的**子节**也算非正文（附录 A.1 之类）：靠层级判断，遇到层级不深于
    # 标记点的标题就退出跳过状态。
    skip_level: int | None = None
    for s in index.list():
        body = index.body_of(s)
        bodies[s.id] = body
        if skip_level is not None and s.level <= skip_level:
            skip_level = None
        non_body = skip_level is not None
        if not non_body and is_non_body_heading(s.title):
            non_body = True
            skip_level = s.level
        infos.append(
            SectionInfo(
                id=s.id, title=s.title, level=s.level,
                chars=len(body.strip()), non_body=non_body,
            )
        )

    try:
        stats = stats_from_text(text, ext).as_dict() if ext else {}
    except Exception:  # noqa: BLE001 - 统计是辅助信息，算不出不该拖垮问答
        stats = {}

    history, history_note = _capture_history(session)
    citations, unresolved, cite_note = _capture_citations(text, ext, facts)

    return AskContext(
        ext=ext,
        sections=tuple(infos),
        stats=stats,
        total_chars=len(text),
        bodies=bodies,
        history=history,
        history_note=history_note,
        citations=citations,
        citation_unresolved=unresolved,
        citation_note=cite_note,
    )


def _capture_citations(text: str, ext: str, facts) -> tuple[dict, tuple, str]:
    """算「哪章引了哪篇」并摊平成标题串。

    摊平在这里做（而非工具里）是为了保持 AskContext「捕获后与 session 完全脱钩」的
    约束：工具只能碰快照，不能反过来查 ledger。

    没传 facts 时返回说明而非空 dict —— 「这篇没有引用」和「我查不到引用归属」
    是两件事，措辞不同（同 `_capture_history` 的先例）。
    """
    if facts is None:
        return {}, (), "当前会话没有提供文献数据，无法回答哪一章引了哪篇。"
    try:
        from paper_agent.literature.citation_usage import scan_usage

        usage = scan_usage(text, ext or "md", facts.ledger)
    except Exception:  # noqa: BLE001 - 引用归属是辅助信息，算不出不该拖垮问答
        return {}, (), "计算引用归属失败。"

    titles: dict[str, tuple[str, ...]] = {}
    for sid, sc in usage.by_section.items():
        rendered = []
        for rid in sc.reference_ids:
            rec = facts.ledger.get(rid)
            # 取不到标题就退回 reference_id：宁可显示一串哈希，也不能悄悄少一篇。
            rendered.append(((rec.title or "").strip() if rec else "") or rid)
        titles[sid] = tuple(rendered)
    return titles, usage.unresolved_tokens(), ""


def _capture_history(session) -> tuple[tuple, str]:
    """取一份只读的编辑历史快照。

    session 未实现 `history()`（测试替身、旧版本）或 git 不可用时，返回空 + 说明。
    **区分"没有历史"与"读不到历史"**：前者是文档确实没被改过，后者是能力缺失，
    答案措辞不同，不能都说成"看不出来"。
    """
    getter = getattr(session, "history", None)
    if not callable(getter):
        return (), "当前会话没有提供编辑历史（该 session 不支持历史查询）。"
    try:
        entries = getter(limit=HISTORY_MAX_ENTRIES)
    except Exception:  # noqa: BLE001 - 历史是辅助信息，读不到不该拖垮问答
        return (), "读取编辑历史失败。"
    if not entries:
        # `git_enabled` 是 property，取值即可（不是方法）。取不到就按"没改过"说。
        git_on = getattr(session, "git_enabled", True)
        if git_on is False:
            note = "本会话未启用 git 版本记录，因此没有可查的编辑历史。"
        else:
            note = "本会话还没有任何编辑记录（文档自载入后未被修改）。"
        return (), note
    return tuple(entries), ""


# ---- 工具 schema ----
#
# 每个都必须带 `"type": "function"`：缺了会让 OpenAI 端点返回
# invalid_request_error，而 `structured_call._looks_unsupported()` 会把它误判成
# "provider 不支持 forced tool" → capability 降级 + 重试，真实错误被吞掉、成本翻倍。
# 这些 schema 要一并纳入 tests/test_tool_schema_protocol.py 的遍历。

_LIST_SECTIONS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_sections",
        "description": (
            "列出文档全部章节的 id、标题、层级与字数。不返回正文。"
            "回答『有哪些章节』『哪章最短』『哪章还没写』用这个就够，不要再读正文。"
            "标了『（非正文）』的是参考文献/致谢/附录：谈『正文有几章』"
            "『哪章最长』时应排除它们。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

_READ_SECTION_TOOL = {
    "type": "function",
    "function": {
        "name": "read_section",
        "description": (
            "读取单个章节的正文。仅在确实需要看具体文字时调用（例如判断写得好不好、"
            f"语言是否通顺）。返回值超过 {READ_SECTION_MAX_CHARS} 字会被截断并明确标注。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "section_id": {
                    "type": "string",
                    "description": "章节 id，取自 list_sections 的返回值。",
                }
            },
            "required": ["section_id"],
        },
    },
}

_DOC_STATS_TOOL = {
    "type": "function",
    "function": {
        "name": "doc_stats",
        "description": (
            "返回正文章节数、正文引用的不同编号数、参考文献条目数、块级公式数、"
            "行内数学符号数、表格数、图数。"
            "注意：『正文引用编号数』与『文献表条目数』是两个不同的数，不要相加；"
            "行内 $x$ 是变量符号，不算公式。这些是正则计数的近似值。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

_EDIT_HISTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_history",
        "description": (
            "返回本会话的编辑历史（最近在前）：每次做了什么操作、改的哪一章、"
            "多久以前。回答『我刚才改了什么』『这篇改过几次』『上次润色的是哪一章』"
            "用这个。只覆盖**本次会话**，不含载入前的历史。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"最多返回几条，默认 10，上限 {HISTORY_MAX_ENTRIES}。",
                }
            },
            "required": [],
        },
    },
}

_SECTION_CITATIONS_TOOL = {
    "type": "function",
    "function": {
        "name": "section_citations",
        "description": (
            "返回每一章引用了哪几篇文献（按标题）。回答『引言引了哪些文献』"
            "『哪一章引用最多』『某篇文献在哪几章被引』用这个，不要去读正文数方括号。"
            "不传 section_id 则返回全部章节。"
            "注意：这是**正文实际引用**，与文献表条目数是两个口径。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "section_id": {
                    "type": "string",
                    "description": "只看某一章时给，取自 list_sections；省略则返回全部。",
                }
            },
            "required": [],
        },
    },
}

ASK_TOOLS = (
    _LIST_SECTIONS_TOOL,
    _READ_SECTION_TOOL,
    _DOC_STATS_TOOL,
    _EDIT_HISTORY_TOOL,
    _SECTION_CITATIONS_TOOL,
)

#: commit message 前缀 → 人话。历史是给人看的，`polish 3_2_x` 直接抛出去等于没答。
_OP_LABELS = {
    "polish": "润色",
    "rewrite": "改写",
    "add_section": "新增章节",
    "sync_bibliography": "同步参考文献",
    # 历史兼容：`fix_bibliography` 这个意图已删除（B-2），但**老项目的 git 历史里
    # 仍有以它为 message 的 commit**。留着这条映射，否则那些历史条目会显示成原始
    # 英文。新写入不再产生这个前缀。
    "fix_bibliography": "修正参考文献",
    "format": "调整排版",
    "edit": "编辑",
}


def _describe_history_entry(entry: dict, ctx: AskContext) -> str:
    """把一条 commit 渲染成人能读的一行。

    `polish 3_2_语义引导的候选生成` → `润色「3.2 语义引导的候选生成」`。
    章节 id 能对上现有章节就换成标题；对不上（章节后来被改名/删了）就保留 id，
    不猜。
    """
    msg = (entry.get("message") or "").strip()
    op, _, rest = msg.partition(" ")
    label = _OP_LABELS.get(op, op or "改动")
    target = rest.strip()
    if target:
        titles = {s.id: s.title for s in ctx.sections}
        target = f"「{titles.get(target, target)}」"
    rel = entry.get("relative") or ""
    added = entry.get("lines_added") or 0
    removed = entry.get("lines_removed") or 0
    delta = ""
    if added or removed:
        delta = f"，+{added}/-{removed} 行"
    when = f"（{rel}{delta}）" if rel or delta else ""
    return f"{label}{target}{when}".strip()


class ToolDispatchError(Exception):
    """工具参数不合法。循环把它作为 tool_result 回灌给模型，让模型自己纠正。"""


def dispatch(ctx: AskContext, name: str, arguments: dict) -> str:
    """执行一个只读工具，返回给模型看的文本。

    未知工具名/坏参数 → 抛 `ToolDispatchError`，由循环转成 tool_result 文本回灌。
    不静默返回空串：模型必须知道自己调错了，才有机会改。
    """
    if name == "list_sections":
        if not ctx.sections:
            return _NO_SECTIONS_NOTE
        return "\n".join(s.as_line() for s in ctx.sections)

    if name == "doc_stats":
        if not ctx.stats:
            return "没有可用的统计信息（该格式未提供计数）。"
        body_n = len(ctx.body_sections) or ctx.stats.get("body_sections", 0)
        return (
            f"正文章节 {body_n}（另有非正文章节 "
            f"{len(ctx.sections) - len(ctx.body_sections)} 个）、"
            + _stats_line(ctx.stats).removeprefix("计数：")
            + "（正则近似计数）"
        )

    if name == "edit_history":
        if not ctx.history:
            # 如实区分"没改过"与"读不到" —— 措辞不同，别一律说"看不出来"。
            return ctx.history_note or "本会话还没有任何编辑记录。"
        raw = (arguments or {}).get("limit", 10)
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            raise ToolDispatchError(
                "edit_history 的 limit 必须是整数。"
            ) from None
        limit = max(1, min(limit, HISTORY_MAX_ENTRIES))
        picked = ctx.history[:limit]
        lines = [
            f"本会话共 {len(ctx.history)} 次编辑，最近 {len(picked)} 次（新→旧）："
        ]
        lines += [
            f"  {i}. {_describe_history_entry(e, ctx)}"
            for i, e in enumerate(picked, 1)
        ]
        return "\n".join(lines)

    if name == "read_section":
        sid = (arguments or {}).get("section_id")
        if not isinstance(sid, str) or not sid.strip():
            raise ToolDispatchError("read_section 需要字符串参数 section_id。")
        sid = sid.strip()
        if sid not in ctx.bodies:
            known = "、".join(s.id for s in ctx.sections) or "（无）"
            raise ToolDispatchError(
                f"没有 id 为 {sid!r} 的章节。可用 id：{known}"
            )
        body = ctx.bodies[sid]
        if not body.strip():
            return f"章节 {sid} 目前是空的（没有正文）。"
        if len(body) > READ_SECTION_MAX_CHARS:
            head = body[:READ_SECTION_MAX_CHARS]
            return (
                f"{head}\n\n[已截断：仅前 {READ_SECTION_MAX_CHARS} 字，"
                f"全长 {len(body)} 字。下面的判断只能基于这部分。]"
            )
        return body

    if name == "section_citations":
        sid = (arguments or {}).get("section_id")
        if sid is not None and not isinstance(sid, str):
            raise ToolDispatchError("section_citations 的 section_id 必须是字符串。")
        sid = (sid or "").strip()
        if not ctx.citations:
            # 区分"查不到"与"确实没引用"，同 edit_history 的先例。
            return ctx.citation_note or "全文没有可解析的引用。"
        if sid:
            if sid not in ctx.citations:
                known = "、".join(ctx.citations) or "（无）"
                raise ToolDispatchError(
                    f"章节 {sid!r} 没有引用记录。有引用的章节：{known}"
                )
            titles = ctx.citations[sid]
            lines = [f"{sid} 引用了 {len(titles)} 篇："]
            lines += [f"  - {t}" for t in titles]
        else:
            lines = []
            for s, titles in ctx.citations.items():
                lines.append(f"{s}（{len(titles)} 篇）：")
                lines += [f"  - {t}" for t in titles]
        bad = [u for u in ctx.citation_unresolved if not sid or u[0] == sid]
        if bad:
            # 解析不了的必须报出来：藏起来会让模型据"本章只引了 N 篇"下错结论。
            lines.append(
                f"另有 {len(bad)} 个引用标记解析不到对应文献："
                + "、".join(f"{t}（{why}，在 {s}）" for s, t, why in bad)
            )
        return "\n".join(lines)

    raise ToolDispatchError(
        f"没有名为 {name!r} 的工具。"
        "可用：list_sections、read_section、doc_stats、edit_history、section_citations"
    )
