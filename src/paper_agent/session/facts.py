"""SessionFacts（A1 起以 ReferenceLedger 为文献后端）。

渐进式积累的事实库。字段：
- **文献**：由 :class:`~paper_agent.literature.ledger.ReferenceLedger` 承载
  （唯一可写真相源）。``known_refs`` 降级为**只读投影 + 过渡 setter**。
- ``known_numbers``：已知数字（普通 list，行为不变）。
- ``claims``：跨 chat 复用的项目记忆（普通 dict，行为不变）。

**两条红线**（A1 行为逐字节不变）：
1. ``bibkeys()`` / ``ref_ids()`` 返回 **resolvable** 集合（含用户原稿的未核验引用），
   不是 citable——否则 RewriteGate 会把用户原有引用判成非法。
2. ``SessionFacts(path=..., known_refs=[...])`` 的构造签名保留（四处测试依赖），
   故本类用**自定义 __init__**，不用 dataclass 自动生成（property 与 dataclass
   的 __init__ 不兼容）。

持久化：facts.json **v2**（schema_version=2 + references + dedup_suggestions +
known_numbers + claims），经原子写回；加载兼容 v1（known_refs）与混合 schema。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, asdict
from typing import TYPE_CHECKING

# 注意：literature 包依赖本模块的 KnownRef（literature.models / ledger 都 import 它），
# 故本模块**不能**在顶层 import literature，否则成环。全部 literature 导入惰性化。
if TYPE_CHECKING:
    # 仅供类型标注：TYPE_CHECKING 块运行时不执行，故不会与上述循环依赖冲突。
    # 靠 `from __future__ import annotations` 让签名里的标注延迟求值。
    from paper_agent.literature.citation_usage import CitationUsage
    from paper_agent.literature.ledger import ReferenceLedger


def project_facts_path(workspace_root: str, source_path: str) -> str:
    """由源文件绝对路径派生稳定的 project facts 路径，使 claims 跨 chat 复用。"""
    key = hashlib.sha1(os.path.abspath(source_path).encode("utf-8")).hexdigest()[:12]
    return os.path.join(workspace_root, f"proj_{key}", "facts.json")


@dataclass
class KnownRef:
    """一条已知文献的**只读投影**。``bibkey`` 供 .tex 的 \\cite{}，``id`` 供 .md/.docx 的 [id]。"""

    id: str
    bibkey: str = ""
    title: str = ""
    verified: bool = False


@dataclass
class KnownNumber:
    """一个已知数字。``source`` 标注来源，``confirmed`` 表示用户/抽取已确认真实。"""

    value: str
    source: str = ""
    confirmed: bool = False


class SessionFacts:
    def __init__(
        self,
        path: str,
        known_refs: list[KnownRef] | None = None,     # 过渡入参，A2 移除
        known_numbers: list[KnownNumber] | None = None,
        claims: dict | None = None,
        *,
        ledger: ReferenceLedger | None = None,
    ) -> None:
        from paper_agent.literature.ledger import ReferenceLedger

        from paper_agent.literature.citation_usage import CitationUsage
        from paper_agent.literature.relevance import RelevanceStore

        self.path = path
        self.ledger = ledger if ledger is not None else ReferenceLedger()
        self.known_numbers: list[KnownNumber] = list(known_numbers or [])
        self.claims: dict = dict(claims) if claims is not None else {}
        # A2c-5：单 envelope 同文件持久化的两段（默认空 → 向后兼容）。
        self.relevance_store = RelevanceStore()
        self.pending_batches: list[dict] = []   # 序列化的 pending batch（staged/confirmed）
        self.committed_batch_ids: list[str] = []  # 幂等键
        # A2d-4：可恢复的写作文献流 checkpoint（不含 Bundle/摘要/查询原文）。
        self.pending_literature_flows: list[dict] = []
        self.corrupt_flow_count: int = 0        # 加载时被隔离的损坏条目数（报告用）
        # A2f：暂停时冻结的检索意图（canonical payload；会话状态，不进 trace）。
        self.pending_search_intents: list[dict] = []
        self.corrupt_search_intent_count: int = 0
        # P-2：正文实际引用的章节归属（`refresh_citation_usage` 重算）。
        self.citation_usage = CitationUsage()
        if known_refs:
            self.ledger.ingest_known_refs(known_refs)

    def __repr__(self) -> str:  # dataclass 的 __repr__ 已失去，手写便于调试
        return (
            f"SessionFacts(path={self.path!r}, refs={len(self.ledger)}, "
            f"numbers={len(self.known_numbers)})"
        )

    # ---- 文献：只读投影 + 过渡 setter ----
    @property
    def known_refs(self) -> list[KnownRef]:
        return [r.to_known_ref() for r in self.ledger.all_records()]

    @known_refs.setter
    def known_refs(self, scanned: list[KnownRef]) -> None:
        self.replace_scanned_refs(scanned)

    def replace_scanned_refs(self, scanned: list[KnownRef]) -> int:
        """过渡 API：按 merge 语义把扫描到的引用并入（不覆盖已核验），返回新增条数。"""
        return self.merge_refs(scanned)

    # ---- 持久化 ----
    def save(self) -> None:
        from paper_agent.literature.migration import atomic_write_json

        atomic_write_json(self.path, self.to_envelope())

    def to_envelope(self) -> dict:
        """整个 project facts 的单一 envelope（A2c-5：Ledger + RelevanceStore + pending batch）。"""
        from paper_agent.literature.migration import suggestion_to_dict

        return {
            "schema_version": 4,
            "references": self.ledger.to_json(),
            "dedup_suggestions": [suggestion_to_dict(s) for s in self.ledger.suggestions()],
            "known_numbers": [asdict(n) for n in self.known_numbers],
            "claims": self.claims,
            "relevance_store": self.relevance_store.to_json(),
            # P-2 唯一真缺口：正文实际引用的章节归属。派生数据（可从正文重算），
            # 但落盘是必要的 —— B-2 写 bibitem 需要它，而重算要有 working copy 在手。
            "citation_usage": self.citation_usage.to_json(),
            "pending_batches": list(self.pending_batches),
            "committed_batch_ids": list(self.committed_batch_ids),
            "pending_literature_flows": list(self.pending_literature_flows),
            "pending_search_intents": list(self.pending_search_intents),
        }

    # ---- 写作文献流 checkpoint（A2d-4；envelope 单一事实源）----
    def put_literature_flow(self, flow: dict) -> None:
        """按 flow_id 覆盖写入一条 checkpoint。"""
        fid = flow.get("flow_id")
        if not fid:
            raise ValueError("flow 缺少 flow_id")
        self.pending_literature_flows = [
            f for f in self.pending_literature_flows if f.get("flow_id") != fid
        ] + [flow]

    def get_literature_flow(self, flow_id: str) -> dict | None:
        for f in self.pending_literature_flows:
            if f.get("flow_id") == flow_id:
                return f
        return None

    def drop_literature_flow(self, flow_id: str) -> bool:
        before = len(self.pending_literature_flows)
        self.pending_literature_flows = [
            f for f in self.pending_literature_flows if f.get("flow_id") != flow_id
        ]
        return len(self.pending_literature_flows) < before

    # ---- 冻结检索意图（A2f；envelope 单一事实源，会话状态不进 trace）----
    def put_search_intent(self, intent_id: str, payload: dict) -> None:
        """按 search_intent_id 覆盖写入冻结意图 payload。"""
        if not intent_id:
            raise ValueError("search intent 缺少 id")
        row = {"search_intent_id": intent_id, **payload}
        self.pending_search_intents = [
            s for s in self.pending_search_intents
            if s.get("search_intent_id") != intent_id
        ] + [row]

    def get_search_intent(self, intent_id: str) -> dict | None:
        for s in self.pending_search_intents:
            if s.get("search_intent_id") == intent_id:
                return s
        return None

    def drop_search_intent(self, intent_id: str) -> bool:
        before = len(self.pending_search_intents)
        self.pending_search_intents = [
            s for s in self.pending_search_intents
            if s.get("search_intent_id") != intent_id
        ]
        return len(self.pending_search_intents) < before

    @classmethod
    def load(cls, path: str) -> "SessionFacts":
        from paper_agent.literature.citation_usage import CitationUsage
        from paper_agent.literature.ledger import ReferenceLedger

        if not os.path.isfile(path):
            return cls(path=path)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        known_numbers = [KnownNumber(**n) for n in data.get("known_numbers", [])]
        claims = data.get("claims", {})

        has_v2 = "references" in data
        has_v1 = "known_refs" in data
        if has_v2:
            ledger = ReferenceLedger.from_json(
                data.get("references", []),
                data.get("dedup_suggestions", []),
            )
            # 混合 schema：把 v1 结构里 v2 尚无对应的条目一并迁入（不静默忽略）。
            if has_v1:
                ledger.ingest_known_refs(
                    [KnownRef(**r) for r in data.get("known_refs", [])]
                )
        elif has_v1:
            ledger = ReferenceLedger()
            ledger.ingest_known_refs([KnownRef(**r) for r in data.get("known_refs", [])])
        else:
            ledger = ReferenceLedger()

        facts = cls(path=path, known_numbers=known_numbers, claims=claims, ledger=ledger)
        # A2c-5：单 envelope 的两段（旧 v1/v2 无此段 → 按空，无损向后兼容）。
        facts.relevance_store.merge_from(data.get("relevance_store", []))
        facts.pending_batches = list(data.get("pending_batches", []))
        facts.committed_batch_ids = list(data.get("committed_batch_ids", []))
        # A2d-4：损坏的 flow 条目（非 dict / 缺 flow_id）被**隔离并计数**，不影响 Ledger 与其余 flow。
        flows: list[dict] = []
        corrupt = 0
        for row in data.get("pending_literature_flows", []) or []:
            if isinstance(row, dict) and row.get("flow_id"):
                flows.append(row)
            else:
                corrupt += 1
        facts.pending_literature_flows = flows
        facts.corrupt_flow_count = corrupt
        # A2f：冻结检索意图（旧 envelope 无此段 → 空；损坏条目隔离计数，同 flow 先例）。
        intents: list[dict] = []
        corrupt_si = 0
        for row in data.get("pending_search_intents", []) or []:
            if isinstance(row, dict) and row.get("search_intent_id"):
                intents.append(row)
            else:
                corrupt_si += 1
        facts.pending_search_intents = intents
        facts.corrupt_search_intent_count = corrupt_si
        # P-2：章节引用归属（旧 envelope 无此段 → 空表；可由 refresh_citation_usage 重算）。
        facts.citation_usage = CitationUsage.from_json(data.get("citation_usage", []))
        return facts

    # ---- 引用查询 / 变更（保持 resolvable 语义）----
    def ref_ids(self) -> set[str]:
        return self.ledger.resolvable_numeric_aliases()

    def bibkeys(self) -> set[str]:
        return self.ledger.resolvable_bibkeys()

    def next_ref_id(self) -> str:
        return self.ledger.next_numeric_alias()

    def add_ref(self, ref: KnownRef) -> bool:
        """加入一条文献；按 bibkey / id 去重。新增返回 True（语义同旧实现）。"""
        if (ref.bibkey and ref.bibkey in self.bibkeys()) or (
            ref.id and ref.id in self.ref_ids()
        ):
            return False
        return self.ledger.ingest_known_refs([ref]) > 0

    def merge_refs(self, scanned: list[KnownRef]) -> int:
        """合并扫描到的引用（P0#18：不覆盖已核验、只补空标题、新条目追加）。返回新增数。"""
        from paper_agent.literature.migration import known_ref_to_record

        added = 0
        for ref in scanned:
            rid = self._resolve_scanned(ref)
            if rid is not None:
                rec = self.ledger.get(rid)
                if rec is not None and not rec.title and ref.title:
                    self.ledger.replace(rec.with_changes(title=ref.title))
                continue                     # 不动 verified：已核验状态不被扫描降级
            self.ledger.add_record(known_ref_to_record(ref))
            added += 1
        return added

    def _resolve_scanned(self, ref: KnownRef) -> str | None:
        from paper_agent.literature.ledger import ResolutionStatus

        if ref.bibkey:
            res = self.ledger.resolve_token(ref.bibkey, ext="tex")
            if res.status == ResolutionStatus.RESOLVED:
                return res.reference_id
        if ref.id:
            res = self.ledger.resolve_token(ref.id, ext="md")
            if res.status == ResolutionStatus.RESOLVED:
                return res.reference_id
        return None

    def merge_numbers(self, scanned: list[KnownNumber]) -> int:
        added = 0
        known = self.number_values()
        for num in scanned:
            if num.value in known:
                continue
            self.known_numbers.append(num)
            known.add(num.value)
            added += 1
        return added

    # ---- 数字查询 / 变更 ----
    def number_values(self) -> set[str]:
        return {n.value for n in self.known_numbers}

    def add_number(self, value: str, source: str = "", confirmed: bool = True) -> None:
        if value not in self.number_values():
            self.known_numbers.append(
                KnownNumber(value=value, source=source, confirmed=confirmed)
            )

    # ---- claims（跨 chat 复用的项目记忆）----
    def set_section_intent(self, section_id: str, purpose: str) -> None:
        self.claims.setdefault("section_intents", {})[section_id] = purpose

    def set_section_structure(self, section_type: str, structure: str) -> None:
        self.claims.setdefault("section_structures", {})[section_type] = structure

    def get_section_structure(self, section_type: str) -> str | None:
        return self.claims.get("section_structures", {}).get(section_type)

    def set_claim(self, key: str, value) -> None:
        self.claims[key] = value

    def get_claim(self, key: str, default=None):
        return self.claims.get(key, default)

    # ---- 章节引用归属（P-2）----
    def refresh_citation_usage(self, text: str, ext: str) -> "CitationUsage":
        """按当前正文整体重算「哪章引了哪篇」，替换旧表并返回。

        整体重算而非增量：正文可被任意意图改写，增量维护要求每个写入路径都记得更新，
        那正是 `citation_aliases` 语义漂移的成因。**不自动 save** —— 调用方决定何时落盘，
        与本类其他 mutator 一致。
        """
        from paper_agent.literature.citation_usage import scan_usage

        self.citation_usage = scan_usage(text, ext, self.ledger)
        return self.citation_usage


__all__ = ["KnownRef", "KnownNumber", "SessionFacts", "project_facts_path"]
