"""Chat REPL：对一篇论文做润色 / 改写 / 新增章节 / 转格式 / 排版。

用法：
    python scripts/chat.py path/to/paper.tex

环境变量（可选，完整清单见 paper_agent/config.py）：
    PAPER_LLM=mock                      # 无 API key 时用 mock provider 走通流程
    PAPER_LLM=deepseek                  # 或任意预设厂商
    PAPER_LLM_MODEL=...
    PAPER_INTENT_PLANNING_MODE=active   # off | shadow | active（默认 off，走关键词路由）
    PAPER_TRACE=true                    # 落 runs/<run_id>/trace.json
    PAPER_LANGFUSE=true                 # 额外上传 Langfuse（需 PAPER_TRACE 同开）
                                        # 另需 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
                                        # 自托管再加 LANGFUSE_HOST=http://localhost:3000

命令：
    :sections   列出章节
    :refs       回填参考文献著录字段（刊名/卷/期/页，按 DOI 查 Crossref）
    :save       把 working copy 落盘到源文件
    :undo       撤销上一次修改
    :stats      本次会话的 Turn 归属统计（新栈 / shadow / 静默降级到旧栈）
    :quit       退出（不自动保存源文件）
其余输入按自然语言意图处理。
"""

from __future__ import annotations

import os
import sys

# 让脚本无需安装即可 import paper_agent
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.config import load_config
from paper_agent.utils.dotenv import load_dotenv
from paper_agent.providers.factory import (
    build_llm_provider,
    build_retrieval_provider,
    build_reviewer_llm_provider,
)
from paper_agent.session.extractor import extract_refs, extract_numbers
from paper_agent.session.facts import SessionFacts, project_facts_path
from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.section_index import SECTIONABLE_EXTS, SectionIndex
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.bibliography import fix_bibliography
from paper_agent.intents.router import Router, Intent
from paper_agent.intents.interview import (
    interview_polish, interview_convert, interview_rewrite,
    interview_add_section, InterviewError,
)
from paper_agent.intents.polish import execute_polish
from paper_agent.intents.convert import execute_convert
from paper_agent.intents.rewrite import execute_rewrite
from paper_agent.intents.add_section import execute_add_section
from paper_agent.intents.format_docx import execute_format
from paper_agent.coordinator import Coordinator
from paper_agent.errors import OperationCancelled
from paper_agent.application.console_interactor import ConsolePlanningInteractor
from paper_agent.application.turn_service import TurnService
from paper_agent.runtime.handlers import HandlerContext
from paper_agent.observability.events import EventKind
from paper_agent.observability.renderer import ConsoleRenderer
from paper_agent.observability.turn_metrics import TurnMetrics
from paper_agent.providers.llm.base import CancellationToken

_COMMON_SECTIONS = [
    "Introduction", "Related Work", "Method", "Experiments",
    "Discussion", "Conclusion",
]
#: 复用 section_index 的唯一真相源（此前这里硬编码了同一份集合 = 第二真相源）。
#: 语义一致：能解析章节 ⇔ 能按 utf-8 当文本读。docx 两者皆不成立。
_TEXT_EXT = SECTIONABLE_EXTS


def _build_index(session) -> SectionIndex:
    """构建章节索引；docx 等二进制格式返回空索引（仅用于路由取 ext）。"""
    if session.ext in _TEXT_EXT:
        return SectionIndex(session.read_working(), session.ext)
    return SectionIndex("", session.ext)


def _print_sections(index: SectionIndex) -> None:
    if not index.list():
        print("（未识别到任何章节）")
        return
    print("章节：")
    for i, s in enumerate(index.list(), start=1):
        indent = "  " * (s.level - 1)
        print(f"  {i}. {indent}{s.title}  [{s.id}]")


def _warn_if_no_sections(session, index: SectionIndex) -> None:
    """载入时就说清「这个格式做不了什么」，而不是等用户提请求后报 section_not_found。

    docx 是 zip 二进制，解析不出标题 → 章节索引恒为空 → polish/rewrite/add_section
    在计划期必然被拒。以前这条信息只体现为一句「（未识别到任何章节）」，
    用户拿到的是 `section_not_found`，会去排查章节名而不是格式。
    """
    if index.supports_sections:
        return
    print(
        f"\n[注意] 当前格式 .{session.ext} 解析不出章节结构，"
        "按章节定位的操作全部不可用：润色 / 改写 / 新增章节。"
        f"\n       可用：转格式、docx 排版。"
        "\n       要做章节级写作，请先转成 .md 或 .tex，例如："
        f"\n         pandoc \"{session.source_path}\" -t markdown -o paper.md"
        "\n         python scripts/chat.py paper.md"
    )


def _note_docx_scope(session) -> None:
    """docx 源：说清就地编辑的能力与**覆盖不到的范围**。

    要说的不再是"排版会丢"（就地编辑不丢），而是表格/文本框内的文字看不见 ——
    否则用户会以为全文都被处理了，而表格里的数字其实没动过。
    """
    if not getattr(session, "docx_source", ""):
        return
    lines = [
        "\n[提示] 源文件是 .docx，直接在其副本上就地编辑"
        "（润色 / 改写 / 新增章节 / 排版均可用）。",
        "       页边距、分栏、页眉页脚、段内加粗等排版保持不变；:save 覆盖原文件。",
    ]
    try:
        from paper_agent.session.docx_document import table_text_ignored
        cells = table_text_ignored(session.working_path)
    except Exception:      # noqa: BLE001 - 提示不该阻断启动
        cells = 0
    if cells:
        lines.append(
            f"       注意：表格内有 {cells} 处文字不在处理范围内（正文段落之外），"
            "需手动改。"
        )
    print("\n".join(lines))


def _ask(prompt: str) -> str:
    return input(prompt)


def _confirm_number(value: str, ctx: str) -> bool:
    ans = input(
        f"\n出现新数字 {value}（{ctx}）。\n"
        "它是真实来源的吗？(y=确认可用 / 其他=视为编造) > "
    ).strip().lower()
    return ans in ("y", "yes", "是")


def _run_add_section(session, facts, llm, retrieval, verifier, title_hint, position_hint, *, bus=None):
    """执行一次 add_section（供单章与批处理复用）。返回 result。"""
    index = _build_index(session)
    plan = interview_add_section(title_hint, position_hint, index, facts, ask=_ask)
    return execute_add_section(
        session, plan, facts, llm,
        retrieval_provider=retrieval, verifier=verifier, confirm=_confirm_number,
        bus=bus,
    )


def _handle_planned_turn(turn_service, msg, *, session, facts, legacy_intent,
                        metrics=None) -> bool:
    """走 intent-planning 理解层。返回 True 表示本轮已接管（跳过旧链路）。

    - shadow：只读旁路（`legacy` 传 no-op，真正的旧链路仍由调用方在其后执行），
      打印一行差异对比，返回 False。
    - active：规划层接管并执行，打印计划结果，返回 True。

    `metrics`（可选 `TurnMetrics`，backlog B-4）：记录本轮**实际由谁服务**。
    返回 False 时旧链路会接手 —— 这条降级路径以前只印一行提示、不留痕，导致
    E2E 数据无法区分「新栈处理了」与「新栈挂了、旧栈兜的」。
    """
    try:
        out = turn_service.handle_turn(
            msg, session=session, facts=facts,
            legacy=lambda: None, legacy_intent=legacy_intent,
        )
    except OperationCancelled:
        print("\n已取消。")
        if metrics is not None:
            metrics.record_cancelled()
            metrics.record_served_by("cancelled")
        return True
    except Exception as exc:  # noqa: BLE001 - 理解层异常绝不拖垮 REPL
        category = type(exc).__name__
        print(f"\n理解层出错（{category}），本轮改用原有流程。")
        if metrics is not None:
            # B-4：静默降级必须留痕 —— 否则这一轮会被误算成「新栈处理成功」
            metrics.record_fallback(category)
            metrics.record_served_by("legacy_fallback")
        return False

    if out.mode == "shadow":
        s = out.shadow
        if s is None:
            if metrics is not None:
                metrics.record_served_by("legacy_fallback")
                metrics.record_fallback("shadow_result_missing")
            return False
        if s.ok:
            same = "一致" if s.matches_legacy else "不同"
            print(
                f"[shadow] 理解为 {list(s.operation_intents)}（与原路由{same}）；"
                f"校验={s.validation_result}；耗时 {s.added_latency_ms:.0f}ms"
            )
        else:
            print(f"[shadow] 未完成（{s.analyze_status or s.error}），不影响本次执行")
        if metrics is not None:
            metrics.record_served_by("shadow")
            metrics.record_shadow(ok=s.ok, matches_legacy=s.matches_legacy)
        return False   # 旧链路照常继续

    # active
    if metrics is not None:
        metrics.record_served_by("active")
        _absorb_active_metrics(out, metrics)
    _print_plan_outcome(out)
    return True


def _absorb_active_metrics(out, metrics) -> None:
    """把 active 结果的终态归入计数（结构化字段，不解析人类可读文本）。"""
    if getattr(out, "run_error_category", ""):
        metrics.record_active_failed(out.run_error_category)
    if out.planning_exit is not None:
        metrics.record_planning_exit(out.planning_exit.status.value)
    elif out.report is not None:
        metrics.record_plan_status(out.report.plan_status.value)


def _print_plan_outcome(out) -> None:
    """打印 active 模式的结果（计划出口 或 执行报告）。"""
    if out.planning_exit is not None:
        print(f"\n未执行：{out.planning_exit.status.value}"
              + (f"（{out.planning_exit.message}）" if out.planning_exit.message else ""))
        return
    report = out.report
    if report is None:
        print("\n本轮没有产生可执行计划。")
        return
    if report.plan_status.value == "no_action":
        print("\n未做任何改动（没有可执行的操作）。")
        return
    print(f"\n计划结果：{report.plan_status.value}")
    for i, r in enumerate(report.operations, 1):
        # 打印**整条** message：Gate 失败原因在第 2 行起（「最后一次违反：」之后），
        # 此前只取首行 ⇒ 用户看到一个冒号后面什么都没有，等于没给失败原因。
        head, *rest = r.message.splitlines() if r.message else ("",)
        print(f"  {i}. {r.status.value}" + (f" — {head}" if head else ""))
        for extra in rest:
            if extra.strip():
                print(f"     {extra.strip()}")
        for path in r.generated_outputs:
            print(f"     产物：{path}")
    if report.rollback_available:
        print("（如需回滚已完成的改动，可用 :undo 逐步撤销）")
    if any(r.changed_working_copy for r in report.operations):
        print("（:save 落盘到源文件，:undo 撤销）")


def _dispatch(coordinator, intent_name, work, **route_data):
    """在一次 run 内执行 work(bus, cancel_token)：发 ROUTE_DECISION、统一取消/失败收尾。

    任何意图都经此包裹 → 每条消息都是一次有始有终、有 trace、异常不炸退 REPL 的 run。
    返回 RunReport；业务结果在 report.value（取消/失败时为 None）。
    """
    def fn(bus, ct):
        bus.publish(EventKind.ROUTE_DECISION, intent=intent_name, **route_data)
        try:
            return work(bus, ct)
        except KeyboardInterrupt:  # Ctrl-C → 统一取消模型
            # from None：用户主动中断不是"错误的根因"，链上挂个 KeyboardInterrupt
            # 只会让 traceback 更吵。
            raise OperationCancelled() from None

    report = coordinator.run(fn, cancel_token=CancellationToken())
    if report.run_status == "cancelled":
        print("\n已取消，本次操作未改动文档。")
    elif report.run_status == "failed":
        cat = report.error.get("category") if report.error else "未知"
        print(f"\n执行失败（{cat}）。")
    return report


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法：python scripts/chat.py <论文文件路径>")
        return 2

    source = argv[1]
    load_dotenv()          # 先加载 .env，使其中的 PAPER_* 配置对 load_config 可见
    config = load_config()
    try:
        llm = build_llm_provider(config)
    except Exception as exc:  # noqa: BLE001
        print(f"构造 LLM provider 失败：{exc}")
        print("提示：无 API key 时可设 PAPER_LLM=mock 走通流程。")
        return 1

    session = FileBackedSession(source, workspace_root=config.workspace_root)
    retrieval = build_retrieval_provider(config)
    verifier = CitationVerifier(retrieval)

    # 观测编排器：流式开则挂 ConsoleRenderer，追踪开则落 trace.json；两者全关时
    # Coordinator 内部走 NullEventBus（零开销，行为与既有一致）。
    coordinator = Coordinator(
        enable_trace=config.trace_enabled,
        enable_stream=config.stream_enabled,
        renderer=ConsoleRenderer() if config.stream_enabled else None,
        runs_dir=config.runs_dir,
        enable_langfuse=config.langfuse_enabled,
    )
    if config.trace_enabled or config.stream_enabled:
        flags = []
        if config.stream_enabled:
            flags.append("流式")
        if config.trace_enabled:
            flags.append(f"追踪→{config.runs_dir}/<run_id>/trace.json")
        print("观测已启用：" + "、".join(flags))

    # Facts 按「论文」持久化（claims 跨 chat 复用）；refs/数字每次从源刷新
    facts = SessionFacts.load(project_facts_path(config.workspace_root, source))
    if session.ext in _TEXT_EXT:
        # **合并**而非覆盖：此前经检索+核验入库的文献不能被源文件扫描抹掉（#18）
        facts.merge_refs(extract_refs(session.read_source(), session.ext))
        facts.merge_numbers(extract_numbers(session.read_source(), session.ext))
    facts.save()
    _remembered = len(facts.claims.get("section_intents", {}))

    # 写作前文献编排器装配（A2d 引用配额路径；默认关 → 装不出即保持旧路径，零影响）
    lit_orchestrator = None
    lit_port = None
    lit_classifier = None
    lit_synthesizer = None
    if config.literature_writing_integration:
        from paper_agent.application.console_literature_port import (
            ConsoleLiteratureInteractor,
        )
        from paper_agent.application.literature_composition import (
            CompositionUnavailable,
            build_writing_orchestrator,
        )
        from paper_agent.literature.language import (
            LanguageThresholds,
            classify_record_language,
        )

        try:
            lit_orchestrator = build_writing_orchestrator(
                config, writer_llm=llm,
                reviewer_llm=build_reviewer_llm_provider(config),
                retrieval_provider=retrieval, facts=facts,
            )
            from paper_agent.application.literature_composition import (
                build_search_synthesizer,
            )
            lit_synthesizer = build_search_synthesizer(config, writer_llm=llm)
            lit_port = ConsoleLiteratureInteractor()
            _th = LanguageThresholds(
                han_ratio_zh=config.literature_lang_han_zh,
                latin_ratio=config.literature_lang_latin,
                kana_min=config.literature_lang_kana_min,
                hangul_min=config.literature_lang_hangul_min,
            )
            lit_classifier = lambda rec: classify_record_language(rec, _th)
            _bc = config.literature_bundle_budget_chars
            print("引用配额写作已启用（supervised：检索/证据集需人工确认）。"
                  + f"\n  证据集上下文预算：{'不限' if _bc <= 0 else f'{_bc} 字符'}"
                    f"（约 {'—' if _bc <= 0 else _bc // 1500} 篇全摘要，"
                    f"PAPER_LIT_BUNDLE_BUDGET_CHARS 可调）")
        except CompositionUnavailable as exc:
            print(f"引用配额写作未能启用：{exc} 将按旧路径处理带配额请求。")

    # intent-planning 理解层装配（PAPER_INTENT_PLANNING_MODE=off|shadow|active）
    turn_service = None
    if config.intent_planning_mode != "off":
        handler_ctx = HandlerContext(
            session=session, llm=llm, facts=facts,
            retrieval_provider=retrieval, verifier=verifier,
            confirm_number=_confirm_number,
            max_retry=config.polish_max_retry,
            max_char_change_ratio=config.polish_max_char_change_ratio,
            stream=config.stream_enabled,
            writing_integration_enabled=config.literature_writing_integration,
            literature_orchestrator=lit_orchestrator,
            literature_port=lit_port,
            classifier=lit_classifier,
            language_policy=config.literature_lang_policy,
            search_synthesis_enabled=config.literature_search_synthesis,
            search_synthesizer=lit_synthesizer,
            max_search_intent_revisions=config.literature_max_search_intent_revisions,
            retrieval_limits=(lit_orchestrator._limits if lit_orchestrator is not None else None),
        )
        turn_service = TurnService(
            config=config, llm=llm,
            coordinator=coordinator,
            # 影子 run 刻意不接 Langfuse：它是对照用的第二次执行，上传会让
            # UI 里每个操作出现两条 trace，聚合统计直接翻倍失真。
            shadow_coordinator=Coordinator(
                enable_trace=config.trace_enabled, runs_dir=config.runs_dir
            ),
            handler_context=handler_ctx,
            interactor=ConsolePlanningInteractor(),
        )
        print(f"理解层模式：{config.intent_planning_mode}"
              + ("（接管自然语言请求，支持多操作规划）"
                 if config.intent_planning_mode == "active"
                 else "（只旁路记录，执行仍走原流程）"))

    # B-4：Turn 归属计数。每轮落盘一次，使崩溃/强杀后仍能看到已发生的归属，
    # 且能回答「这次 E2E 的结论是否可直接采信」（attribution_is_clean）。
    metrics_path = os.path.join(config.runs_dir, "turn_metrics.json")
    # 读回既有累积值：计数要跨会话累积才能回答「新栈这周降级了几次」。
    # 文件不存在/损坏 → 全零，不阻塞启动。
    metrics = TurnMetrics.load(metrics_path)

    index = _build_index(session)
    print(f"已载入 {source}（working: {session.working_path}）")
    print(f"抽取到 {len(facts.known_refs)} 条文献引用、{len(facts.known_numbers)} 个表格数字。")
    if _remembered:
        print(f"（已记住 {_remembered} 个此前章节的意图/结构，可复用）")
    _print_sections(index)
    _warn_if_no_sections(session, index)
    _note_docx_scope(session)
    print("\n输入润色需求，或 :help 查看命令。")

    while True:
        try:
            msg = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not msg:
            continue

        if msg in (":quit", ":q", ":exit"):
            break
        if msg == ":help":
            print(__doc__)
            continue
        if msg == ":stats":
            print("\n" + metrics.summary())
            print(f"（明细：{metrics_path}）")
            continue
        if msg == ":sections":
            index = _build_index(session)
            _print_sections(index)
            continue
        if msg == ":refs":
            # 修复 provider 之前抓的记录著录字段为空，需要一次性回填；
            # 之后新检索的文献字段本就是齐的。只按 DOI 精确查，不猜。
            ledger = getattr(facts, "ledger", None)
            if ledger is None:
                print("本会话没有文献账本。")
                continue
            from paper_agent.literature.backfill import backfill_ledger
            from paper_agent.providers.retrieval.crossref import CrossrefVerifyProvider
            print("正在按 DOI 回填著录字段（走 Crossref，可能需要一会儿）…")
            report = backfill_ledger(ledger, CrossrefVerifyProvider())
            facts.save()
            print(report.message)
            continue
        if msg == ":save":
            # docx 与 md/tex 走同一条路：working 本身就是同格式副本，直接拷回。
            # 就地编辑不再有格式转换，因此不需要"另存以防丢排版"那套额外问答。
            saved: list[str] = []

            def _do_save(bus, _ct):
                saved.append(session.save_to_source(bus=bus))
                return None
            coordinator.run(_do_save)  # :save 是独立 run
            print(f"已保存到：{saved[0] if saved else session.source_path}")
            continue
        if msg == ":undo":
            print("已撤销。" if session.undo() else "没有可撤销的修改。")
            continue

        # 自然语言意图
        metrics.record_turn()
        index = _build_index(session)
        route = Router(index).route(msg)

        # intent-planning 理解层（默认 off，行为与既有逐字节一致）：
        # - active：接管整条自然语言链路（多操作规划 → 澄清 → 未映射处置 → 确认 → 有序执行）
        # - shadow：只旁路分析并记录差异，随后仍走下面的旧链路（旧行为不变）
        if turn_service is not None and turn_service.mode != "off":
            taken_over = _handle_planned_turn(
                turn_service, msg, session=session, facts=facts,
                legacy_intent=route.intent.value, metrics=metrics,
            )
            metrics.save(metrics_path)
            if taken_over:
                continue
        else:
            metrics.record_served_by("off")
            metrics.save(metrics_path)

        if route.intent is Intent.UNCLEAR:
            print(route.note)
            continue

        if route.intent is Intent.FIX_BIBLIOGRAPHY:
            report = _dispatch(
                coordinator, "fix_bibliography",
                lambda bus, ct: fix_bibliography(session, facts),
            )
            if report.value is not None:
                print("\n" + report.value.message)
            continue

        if route.intent is Intent.FORMAT_DOCX:
            report = _dispatch(
                coordinator, "format_docx",
                lambda bus, ct: execute_format(session, msg),
            )
            fresult = report.value
            if fresult is not None:
                print("\n" + fresult.message)
                if fresult.ok and fresult.applied:
                    print("操作清单：" + "；".join(fresult.applied))
                    print("（:save 落盘到源文件，:undo 撤销）")
            continue

        if route.intent is Intent.ADD_SECTION:
            # 访谈在 run 外（InterviewError 是用户流程控制，不应被当作 run 失败）
            try:
                as_index = _build_index(session)
                as_plan = interview_add_section(route.title, None, as_index, facts, ask=_ask)
            except InterviewError as exc:
                print(f"访谈中止：{exc}")
                continue
            report = _dispatch(
                coordinator, "add_section",
                lambda bus, ct: execute_add_section(
                    session, as_plan, facts, llm,
                    retrieval_provider=retrieval, verifier=verifier,
                    confirm=_confirm_number, bus=bus,
                ),
                title=route.title or "",
            )
            ares = report.value
            if ares is not None:
                if ares.search_note:
                    print("\n" + ares.search_note)
                print("\n" + ares.message)
                if ares.ok:
                    print("\n--- 预览 ---\n" + ares.preview)
                    print("\n（:save 落盘到源文件，:undo 撤销）")
            continue

        if route.intent is Intent.ADD_SECTION_BATCH:
            index = _build_index(session)
            present = {s.title.lower() for s in index.list()}
            missing = [s for s in _COMMON_SECTIONS if s.lower() not in present]
            print("\n常见学术章节（仅供参考，不是必须）：")
            for s in _COMMON_SECTIONS:
                mark = "✓已有" if s.lower() in present else "—缺"
                print(f"  [{mark}] {s}")
            if not missing:
                print("常见章节都已具备。也可补自定义章节。")
            pick = input(
                "想补哪些？（逗号分隔标题 / 回车跳过）\n> "
            ).strip()
            titles = [t.strip() for t in pick.split(",") if t.strip()]
            for t in titles:
                try:
                    ares = _run_add_section(
                        session, facts, llm, retrieval, verifier, t, None
                    )
                except InterviewError as exc:
                    print(f"「{t}」访谈中止：{exc}")
                    continue
                print("\n" + ares.message)
                if ares.ok and input("继续下一章？(y/其他=停) > ").strip().lower() not in ("y", "yes", "是"):
                    break
            continue

        if route.intent is Intent.CONVERT_FORMAT:
            try:
                cplan = interview_convert(
                    route.target_ext, route.presets, ask=lambda p: input(p)
                )
            except InterviewError as exc:
                print(f"访谈中止：{exc}")
                continue
            report = _dispatch(
                coordinator, "convert_format",
                lambda bus, ct: execute_convert(session, cplan),
                target_ext=route.target_ext or "",
            )
            cresult = report.value
            if cresult is not None:
                print("\n" + cresult.message)
                if cresult.report_lines:
                    print("转换后校验：")
                    print("\n".join(cresult.report_lines))
            continue

        if route.intent is Intent.REWRITE:
            try:
                rplan = interview_rewrite(route.section_id, index, ask=lambda p: input(p))
            except InterviewError as exc:
                print(f"访谈中止：{exc}")
                continue

            def _confirm(value: str, ctx: str) -> bool:
                ans = input(
                    f"\n出现新数字 {value}（{ctx}）。\n"
                    "它是真实来源的吗？(y=确认可用 / 其他=视为编造) > "
                ).strip().lower()
                return ans in ("y", "yes", "是")

            report = _dispatch(
                coordinator, "rewrite",
                lambda bus, ct: execute_rewrite(
                    session, rplan, facts, llm,
                    retrieval_provider=retrieval, verifier=verifier,
                    confirm=_confirm, max_retry=config.polish_max_retry, bus=bus,
                ),
                section_id=route.section_id or "",
            )
            rresult = report.value
            if rresult is not None:
                if rresult.search_note:
                    print("\n" + rresult.search_note)
                print("\n" + rresult.message)
                if rresult.ok and rresult.diff:
                    print("\n--- diff ---")
                    print(rresult.diff)
                    print("（:save 落盘到源文件，:undo 撤销）")
            continue

        # polish
        try:
            plan = interview_polish(route.section_id, index, ask=lambda p: input(p))
        except InterviewError as exc:
            print(f"访谈中止：{exc}")
            continue

        section_id = route.section_id

        def _do_polish(bus, ct):
            bus.publish(EventKind.ROUTE_DECISION, intent="polish", section_id=section_id)
            try:
                return execute_polish(
                    session, plan, llm,
                    max_char_change_ratio=config.polish_max_char_change_ratio,
                    max_retry=config.polish_max_retry,
                    bus=bus, cancel_token=ct, stream=config.stream_enabled,
                )
            except KeyboardInterrupt:  # Ctrl-C → 统一取消模型
                raise OperationCancelled() from None

        report = coordinator.run(_do_polish, cancel_token=CancellationToken())
        result = report.value
        if result is None:
            if report.run_status == "cancelled":
                print("\n已取消本次润色，文档未改动。")
            elif report.error:
                print(f"\n生成失败（{report.error.get('category')}）。")
            continue
        print("\n" + result.message)
        if result.ok and result.diff:
            print("\n--- diff ---")
            print(result.diff)
            print("（:save 落盘到源文件，:undo 撤销）")

    # 退出时给出归属摘要：静默降级过就显式警告，别让读者自己去推
    if metrics.turns:
        metrics.save(metrics_path)
        print("\n" + metrics.summary())
        print(f"（明细：{metrics_path}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
