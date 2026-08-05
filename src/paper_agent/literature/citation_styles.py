"""参考文献著录格式（可注册，Req 1.9 的渲染侧）。

## 为什么做成注册表

用户会说「改成 XX 期刊的格式」。若把格式知识写进同步流程，每加一种格式就要动一次同步逻辑
—— 那正是引用标记那组缺陷的成因（同一条知识散落多处，漏一处即真机故障）。
这里把「一种格式怎么写」收敛成一个 :class:`CitationStyle`，同步流程只认接口不认格式。
新增期刊格式 = 加一个类 + `register()` 一行，不碰别处。

## 字段缺失是常态，不是异常

真机账本（OpenAlex 检索所得）里 `container_title` / `volume` / `pages` 覆盖率为 **0**：
`providers/retrieval/openalex.py` 的 mapping 只取了 title/authors/year/doi，
`primary_location.source.display_name`（期刊名）这些在响应里但没被接进 `ReferenceEntry`。
所以渲染器必须在字段不全时**优雅退化**，输出「作者. 标题. 年.」而不是留下
`. , (): .` 这种空骨架。缺什么由 :func:`missing_for_style` 如实报告，供上层提示用户补全。

严格的期刊格式（要求卷期页）在数据补齐前无法真正达标 —— 这是数据侧的限制，
不是渲染器的。诚实报告比伪造格式重要。
"""

from __future__ import annotations

from dataclasses import dataclass


def _join_authors(authors: tuple[str, ...], *, limit: int, et_al: str) -> str:
    """按 limit 截断作者表；超出补 et_al。limit<=0 表示不截断。"""
    names = [a.strip() for a in authors if a and a.strip()]
    if not names:
        return ""
    if limit > 0 and len(names) > limit:
        return ", ".join(names[:limit]) + et_al
    return ", ".join(names)


@dataclass(frozen=True)
class RenderedEntry:
    """一条渲染结果。`missing` 非空表示该格式下字段不全（已退化输出）。"""

    marker: str            # 该方言下的引用标记（md 数字 / tex bibkey）
    text: str              # 著录文本（不含标记）
    missing: tuple[str, ...] = ()


class CitationStyle:
    """一种著录格式。子类实现 :meth:`render_text` 与 :attr:`required`。"""

    name = ""
    label = ""
    required: tuple[str, ...] = ()

    def render_text(self, record) -> str:      # pragma: no cover - 抽象
        raise NotImplementedError

    def missing(self, record) -> tuple[str, ...]:
        out: list[str] = []
        for field in self.required:
            value = getattr(record, field, "")
            if field == "authors":
                if not record.authors:
                    out.append(field)
            elif field == "year":
                if record.year is None:
                    out.append(field)
            elif not value:
                out.append(field)
        return tuple(out)


class GBT7714(CitationStyle):
    """GB/T 7714-2015（数字顺序制）。

    完整形态：`作者. 题名[J]. 刊名, 年, 卷(期): 页.`
    字段不全时按「有什么写什么」退化，顺序与标点仍遵国标骨架。
    国标要求 3 名以上作者写「前三名, 等」。
    """

    name = "gbt7714"
    label = "GB/T 7714-2015（国标，数字顺序制）"
    required = ("authors", "year", "title", "container_title")

    _TYPE_MARK = {
        "journal-article": "J", "article": "J",
        "proceedings-article": "C", "conference-paper": "C",
        "book": "M", "monograph": "M",
        "thesis": "D", "dissertation": "D",
        "report": "R", "standard": "S", "patent": "P",
        "dataset": "DB", "webpage": "EB/OL",
    }

    def render_text(self, record) -> str:
        parts: list[str] = []
        authors = _join_authors(record.authors, limit=3, et_al=", 等")
        if authors:
            parts.append(authors + ".")

        title = (record.title or "").rstrip(".")
        if title:
            # reference_type 缺失（真机常态）时不写 `[]` 空标识，直接省略。
            mark = self._TYPE_MARK.get((record.reference_type or "").lower(), "")
            parts.append(f"{title}[{mark}]." if mark else f"{title}.")

        venue = record.container_title or record.conference_name or record.publisher
        if venue:
            parts.append(venue.rstrip(".") + ",")

        # 年, 卷(期): 页  —— 逐段可缺
        tail = ""
        if record.year is not None:
            tail = str(record.year)
        if record.volume:
            tail += f", {record.volume}" if tail else record.volume
            if record.issue:
                tail += f"({record.issue})"
        elif record.issue:
            tail += f", ({record.issue})" if tail else f"({record.issue})"
        pages = record.pages or record.article_number
        if pages:
            tail += f": {pages}" if tail else pages
        if tail:
            parts.append(tail + ".")

        if not record.container_title and not venue and record.url:
            parts.append(record.url)
        return " ".join(p for p in parts if p).strip()


class Simple(CitationStyle):
    """简明格式：`作者. 标题. 刊名, 年.` 不追求任何标准的严格字段。

    账本现有字段即可完整渲染，适合数据不全时先把文末列表立起来。
    """

    name = "simple"
    label = "简明格式（作者. 标题. 期刊, 年.）"
    required = ("authors", "title", "year")

    def render_text(self, record) -> str:
        parts: list[str] = []
        authors = _join_authors(record.authors, limit=3, et_al=" et al.")
        if authors:
            parts.append(authors + ".")
        if record.title:
            parts.append(record.title.rstrip(".") + ".")
        venue = record.container_title or record.conference_name
        if venue:
            parts.append(venue.rstrip(".") + ",")
        if record.year is not None:
            parts.append(f"{record.year}.")
        return " ".join(parts).strip()


_REGISTRY: dict[str, CitationStyle] = {}


def register(style: CitationStyle) -> None:
    """注册一种格式。同名覆盖（便于用户自定义期刊格式替换内置实现）。"""
    _REGISTRY[style.name] = style


def get_style(name: str = "") -> CitationStyle:
    """按名取格式；未知或空名回落 GB/T 7714（当前默认）。"""
    key = (name or "").strip().lower().replace("/", "").replace("-", "").replace(" ", "")
    if key in ("", "default", "gbt7714", "gb", "gbt77142015", "国标"):
        return _REGISTRY["gbt7714"]
    return _REGISTRY.get(key, _REGISTRY["gbt7714"])


def available_styles() -> tuple[tuple[str, str], ...]:
    """(name, label) 列表，供交互层展示可选格式。"""
    return tuple(sorted((s.name, s.label) for s in _REGISTRY.values()))


def missing_for_style(record, style_name: str = "") -> tuple[str, ...]:
    return get_style(style_name).missing(record)


register(GBT7714())
register(Simple())

__all__ = [
    "CitationStyle", "GBT7714", "Simple", "RenderedEntry",
    "register", "get_style", "available_styles", "missing_for_style",
]
