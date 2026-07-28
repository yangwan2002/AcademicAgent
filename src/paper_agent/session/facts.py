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

# 注意：literature 包依赖本模块的 KnownRef（literature.models / ledger 都 import 它），
# 故本模块**不能**在顶层 import literature，否则成环。全部 literature 导入惰性化。


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

        self.path = path
        self.ledger = ledger if ledger is not None else ReferenceLedger()
        self.known_numbers: list[KnownNumber] = list(known_numbers or [])
        self.claims: dict = dict(claims) if claims is not None else {}
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
        from paper_agent.literature.migration import atomic_write_json, suggestion_to_dict

        data = {
            "schema_version": 2,
            "references": self.ledger.to_json(),
            "dedup_suggestions": [suggestion_to_dict(s) for s in self.ledger.suggestions()],
            "known_numbers": [asdict(n) for n in self.known_numbers],
            "claims": self.claims,
        }
        atomic_write_json(self.path, data)

    @classmethod
    def load(cls, path: str) -> "SessionFacts":
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

        return cls(path=path, known_numbers=known_numbers, claims=claims, ledger=ledger)

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


__all__ = ["KnownRef", "KnownNumber", "SessionFacts", "project_facts_path"]
