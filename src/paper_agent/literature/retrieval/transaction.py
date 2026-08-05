"""CommitPlan：纯内存提交试算 + 幂等/revision 校验（A2c-2 / 任务 4，design D7 内存部分）。

**本期只在内存副本上试算**：校验 batch=CONFIRMED、幂等键、MERGE 的乐观并发基线，
在 **Ledger 深拷贝副本**上应用 ACCEPT_NEW/MERGE 得「拟提交后账本」，**不 mutate 主
Ledger、不写磁盘、不宣称跨文件原子性**。

真正的单 envelope 原子落盘（Ledger + RelevanceStore + pending batch 一次原子写）+
崩溃恢复 + 故障注入测试属 **A2c-5**；本模块只交付试算 + 冲突检测骨架。

commit 是**决定的忠实执行者**：disposition（ACCEPT_NEW/MERGE/…）在 Attempt（A2c-3）已定，
plan_commit 只据 `canonical_record_payload` 材料化，**不再重跑去重/改判**（ACCEPT_NEW 用
`add_record` 忠实落位，不触发 upsert 的二次去重）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from paper_agent.literature.ledger import ReferenceLedger
from paper_agent.literature.migration import record_from_dict
from paper_agent.literature.retrieval.batch import (
    BatchDisposition,
    BatchStatus,
    ProposedRecordSnapshot,
    RetrievalBatch,
)


@dataclass(frozen=True)
class CommitPlan:
    """提交试算结果（未落盘）。``projected_ledger_json`` 仅 ok 时非空。"""

    ok: bool
    idempotency_key: str
    ledger_delta: int = 0
    conflicts: tuple[str, ...] = ()
    projected_ledger_json: list | None = None
    already_committed: bool = False


def _record_from_payload(payload: str):
    return record_from_dict(json.loads(payload))


def _dealias(record, projected: ReferenceLedger):
    """把与已入账记录冲突的数字别名换成下一个空位。

    Attempt 期（attempt.py）按 `next_numeric_alias()` 预分配 `numeric_base`，**两个批次
    若在彼此提交前各自算基线，就会拿到同一段号**。`add_record` 是忠实追加、不校验别名
    唯一性 ⇒ 冲突直接落进账本。后果是 `resolve_token(ext="md")` 对该号返回 AMBIGUOUS：
    md 论文里这些引用一律判「无法解析」，配额永远差满额。

    这里只改**冲突的数字别名**（reference_id、bibkey、其他字段一律不动），保持 commit
    「忠实执行既定 disposition」的语义。
    """
    aliases = tuple(record.numeric_aliases or ())
    if not aliases:
        return record
    taken = projected.resolvable_numeric_aliases()
    if not any(a in taken for a in aliases):
        return record

    nums = [int(n) for n in taken if str(n).isdigit()]
    nxt = max(nums) + 1 if nums else 1
    out: list[str] = []
    for a in aliases:
        if a in taken:
            while str(nxt) in taken:
                nxt += 1
            a = str(nxt)
            nxt += 1
        taken = taken | {a}
        out.append(a)
    from dataclasses import replace as _replace
    return _replace(record, numeric_aliases=tuple(out))


def plan_commit(batch: RetrievalBatch, ledger: ReferenceLedger) -> CommitPlan:
    """在 Ledger 副本上试算提交，返回 CommitPlan（**不改主 Ledger、不落盘**）。"""
    key = batch.batch_id

    # 幂等：已 COMMITTED 的 batch 再 plan → 不重复增加
    if batch.status is BatchStatus.COMMITTED:
        return CommitPlan(ok=True, idempotency_key=key, already_committed=True)

    if batch.status is not BatchStatus.CONFIRMED:
        return CommitPlan(ok=False, idempotency_key=key,
                          conflicts=(f"not_confirmed:{batch.status.value}",))

    # 深拷贝副本（序列化往返），主 Ledger 全程不动
    projected = ReferenceLedger.from_json(ledger.to_json())
    conflicts: list[str] = []
    delta = 0

    for it in batch.items:
        d = it.disposition
        if d is BatchDisposition.ACCEPT_NEW:
            if it.proposed_record_snapshot is None:
                conflicts.append(f"accept_new_missing_payload:{it.item_id}")
                continue
            rec = _record_from_payload(it.proposed_record_snapshot.canonical_record_payload)
            rec = _dealias(rec, projected)     # 数字别名去碰撞（见 _dealias）
            projected.add_record(rec)          # 忠实落位，不二次去重
            delta += 1
        elif d is BatchDisposition.MERGE_DUPLICATE:
            prs = it.proposed_record_snapshot
            if prs is None or not it.target_reference_id:
                conflicts.append(f"merge_missing_payload:{it.item_id}")
                continue
            target = projected.get(it.target_reference_id)
            if target is None:
                conflicts.append(f"merge_target_missing:{it.target_reference_id}")
                continue
            # 乐观并发：确认期间目标已变 → revision_conflict，禁止覆盖新数据
            if ProposedRecordSnapshot.hash_of(target) != prs.expected_base_hash:
                conflicts.append(f"revision_conflict:{it.target_reference_id}")
                continue
            projected.replace(_record_from_payload(prs.canonical_record_payload))
        # REUSE_EXISTING / REJECT：不改 Ledger

    ok = not conflicts
    return CommitPlan(
        ok=ok,
        idempotency_key=key,
        ledger_delta=delta,
        conflicts=tuple(conflicts),
        projected_ledger_json=projected.to_json() if ok else None,
    )



# ===========================================================================
# A2c-5：单 envelope 原子提交（Ledger + RelevanceStore + pending batch 一次落盘）
# ===========================================================================


@dataclass(frozen=True)
class CommitOutcome:
    ok: bool
    idempotency_key: str
    ledger_delta: int = 0
    conflicts: tuple[str, ...] = ()
    already_committed: bool = False


def commit_batch(facts, batch) -> CommitOutcome:
    """把 CONFIRMED batch 以**单 envelope 一次原子写**提交进 project facts（design D7）。

    步骤：plan_commit 试算 → 组装完整 envelope（projected ledger + relevance_store +
    去除本 batch 的 pending + committed_batch_ids）→ 一次 atomic_write_json → 磁盘成功后
    才换内存。**任一步失败：内存与磁盘的 Ledger/RelevanceStore/Batch 状态均不变。**
    """
    from paper_agent.literature.ledger import ReferenceLedger
    from paper_agent.literature.migration import atomic_write_json

    key = batch.batch_id

    # 幂等：已提交过 → 不重复增加
    if key in getattr(facts, "committed_batch_ids", []):
        return CommitOutcome(ok=True, idempotency_key=key, already_committed=True)

    plan = plan_commit(batch, facts.ledger)
    if not plan.ok:
        return CommitOutcome(ok=False, idempotency_key=key, conflicts=plan.conflicts)

    # 组装完整新 envelope：**从 facts.to_envelope() 派生**（避免新增段被漏掉），
    # 再覆盖 references / pending_batches / committed_batch_ids 三处。
    remaining_pending = [
        pb for pb in getattr(facts, "pending_batches", []) if pb.get("batch_id") != key
    ]
    envelope = dict(facts.to_envelope())
    envelope["references"] = plan.projected_ledger_json
    envelope["pending_batches"] = remaining_pending
    envelope["committed_batch_ids"] = list(getattr(facts, "committed_batch_ids", [])) + [key]

    # 一次原子写；失败则内存磁盘均不变（atomic_write_json 会清理 tmp）
    atomic_write_json(facts.path, envelope)

    # 磁盘成功后才换内存
    facts.ledger = ReferenceLedger.from_json(plan.projected_ledger_json)
    facts.pending_batches = remaining_pending
    facts.committed_batch_ids = list(envelope["committed_batch_ids"])
    batch.mark_committed()
    return CommitOutcome(ok=True, idempotency_key=key, ledger_delta=plan.ledger_delta)


__all__ = ["CommitPlan", "plan_commit", "CommitOutcome", "commit_batch"]
