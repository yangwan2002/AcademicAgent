"""SessionFacts（A3，Week 3）。

渐进式积累的事实库，替代 v1 重量级 artifact。字段（均可选）：
- ``known_refs``   ：已知文献（Layer 1 抽取 + rewrite 中搜文献新增）
- ``known_numbers``：已知数字（来源标注 + 是否用户确认）——供 rewrite gate 判断
  「新出现的数字」是否为编造
- ``claims``       ：对话中累积的核心声明（novelty / gap / contributions ...）

持久化：``<workspace>/<sid>/facts.json``。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict


def project_facts_path(workspace_root: str, source_path: str) -> str:
    """由源文件绝对路径派生稳定的 project facts 路径，使 claims 跨 chat 复用。

    working copy 按 session 隔离（临时），而 facts/claims 属于「这篇论文」，
    因此按源文件路径哈希落到固定位置。
    """
    key = hashlib.sha1(os.path.abspath(source_path).encode("utf-8")).hexdigest()[:12]
    return os.path.join(workspace_root, f"proj_{key}", "facts.json")


@dataclass
class KnownRef:
    """一条已知文献。``bibkey`` 供 .tex 的 \\cite{} 用，``id`` 供 .md/.docx 的 [id] 用。"""

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


@dataclass
class SessionFacts:
    path: str
    known_refs: list[KnownRef] = field(default_factory=list)
    known_numbers: list[KnownNumber] = field(default_factory=list)
    claims: dict = field(default_factory=dict)

    # ---- 持久化 ----
    def save(self) -> None:
        data = {
            "known_refs": [asdict(r) for r in self.known_refs],
            "known_numbers": [asdict(n) for n in self.known_numbers],
            "claims": self.claims,
        }
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "SessionFacts":
        if not os.path.isfile(path):
            return cls(path=path)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(
            path=path,
            known_refs=[KnownRef(**r) for r in data.get("known_refs", [])],
            known_numbers=[KnownNumber(**n) for n in data.get("known_numbers", [])],
            claims=data.get("claims", {}),
        )

    # ---- 引用查询 / 变更 ----
    def ref_ids(self) -> set[str]:
        return {r.id for r in self.known_refs if r.id}

    def bibkeys(self) -> set[str]:
        return {r.bibkey for r in self.known_refs if r.bibkey}

    def next_ref_id(self) -> str:
        nums = [int(r.id) for r in self.known_refs if r.id.isdigit()]
        return str(max(nums) + 1 if nums else 1)

    def add_ref(self, ref: KnownRef) -> bool:
        """加入一条文献；按 bibkey / id 去重。新增返回 True。"""
        keys = self.bibkeys() | self.ref_ids()
        if (ref.bibkey and ref.bibkey in keys) or (ref.id and ref.id in self.ref_ids()):
            return False
        self.known_refs.append(ref)
        return True

    def merge_refs(self, scanned: list[KnownRef]) -> int:
        """把从源文件扫出的引用**合并**进已有文献库，返回新增条数。

        **绝不覆盖**（P0/#18）：直接 `known_refs = extract_refs(...)` 会把此前经检索
        + 核验入库的文献整批丢掉，`verified` 也被重置。合并规则：
        - 已存在（按 bibkey 或 id 命中）→ 保留原记录，只在原标题为空时补标题；
          **不下调 verified**（已核验过的不会因为源文件里只有个 key 就变未核验）。
        - 不存在 → 追加。
        """
        added = 0
        by_bibkey = {r.bibkey: r for r in self.known_refs if r.bibkey}
        by_id = {r.id: r for r in self.known_refs if r.id}
        for ref in scanned:
            existing = by_bibkey.get(ref.bibkey) if ref.bibkey else None
            if existing is None and ref.id:
                existing = by_id.get(ref.id)
            if existing is not None:
                if not existing.title and ref.title:
                    existing.title = ref.title
                continue          # 不动 verified：已核验状态不可被源文件扫描降级
            self.known_refs.append(ref)
            if ref.bibkey:
                by_bibkey[ref.bibkey] = ref
            if ref.id:
                by_id[ref.id] = ref
            added += 1
        return added

    def merge_numbers(self, scanned: list[KnownNumber]) -> int:
        """合并源文件扫出的数字；已存在的保留原 source/confirmed（同上，不覆盖）。"""
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
