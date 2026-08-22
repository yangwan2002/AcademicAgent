"""add_section SOP（卡 3）：为论文新增一个不存在的章节。

流程：（可选）搜文献 → 可观测生成 → 事实依据 Gate → 新数字确认 →
AddSectionGate（可选 CitationQuotaGate）→ 全部通过才在指定位置插入 working
（其他章节逐字不变）→ 更新 facts.claims（跨 chat 复用）→ 返回 diff 与预览。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import uuid4

from paper_agent.errors import OperationCancelled
from paper_agent.gates.diff_gate import AddSectionGate, data_numbers
from paper_agent.gates.grounding_gate import AddSectionGroundingGate
from paper_agent.generate.context_builder import (
    build_add_section_grounding_material,
    build_add_section_messages,
)
from paper_agent.intents.plans import AddSectionPlan
from paper_agent.literature.bibliography_sync import apply_bibliography
from paper_agent.intents.retry_feedback import (
    exhausted_message,
    is_stagnant,
    merge_violations,
    stagnation_message,
)
from paper_agent.intents.rewrite import ConfirmNumber
from paper_agent.observability.bus import NullEventBus
from paper_agent.observability.events import EventKind
from paper_agent.observability.sanitize import sanitize_messages, sanitize_text
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.session.facts import SessionFacts
from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.section_index import SectionIndex
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.literature import search_and_verify

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass
class AddSectionResult:
    ok: bool
    title: str
    attempts: int = 0
    preview: str = ""
    message: str = ""
    search_note: str = ""
    # A2d-5：带配额时的配额报告；无配额路径为 None → 结果结构与现状一致。
    quota_report: object | None = None


def _snippet(text: str, value: str, width: int = 40) -> str:
    i = text.find(value)
    if i < 0:
        return value
    return "…" + text[max(0, i - width): i + len(value) + width].replace("\n", " ") + "…"


def _raise_if_cancelled(cancel_token) -> None:
    """Cooperative cancellation boundary before prompts, gates, and mutations."""
    if cancel_token is not None and cancel_token.cancelled:
        raise OperationCancelled()


def execute_add_section(
    session: FileBackedSession,
    plan: AddSectionPlan,
    facts: SessionFacts,
    llm: LLMProvider,
    *,
    retrieval_provider: RetrievalProvider,
    verifier: CitationVerifier,
    confirm: ConfirmNumber,
    max_retry: int = 2,
    bus=None,
    cancel_token=None,
    citation_context=None,
) -> AddSectionResult:
    index = SectionIndex(session.read_working(), session.ext)
    if index.find(plan.title) is not None:
        return AddSectionResult(
            ok=False, title=plan.title,
            message=f"章节「{plan.title}」已存在，请用 rewrite 修改。",
        )
    _raise_if_cancelled(cancel_token)

    # 搜文献子 SOP。**带配额时跳过**：Preflight 已是唯一检索入口（Req 13.12 / design D8）。
    search_note = ""
    if citation_context is not None:
        search_note = "（带引用配额：文献已由写作前预检备齐，未再触发旧搜索流程。）"
    elif plan.want_search and plan.search_query:
        out = search_and_verify(
            plan.search_query, plan.search_limit, retrieval_provider, verifier, facts
        )
        search_note = out.error or (
            f"搜「{plan.search_query}」：请求 {out.requested} 条，"
            f"核验通过 {out.verified} 条，新增 {len(out.added)} 条。"
        )
        facts.save()
    _raise_if_cancelled(cancel_token)

    heading = index.render_heading(plan.title, level=1)
    expected_subs = plan.subsections if plan.structure_kind in ("list", "mimic") else None
    gate = AddSectionGate(session.ext, facts)
    grounding_gate = AddSectionGroundingGate(llm)
    # 当前 working copy 本身就是允许的事实依据；其中已有数字不应再次弹用户确认。
    # 只作本轮 Gate allow-list，不写入 project facts（避免把正文扫描结果冒充用户确认）。
    document_numbers = data_numbers(index.text)
    known_numbers = sorted(facts.number_values() | document_numbers)
    confirmed: set[str] = set()
    violations: list[str] = []
    attempts = 0
    result = None
    stagnated = False

    quota_gate = None
    bundle = getattr(citation_context, "bundle", None)
    if citation_context is not None:
        from paper_agent.gates.citation_quota_gate import CitationQuotaGate
        quota_gate = CitationQuotaGate()
    quota_result = None
    source_text = getattr(plan, "source_text", "") or ""
    grounding_material = build_add_section_grounding_material(
        current_document=index.text,
        source_text=source_text,
        claims=facts.claims,
        refs=facts.known_refs,
        known_numbers=known_numbers,
        ext=session.ext,
        bundle=bundle,
    )

    bus = bus or NullEventBus()
    generation_id = uuid4().hex
    grounding_result = None
    block = ""
    with bus.generation_scope(generation_id):
        for attempt in range(1, max_retry + 2):
            with bus.attempt_scope(attempt), bus.span("generation_attempt") as attempt_span:
                attempt_status = "error"
                try:
                    _raise_if_cancelled(cancel_token)
                    bus.publish(EventKind.GENERATION_ATTEMPT_START, attempt=attempt)
                    if attempt > 1:
                        bus.publish(EventKind.GATE_RETRY, attempt=attempt)

                    messages = build_add_section_messages(
                        plan.title, plan.purpose, plan.structure_desc, plan.subsections,
                        facts.known_refs, known_numbers,
                        session.ext, facts.claims, violations or None, bundle=bundle,
                        source_text=source_text,
                        current_document=index.text,
                    )
                    call_id = uuid4().hex
                    candidate_id = uuid4().hex
                    with bus.llm_call_scope(call_id), bus.span("llm_call"):
                        try:
                            bus.publish(
                                EventKind.LLM_CALL_START,
                                model=_model_name(llm),
                                call_mode="complete",
                                prompt=sanitize_messages(messages),
                            )
                            kwargs = _provider_kwargs(bus)
                            resp = llm.complete(messages, **kwargs)
                            attempts += 1
                            body = (resp.content or "").strip()
                            usage = {
                                "prompt_tokens": resp.prompt_tokens,
                                "completion_tokens": resp.completion_tokens,
                            }
                            bus.publish(
                                EventKind.LLM_USAGE,
                                usage_status=(
                                    "reported"
                                    if any(v is not None for v in usage.values())
                                    else "unavailable"
                                ),
                                **usage,
                            )
                            bus.publish(
                                EventKind.LLM_CALL_END,
                                status="completed",
                                response=sanitize_text(body),
                                delta_count=0,
                            )
                        except OperationCancelled:
                            bus.publish(EventKind.LLM_CALL_END, status="cancelled")
                            raise
                        except Exception as exc:
                            bus.publish(
                                EventKind.LLM_CALL_END,
                                status="failed",
                                error=sanitize_text(str(exc)),
                            )
                            raise

                    _raise_if_cancelled(cancel_token)
                    block = f"{heading}\n{body}"

                    # 先做事实依据 Gate；未通过时不弹“确认新数字”，避免让用户通过一次
                    # 数字确认，间接为整段编造的研究设计/结果背书。
                    with bus.span("gate"):
                        bus.publish(EventKind.GATE_START)
                        grounding_result = grounding_gate.check(body, grounding_material)
                        # structured verifier 是不可中断的同步长调用；取消可能
                        # 在它返回 supported 前已发生。必须先重新取样 token，
                        # 否则这个过时的 supported 会继续走到 commit。
                        _raise_if_cancelled(cancel_token)

                        if grounding_result.passed:
                            for value in data_numbers(block) - (
                                facts.number_values() | document_numbers | confirmed
                            ):
                                _raise_if_cancelled(cancel_token)
                                accepted = confirm(value, _snippet(block, value))
                                # 用户在确认提示期间取消时，不得先把数字
                                # 写入 facts，再在之后的 commit 边界才检测到。
                                _raise_if_cancelled(cancel_token)
                                if accepted:
                                    confirmed.add(value)
                                    facts.add_number(
                                        value, source="user_confirmed", confirmed=True
                                    )

                        result = gate.check(
                            block, title=plan.title,
                            expected_subsections=expected_subs,
                            confirmed_numbers=confirmed | document_numbers,
                        )

                        # 带配额：同轮跑 CitationQuotaGate（新增章节 → original_text=""，
                        # 全部引用都须在 Bundle）。
                        quota_result = None
                        if quota_gate is not None:
                            quota_result = quota_gate.check(
                                block, ext=session.ext, bundle=bundle, ledger=facts.ledger,
                                quota=citation_context.quota,
                                effective_constraints=citation_context.effective_constraints,
                                original_text="", classifier=citation_context.classifier,
                                policy=citation_context.policy,
                            )
                        _raise_if_cancelled(cancel_token)

                        structural_violations = merge_violations(
                            result, quota_result, ledger=facts.ledger, ext=session.ext
                        )
                        combined_violations = (
                            list(grounding_result.violations) + structural_violations
                        )
                        passed = (
                            grounding_result.passed
                            and result.passed
                            and (quota_result is None or quota_result.passed)
                        )
                        bus.publish(
                            EventKind.GATE_RESULT,
                            passed=passed,
                            violations=combined_violations,
                            candidate_id=candidate_id,
                            source_llm_call_id=call_id,
                            structural_passed=result.passed,
                            grounding_passed=grounding_result.passed,
                            grounding_reason=grounding_result.reason_code,
                            grounding_unsupported_count=grounding_result.unsupported_count,
                            quota_passed=(None if quota_result is None else quota_result.passed),
                        )

                    if passed:
                        _raise_if_cancelled(cancel_token)
                        attempt_status = "accepted"
                        return _commit_add_section(
                            session, index, plan, block, facts, attempts,
                            search_note, bus, quota_result, ctx=citation_context,
                            cancel_token=cancel_token,
                        )

                    attempt_status = "rejected"
                    prev_violations = violations
                    violations = combined_violations
                    # verifier 自身不可靠或材料明确不足时，再生成也不能修复依据，立即
                    # 失败关闭；unsupported_facts 可回灌一次让生成器删掉无依据陈述。
                    if not grounding_result.retryable:
                        break
                    # 违规与上一轮逐字相同 ⇒ 回灌不再带来新信息，提前停。
                    if is_stagnant(prev_violations, violations):
                        stagnated = True
                        break
                except OperationCancelled:
                    attempt_status = "cancelled"
                    attempt_span.cancel()
                    raise
                finally:
                    bus.publish(
                        EventKind.GENERATION_ATTEMPT_END, status=attempt_status
                    )

    # 预算耗尽：只差配额（结构 Gate 通过 + 无非法引用）且用户显式接受 → 才写回
    if (quota_result is not None and result is not None and result.passed
            and grounding_result is not None and grounding_result.passed
            and not (quota_result.new_unresolved_tokens or quota_result.new_ambiguous_tokens
                     or quota_result.new_out_of_bundle_ids)):
        port = getattr(citation_context, "port", None)
        if port is not None:
            _raise_if_cancelled(cancel_token)
            decision = port.resolve_final_gate({
                "effective_gaps": [
                    (g.dimension.value, g.amount) for g in quota_result.effective_gaps
                ],
                "original_gaps": [
                    (g.dimension.value, g.amount) for g in quota_result.original_gaps
                ],
                "used": list(quota_result.used_reference_ids),
            })
            _raise_if_cancelled(cancel_token)
            if getattr(decision, "action", "") == "accept":
                return _commit_add_section(session, index, plan, block, facts, attempts,
                                           search_note, bus, quota_result,
                                           ctx=citation_context, accepted_final_gate=True,
                                           cancel_token=cancel_token)

    grounding_message = _grounding_failure_message(
        grounding_result,
        title_only_search=bool(
            citation_context is None and plan.want_search and plan.search_query
        ),
    )
    return AddSectionResult(
        ok=False, title=plan.title, attempts=attempts,
        message=(grounding_message or
                 (stagnation_message(attempts, violations) if stagnated
                  else exhausted_message(attempts, violations))),
        search_note=search_note,
        quota_report=_quota_report(quota_result, citation_context) if quota_result else None,
    )


def _model_name(llm: LLMProvider) -> str:
    for attr in ("model_name", "_model", "model"):
        val = getattr(llm, attr, None)
        if isinstance(val, str) and val:
            return val
    return type(llm).__name__


def _provider_kwargs(bus) -> dict:
    sink = bus.as_sink_or_none()
    return {"event_sink": sink} if sink is not None else {}


def _grounding_failure_message(result, *, title_only_search: bool = False) -> str:
    if result is None or result.passed:
        return ""
    if result.reason_code == "verification_failed":
        return (
            "新增章节未写入：事实依据校验未能可靠完成，严格模式已安全拒绝。"
            "请稍后重试。"
        )
    if title_only_search:
        return (
            "新增章节未写入：候选正文包含当前文档或显式用户材料"
            "无法支持的事实陈述。本次使用的旧检索路径即使核验成功，"
            "也只会向写作器提供题录，"
            "题录不能支撑具体结论；请补充可核验的摘要、原文或项目材料。"
        )
    return (
        "新增章节未写入：候选正文包含当前文档或显式用户材料无法支持的事实陈述。"
        "请直接补充可核验的研究设计、数据、结果、摘要或原文材料。"
    )


def _quota_report(qr, ctx=None, *, accepted_final_gate: bool = False):
    from paper_agent.literature.citation_context import QuotaReport

    if qr is None:
        return None
    return QuotaReport(
        original_gaps=qr.original_gaps, effective_gaps=qr.effective_gaps,
        used_reference_ids=qr.used_reference_ids,
        unused_reference_ids=qr.unused_reference_ids,
        accepted_final_gate=accepted_final_gate,
        deviations=getattr(ctx, "deviations", ()) if ctx is not None else (),
    )


def _commit_add_section(session, index, plan, block, facts, attempts, search_note, bus,
                        quota_result, *, ctx=None, accepted_final_gate: bool = False,
                        cancel_token=None):
    offset = index.insertion_offset(plan.position)
    new_full = index.insert_text(offset, f"\n\n{block}\n")
    # 与正文同一次写入，理由见 apply_bibliography docstring（:undo 原子性）。
    new_full, bib_report = apply_bibliography(new_full, session.ext, facts.ledger)
    # commit point：此后 working + facts 按一个不可中断单元完成。
    _raise_if_cancelled(cancel_token)
    session.write_working(new_full, message=f"add_section {plan.title}", bus=bus)
    _update_claims(facts, plan)
    facts.save()
    note = f"已新增章节「{plan.title}」（{attempts} 次生成通过校验）。"
    if accepted_final_gate:
        note += "（配额未完全达标，已按你的确认接受当前结果。）"
    if bib_report is not None and bib_report.message:
        note += "\n" + bib_report.message
    return AddSectionResult(
        ok=True, title=plan.title, attempts=attempts, preview=block[:300],
        message=note, search_note=search_note,
        quota_report=_quota_report(quota_result, ctx, accepted_final_gate=accepted_final_gate),
    )


def _update_claims(facts: SessionFacts, plan: AddSectionPlan) -> None:
    facts.set_section_intent(plan.section_type, plan.purpose)
    structure_repr = ", ".join(plan.subsections) if plan.subsections else plan.structure_desc
    if structure_repr:
        facts.set_section_structure(plan.section_type, structure_repr)


__all__ = ["AddSectionResult", "execute_add_section"]
