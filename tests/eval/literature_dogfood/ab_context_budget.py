"""P-3a 三臂 A/B：同一预算下「少而全」vs「多而截」哪个更值。

背景：`build_bundle` 第 5 步「剩余预算补高相关」在尽量**填满**预算，而 context rot
说输入越长召回越差。这个质疑此前跑不起来 —— 真机 total=20 时 25 条需 28698 字符
> 预算 24000，`build_bundle` 恒判 CONTEXT_BUDGET_INSUFFICIENT，压根产不出 bundle。
分级降级（`abstract_ladder`）落地后才有三个可比的臂：

  A  full        全摘要、不降级（旧行为）→ 预算内只装得下 17 篇
  B  trunc1000   摘要截 1000 字符        → 23 篇
  C  trunc700    摘要截 700 字符         → 25 篇（全部入选）

三臂**预算相同**，差别只在「字符怎么分配」：给少数文献完整摘要，还是给更多文献
截短摘要。这是个真实的上下文工程取舍，答案不显然 —— 摘要后半段多为实验细节与
数据集，对写引言的价值可能低于多覆盖几篇文献。

用法：
    python ab_context_budget.py                  # 只做组成分析（离线、零 API 成本）
    python ab_context_budget.py --generate       # 追加真机生成对比（**花钱**）

组成分析复用盘上 `_dogfood_facts.json`，不发任何网络请求。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

_FACTS = _HERE / "_dogfood_facts.json"
#: 输出按语料规模分文件。曾经固定写 `ab_context_budget.txt`，结果一次 13 条语料的
#: 离线重跑覆盖了 25 条那次的**真机付费结果**。语料一换，三臂就可能全都装得下、
#: 退化成同一个 bundle（13 条时实测如此），两份报告不可比却同名 —— 必须分开存。
def _out_path(n_records: int) -> Path:
    return _HERE / f"ab_context_budget.{n_records}rec.txt"

# 三臂定义：(名字, abstract_ladder)。() = 关闭降级 = 旧行为。
_ARMS = (
    ("A_full", ()),
    ("B_trunc1000", (1000,)),
    ("C_trunc700", (700,)),
)

# A 臂的 total 下限需要**放宽**才能参与对比：20 篇全摘要需 30645 字符 > 24000，
# 按原配额它只会判 INSUFFICIENT（那正是旧实现的 bug，不是一个可比的臂）。
# P-3a 要问的是「同样预算，少而全 vs 多而截」，所以 A 必须被允许少给几篇 ——
# 这也正是改法 C 之前用户唯一的出路（接受偏差）。
#
# 放宽到几篇不能拍脑袋：选取按 relevance 排名而非成本升序，所以「最省的 N 条合计」
# 算出来的篇数会偏乐观（我按成本估的 17 实测仍需 26811 > 24000）。改为**实测下探**。

_log: list[str] = []

#: 每臂实际生效的约束（A 臂下探过 total）。算 floor 必须用该臂自己的，见 `_dump_bundles`。
_ARM_EFF: dict[str, object] = {}


def _emit(line: str = "") -> None:
    _log.append(line)
    try:
        print(line)
    except UnicodeEncodeError:
        # 同 runner：GBK 控制台遇 ⇒/→ 会崩，日志已在 _log 里，不能让呈现拖垮实验
        print(line.encode("ascii", "replace").decode("ascii"))


def _load_context():
    """装配离线重放所需的一切。

    关键：`task_fingerprint` 必须**复用盘上的**。手搓 RelevanceCriteria 得到的指纹与
    真机那次（合成检索意图产出）不同，`relevance_store` 全查不到 → eligible 全空 →
    选入 0 篇，整个实验静默变成空跑。
    """
    from paper_agent.application.literature_composition import (
        build_acceptance_resolver,
        build_writing_orchestrator,
    )
    from paper_agent.config import load_config
    from paper_agent.providers.factory import (
        build_llm_provider,
        build_retrieval_provider,
        build_reviewer_llm_provider,
    )
    from paper_agent.literature.preflight import effective_constraints
    from paper_agent.session.facts import SessionFacts
    from paper_agent.utils.dotenv import load_dotenv

    from runner import _build_quota

    load_dotenv()
    cfg = load_config()
    cfg.literature_writing_integration = True

    raw = json.loads(_FACTS.read_text(encoding="utf-8"))
    store = raw["relevance_store"]
    if not store:
        raise SystemExit("relevance_store 为空,先跑一次 runner 攒事实")
    fingerprint = store[0]["task_fingerprint"]
    judge_key = store[0]["judge_key"]

    facts = SessionFacts.load(str(_FACTS))
    orch = build_writing_orchestrator(
        cfg,
        writer_llm=build_llm_provider(cfg),
        reviewer_llm=build_reviewer_llm_provider(cfg),
        retrieval_provider=build_retrieval_provider(cfg),
        facts=facts,
    )

    class _FixedCriteria:
        """只需 fingerprint()：build_bundle 拿它查 relevance_store。"""

        def fingerprint(self):
            return fingerprint

    quota = _build_quota(20, 5, 15)
    eff = effective_constraints(quota, (), operation_id="ab",
                               task_fingerprint=fingerprint)
    return {
        "cfg": cfg, "facts": facts, "orch": orch, "quota": quota, "eff": eff,
        "criteria": _FixedCriteria(), "judge_key": judge_key,
        "acceptance": build_acceptance_resolver(cfg)("引言"),
        "records": list(facts.ledger.all_records()),
    }


def _lower_bound_of(eff, dim_name: str) -> int:
    from paper_agent.literature.evidence import _lower_bound
    from paper_agent.literature.quota import CountDimension

    return _lower_bound(eff, getattr(CountDimension, dim_name))


def _relaxed_eff(eff, total: int):
    """把 TOTAL 下限降到 `total`，其余约束原样保留。"""
    from paper_agent.literature.quota import CountDimension, CountOperator

    out = []
    for c in eff:
        if c.dimension is CountDimension.TOTAL:
            out.append(type(c)(CountDimension.TOTAL, CountOperator.MIN, total))
        else:
            out.append(c)
    return tuple(out)


def _floor_topup_line(ctx, bundle, used: int, *, arm_eff=None) -> str:
    """把选入的篇目拆成「下限必需」与「第 5 步补满」两段。

    P-3a 质疑的是**第 5 步**（剩余预算补高相关），不是整个 bundle。不拆开就分不清
    「20 篇里 20 篇都是配额要求的」和「下限只要 5 篇、另 15 篇是补满塞的」——
    前者与 context rot 无关（少给就违背用户明确的引用数量要求），后者才是那个质疑。

    `build_bundle` 的选取顺序是 2→5 步依次 append，所以前 `floor_n` 条即下限部分；
    这与 runner 的算法一致，两处口径必须相同。`arm_eff` 是该臂实际用的约束
    （A 臂下探过 total，不能用 `ctx["eff"]`，否则 floor 会被高估）。
    """
    eff = arm_eff if arm_eff is not None else ctx["eff"]
    need_total = _lower_bound_of(eff, "TOTAL")
    need_zh = _lower_bound_of(eff, "LANGUAGE_ZH")
    need_en = _lower_bound_of(eff, "LANGUAGE_EN")
    want_n = max(need_total, need_zh + need_en)
    n = len(bundle.entries)
    floor_n = min(want_n, n)
    floor_cost = sum(e.char_cost() for e in bundle.entries[:floor_n])
    extra_n = n - floor_n
    if extra_n <= 0:
        # 夹紧过就必须说出来：`下限 13 篇` 会把"候选只有 13 篇、配额其实要 20 篇"
        # 读成"配额只要 13 篇"，而这正是本函数要区分开的两种情形之一。
        short = f",配额下限 {want_n} 篇但候选只够 {n} 篇" if want_n > n else ""
        return (f"第5步补满: 未触发（选入 {n} 篇全是配额必需{short}）")
    pct = (used - floor_cost) * 100 // max(1, used)
    return (f"第5步补满: 下限需 {floor_n} 篇({floor_cost} 字符), "
            f"补满多塞 {extra_n} 篇({used - floor_cost} 字符 = {pct}% 的证据字符)")


def _build_arm(ctx, ladder, budget, *, eff=None):
    from paper_agent.literature.evidence import build_bundle

    return build_bundle(
        ctx["records"], criteria=ctx["criteria"], quota=ctx["quota"],
        relevance_store=ctx["facts"].relevance_store,
        effective_constraints=(eff if eff is not None else ctx["eff"]),
        budget_chars=budget,
        classifier=ctx["orch"]._classifier, policy=ctx["orch"]._policy,
        judge_key=ctx["judge_key"], acceptance=ctx["acceptance"],
        abstract_ladder=ladder,
        # 必须显式关掉退回题录档（X-10 A 之后它是默认开的）：本实验问的是
        # 「同样预算，少而全 vs 多而截」，三臂的自变量只能是 `abstract_ladder`。
        # 留着默认档，A 臂就会从"少给几篇全摘要"变成"给满篇数但部分只有题录",
        # 与**已归档的 25rec 真机付费结果**不再可比。要评测退题录档得另开一臂。
        drop_ladder=(),
    )


def _compose(ctx, budget: int):
    """组成分析：三臂各自选入什么。离线,零 API 成本。"""
    from paper_agent.literature.evidence import BundleStatus

    _emit(f"\n{'=' * 68}")
    _emit(f"组成分析（预算固定 {budget} 字符，离线）")
    _emit("=" * 68)

    zh_floor = _lower_bound_of(ctx["eff"], "LANGUAGE_ZH")
    if zh_floor:
        import collections
        buckets = collections.Counter(
            ctx["orch"]._classifier(r).quota_bucket for r in ctx["records"])
        _emit(f"\n注：配额要求 zh>={zh_floor}，但本语料语言桶分布为 {dict(buckets)}，"
              f"故各臂 zh 恒为 0。")
        _emit("    这不是 bundle 的缺陷：语言缺口由上游 assess_availability 在"
              " NEEDS_GAP_DECISION 拦（literature_flow.py:263），")
        _emit("    本脚本直接调 build_bundle 绕过了那一步。三臂同受此限，对比仍成立。")

    results = {}
    for name, ladder in _ARMS:
        eff = None
        if name == "A_full":
            # 先按原配额试一次,把「旧实现在这里就死了」这个事实记录下来
            strict = _build_arm(ctx, ladder, budget)
            if strict.status is not BundleStatus.OK:
                _emit(f"\n[A_full] 按原配额(total=20)全摘要装不下: "
                      f"需 {strict.required_chars} > {budget}, "
                      f"差 {strict.shortfall_chars()}")
                # 从 20 往下实测第一个装得下的篇数（成本估算会偏乐观，见上）
                relaxed_to = None
                for cand in range(20, 0, -1):
                    trial_eff = _relaxed_eff(ctx["eff"], cand)
                    if _build_arm(ctx, ladder, budget,
                                  eff=trial_eff).status is BundleStatus.OK:
                        relaxed_to, eff = cand, trial_eff
                        break
                _emit(f"    ← 这就是旧实现的死点。为让 A 参与对比,实测下探 total 下限"
                      f"到 {relaxed_to}（= 接受偏差,改法 C 之前用户唯一的出路）。")
        res = _build_arm(ctx, ladder, budget, eff=eff)
        results[name] = res
        _ARM_EFF[name] = eff if eff is not None else ctx["eff"]
        if res.status is not BundleStatus.OK or res.bundle is None:
            _emit(f"\n[{name}] 装不下 → status={res.status.value} "
                  f"需 {res.required_chars} 差 {res.shortfall_chars()}")
            _emit("    这一臂在本预算下无法参与对比。")
            continue
        b = res.bundle
        used = sum(e.char_cost() for e in b.entries)
        zh = sum(1 for e in b.entries if e.language_bucket == "zh")
        en = sum(1 for e in b.entries if e.language_bucket == "en")
        _emit(f"\n[{name}] 降级={b.degradation} 上限={b.abstract_limit or '不限'}")
        _emit(f"    选入 {len(b.entries)} 篇（zh={zh} en={en}）, "
              f"占用 {used}/{budget}（{used * 100 // budget}%）")
        _emit(f"    截断 {b.truncated_entry_count()} 条, 落选 {b.truncated_count} 篇")
        ev = sum(1 for e in b.entries if e.evidence_available)
        _emit(f"    有摘要证据（可支撑 Claim）: {ev}/{len(b.entries)} 篇")
        _emit("    " + _floor_topup_line(ctx, b, used, arm_eff=eff))
        # 同题不同 DOI = 同一研究的会议/期刊两个版本（本语料的 InLoc 即 CVPR'18 与
        # TPAMI'19）。**不是重复入库**：标识符各自唯一,是两条合法记录,配额也该各记一篇。
        # 但写引言时通常只引其一,故作为「引用冗余」信号报出,供判断覆盖面的真实增量。
        titles = [(e.title or "").strip().lower() for e in b.entries]
        uniq_titles = len(set(titles))
        if uniq_titles != len(titles):
            _emit(f"    注：含 {len(titles) - uniq_titles} 组同题多版本（会议/期刊），"
                  f"独立研究约 {uniq_titles} 项 —— 正文通常只引其一")

    ok = {k: v for k, v in results.items()
          if v.status is BundleStatus.OK and v.bundle}
    # 语料太小时全摘要就装得下 ⇒ 截断阶梯根本不触发 ⇒ 三臂产出同一个 bundle。
    # 这时报告看着正常但**没有对比价值**，必须显式说出来，否则会被当成「三臂无差异」
    # 这个结论（13 条语料时实测如此：三臂全是 13 篇、21950 字符、degradation=full）。
    sigs = {tuple(r.bundle.reference_ids()) for r in ok.values()}
    if len(ok) >= 2 and len(sigs) == 1:
        _emit(f"\n⚠ 三臂产出完全相同（各 {len(next(iter(sigs)))} 篇）：本语料全摘要即装得下,")
        _emit("  截断阶梯未触发,故本次跑**不构成 A/B**。要有对比需更大语料")
        _emit("  （历史上 25 条时 A 装 15 篇、C 装 25 篇才有差异）。")
    if len(ok) >= 2:
        _emit("\n--- 覆盖面差异（同预算下多覆盖了哪些文献）---")
        base = min(ok.values(), key=lambda r: len(r.bundle.entries))
        base_ids = set(base.bundle.reference_ids())
        base_name = next(k for k, v in ok.items() if v is base)
        for name, res in ok.items():
            if res is base:
                continue
            extra = [e for e in res.bundle.entries
                     if e.reference_id not in base_ids]
            _emit(f"  {name} 比 {base_name} 多 {len(extra)} 篇：")
            for e in extra[:6]:
                _emit(f"    + [{e.language_bucket}] {(e.title or '')[:58]}")
            if len(extra) > 6:
                _emit(f"    ... 另 {len(extra) - 6} 篇")
        _emit("\n  多覆盖的文献若最终被正文引用 → 降级是净赚；若没被引用 → 纯浪费(P-3a)。")
        _emit("  这一步只能量到「可引用面」，引不引得动要靠 --generate 实测。")
    _dump_bundles(ctx, results)
    return results


_BUNDLE_DIR = _HERE


def _dump_bundles(ctx, results) -> None:
    """逐条明细落盘，每臂一个 `bundle_<arm>.jsonl`。

    runner 也写 `bundle.jsonl`，但那要一次联网真机跑；组成分析是离线的，能零成本
    得到同样的明细。`role` 字段是关键：floor / topup 决定了 P-3a 该看哪些条目。
    """
    from paper_agent.literature.evidence import BundleStatus

    for name, res in results.items():
        if res.status is not BundleStatus.OK or not res.bundle:
            continue
        b = res.bundle
        # A 臂下探过 total，必须用该臂实际的约束算 floor，否则会被高估
        eff = _ARM_EFF.get(name) or ctx["eff"]
        need = max(_lower_bound_of(eff, "TOTAL"),
                   _lower_bound_of(eff, "LANGUAGE_ZH") + _lower_bound_of(eff, "LANGUAGE_EN"))
        floor_n = min(need, len(b.entries))
        path = _BUNDLE_DIR / f"bundle_{name}.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i, e in enumerate(b.entries):
                fh.write(json.dumps({
                    "rank": i,
                    "role": "floor" if i < floor_n else "topup",
                    "rid": e.reference_id,
                    "chars": e.char_cost(),
                    "spans": len(e.spans),
                    "title": e.title,
                    "year": e.year,
                    "bucket": e.language_bucket,
                    "verdict": e.relevance_verdict,
                    "confidence": e.relevance_confidence,
                    "evidence_available": e.evidence_available,
                    # >0 = 摘要被截断，值为截断前长度（量降级损失了多少信息）
                    "abstract_truncated_from": e.abstract_truncated_from,
                }, ensure_ascii=False) + "\n")
        _emit(f"  逐条明细 → {path.name}（role=floor 为下限必需, topup 为第5步补满）")


def _describe_generation_cost(ctx, results) -> None:
    from paper_agent.literature.evidence import BundleStatus
    from paper_agent.generate.context_builder import build_evidence_block

    _emit(f"\n{'=' * 68}")
    _emit("生成对比（未运行）")
    _emit("=" * 68)
    cfg = ctx["cfg"]
    n = sum(1 for r in results.values()
            if r.status is BundleStatus.OK and r.bundle)
    _emit(f"  可比臂数={n}，每臂 1 次写作调用 → 共 {n} 次 LLM 调用")
    _emit(f"  写作模型: {cfg.llm_provider}/{cfg.llm_model}")
    for name, res in results.items():
        if res.status is BundleStatus.OK and res.bundle:
            block = build_evidence_block(res.bundle, "md")
            _emit(f"    {name}: 证据块 {len(block)} 字符")
    _emit("  加 --generate 才会真正发请求（会产生费用）。")


def _report_specificity(ctx, name: str, body: str, bundle) -> None:
    """量「引用了多少篇」之外的东西：正文有多具体，以及具体数据是否真有出处。

    引用率会误导 —— 三臂都能做到 ~100%（模型倾向把给它的文献都引一遍），但**引一句
    「该方法有效[7]」和引一句「提升 5%[7]」不是同等质量**。这里用「可核验的百分比数据」
    当具体性代理：既能区分深度，又能顺手查它是不是模型编的（编造比引不动更糟）。
    """
    import re

    abstracts = {r.reference_id: (r.abstract or "") for r in ctx["records"]}
    pcts = sorted(set(re.findall(r"\d+(?:\.\d+)?%", body)))
    if not pcts:
        _emit("    具体数据: 0 个百分比 —— 行文停留在定性描述")
        return

    grounded, invented, late = [], [], []
    limit = bundle.abstract_limit or 0
    for p in pcts:
        num = p.rstrip("%")
        holders = [a for a in abstracts.values() if num in a]
        if not holders:
            invented.append(p)
            continue
        grounded.append(p)
        # 只在截断线之后出现 → 截断档必然看不到它（量化降级的真实损失）
        if limit and all(num not in a[:limit] for a in holders):
            late.append(p)
    _emit(f"    具体数据: {len(pcts)} 个百分比,有摘要出处 {len(grounded)},"
          f"查无出处 {len(invented)}")
    if invented:
        _emit(f"    ⚠ 疑似编造(摘要中查不到): {invented[:6]} —— 比引不动更严重")
    if limit and late:
        _emit(f"    其中 {len(late)} 个只出现在摘要 {limit} 字符之后 → 本档看不到,"
              f"是降级的直接损失")


def _generate(ctx, results) -> None:
    """真机生成对比：三臂各写一次引言，比引用行为。**发请求、花钱**。"""
    from paper_agent.literature.evidence import BundleStatus
    from paper_agent.gates.diff_gate import extract_citation_tokens
    from paper_agent.generate.context_builder import build_add_section_messages

    _emit(f"\n{'=' * 68}")
    _emit("生成对比（真机，已发请求）")
    _emit("=" * 68)

    llm = ctx["orch"]._writer_llm if hasattr(ctx["orch"], "_writer_llm") else None
    if llm is None:
        from paper_agent.providers.factory import build_llm_provider
        llm = build_llm_provider(ctx["cfg"])

    ledger = ctx["facts"].ledger
    for name, res in results.items():
        if res.status is not BundleStatus.OK or not res.bundle:
            continue
        msgs = build_add_section_messages(
            title="引言", purpose="综述跨视角图像匹配与位姿估计的研究进展，指出现有方法的不足",
            structure_desc="按研究脉络展开，每个论断都要有文献支撑",
            subsections=[], refs=[], known_numbers=[], ext="md",
            bundle=res.bundle,
        )
        try:
            resp = llm.complete(msgs)
        except Exception as exc:
            _emit(f"\n[{name}] 生成失败: {type(exc).__name__}: {exc}")
            continue
        # LLMResponse 的正文字段是 `content`（不是 `text`）；reasoning 另存,不算正文
        body = getattr(resp, "content", None) or ""
        if not isinstance(body, str):
            _emit(f"\n[{name}] 返回类型异常: {type(body).__name__}")
            continue
        toks = extract_citation_tokens(body, "md")
        uniq = []
        for t in toks:
            if t not in uniq:
                uniq.append(t)
        resolved = [t for t in uniq
                    if ledger.resolve_token(t, ext="md").status.name == "RESOLVED"]
        supplied = len(res.bundle.entries)
        _emit(f"\n[{name}] 正文 {len(body)} 字符")
        _emit(f"    引用标记 {len(toks)} 次 / 去重 {len(uniq)} 个 / 可解析 {len(resolved)} 个")
        _emit(f"    实际引用/供给 = {len(resolved)}/{supplied} "
              f"（{len(resolved) * 100 // max(1, supplied)}%）")
        _emit(f"    → 未被引用的 {supplied - len(resolved)} 篇即该臂的浪费")
        bad = [t for t in uniq if t not in resolved]
        if bad:
            # 解析不了的标记 = 模型编的引用,比「没引用」更糟(会进正文成假引用)
            _emit(f"    ⚠ 无法解析的标记 {len(bad)} 个: {bad[:8]}")
        _report_specificity(ctx, name, body, res.bundle)
        out = _HERE / f"ab_gen_{name}.md"
        out.write_text(body, encoding="utf-8")
        _emit(f"    正文存档 → {out.name}（供人工判正文质量,计数看不出行文好坏）")


def main() -> int:
    ap = argparse.ArgumentParser(description="P-3a 三臂 A/B：上下文预算怎么分配")
    ap.add_argument("--budget", type=int, default=24000,
                    help="固定预算（三臂相同）；默认 24000 = 当前 config 默认值")
    ap.add_argument("--generate", action="store_true",
                    help="追加真机生成对比（会发 LLM 请求、产生费用）")
    args = ap.parse_args()

    if not _FACTS.exists():
        raise SystemExit(f"缺少事实文件 {_FACTS}，先跑一次 runner")

    sys.path.insert(0, str(_HERE))          # 供 `from runner import ...`
    ctx = _load_context()
    _emit(f"入库记录 {len(ctx['records'])} 条，预算 {args.budget} 字符")

    results = _compose(ctx, args.budget)
    if args.generate:
        _generate(ctx, results)
    else:
        _describe_generation_cost(ctx, results)

    out = _out_path(len(ctx["records"]))
    # 真机 `--generate` 的结果是**花过钱的**，不许被离线重跑悄悄盖掉。
    if out.exists() and not args.generate and "已发请求" in out.read_text(encoding="utf-8"):
        alt = out.with_suffix(".offline.txt")
        _emit(f"\n注意：{out.name} 含真机生成结果，本次为离线跑，改写入 {alt.name}")
        out = alt
    out.write_text("\n".join(_log) + "\n", encoding="utf-8")
    _emit(f"\n完整报告 → {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
