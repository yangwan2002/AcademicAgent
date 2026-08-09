"""Langfuse 上传连通性自检（临时脚本，验证完即可删）。

不打真 LLM、不打 OpenAlex —— 只造一棵有代表性的 span 树上传，用来确认：
配置读到了、网络通、UI 上结构和字段都对。

    python scripts/_langfuse_smoke.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from paper_agent.config import load_config
from paper_agent.coordinator import Coordinator


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass  # 没装 python-dotenv 就靠外部已 export 的环境变量

    config = load_config()
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

    print("配置自检")
    print(f"  PAPER_TRACE      = {config.trace_enabled}")
    print(f"  PAPER_LANGFUSE   = {config.langfuse_enabled}")
    # 只打印前缀和长度，不回显密钥本身。
    print(f"  PUBLIC_KEY       = {pk[:8] + '…' if pk else '(空)'} len={len(pk)}")
    print(f"  SECRET_KEY       = {sk[:8] + '…' if sk else '(空)'} len={len(sk)}")
    print(f"  HOST             = {host}")

    if not (pk and sk):
        print("\n!! 两把 key 缺一，请填 .env 的 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")
        return 1
    if not config.trace_enabled:
        print("\n!! PAPER_TRACE 未开，Langfuse 那一路不会挂上")
        return 1

    tmp = tempfile.mkdtemp()
    coord = Coordinator(
        enable_trace=True, runs_dir=tmp, enable_langfuse=config.langfuse_enabled
    )
    exporter = coord._build_exporter()
    print(f"\nexporter = {type(exporter).__name__}")
    if type(exporter).__name__ != "CompositeExporter":
        print("!! 期望 CompositeExporter；说明 Langfuse 那一路没挂上（key 或包的问题）")
        return 1

    # 认证必须先单独查。上传走后台线程批量发送，401 只会打一行 SDK 日志，
    # `exporter.failures` 收不到 —— 光看 failures 会把认证失败误报成成功。
    from langfuse import Langfuse

    try:
        authed = Langfuse(public_key=pk, secret_key=sk, host=host).auth_check()
    except Exception as exc:  # noqa: BLE001 - 这里就是要把失败原因显示出来
        authed = False
        print(f"\n!! 认证失败: {type(exc).__name__}: {str(exc)[:200]}")
    if not authed:
        print("\n!! Langfuse 认证不通过。按以下顺序排查：")
        # region 排第一：key 按区隔离，配错区必 401。真实踩到过 ——
        # 项目在 US 区而默认值是 EU 区，误诊成「两把 key 不是一对」。
        print(f"   1. region 对不对？当前 host = {host}")
        print("      US 区项目 → https://us.cloud.langfuse.com")
        print("      EU 区项目 → https://cloud.langfuse.com")
        print("      建项目时那个页面上的 LANGFUSE_BASE_URL 就是答案。")
        print("   2. 两把 key 是否来自同一次创建（同项目、同一次生成）。")
        print(f"      当前 pk …{pk[-6:]}  sk …{sk[-6:]}")
        print("   3. key 有没有被吊销。")
        return 1
    print("认证 = 通过")

    # 造一棵形状接近真实检索的树：run → attempt → {search, llm 判定}
    def work(bus, _cancel):
        with bus.span("retrieval_attempt", attempt=1):
            with bus.span("openalex_search", strategy="phrase_exact", results=9):
                pass
            # llm_call_id 走 scope，**不是** span 参数 —— 当参数传只会进
            # attributes，Span.llm_call_id 仍为空，exporter 便不认它是 generation。
            with bus.llm_call_scope("smoke_call_1"):
                with bus.span("llm_report_relevance", size=9):
                    pass
        return "ok"

    report = coord.run(work)
    print(f"run_status = {report.run_status}")

    local = list(pathlib.Path(tmp).rglob("trace.json"))
    print(f"本地 trace.json = {'存在' if local else '!! 丢了'}")

    failures = getattr(exporter, "failures", [])
    if failures:
        print(f"\n!! Langfuse 上传失败: {failures}")
        print("   常见原因：key 不对、host 不对、网络不通")
        return 1

    print("\n上传成功。去 Langfuse 里找这条 trace：")
    print(f"  {host} → Tracing")
    print("  应能看到 run → retrieval_attempt → openalex_search(strategy=phrase_exact)")
    print("  其中 report_relevance_batch 应显示为 generation 类型")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
