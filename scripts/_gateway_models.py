"""探针：列出网关可用模型（只打印模型 id，绝不打印密钥）。

用法：
    python scripts/_gateway_models.py                     # 用 .env 里的 PAPER_BASE_URL
    python scripts/_gateway_models.py <base_url> [key_env] # 指定端点（key 默认沿用 PAPER_KEY_ENV）

用途：为 reviewer LLM 选一个与 writer **不同**的模型（打破自评 reward-hack）。
用完即删。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.config import load_config
from paper_agent.utils.dotenv import load_dotenv

load_dotenv()
cfg = load_config()

base = (sys.argv[1] if len(sys.argv) > 1 else (cfg.llm_base_url or "")).rstrip("/")
key_env = sys.argv[2] if len(sys.argv) > 2 else (cfg.llm_api_key_env or "ANTHROPIC_API_KEY")
key = os.environ.get(key_env, "")

print(f"base_url : {base or '(SDK default)'}")
print(f"key env  : {key_env} -> {'set' if key else 'MISSING'}")
print("-" * 64)

ids: list[str] = []
raw_rows: list[dict] = []


def _try_rest(url: str) -> bool:
    global ids, raw_rows
    try:
        req = urllib.request.Request(url, headers={
            "authorization": f"Bearer {key}",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        print(f"[rest] {url} -> HTTP {exc.code} {detail}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[rest] {url} -> {type(exc).__name__}: {str(exc)[:160]}")
        return False

    rows = body.get("data") or body.get("models") or []
    if isinstance(rows, list) and rows:
        raw_rows = [r for r in rows if isinstance(r, dict)]
        ids = [str(r.get("id") or r.get("name") or r) for r in rows]
        print(f"[rest] {url} -> OK, {len(ids)} models")
        return True
    print(f"[rest] {url} -> OK but empty payload keys={list(body)[:6]}")
    return False


candidates = [base + "/models"]
if not base.endswith("/v1"):
    candidates.append(base + "/v1/models")

for url in candidates:
    if _try_rest(url):
        break

if not ids:
    print("\n未能列出模型。")
    raise SystemExit(1)

print("\nall models:")
for m in sorted(ids):
    print(f"  {m}")

print("\ncandidates by family:")
for kw in ("gpt-5", "gpt5", "gpt-4", "o3", "o4", "chatgpt",
           "claude", "opus", "sonnet", "gemini", "deepseek", "qwen"):
    hits = [m for m in sorted(ids) if kw in m.lower()]
    if hits:
        print(f"  [{kw}] " + ", ".join(hits))
