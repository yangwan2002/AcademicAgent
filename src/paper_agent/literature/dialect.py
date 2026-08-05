"""引用方言：**引用标记形态的唯一归属地**。

## 为什么有这个模块

「引用标记在 tex 下是 bibkey、在 md/docx 下是数字别名」这一条规则,原先在至少 5 个模块
里各自重新实现了一遍:

    gates/diff_gate.py       抽取 `\\cite{}` / `[id]`、选 bibkeys() 还是 ref_ids()
    generate/context_builder 决定给模型看 bibkey 还是数字
    literature/ledger.py     resolve_token 查哪个索引
    session/extractor.py     从既有正文抽取参考条目
    session/section_index.py 标题/结构切分

没有集中抽象,任何一处忘了跟上就是一个真机故障。真机「rewrite 重试三次全失败」正是
两个副本失配:`context_builder` 公布 `[chen2023overview]`,而 md 只认 `[3]` ⇒ 模型照抄
即「无法解析」,配额恒差满额,重试必然全部烧掉。

所以本模块回答两个问题,且**只在这里回答**:

    marker_for(record, ext)        给定记录与格式,公布出去的标记长什么样
    resolution_order(ext)          给定格式,该按什么顺序把 token 解析回 reference_id

「公布」与「解析」必须是同一套口径 —— 这是本模块存在的全部意义。
`tests/test_citation_dialect.py` 对每种 ext 断言二者闭合。

## 边界

本模块**只管标记形态**,不碰:
- 标记的分配(谁拿到 3 号)→ `ledger.upsert` / `retrieval/transaction._dealias`
- 引用是否够数(配额)→ `literature/quota.py`
- 文本里怎么抽 token → `gates/diff_gate.extract_citation_tokens`(调用本模块判形态)
"""

from __future__ import annotations

from dataclasses import dataclass

# tex 用 \cite{key};其余(md/markdown/docx)用 [n]。未知 ext 按方括号处理 ——
# 保守选择:方括号是纯文本格式的通行写法,误判成 tex 会连 \cite 都抽不到。
_TEX = ("tex", "latex")
_BRACKET = ("md", "markdown", "docx", "txt", "rst")


def normalize_ext(ext: str) -> str:
    """统一 ext 口径:去前导点、转小写。`".MD"` → `"md"`。

    各调用点原先各自 `.lower().lstrip(".")`,漏一处就是一个静默分支错误。
    """
    return (ext or "").lower().lstrip(".")


def is_tex(ext: str) -> bool:
    return normalize_ext(ext) in _TEX


@dataclass(frozen=True)
class MarkerStyle:
    """某种 ext 下引用标记的形态。"""

    name: str                 # "bibkey" | "numeric"
    wrap: str                 # 渲染模板,用于 prompt 里公布可引用标记
    resolution_order: tuple[str, ...]   # ledger 解析时依次尝试的索引名

    def render(self, marker: str) -> str:
        return self.wrap.format(marker=marker)


#: tex:公布 `\cite{key}`,解析查 bibkey 及其别名
TEX_STYLE = MarkerStyle(
    name="bibkey",
    wrap=r"\cite{{{marker}}}",
    resolution_order=("bibkey", "bibkey_alias"),
)

#: md/docx:公布 `[n]`,解析只查数字别名
BRACKET_STYLE = MarkerStyle(
    name="numeric",
    wrap="[{marker}]",
    resolution_order=("numeric",),
)


def style_for(ext: str) -> MarkerStyle:
    """给定 ext 返回标记形态。**所有分叉都应经此函数**,不要再写 `if ext == "tex"`。"""
    return TEX_STYLE if is_tex(ext) else BRACKET_STYLE


def resolution_order(ext: str) -> tuple[str, ...]:
    """解析 token 时依次尝试的索引名(不含所有格式共有的 `merged_from` 兜底)。"""
    return style_for(ext).resolution_order


def marker_for(record, ext: str) -> str:
    """取一条记录在该 ext 下**可被解析**的引用标记;取不到返回 `""`。

    关键约束:返回值必须能被 `ledger.resolve_token(token, ext=ext)` 解析回同一条记录。
    因此 md 下**不回退到 bibkey**,tex 下**不回退到数字** —— 回退等于公布一个解析不了的
    标记,正是真机故障的成因。宁可返回空(调用方可显式跳过)也不给假标记。
    """
    if is_tex(ext):
        return getattr(record, "bibkey", "") or ""
    alias = getattr(record, "numeric_alias", None)
    if alias:
        return str(alias)
    aliases = getattr(record, "numeric_aliases", ()) or ()
    return str(aliases[0]) if aliases else ""


def citable_tokens(facts, ext: str) -> set[str]:
    """该 ext 下所有「已核验、可被引用」的 token 集合。

    Gate 用它判断候选正文里的引用是否都已入库。tex 查 bibkey、md 查数字别名 ——
    与 `marker_for` / `resolution_order` 同源。
    """
    return facts.bibkeys() if is_tex(ext) else facts.ref_ids()


__all__ = [
    "normalize_ext", "is_tex", "MarkerStyle", "TEX_STYLE", "BRACKET_STYLE",
    "style_for", "resolution_order", "marker_for", "citable_tokens",
]
