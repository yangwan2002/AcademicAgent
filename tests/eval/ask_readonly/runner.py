"""只读问答真机 runner(P-4 第二步)。

用法::

    python tests/eval/ask_readonly/runner.py                    # 全部 24 问
    python tests/eval/ask_readonly/runner.py --only A2,D1,F1    # 挑几个
    python tests/eval/ask_readonly/runner.py --provider mock    # 不花钱的自检

**这不是 pass/fail 测试。** 它的产出是一张"哪一类问题答不上来"的表 —— 那张表是
第三步(记忆模块 / 补工具)的输入。所以 runner 只做机械可判的三件事:

1. 终态是否 ANSWERED(护栏是否误触发)
2. `expect_any` 是否命中(信息是否找到)
3. `forbid` 是否出现(是否幻觉 / 是否假装改了文档)

主观质量仍需人读。报告落 ``runs/ask_eval/<时间戳>/`` 便于逐条翻。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, _ROOT)

from tests.eval.ask_readonly.questions import CASES, AskCase

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "uav_slam.md")


#: 造给历史类问题(F1-F3)的编辑记录,**倒序**(最近在前)。
#:
#: 用脚本而不是真起一个 git 仓库:评测要的是"拿到历史后模型答得对不对",历史本身
#: 由 tests/test_session_history.py 对真 git 验证。脚本化让 F1-F3 的期望值确定。
#:
#: 刻意让最后一次编辑是**改写**而非润色 —— F3 问"上次润色的是哪一章",模型必须挑出
#: 润色那条,而不是简单取最近一条。
_FAKE_HISTORY = (
    {"commit": "c3c3c3c3", "message": "rewrite 5_结论",
     "relative": "2 minutes ago", "lines_added": 6, "lines_removed": 4},
    {"commit": "c2c2c2c2", "message": "polish 3_2_语义引导的候选生成",
     "relative": "15 minutes ago", "lines_added": 3, "lines_removed": 3},
    {"commit": "c1c1c1c1", "message": "add_section 4.3 外场实验",
     "relative": "1 hour ago", "lines_added": 2, "lines_removed": 0},
)


class _ReadOnlySession:
    """只读 session。写入面直接抛 —— 真机跑也不给写的机会。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self.ext = "md"
        self.git_enabled = True

    def read_working(self) -> str:
        with open(self._path, encoding="utf-8") as fh:
            return fh.read()

    def history(self, limit: int = 20):
        return list(_FAKE_HISTORY)[:limit]

    def write_working(self, *a, **k):
        raise AssertionError("只读问答不该写入 session")

    save_to_source = write_working


def _judge(case: AskCase, answer: str, ok: bool) -> dict:
    """机械可判的三项。主观质量不在这里判。"""
    hit = [s for s in case.expect_any if s in answer]
    bad = [s for s in case.forbid if s in answer]
    verdict = "PASS"
    if not ok:
        verdict = "GUARD"          # 护栏触发,没正常答完
    elif bad:
        verdict = "HALLUC"         # 出现了禁止内容
    elif case.expect_any and not hit:
        verdict = "MISS"           # 该找到的信息没找到
    elif not case.expect_any:
        verdict = "MANUAL"         # 无机械判据,需人读
    return {"verdict": verdict, "hit": hit, "forbidden_found": bad}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的 qid")
    ap.add_argument("--provider", default="", help="覆盖 PAPER_LLM,如 mock")
    ap.add_argument("--max-iterations", type=int, default=10)
    args = ap.parse_args()

    if args.provider:
        os.environ["PAPER_LLM"] = args.provider

    from dotenv import load_dotenv

    load_dotenv()
    if args.provider:                      # load_dotenv 不覆盖已有值,但要防 .env 反压
        os.environ["PAPER_LLM"] = args.provider

    from paper_agent.application.ask_context import capture
    from paper_agent.application.ask_loop import run_ask
    from paper_agent.config import load_config
    from paper_agent.providers.factory import build_llm_provider

    cfg = load_config()
    llm = build_llm_provider(cfg)

    picked = [c for c in CASES if not args.only or c.qid in args.only.split(",")]
    ctx = capture(_ReadOnlySession(_FIXTURE))

    out_dir = os.path.join(
        _ROOT, "runs", "ask_eval", datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    os.makedirs(out_dir, exist_ok=True)

    print(f"provider={cfg.llm_provider} model={cfg.llm_model} 共 {len(picked)} 问")
    print(f"文档:{len(ctx.bodies)} 章 / {ctx.total_chars} 字\n")

    rows = []
    for case in picked:
        t0 = time.time()
        try:
            res = run_ask(
                case.question, ctx, llm, max_iterations=args.max_iterations
            )
            answer, ok = res.display(), res.ok
            meta = {
                "stop_reason": res.stop_reason.value,
                "llm_calls": res.llm_calls,
                "iterations": res.iterations,
                "tools": res.tool_calls,
                "error": res.error,
            }
        except Exception as exc:
            answer, ok = f"[runner 异常] {type(exc).__name__}: {exc}", False
            meta = {"stop_reason": "runner_error", "llm_calls": 0,
                    "iterations": 0, "tools": [], "error": str(exc)}

        j = _judge(case, answer, ok)
        row = {
            "qid": case.qid, "category": case.category,
            "needs": list(case.needs), "question": case.question,
            "answer": answer, "elapsed_s": round(time.time() - t0, 1),
            "note": case.note, **meta, **j,
        }
        rows.append(row)
        print(
            f"[{j['verdict']:6}] {case.qid} ({case.category}) "
            f"{meta['llm_calls']}调用/{len(meta['tools'])}工具 "
            f"{row['elapsed_s']}s  {case.question}"
        )
        print(f"         {answer[:160].replace(chr(10), ' ')}")
        if j["forbidden_found"]:
            print(f"         !! 出现禁止内容:{j['forbidden_found']}")
        print()

    _write_report(out_dir, rows)
    _print_summary(rows, out_dir)
    return 0


def _write_report(out_dir: str, rows: list[dict]) -> None:
    with open(os.path.join(out_dir, "results.jsonl"), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 按 needs 聚合:这才是第三步的输入
    gap: dict[str, dict] = {}
    for r in rows:
        for need in r["needs"]:
            g = gap.setdefault(need, {"total": 0, "failed": 0, "qids": []})
            g["total"] += 1
            if r["verdict"] in ("MISS", "HALLUC", "GUARD"):
                g["failed"] += 1
                g["qids"].append(r["qid"])
    with open(os.path.join(out_dir, "capability_gap.json"), "w", encoding="utf-8") as fh:
        json.dump(gap, fh, ensure_ascii=False, indent=2)


def _print_summary(rows: list[dict], out_dir: str) -> None:
    print("=" * 72)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("终态:", "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    print("\n按能力维度聚合(失败/总数)—— 这是第三步该补什么的依据:")
    gap: dict[str, list] = {}
    for r in rows:
        for need in r["needs"]:
            gap.setdefault(need, []).append(r["verdict"])
    for need, verdicts in sorted(gap.items()):
        bad = sum(1 for v in verdicts if v in ("MISS", "HALLUC", "GUARD"))
        flag = "  <-- 缺口" if bad and bad >= len(verdicts) / 2 else ""
        print(f"  {need:10} {bad}/{len(verdicts)}{flag}")

    tool_use: dict[str, int] = {}
    for r in rows:
        for t in r["tools"]:
            tool_use[t] = tool_use.get(t, 0) + 1
    print("\n工具调用次数:", tool_use or "(无)")
    calls = [r["llm_calls"] for r in rows if r["llm_calls"]]
    if calls:
        print(f"LLM 调用:总 {sum(calls)} 次,单问均 {sum(calls)/len(calls):.1f} 次")
    print(f"\n明细:{out_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
