"""A1 wave 5：HTTP 分层异常 + 候选 DTO 多标识符。

HTTP 层必须直接测（原缺陷正在这一层，stub provider 测不到）：monkeypatch httpx，
断言 404 → RetrievalNotFound、429/5xx/超时 → RetrievalUnavailable、其余 4xx →
RetrievalUnavailable，且二者都能被 except RetrievalError 捕获。
"""

from __future__ import annotations

import pytest

from paper_agent.providers.retrieval import http_util
from paper_agent.providers.retrieval.base import (
    RetrievalError,
    RetrievalNotFound,
    RetrievalUnavailable,
)


class _Resp:
    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _HTTPStatusError(f"HTTP {self.status_code}")


class _HTTPStatusError(Exception):
    pass


class _TimeoutError(Exception):
    pass


class _FakeHttpx:
    """伪造 httpx.get，按预设序列返回响应或抛异常。"""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None,
            follow_redirects=None):
        self.calls += 1
        self.last_follow_redirects = follow_redirects
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def patch_httpx(monkeypatch):
    def _install(outcomes):
        fake = _FakeHttpx(outcomes)
        monkeypatch.setattr(http_util, "get_httpx", lambda: fake)
        return fake
    return _install


def test_404_raises_not_found_no_retry(patch_httpx):
    fake = patch_httpx([_Resp(404)])
    with pytest.raises(RetrievalNotFound):
        http_util.get_with_retry("http://x", {}, max_retries=3, backoff=0)
    assert fake.calls == 1          # 404 不重试


def test_follows_redirects(patch_httpx):
    """必须跟随重定向：Crossref 对被取代的 DOI 回 301 指向新号。

    httpx 默认不跟随，于是 301 会被 raise_for_status 变成 RetrievalUnavailable ——
    记录明明在库里，却报成「源不可用」，回填静默留空、核验落到「查不到」。
    实测 10.55417/fr.2023001 → 10.1109/tfr.2024.3492160。
    """
    fake = patch_httpx([_Resp(200)])
    http_util.get_with_retry("http://x", {}, max_retries=0, backoff=0)
    assert fake.last_follow_redirects is True


def test_404_is_retrieval_error_subclass(patch_httpx):
    patch_httpx([_Resp(404)])
    with pytest.raises(RetrievalError):     # 既有捕获点仍能抓到
        http_util.get_with_retry("http://x", {}, max_retries=1, backoff=0)


def test_500_retries_then_unavailable(patch_httpx):
    fake = patch_httpx([_Resp(500), _Resp(500), _Resp(500)])
    with pytest.raises(RetrievalUnavailable):
        http_util.get_with_retry("http://x", {}, max_retries=2, backoff=0)
    assert fake.calls == 3          # 初次 + 2 次重试


def test_429_retries_then_unavailable(patch_httpx):
    patch_httpx([_Resp(429)])
    with pytest.raises(RetrievalUnavailable):
        http_util.get_with_retry("http://x", {}, max_retries=1, backoff=0)


def test_429_without_retry_after_still_retries(patch_httpx):
    """无 Retry-After 时无法判定，保持原有退避重试（宁可多试）。"""

    fake = patch_httpx([_Resp(429)])
    with pytest.raises(RetrievalUnavailable):
        http_util.get_with_retry("http://x", {}, max_retries=2, backoff=0)
    assert fake.calls == 3


def test_quota_exhausted_429_does_not_retry_and_reports_wait():
    """配额耗尽型 429（Retry-After 远超退避窗口）必须只发一次、不白烧额度。

    真机（2026-08-06）：OpenAlex 日额度 $0.1 用尽后回
    429 + `Retry-After: 55256`（≈15.3h，UTC 午夜重置，信用点计费）。
    原实现把 429 一律当瞬时故障，退避 1.5/3/6s 重试 3 次 —— 注定失败，
    只是多烧 3 个额度、多等 10.5s，并把「等 15 小时」的信息丢掉，
    错误信息只剩「已重试 3 次」，让人误以为是短时限流而反复探测。
    """

    import pytest as _pytest

    fake = _FakeHttpx([_Resp(429, {"Retry-After": "55256"})])
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(http_util, "get_httpx", lambda: fake)
        with _pytest.raises(RetrievalUnavailable) as excinfo:
            http_util.get_with_retry("http://x", {}, max_retries=3, backoff=0)

    assert fake.calls == 1, "配额耗尽不应重试（每次重试都白烧额度）"
    # 等待时长必须出现在错误信息里，否则调用方只能靠猜。
    assert "15.3 小时" in str(excinfo.value)
    assert "55256" in str(excinfo.value)


def test_short_rate_window_429_still_retries():
    """短时速率窗口（Retry-After 小）仍要重试 —— 别把修复扩大成「429 一律不重试」。"""

    import pytest as _pytest

    fake = _FakeHttpx([_Resp(429, {"Retry-After": "2"}), _Resp(200)])
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(http_util, "get_httpx", lambda: fake)
        resp = http_util.get_with_retry("http://x", {}, max_retries=3, backoff=0)

    assert resp.status_code == 200
    assert fake.calls == 2


def test_timeout_retries_then_unavailable(patch_httpx):
    fake = patch_httpx([_TimeoutError("read timed out")])
    with pytest.raises(RetrievalUnavailable):
        http_util.get_with_retry("http://x", {}, max_retries=1, backoff=0)
    assert fake.calls == 2


def test_other_4xx_is_unavailable_not_notfound(patch_httpx):
    # 403 等权限/参数问题不是「不存在」
    patch_httpx([_Resp(403)])
    with pytest.raises(RetrievalUnavailable):
        http_util.get_with_retry("http://x", {}, max_retries=1, backoff=0)
    # 且不是 NotFound
    patch_httpx([_Resp(403)])
    with pytest.raises(RetrievalError):
        try:
            http_util.get_with_retry("http://x", {}, max_retries=0, backoff=0)
        except RetrievalNotFound as exc:  # pragma: no cover
            raise AssertionError("403 不应判为 not_found") from exc


def test_200_returns_response(patch_httpx):
    patch_httpx([_Resp(200)])
    resp = http_util.get_with_retry("http://x", {}, max_retries=1, backoff=0)
    assert resp.status_code == 200


def test_recovers_after_transient(patch_httpx):
    fake = patch_httpx([_Resp(500), _Resp(200)])
    resp = http_util.get_with_retry("http://x", {}, max_retries=3, backoff=0)
    assert resp.status_code == 200 and fake.calls == 2


# ---------------------------------------------------------------- DTO 映射

def test_openalex_keeps_both_doi_and_openalex_id():
    from paper_agent.providers.retrieval.openalex import OpenAlexRetrievalProvider

    work = {
        "id": "https://openalex.org/W2741809807",
        "doi": "https://doi.org/10.1145/ABC",
        "display_name": "T",
        "publication_year": 2019,
        "authorships": [],
        "primary_location": {"source": {"issn_l": "1557-7317"}},
    }
    e = OpenAlexRetrievalProvider._to_entry(work)
    # _strip_doi 已去掉 URL 前缀（与 source_id 一致）
    assert e.work_identifiers == {"doi": "10.1145/ABC", "openalex_work": "W2741809807"}
    assert e.container_identifiers == {"issn": "1557-7317"}
    assert e.source_id == "10.1145/ABC"     # source_id 保留旧语义


def test_s2_uses_paperid_and_externalids():
    from paper_agent.providers.retrieval.api import SemanticScholarRetrievalProvider

    paper = {
        "paperId": "abc123",
        "externalIds": {"DOI": "10.1/x", "ArXiv": "1706.03762", "PubMed": "999"},
        "title": "T",
        "authors": [],
        "year": 2020,
    }
    e = SemanticScholarRetrievalProvider._to_entry(paper)
    assert e.work_identifiers["semantic_scholar_paper"] == "abc123"
    assert e.work_identifiers["doi"] == "10.1/x"
    assert e.work_identifiers["arxiv"] == "1706.03762"
    assert e.work_identifiers["pmid"] == "999"


def test_reference_entry_backward_compatible_reflection():
    # vars(entry).copy() 那类反射用法（CitationVerifier.verify_and_mark 依赖）不受影响
    from paper_agent.workspace.models import ReferenceEntry, RetrievalCandidate

    e = ReferenceEntry(id="1", title="T", authors=["A"], year=2020, source_id="10.1/x")
    data = vars(e).copy()
    data["verified"] = True
    e2 = ReferenceEntry(**data)
    assert e2.verified is True and e2.work_identifiers == {}
    assert RetrievalCandidate is ReferenceEntry
