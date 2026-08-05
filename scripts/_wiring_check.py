"""Active 模式装配冒烟：复刻 chat.py 的接线，全 mock，零网络。

只验证「装得起来 + 该注入的都注入了」，不跑 LLM 业务逻辑。
"""

from __future__ import annotations

import os
import sys
import tempfile

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# 全开新栈，全 mock provider
os.environ.update(
    PAPER_LLM="mock",
    PAPER_RETRIEVAL="mock",
    PAPER_INTENT_PLANNING_MODE="active",
    PAPER_LIT_WRITING_INTEGRATION="true",
    PAPER_LIT_SEARCH_SYNTHESIS="true",
    PAPER_TRACE="true",
)

from paper_agent.config import load_config
from paper_agent.providers.factory import (
    build_llm_provider,
    build_retrieval_provider,
    build_reviewer_llm_provider,
)
from paper_agent.session.facts import SessionFacts, project_facts_path
from paper_agent.session.file_session import FileBackedSession
from paper_agent.tools.citation import CitationVerifier
from paper_agent.coordinator import Coordinator
from paper_agent.application.console_interactor import ConsolePlanningInteractor
from paper_agent.application.turn_service import TurnService
from paper_agent.runtime.handlers import HandlerContext

FAIL: list[str] = []
OK: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (OK if cond else FAIL).append(f"{name}{(' — ' + detail) if detail else ''}")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wiring_")
    paper = os.path.join(tmp, "paper.tex")
    with open(paper, "w", encoding="utf-8") as f:
        f.write(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\section{Introduction}\nHello world.\n"
            "\\section{Method}\nWe propose a method.\n"
            "\\end{document}\n"
        )

    config = load_config()
    check("config.intent_planning_mode==active", config.intent_planning_mode == "active",
          config.intent_planning_mode)
    check("config.literature_writing_integration", config.literature_writing_integration is True)
    check("config.literature_search_synthesis", config.literature_search_synthesis is True)

    llm = build_llm_provider(config)
    retrieval = build_retrieval_provider(config)
    verifier = CitationVerifier(retrieval)
    session = FileBackedSession(paper, workspace_root=os.path.join(tmp, "ws"))
    facts = SessionFacts.load(project_facts_path(os.path.join(tmp, "ws"), paper))

    coordinator = Coordinator(enable_trace=True, runs_dir=os.path.join(tmp, "runs"))

    # --- 文献编排器装配（chat.py 同逻辑）---
    lit_orchestrator = lit_port = lit_classifier = lit_synthesizer = None
    from paper_agent.application.console_literature_port import ConsoleLiteratureInteractor
    from paper_agent.application.literature_composition import (
        CompositionUnavailable,
        build_search_synthesizer,
        build_writing_orchestrator,
    )
    from paper_agent.literature.language import LanguageThresholds, classify_record_language

    try:
        lit_orchestrator = build_writing_orchestrator(
            config, writer_llm=llm,
            reviewer_llm=build_reviewer_llm_provider(config),
            retrieval_provider=retrieval, facts=facts,
        )
        lit_synthesizer = build_search_synthesizer(config, writer_llm=llm)
        lit_port = ConsoleLiteratureInteractor()
        _th = LanguageThresholds(
            han_ratio_zh=config.literature_lang_han_zh,
            latin_ratio=config.literature_lang_latin,
            kana_min=config.literature_lang_kana_min,
            hangul_min=config.literature_lang_hangul_min,
        )
        lit_classifier = lambda rec: classify_record_language(rec, _th)  # noqa: E731
        check("build_writing_orchestrator", lit_orchestrator is not None)
    except CompositionUnavailable as exc:
        check("build_writing_orchestrator", False, f"CompositionUnavailable: {exc}")
    except Exception as exc:  # noqa: BLE001
        check("build_writing_orchestrator", False, f"{type(exc).__name__}: {exc}")

    check("build_search_synthesizer", lit_synthesizer is not None)
    check("orchestrator._limits 可读（chat.py 依赖私有属性）",
          lit_orchestrator is not None and getattr(lit_orchestrator, "_limits", None) is not None)

    ctx = HandlerContext(
        session=session, llm=llm, facts=facts,
        retrieval_provider=retrieval, verifier=verifier,
        confirm_number=lambda v, c: False,
        writing_integration_enabled=config.literature_writing_integration,
        literature_orchestrator=lit_orchestrator,
        literature_port=lit_port,
        classifier=lit_classifier,
        language_policy=config.literature_lang_policy,
        search_synthesis_enabled=config.literature_search_synthesis,
        search_synthesizer=lit_synthesizer,
        max_search_intent_revisions=config.literature_max_search_intent_revisions,
        retrieval_limits=(getattr(lit_orchestrator, "_limits", None)),
    )
    for field in ("literature_orchestrator", "literature_port", "classifier",
                  "search_synthesizer", "retrieval_limits"):
        check(f"HandlerContext.{field} 已注入", getattr(ctx, field) is not None)
    check("HandlerContext.base_year 未注入（回落当前年）", ctx.base_year is None,
          "非阻塞，_prepare_citation_quota 内 fallback")

    interactor = ConsolePlanningInteractor(ask=lambda p: "", out=lambda *_: None)
    for m in ("ask_field", "resolve_unmapped", "confirm_plan", "ask_disposition"):
        check(f"PlanningInteractor.{m}", callable(getattr(interactor, m, None)))
    for m in ("confirm_retrieval_batch", "resolve_quota_gap", "confirm_evidence_bundle",
              "resolve_final_gate", "request_search_topic", "confirm_search_intent"):
        check(f"LiteraturePort.{m}", callable(getattr(lit_port, m, None)))

    ts = TurnService(
        config=config, llm=llm, coordinator=coordinator,
        shadow_coordinator=Coordinator(enable_trace=True, runs_dir=os.path.join(tmp, "runs")),
        handler_context=ctx, interactor=interactor,
    )
    check("TurnService.mode==active", ts.mode == "active", ts.mode)

    # --- 真跑一个 Turn（mock LLM 会让 analyze 走 parse/schema 失败路径）---
    out = ts.handle_turn("润色一下引言", session=session, facts=facts, legacy=lambda: None)
    check("handle_turn 未抛异常", True)
    exit_status = getattr(getattr(out, "planning_exit", None), "status", None)
    report_status = getattr(getattr(out, "report", None), "plan_status", None)
    print(f"\n[turn] mode={out.mode} planning_exit={exit_status} report={report_status}")
    if out.run_report is not None:
        print(f"[turn] run_status={out.run_report.run_status} error={out.run_report.error}")
    check("mock LLM 下诚实出口（未伪装成功）",
          report_status is None or str(report_status.value) != "done",
          f"report={report_status}")

    # --- 意图覆盖面 ---
    from paper_agent.intents.router import Intent
    from paper_agent.runtime.handlers import _HANDLERS
    from paper_agent.tasking.operation_analyzer import _SUPPORTED

    all_intents = {i for i in Intent if i is not Intent.UNCLEAR}
    print(f"\n[intents] Router 全集: {sorted(i.value for i in all_intents)}")
    print(f"[intents] analyzer 支持: {sorted(i.value for i in _SUPPORTED)}")
    print(f"[intents] handler 注册: {sorted(i.value for i in _HANDLERS)}")
    print(f"[intents] active 模式下不可达: {sorted(i.value for i in all_intents - _SUPPORTED)}")

    print("\n=== PASS ===")
    for line in OK:
        print("  ok   " + line)
    if FAIL:
        print("=== FAIL ===")
        for line in FAIL:
            print("  FAIL " + line)
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
