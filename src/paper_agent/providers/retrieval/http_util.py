"""检索 provider 的 HTTP 工具：惰性导入 httpx + 带退避的 GET。

对 429（限流）、超时、连接错误自动指数退避重试，提升真实联调时的稳定性
（Semantic Scholar 匿名访问极易 429）。
"""

from __future__ import annotations

import time

from paper_agent.providers.retrieval.base import RetrievalError


def get_httpx():
    try:
        import httpx  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise RetrievalError(
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
    """带退避重试的 GET。返回 httpx.Response（已 raise_for_status）。"""
    httpx = get_httpx()
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.get(
                url, params=params, headers=headers or {}, timeout=timeout
            )
            # 429/5xx 视为可重试。
            if resp.status_code == 429 or resp.status_code >= 500:
                raise _Transient(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp
        except _Transient as exc:
            last_exc = exc
        except Exception as exc:  # noqa: BLE001
            # 连接/超时类视为可重试；其余直接抛出。
            name = type(exc).__name__
            if "Timeout" in name or "Connect" in name or "Read" in name:
                last_exc = exc
            else:
                raise RetrievalError(str(exc)) from exc
        if attempt < max_retries:
            time.sleep(backoff * (2**attempt))
    raise RetrievalError(f"请求失败（已重试 {max_retries} 次）：{last_exc}")


class _Transient(Exception):
    """内部用：标记可重试的 HTTP 状态。"""
