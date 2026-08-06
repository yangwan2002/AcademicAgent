"""检索 provider 的 HTTP 工具：惰性导入 httpx + 带退避的 GET。

对 429（限流）、超时、连接错误自动指数退避重试，提升真实联调时的稳定性
（Semantic Scholar 匿名访问极易 429）。但**配额耗尽型** 429（`Retry-After`
远超退避窗口，如 OpenAlex 日额度用尽给的 ≈15h）不重试 —— 见
:data:`_RETRY_AFTER_CEILING_S`。
"""

from __future__ import annotations

import time

from paper_agent.providers.retrieval.base import (
    RetrievalError,
    RetrievalNotFound,
    RetrievalUnavailable,
)

# Retry-After 超过此值即判定为「配额/额度耗尽」而非短时速率窗口：退避重试
# 无论如何都等不到窗口打开，只会白烧额度。30s 的量级足以覆盖常见的
# 秒级/十秒级速率窗口（Semantic Scholar 匿名访问那类）。
_RETRY_AFTER_CEILING_S = 30.0


def _parse_retry_after(resp) -> float | None:
    """读取 ``Retry-After``（仅支持 delta-seconds 形式，HTTP-date 返回 None）。"""

    raw = resp.headers.get("Retry-After") or resp.headers.get("X-RateLimit-Reset")
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        # HTTP-date 形式不解析：无法判定就走原有退避路径，宁可多重试。
        return None


def get_httpx():
    try:
        import httpx  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise RetrievalUnavailable(
            "真实检索 provider 需要 httpx：pip install '.[api]'"
        ) from exc
    return httpx


def get_with_retry(
    url: str,
    params: dict,
    headers: dict | None = None,
    timeout: float = 15.0,
    max_retries: int = 3,
    backoff: float = 1.5,
):
    """带退避重试的 GET。返回 httpx.Response（已 raise_for_status）。

    异常分层（Req 2.7）：HTTP 404 → :class:`RetrievalNotFound`（查无此文）；
    429 / 5xx / 超时 / 连接失败（重试耗尽后）以及其余 4xx →
    :class:`RetrievalUnavailable`（调用失败，非「不存在」）。二者均为
    :class:`RetrievalError` 子类，既有 ``except RetrievalError`` 调用点不受影响。
    """
    httpx = get_httpx()
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.get(
                url, params=params, headers=headers or {}, timeout=timeout
            )
            # 404 立即判「查无此文」，不重试。
            if resp.status_code == 404:
                raise RetrievalNotFound(f"HTTP 404: {url}")
            # 429 要分两种：短时速率窗口（重试有用）vs 配额/额度耗尽（重试
            # 必然失败）。OpenAlex 现为信用点计费，日额度用尽时回 429 +
            # `Retry-After: 55256`（≈15h，UTC 午夜重置）—— 这种情况下退避重试
            # 3 次只是白烧 3 个额度和 10.5s。以 Retry-After 是否超过
            # _RETRY_AFTER_CEILING_S 区分：超过就直接判不可用并把等待时长写进
            # 错误信息，让调用方知道该等多久、而不是反复探测。
            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp)
                if retry_after is not None and retry_after > _RETRY_AFTER_CEILING_S:
                    raise RetrievalUnavailable(
                        f"HTTP 429 配额耗尽，需等待 {retry_after / 3600:.1f} 小时"
                        f"（Retry-After={retry_after}s）：{url}"
                    )
                raise _Transient(f"HTTP {resp.status_code}")
            if resp.status_code >= 500:
                raise _Transient(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp
        except RetrievalNotFound:
            raise                       # 不重试、不降级
        except _Transient as exc:
            last_exc = exc
        except Exception as exc:  # noqa: BLE001
            # 连接/超时类视为可重试；其余（含非 404 的 4xx）判不可用、直接抛。
            name = type(exc).__name__
            if "Timeout" in name or "Connect" in name or "Read" in name:
                last_exc = exc
            else:
                raise RetrievalUnavailable(str(exc)) from exc
        if attempt < max_retries:
            time.sleep(backoff * (2**attempt))
    raise RetrievalUnavailable(f"请求失败（已重试 {max_retries} 次）：{last_exc}")


class _Transient(Exception):
    """内部用：标记可重试的 HTTP 状态。"""
