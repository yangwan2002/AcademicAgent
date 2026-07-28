"""标识符归一化与分类（A1 / 任务 1）。

**本模块的核心约束**：标识符分两类，只有**作品级**标识符可用于去重。

- ``WORK_TYPES``（作品级）：标识「这一篇作品」——doi / arxiv / openalex_work /
  semantic_scholar_paper / pmid。
- ``CONTAINER_TYPES``（载体级）：标识「刊物或书」——issn / eissn / isbn。
  同一期刊的不同论文、同一本书的不同章节**会共享**这些值，拿它们去重会把大量
  不同文献错误合并成一条。

因此 :func:`work_keys` 只遍历 ``WORK_TYPES``；``dedup`` 模块也只调用它，
使「用 ISSN 去重」在结构上不可达，而非靠运行时判断跳过。

归一化的幂等性是硬要求：``normalize(t, normalize(t, x)) == normalize(t, x)``。
否则同一标识符经不同路径入库会算出不同的去重键。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping

#: 作品级标识符类型。**只有这些可以触发去重与核验**。
WORK_TYPES = frozenset(
    {"doi", "arxiv", "openalex_work", "semantic_scholar_paper", "pmid"}
)

#: 载体级标识符类型。绝不可用于作品级去重。
CONTAINER_TYPES = frozenset({"issn", "eissn", "isbn"})

#: 选取 canonical identifier 与「用哪个标识符去回查」时的类型优先级。
WORK_PRIORITY = ("doi", "arxiv", "pmid", "openalex_work", "semantic_scholar_paper")

_DOI_URL_PREFIX = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
_ARXIV_URL_PREFIX = re.compile(r"^https?://arxiv\.org/abs/", re.IGNORECASE)
_ARXIV_SCHEME_PREFIX = re.compile(r"^arxiv[:/]\s*", re.IGNORECASE)
_ARXIV_VERSION_SUFFIX = re.compile(r"v\d+$", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]")

# ---- classify 用的形态判定 ----
_DOI_SHAPE = re.compile(r"^10\.\d{4,}/\S+$")
# arXiv 新式：1706.03762 / 2301.10140v2；旧式：physics/0506741、math.GT/0309136
_ARXIV_NEW_SHAPE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$", re.IGNORECASE)
_ARXIV_OLD_SHAPE = re.compile(r"^[a-z-]+(\.[A-Za-z]{2})?/\d{7}(v\d+)?$")
_OPENALEX_SHAPE = re.compile(r"^W\d+$")
_PMID_SHAPE = re.compile(r"^\d{1,8}$")


def normalize(id_type: str, raw: str) -> str:
    """把某类型标识符归一化为可比较的规范形式。

    未知类型按「NFC + strip」保守处理，不做任何猜测性改写。
    归一化后为空串表示「该标识符不可用」，调用方应视作不存在（见 :func:`work_keys`）。
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFC", str(raw)).strip()
    if not text:
        return ""
    kind = (id_type or "").strip().lower()

    if kind == "doi":
        text = _DOI_URL_PREFIX.sub("", text)
        return text.strip().lower()

    if kind == "arxiv":
        text = _ARXIV_URL_PREFIX.sub("", text)
        text = _ARXIV_SCHEME_PREFIX.sub("", text)
        text = text.strip()
        # 去版本号：同一篇的 v1/v2 是同一件作品，必须归一到同一个去重键。
        text = _ARXIV_VERSION_SUFFIX.sub("", text)
        return text.lower()

    if kind == "openalex_work":
        # 形如 https://openalex.org/W2741809807 → W2741809807
        return text.rsplit("/", 1)[-1].strip().upper()

    if kind == "semantic_scholar_paper":
        return text.rsplit("/", 1)[-1].strip().lower()

    if kind == "pmid":
        digits = _NON_ALNUM.sub("", text)
        return digits.lstrip("0") or digits

    if kind in CONTAINER_TYPES:
        # ISSN/ISBN 的连字符只是排版分组，不承载信息。
        return _NON_ALNUM.sub("", text).upper()

    return text


def classify(raw: str) -> str | None:
    """从裸标识符串反推其类型；**无法判定时返回 None，绝不猜测**。

    用途：兼容既有 ``ReferenceEntry.source_id``（一个不带类型的裸串），
    以及迁移旧数据。返回 None 的串由调用方存入 ``unclassified_identifiers``，
    绝不塞进引用别名或任何参与去重的字段。
    """
    if not raw:
        return None
    text = unicodedata.normalize("NFC", str(raw)).strip()
    if not text:
        return None

    probe = _DOI_URL_PREFIX.sub("", text).strip()
    if _DOI_SHAPE.match(probe):
        return "doi"

    arxiv_probe = _ARXIV_URL_PREFIX.sub("", text)
    arxiv_probe = _ARXIV_SCHEME_PREFIX.sub("", arxiv_probe).strip()
    if _ARXIV_NEW_SHAPE.match(arxiv_probe) or _ARXIV_OLD_SHAPE.match(arxiv_probe):
        return "arxiv"

    openalex_probe = text.rsplit("/", 1)[-1].strip().upper()
    if _OPENALEX_SHAPE.match(openalex_probe):
        return "openalex_work"

    if _PMID_SHAPE.match(text):
        return "pmid"

    return None


def _pairs(source: Mapping[str, str] | Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    if isinstance(source, Mapping):
        return list(source.items())
    return [(t, v) for t, v in source]


def work_keys(
    identifiers: Mapping[str, str] | Iterable[tuple[str, str]],
) -> set[tuple[str, str]]:
    """产出用于去重的 ``(类型, 归一化值)`` 键集合。

    **只遍历 WORK_TYPES**：这是「不得用 ISSN/ISBN 去重」这条不变量的实现点
    （Property 3）。归一化后为空串的不产生键——否则两条各自缺标识符的记录会
    因为都持有空串而互相命中。
    """
    keys: set[tuple[str, str]] = set()
    for id_type, raw in _pairs(identifiers):
        kind = (id_type or "").strip().lower()
        if kind not in WORK_TYPES:
            continue
        value = normalize(kind, raw)
        if value:
            keys.add((kind, value))
    return keys


def container_pairs(
    identifiers: Mapping[str, str] | Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    """归一化载体级标识符（供入库存储，**不供去重**）。"""
    out: list[tuple[str, str]] = []
    for id_type, raw in _pairs(identifiers):
        kind = (id_type or "").strip().lower()
        if kind not in CONTAINER_TYPES:
            continue
        value = normalize(kind, raw)
        if value:
            out.append((kind, value))
    return out


def pick_by_priority(
    identifiers: Mapping[str, str] | Iterable[tuple[str, str]],
) -> tuple[str, str] | None:
    """按 :data:`WORK_PRIORITY` 取一个作品级标识符。

    供 ``canonical_identifier`` 与「用哪个标识符去回查」共用。
    因为只在 ``WORK_TYPES`` 里挑，所以结构上不可能选到 container（Req 1.5）。
    """
    normalized = {kind: value for kind, value in work_keys(identifiers)}
    for kind in WORK_PRIORITY:
        if normalized.get(kind):
            return (kind, normalized[kind])
    return None


__all__ = [
    "WORK_TYPES",
    "CONTAINER_TYPES",
    "WORK_PRIORITY",
    "normalize",
    "classify",
    "work_keys",
    "container_pairs",
    "pick_by_priority",
]
