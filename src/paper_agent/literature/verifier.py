"""ReferenceVerifier：把核验尝试翻译成核验事件（A1 / 任务 11）。

薄包 :class:`~paper_agent.tools.citation.CitationVerifier`——只做**结果翻译**，
不改变核验时机、不增加网络调用。A1 只做**一次回查**（沿用 ``search_and_verify`` 的
单次调用），按 ``WORK_PRIORITY`` 取第一个可用 work identifier 作为该次核验的类型/值。
多标识符交叉核验属 A2。

``require_final`` 控制核验事件的 ``authority``：

- ``False``（默认，A1）：成功核验一律记为 ``final`` → 状态 VERIFIED，与现状一致。
  A1 尚未接 Crossref，若此时强制 final，OpenAlex 对 DOI 只有佐证权 →
  现有 ``search_and_verify`` 的入库结果会从「核验通过」静默变成「仅佐证」。
- ``True``（A2 接入 Crossref 后）：按 :func:`verification_authority` 的标识符类型级
  权限决定 final / corroboration。
"""

from __future__ import annotations

from datetime import datetime, timezone

from paper_agent.literature import identifiers as _ids
from paper_agent.literature.sources import provenance_of, verification_authority
from paper_agent.literature.verification import (
    EventKind,
    VerificationEvent,
    make_event,
)
from paper_agent.tools.citation import CitationVerifier
from paper_agent.workspace.models import ReferenceEntry


class ReferenceVerifier:
    def __init__(
        self,
        citation_verifier: CitationVerifier,
        *,
        require_final: bool = False,
    ) -> None:
        self._cv = citation_verifier
        self._require_final = require_final

    def _lookup_identifier(self, candidate: ReferenceEntry) -> tuple[str, str]:
        """选定本次回查的 (类型, 值)：优先 work_identifiers，回退 source_id。"""
        picked = _ids.pick_by_priority(candidate.work_identifiers)
        if picked is not None:
            return picked
        raw = candidate.source_id or ""
        return (_ids.classify(raw) or "", raw)

    def assess(self, candidate: ReferenceEntry) -> VerificationEvent:
        """对候选做一次核验并产出一条 ``VerificationEvent``。"""
        id_type, id_value = self._lookup_identifier(candidate)
        source = candidate.source or ""
        attempt = self._cv.verify_detailed(candidate)

        if attempt.outcome == EventKind.VERIFIED:
            authority = (
                verification_authority(source, id_type)
                if self._require_final
                else "final"
            )
        else:
            authority = verification_authority(source, id_type)

        return make_event(
            attempt.outcome,
            source=source,
            provenance=provenance_of(source),
            authority=authority,
            identifier_type=id_type,
            identifier=id_value,
            checked_at=datetime.now(timezone.utc).isoformat(),
            details=attempt.note,
        )


__all__ = ["ReferenceVerifier"]
