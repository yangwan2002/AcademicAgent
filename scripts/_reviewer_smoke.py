"""探针：候选 reviewer 模型的真实相关性判定冒烟。

测四件事（决定选型）：
1. 网关是否接受该模型名
2. **一次判定花几次物理调用** —— 不支持 forced tool choice 会触发兼容重试 = 成本翻倍
   （tasks-a2c Task 6A 要求兼容开销 <= 1）
3. 判定是否正确（明显相关 / 明显无关 / 只有标题无摘要）
4. 延迟

用完即删。用法：
    python scripts/_reviewer_smoke.py [model ...]
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.config import Config, load_config
from paper_agent.literature.relevance import JudgeVersion, RelevanceCriteria, RelevanceJudge
from paper_agent.providers.factory import build_llm_provider
from paper_agent.utils.dotenv import load_dotenv
from paper_agent.workspace.models import ReferenceEntry

load_dotenv()
_base_cfg = load_config()

GATEWAY = "http://ada-cli-golang.ctripcorp.com/coding-plan/openai/v1"
KEY_ENV = _base_cfg.llm_api_key_env or "ADA_LLM_KEY"

CANDIDATES = [
    "gpt-5.6-sol-2026-07-09",
    "gpt-5.5-2026-04-24",
    "claude-opus-4-7",
]


class _CountingLLM:
    """透明代理：只数 complete() 的物理调用次数。"""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    def complete(self, messages, **opts):
        self.calls += 1
        return self._inner.complete(messages, **opts)

    def stream(self, messages, **opts):
        return self._inner.stream(messages, **opts)

    def __getattr__(self, name):
        return getattr(self._inner, name)


CRITERIA = RelevanceCriteria(
    topic="空地协同 SLAM 中无人机与地面平台间的大视角、大尺度差异图像匹配",
    section_purpose="introduction_background",
    query_intent="cross-view image matching for aerial-ground collaborative SLAM",
    exclude_conditions=("与图像匹配/定位无关的医学、生物、经济类研究",),
)

CASES = [
    ("相关(有摘要)", ReferenceEntry(
        id="r1", title="Cross-View Image Matching for Aerial-Ground Localization",
        authors=["A. Author"], year=2023, source_id="10.1000/rel",
        abstract=("We address wide-baseline image matching between UAV nadir imagery and "
                  "ground-level views for collaborative SLAM, proposing a viewpoint- and "
                  "scale-invariant descriptor evaluated on aerial-ground datasets."),
    ), "relevant"),
    ("无关(有摘要)", ReferenceEntry(
        id="r2", title="A Randomized Trial of Dietary Fiber and Cardiovascular Outcomes",
        authors=["B. Author"], year=2021, source_id="10.1000/unrel",
        abstract=("In a cohort of 12,000 adults we evaluated dietary fiber intake against "
                  "incident cardiovascular events over ten years of follow-up."),
    ), "unrelated"),
    ("仅标题(无摘要)", ReferenceEntry(
        id="r3", title="Viewpoint-Invariant Place Recognition for UAV-UGV Teams",
        authors=["C. Author"], year=2024, source_id="10.1000/titleonly",
        abstract="",
    ), "weakly_relevant"),   # title_only 封顶 weakly（relevance.py 的保守规则）
]


def probe(model: str) -> None:
    cfg = Config(
        llm_provider="openai",          # 该网关是 OpenAI 兼容协议
        llm_model=model,
        llm_base_url=GATEWAY,
        llm_api_key_env=KEY_ENV,
    )
    print(f"\n=== {model} ===", flush=True)
    try:
        counting = _CountingLLM(build_llm_provider(cfg))
    except Exception as exc:  # noqa: BLE001
        print(f"  build failed: {type(exc).__name__}: {str(exc)[:200]}")
        return

    judge = RelevanceJudge(counting, judge_version=JudgeVersion(
        provider="openai", model_id=model))

    total_calls = 0
    hits = 0
    for label, cand, expect in CASES:
        before = counting.calls
        t0 = time.monotonic()
        try:
            a = judge.assess(cand, CRITERIA)
        except Exception as exc:  # noqa: BLE001
            print(f"  {label}: raised {type(exc).__name__}: {str(exc)[:200]}")
            continue
        dt = (time.monotonic() - t0) * 1000
        n = counting.calls - before
        total_calls += n
        ok = a.verdict == expect
        hits += int(ok)
        print(f"  {label}: status={a.status} verdict={a.verdict} conf={a.confidence} "
              f"basis={a.basis} err={a.error_kind} calls={n} {dt:.0f}ms "
              f"expect={expect} -> {'OK' if ok else 'MISMATCH'}", flush=True)
        if a.rationale:
            print(f"      why: {a.rationale[:120]}", flush=True)

    per_judge = total_calls / max(1, len(CASES))
    verdict = "forced-tool OK" if per_judge <= 1.05 else f"COMPAT OVERHEAD ({per_judge:.2f} calls/judge)"
    print(f"  --> 正确 {hits}/{len(CASES)}，物理调用 {total_calls} 次（{per_judge:.2f}/判定）；{verdict}",
          flush=True)


if __name__ == "__main__":
    for m in (sys.argv[1:] or CANDIDATES):
        probe(m)
