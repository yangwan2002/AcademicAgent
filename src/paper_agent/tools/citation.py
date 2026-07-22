"""引用真实性核验（Req 4 硬约束）。

两种核验方式：
1. 按 source_id（DOI / arXiv id）回查——精确、可靠。
2. 按标题 + 作者回查并做模糊比对——用于审计用户初稿中可能没有 DOI 的引用，
   或判断元数据（年份/作者）是否与真实记录一致。

只有核验通过的条目才允许进入已验证文献库；写作智能体只能引用其中的条目。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher

from paper_agent.providers.retrieval.base import RetrievalError, RetrievalProvider
from paper_agent.workspace.models import ReferenceEntry


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


@dataclass
class VerificationResult:
    """元数据核验结果（供引用审计使用）。"""

    exists: bool
    matched: ReferenceEntry | None = None      # 真实记录（若找到）
    title_score: float = 0.0
    year_matches: bool | None = None            # 年份是否一致（None=无法判断）
    note: str = ""


class CitationVerifier:
    def __init__(
        self,
        provider: RetrievalProvider,
        title_threshold: float = 0.82,
        *,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
    ) -> None:
        self._provider = provider
        self._title_threshold = title_threshold
        # 有界重试：单次网络抖动不应误报「引用不存在」（Round 10）。仅对 RetrievalError
        # 重试，退避 retry_backoff * 2^k；耗尽仍失败才向上以 RetrievalError 表达。
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff = max(0.0, float(retry_backoff))
        # 进程内缓存：同一 source_id / title 反复核验（如多轮审计）不重复打网络。
        self._meta_cache: dict[str, ReferenceEntry | None] = {}
        self._search_cache: dict[tuple[str, int], list[ReferenceEntry]] = {}

    # --- 带缓存 + 有界重试的 provider 调用 ---

    def _fetch_metadata_cached(self, source_id: str) -> ReferenceEntry | None:
        """按 source_id 回查（缓存 + 重试）。耗尽重试仍失败 → 抛 RetrievalError。"""
        if source_id in self._meta_cache:
            return self._meta_cache[source_id]
        result = self._with_retry(lambda: self._provider.fetch_metadata(source_id))
        self._meta_cache[source_id] = result
        return result

    def _search_cached(self, title: str, limit: int) -> list[ReferenceEntry]:
        key = (title, limit)
        if key in self._search_cache:
            return self._search_cache[key]
        result = self._with_retry(lambda: self._provider.search(title, limit=limit))
        self._search_cache[key] = result
        return result

    def _with_retry(self, call):
        """执行 provider 调用，对 ``RetrievalError`` 有界重试退避；耗尽后重新抛出。"""
        attempt = 0
        while True:
            try:
                return call()
            except RetrievalError:
                if attempt >= self._max_retries:
                    raise
                if self._retry_backoff > 0:
                    time.sleep(self._retry_backoff * (2 ** attempt))
                attempt += 1

    # --- 按 source_id 的存在性核验（Req 4） ---

    def verify(self, entry: ReferenceEntry) -> bool:
        """核验单条文献是否真实存在（按 source_id 回查）。"""
        if not entry.source_id:
            return False
        try:
            found = self._fetch_metadata_cached(entry.source_id)
        except RetrievalError:
            return False
        return found is not None

    def verify_and_mark(self, entry: ReferenceEntry) -> ReferenceEntry:
        """返回带 verified 标记的新条目（不原地修改入参）。"""
        verified = self.verify(entry)
        data = vars(entry).copy()
        data["verified"] = verified
        return ReferenceEntry(**data)

    # --- 按标题/作者的核验（供审计；存在性 ① + 元数据 ②） ---

    def verify_by_metadata(self, entry: ReferenceEntry) -> VerificationResult:
        """用标题检索真实记录并比对，判断是否存在及元数据是否一致。"""
        # 优先用 source_id 直接定位。
        if entry.source_id:
            try:
                found = self._fetch_metadata_cached(entry.source_id)
            except RetrievalError:
                found = None
            if found is not None:
                return self._compare(entry, found)

        if not entry.title:
            return VerificationResult(exists=False, note="缺少标题与可用标识符，无法核验")

        try:
            candidates = self._search_cached(entry.title, 5)
        except RetrievalError as exc:
            return VerificationResult(exists=False, note=f"检索失败：{exc}")

        best: ReferenceEntry | None = None
        best_score = 0.0
        for cand in candidates:
            score = title_similarity(entry.title, cand.title)
            if score > best_score:
                best_score, best = score, cand

        if best is not None and best_score >= self._title_threshold:
            return self._compare(entry, best, override_score=best_score)
        return VerificationResult(
            exists=False,
            title_score=best_score,
            note="未找到标题高度匹配的真实文献，疑似不存在或信息有误",
        )

    def _compare(
        self,
        entry: ReferenceEntry,
        real: ReferenceEntry,
        override_score: float | None = None,
    ) -> VerificationResult:
        score = (
            override_score
            if override_score is not None
            else title_similarity(entry.title, real.title)
        )
        year_matches: bool | None
        if entry.year is None or real.year is None:
            year_matches = None
        else:
            year_matches = entry.year == real.year
        note = ""
        if year_matches is False:
            note = f"年份可能有误：原文 {entry.year}，真实记录 {real.year}"
        return VerificationResult(
            exists=True,
            matched=real,
            title_score=score,
            year_matches=year_matches,
            note=note,
        )
