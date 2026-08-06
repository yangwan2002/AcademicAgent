"""文献检索链路 dogfood 诊断 runner（真机、非交互、可复跑）。

目的:把「加引言 + 引用配额（如 20篇/5中文/15英文）」这条链路**对着真实源跑一次**,
非交互地报告**卡在哪一阶段、各语言桶实际凑到几篇、候选被排除的原因分布**,用实测数据
决定下一块投入（中文源 / 相关性评测 / 外文维度）。

**默认不进 pytest**（要真实 LLM + 联网 + 花钱）。用法:

    # 先在 .env / 环境变量配好 writer/reviewer LLM 与检索源
    set PAPER_LLM=deepseek
    set PAPER_RETRIEVAL=openalex
    set OPENALEX_MAILTO=you@example.com
    python tests/eval/literature_dogfood/runner.py --topic "大语言模型代码生成" --total 20 --zh 5 --en 15

诊断口径:复用 `build_writing_orchestrator` 装配的**同一套 deps/limits/planner/classifier**,
显式跑 WritingLiteratureOrchestrator 的核心步骤（ASSESSING→RETRIEVING→commit→REASSESS→
BUILD_BUNDLE）并插桩。**只跑一次检索**（省 API）。不接交互确认——本脚本自动接受批次,
仅用于诊断,不写论文。
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _build_quota(total: int, zh: int, en: int):
    from paper_agent.literature.quota import (
        CountConstraint,
        CountDimension,
        CountOperator,
        QuotaKind,
        QuotaScope,
        QuotaSpec,
    )

    cons = []
    if total:
        cons.append(CountConstraint(CountDimension.TOTAL, CountOperator.MIN, total))
    if zh:
        cons.append(CountConstraint(CountDimension.LANGUAGE_ZH, CountOperator.MIN, zh))
    if en:
        cons.append(CountConstraint(CountDimension.LANGUAGE_EN, CountOperator.MIN, en))
    return QuotaSpec(QuotaKind.CITATION, QuotaScope.ACTUALLY_CITED,
                     count_constraints=tuple(cons))


def _bucket_hist(records, classifier, policy):
    """已入库记录按语言桶（含置信）分布,反映 high_only 策略下的实际可计数量。"""
    from paper_agent.literature.language import counts_toward

    hist = collections.Counter()
    for r in records:
        v = classifier(r)
        hist[f"{v.quota_bucket}/{v.confidence}"] += 1
    countable = {
        "zh": sum(1 for r in records if counts_toward(classifier(r), "zh", policy)),
        "en": sum(1 for r in records if counts_toward(classifier(r), "en", policy)),
    }
    return hist, countable


_LOG_PATH = _HERE / "last_run.txt"
_log_fh = None


def _emit(msg: str = "") -> None:
    """打印到 stdout 并同时写 UTF-8 日志（规避 Windows 控制台 GBK 乱码）。"""
    print(msg, flush=True)
    if _log_fh is not None:
        _log_fh.write(msg + "\n")
        _log_fh.flush()


_CAND_PATH = _HERE / "candidates.jsonl"


def _verify_summary(events) -> str:
    """把核验事件压成一行:每个标识符 → 结果类别（供判 canonical_not_found 等）。"""
    import json as _json  # noqa

    parts = []
    for e in events or ():
        parts.append(f"{getattr(e,'identifier_type','?')}={getattr(e,'kind',None).value if getattr(e,'kind',None) else '?'}"
                     f"@{getattr(e,'source','?')}/{getattr(e,'authority','?')}")
    return ";".join(parts) or "-"


def _dump_candidates(items, classifier) -> None:
    """逐候选:标题 + 相关性判定 + 语言桶 + 核验事件 + 处置 + 拒因。"""
    import json

    _emit("\n[detail] 逐候选（判定质量核对用；同时写 candidates.jsonl）:")
    rows = []
    for i, it in enumerate(items, 1):
        snap = it.candidate_snapshot
        rel = it.relevance
        lv = it.language_verdict
        rel_s = (f"{getattr(rel,'verdict',None) or getattr(rel,'error_kind','') or '-'}"
                 f"/{getattr(rel,'confidence','') or '-'}/{getattr(rel,'basis','') or '-'}"
                 if rel is not None else "-")
        lang_s = (f"{getattr(lv,'quota_bucket','')}/{getattr(lv,'confidence','')}"
                  if lv is not None else "-")
        verify_s = _verify_summary(it.verification_events)
        title = (snap.title or "")[:90]
        _emit(f"  #{i:02d} [{it.disposition.value}] rel={rel_s} lang={lang_s} "
              f"verify={verify_s} reasons={list(it.rejection_reasons)}")
        _emit(f"        {title}")
        rows.append({
            "idx": i, "disposition": it.disposition.value,
            "title": snap.title, "year": snap.year, "source": snap.source,
            "relevance_verdict": getattr(rel, "verdict", None) if rel else None,
            "relevance_confidence": getattr(rel, "confidence", None) if rel else None,
            "relevance_basis": getattr(rel, "basis", None) if rel else None,
            "relevance_error": getattr(rel, "error_kind", None) if rel else None,
            "language_bucket": getattr(lv, "quota_bucket", None) if lv else None,
            "language_confidence": getattr(lv, "confidence", None) if lv else None,
            "verify_events": [
                {"id_type": getattr(e, "identifier_type", ""),
                 "kind": getattr(e, "kind", None).value if getattr(e, "kind", None) else "",
                 "source": getattr(e, "source", ""),
                 "authority": getattr(e, "authority", "")}
                for e in (it.verification_events or ())
            ],
            "rejection_reasons": list(it.rejection_reasons),
        })
    _CAND_PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")


def run(topic: str, total: int, zh: int, en: int, query: str) -> int:
    global _log_fh
    _log_fh = open(_LOG_PATH, "w", encoding="utf-8")
    from paper_agent.application.literature_composition import (
        CompositionUnavailable,
        build_writing_orchestrator,
    )
    from paper_agent.config import load_config
    from paper_agent.literature.preflight import (
        acquisition_gaps,
        assess_availability,
        derive_retrieval_quota,
        effective_constraints,
    )
    from paper_agent.literature.retrieval.loop import RetrievalRequest, run_retrieval_loop
    from paper_agent.literature.retrieval.transaction import commit_batch
    from paper_agent.literature.relevance import RelevanceCriteria
    from paper_agent.providers.factory import (
        build_llm_provider,
        build_retrieval_provider,
        build_reviewer_llm_provider,
    )
    from paper_agent.session.facts import SessionFacts
    from paper_agent.utils.dotenv import load_dotenv

    load_dotenv()
    config = load_config()
    # dogfood 强制打开写作集成（脚本本身即 opt-in）;检索源取环境（默认 openalex）。
    config.literature_writing_integration = True
    if config.retrieval_provider == "mock":
        print("[warn] PAPER_RETRIEVAL=mock：只用内置样例(仅英文、无中文),中文桶必然为 0。"
              "真实诊断请设 PAPER_RETRIEVAL=openalex。", flush=True)

    writer = build_llm_provider(config)
    reviewer = build_reviewer_llm_provider(config)
    retrieval = build_retrieval_provider(config)
    facts = SessionFacts(path=str(_HERE / "_dogfood_facts.json"))

    try:
        orch = build_writing_orchestrator(
            config, writer_llm=writer, reviewer_llm=reviewer,
            retrieval_provider=retrieval, facts=facts)
    except CompositionUnavailable as exc:
        print(f"装配失败:{exc}")
        return 1
    assert orch is not None

    quota = _build_quota(total, zh, en)
    classifier, policy, judge_key = orch._classifier, orch._policy, orch._judge_key

    # F-3：接受策略**一次解析、三处共用**（Attempt / assess_availability / build_bundle）。
    # `section_kind` 是显式的章节身份信号（本脚本模拟「写引言」）——不能依赖
    # `criteria.section_purpose`，实测它是自由文本、常等于整个课题名。
    from paper_agent.application.literature_composition import build_acceptance_resolver

    _SECTION_KIND = "引言"
    acceptance = build_acceptance_resolver(config)(_SECTION_KIND)

    _emit(f"\n=== dogfood: topic={topic!r} 配额 total≥{total} zh≥{zh} en≥{en} ===")
    _emit(f"    接受策略: mode={config.literature_acceptance_mode} "
          f"section_kind={_SECTION_KIND!r} → 接受判定={sorted(acceptance.accepted_verdicts)}")
    print(f"检索源={config.retrieval_provider} 语言策略={policy} reviewer={'独立' if reviewer else '复用writer'}",
          flush=True)

    # A2f：合成开启 → 从主题(作 purpose)合成检索意图（多 seed）；否则 legacy 单查询。
    search_intent = None
    if config.literature_search_synthesis:
        from paper_agent.application.literature_composition import build_search_synthesizer
        from paper_agent.application.search_intent_preparation import prepare_search_intent
        from paper_agent.literature.retrieval.search_intent import (
            SearchContext,
            SearchIntentPreparationStatus,
        )

        class _AutoIntentPort:
            def request_search_topic(self, s):
                from paper_agent.application.literature_ports import SearchTopicDecision
                return SearchTopicDecision("cancel")

            def confirm_search_intent(self, s):
                from paper_agent.application.literature_ports import SearchIntentDecision
                return SearchIntentDecision("accept")   # dogfood 自动接受合成意图

        synth = build_search_synthesizer(config, writer_llm=writer)
        sctx = SearchContext(explicit_query=query, operation_kind="add_section",
                             target_title="引言", purpose=topic)
        prep = prepare_search_intent(sctx, synthesizer=synth,
                                     interaction_port=_AutoIntentPort())
        _emit(f"[0] 合成检索意图({config.literature_query_planner} planner): "
              f"status={prep.status.value}")
        if prep.status is SearchIntentPreparationStatus.READY:
            search_intent = prep.intent
            _emit(f"    主题={search_intent.semantic_topic!r} "
                  f"seeds={[(s.text, s.target.value) for s in search_intent.seeds]}")

    if search_intent is not None:
        from paper_agent.literature.retrieval.search_intent import to_relevance_criteria
        criteria = to_relevance_criteria(search_intent, query_intent=query or topic)
    else:
        criteria = RelevanceCriteria(topic=topic, query_intent=query or topic)
    task_fp = criteria.fingerprint()

    # 1) ASSESSING（空库 → 全缺口）
    eff = effective_constraints(quota, (), operation_id="dogfood", task_fingerprint=task_fp)
    gaps = acquisition_gaps(assess_availability(
        quota, criteria, facts.ledger, facts.relevance_store,
        classifier=classifier, policy=policy, judge_key=judge_key,
        acceptance=acceptance), eff)
    _emit(f"\n[1] 初始缺口: {[(g.dimension.value, g.amount) for g in gaps]}")

    # 2) RETRIEVING（一次 Loop）——实时 trace 每个 attempt，便于看时间/进度花在哪。
    retrieval_quota = derive_retrieval_quota(gaps, quota, redundancy_factor=2.0)
    _emit("[2] 检索中（打真实源 + reviewer 判相关性）…")
    import time as _t
    _trace_fh = open(_HERE / "live_trace.jsonl", "w", encoding="utf-8")
    _t0 = [_t.monotonic()]

    def _sink(rec: dict) -> None:
        import json as _j
        rec = {**rec, "wall_s": round(_t.monotonic() - _t0[0], 1)}
        _trace_fh.write(_j.dumps(rec, ensure_ascii=False) + "\n")
        _trace_fh.flush()
        _emit(f"    · attempt: cand={rec.get('candidates')} accept={rec.get('accept_new')} "
              f"reject={rec.get('reject')} verify_calls={rec.get('verify_calls')} "
              f"loop_elapsed={rec.get('elapsed_s')}s wall={rec['wall_s']}s")

    # 本 runner 直接装 orchestrator、**不走 Coordinator**，所以没人登记 ambient bus
    # ⇒ `structured_call` / provider 层的埋点全部退化为 no-op，`runs/` 下也不会有 trace。
    # 诊断脚本恰恰最需要这些细节（如 PROVIDER_SEARCH 的策略分布），故在此自建一个：
    # 收集到内存，跑完按种类汇总打印。
    from paper_agent.observability.bus import EventBus, RunContext
    from paper_agent.observability.current import use_bus
    from paper_agent.observability.health import ObservabilityHealth

    _events: list = []

    class _MemSink:
        def emit(self, event):
            _events.append(event)

    _bus = EventBus(_MemSink(), RunContext("dogfood"), ObservabilityHealth())

    with use_bus(_bus):
        loop = run_retrieval_loop(
            RetrievalRequest(
                quota=retrieval_quota, criteria=criteria,
                initial_query="" if search_intent is not None else (query or topic),
                task_fingerprint=task_fp, search_intent=search_intent),
            deps=orch._deps_with_acceptance(acceptance), limits=orch._limits,
            planner=orch._planner, trace_sink=_sink)
    _trace_fh.close()

    def _kind(e) -> str:
        return getattr(e.kind, "value", str(e.kind))

    _ps = [e for e in _events if _kind(e) == "provider_search"]
    if _ps:
        _dist: dict = {}
        for e in _ps:
            _dist[e.data.get("strategy")] = _dist.get(e.data.get("strategy"), 0) + 1
        _emit(f"    provider 查询策略分布: {_dist}"
              f"（命中数: {[e.data.get('results') for e in _ps]}）")

    # 时间归因：把 elapsed_s 拆成 relevance LLM / verify 网络两块，
    # 用于决定该先上 B1（批量判相关性）还是 B2（先判相关性、只核验 survivor）。
    _llm = [e for e in _events if _kind(e) == "llm_call_end"]
    _vf = [e for e in _events if _kind(e) == "verify_fetch"]
    if _llm or _vf:
        def _ms(evs) -> float:
            return sum(float(e.data.get("latency_ms") or e.data.get("elapsed_ms") or 0)
                       for e in evs)

        _by_label: dict = {}
        for e in _llm:
            lb = e.data.get("label") or e.data.get("purpose") or "unnamed"
            d = float(e.data.get("latency_ms") or e.data.get("elapsed_ms") or 0)
            agg = _by_label.setdefault(lb, [0, 0.0])
            agg[0] += 1
            agg[1] += d

        _vf_net = [e for e in _vf if not e.data.get("cached")]
        _emit("\n[2.5] 时间归因（决定 B1 vs B2 优先级）:")
        for lb, (n, ms) in sorted(_by_label.items(), key=lambda kv: -kv[1][1]):
            _emit(f"    LLM {lb:28s} calls={n:3d} 累计={ms/1000:7.1f}s 均值={ms/max(n,1)/1000:5.2f}s")
        _emit(f"    verify 网络往返            calls={len(_vf_net):3d} "
              f"累计={_ms(_vf_net)/1000:7.1f}s 均值="
              f"{_ms(_vf_net)/max(len(_vf_net),1)/1000:5.2f}s"
              f"（另有 {len(_vf) - len(_vf_net)} 次缓存命中，~0s）")
        _emit(f"    ⇒ LLM 合计 {_ms(_llm)/1000:.1f}s vs verify 合计 {_ms(_vf_net)/1000:.1f}s")
    _emit(f"    停止原因={loop.stop_reason.value} 诊断={loop.diagnostics}")

    # 候选处置 + 被排除原因直方图
    disp = collections.Counter()
    reasons = collections.Counter()
    for it in loop.batch.items:
        disp[it.disposition.value] += 1
        for r in (it.rejection_reasons or ()):
            reasons[r] += 1
    _emit(f"[3] 候选处置分布: {dict(disp)}")
    _emit(f"    被排除原因分布: {dict(reasons)}")

    # 逐候选明细（写 last_run.txt + 结构化 candidates.jsonl,供事后逐条核对判定质量）
    _dump_candidates(loop.batch.items, classifier)

    # 3) commit → REASSESS
    loop.batch.confirm()
    commit_batch(facts, loop.batch)
    avail = assess_availability(
        quota, criteria, facts.ledger, facts.relevance_store,
        classifier=classifier, policy=policy, judge_key=judge_key,
        acceptance=acceptance)
    hist, countable = _bucket_hist(list(facts.ledger.all_records()), classifier, policy)
    _emit(f"\n[4] 入库 {len(facts.ledger)} 条;语言桶/置信分布: {dict(hist)}")
    _emit(f"    high_only 下实际可计数: zh={countable['zh']} en={countable['en']}")
    _emit(f"    可用统计(过滤+相关+核验后): total={avail.total()}")

    # 4) 结论:哪些维度还差
    remaining = acquisition_gaps(avail, eff)
    if not remaining:
        _emit("\n[OK] 配额可满足:检索阶段已凑够,链路可进入写作。")
    else:
        _emit(f"\n[MISS] 仍缺: {[(g.dimension.value, g.amount) for g in remaining]}")
        _emit("    → 这就是当前真实瓶颈所在（按上面的排除原因/语言分布判因）。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="文献检索链路 dogfood 诊断")
    ap.add_argument("--topic", default="大语言模型代码生成", help="研究主题（相关性判定用）")
    ap.add_argument("--query", default="", help="检索查询词（默认用 topic）")
    ap.add_argument("--total", type=int, default=20)
    ap.add_argument("--zh", type=int, default=5)
    ap.add_argument("--en", type=int, default=15)
    args = ap.parse_args()
    return run(args.topic, args.total, args.zh, args.en, args.query)


if __name__ == "__main__":
    raise SystemExit(main())

