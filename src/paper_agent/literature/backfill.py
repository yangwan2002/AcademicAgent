"""给已有账本回填著录字段（刊名 / 卷 / 期 / 页）。

provider 侧修好之后，**新检索**的文献字段是齐的；但已存在的记录是修复前抓的，
`container_title` 等仍为空 —— 不回填，老会话永远渲染不出完整格式。

回填只按 DOI 精确查 Crossref（`fetch_metadata` 权威路径），**不做模糊匹配**：
著录字段错配比缺失更糟（把 A 的刊名安到 B 上，读者按刊名去查会查不到，
而且这种错很难被发现）。查不到就保持为空，如实计入 `failed`。

只填**空字段**，不覆盖已有值：用户手工订正过的内容不该被一次回填冲掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field

_FIELDS = (
    "reference_type", "container_title", "volume", "issue",
    "pages", "article_number", "publisher", "conference_name", "url",
)


@dataclass
class BackfillReport:
    filled: list[str] = field(default_factory=list)     # 有字段被补上的 reference_id
    already_complete: list[str] = field(default_factory=list)
    no_doi: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)      # 有 DOI 但查不到/源不可用
    message: str = ""


def _doi_of(record) -> str:
    for key, value in (record.work_identifiers or ()):
        if key == "doi" and value:
            return value
    return ""


def backfill_record(record, provider):
    """回填单条。返回 (新记录, 是否有变化)。异常不外溢（单条失败不毁整批）。"""
    doi = _doi_of(record)
    if not doi:
        return record, False
    try:
        result = provider.fetch_metadata("doi", doi)
    except Exception:  # noqa: BLE001 - 网络/解析异常按「查不到」处理
        return record, False
    candidate = getattr(result, "candidate", None)
    if candidate is None or getattr(result.status, "name", "") != "FOUND":
        return record, False

    updates = {}
    for name in _FIELDS:
        if getattr(record, name, ""):
            continue                      # 只填空字段，不覆盖已有值
        value = (getattr(candidate, name, "") or "").strip()
        if value:
            updates[name] = value
    if not updates:
        return record, False
    return record.with_changes(**updates), True


def backfill_ledger(ledger, provider, *, style_name: str = "gbt7714") -> BackfillReport:
    """遍历账本回填。返回报告；调用方负责持久化（facts.save()）。"""
    from paper_agent.literature.citation_styles import get_style

    style = get_style(style_name)
    report = BackfillReport()
    for record in list(ledger.all_records()):
        if not style.missing(record):
            report.already_complete.append(record.reference_id)
            continue
        if not _doi_of(record):
            report.no_doi.append(record.reference_id)
            continue
        updated, changed = backfill_record(record, provider)
        if changed:
            ledger.replace(updated)
            report.filled.append(record.reference_id)
        else:
            report.failed.append(record.reference_id)

    parts = [f"回填完成：{len(report.filled)} 条补上著录字段"]
    if report.already_complete:
        parts.append(f"{len(report.already_complete)} 条本已完整")
    if report.failed:
        parts.append(f"{len(report.failed)} 条按 DOI 查不到，保持为空（不猜测）")
    if report.no_doi:
        parts.append(f"{len(report.no_doi)} 条无 DOI，无法精确回填")
    report.message = "；".join(parts)
    return report


__all__ = ["BackfillReport", "backfill_record", "backfill_ledger"]
