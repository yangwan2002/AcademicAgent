"""Week 1 极简 Chat REPL：对一个论文文件做 polish。

用法：
    python scripts/chat.py path/to/paper.tex

环境变量（可选）：
    PAPER_LLM=mock            # 无 API key 时用 mock provider 走通流程
    PAPER_LLM=deepseek        # 或任意预设厂商
    PAPER_LLM_MODEL=...

命令：
    :sections   列出章节
    :save       把 working copy 落盘到源文件
    :undo       撤销上一次润色
    :quit       退出（不自动保存源文件）
其余输入按自然语言意图处理（Week 1 仅 polish）。
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
from paper_agent.providers.factory import build_llm_provider, build_retrieval_provider
from paper_agent.session.extractor import extract_refs, extract_numbers
from paper_agent.session.facts import SessionFacts, project_facts_path
from paper_agent.session.file_session import FileBackedSession
from paper_agent.session.section_index import SectionIndex
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
from paper_agent.providers.llm.base import CancellationToken

_COMMON_SECTIONS = [
    "Introduction", "Related Work", "Method", "Experiments",
    "Discussion", "Conclusion",
]
_TEXT_EXT = {"tex", "md", "markdown"}


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


def _handle_planned_turn(turn_service, msg, *, session, facts, legacy_intent) -> bool:
    """走 intent-planning 理解层。返回 True 表示本轮已接管（跳过旧链路）。

    - shadow：只读旁路（`legacy` 传 no-op，真正的旧链路仍由调用方在其后执行），
      打印一行差异对比，返回 False。
    - active：规划层接管并执行，打印计划结果，返回 True。
    """
    try:
        out = turn_service.handle_turn(
            msg, session=session, facts=facts,
            legacy=lambda: None, legacy_intent=legacy_intent,
        )
    except OperationCancelled:
        print("\n已取消。")
        return True
    except Exception as exc:  # noqa: BLE001 - 理解层异常绝不拖垮 REPL
        print(f"\n理解层出错（{type(exc).__name__}），本轮改用原有流程。")
        return False

    if out.mode == "shadow":
        s = out.shadow
        if s is None:
            return False
        if s.ok:
            same = "一致" if s.matches_legacy else "不同"
            print(
                f"[shadow] 理解为 {list(s.operation_intents)}（与原路由{same}）；"
                f"校验={s.validation_result}；耗时 {s.added_latency_ms:.0f}ms"
            )
        else:
            print(f"[shadow] 未完成（{s.analyze_status or s.error}），不影响本次执行")
        return False   # 旧链路照常继续

    # active
    _print_plan_outcome(out)
    return True


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
    print(f"\n计划结果：{report.plan_status.value}")
    for i, r in enumerate(report.operations, 1):
        line = f"  {i}. {r.status.value}"
        if r.message:
            line += f" — {r.message.splitlines()[0]}"
        print(line)
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
            raise OperationCancelled()

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
        facts.known_refs = extract_refs(session.read_source(), session.ext)
        facts.known_numbers = extract_numbers(session.read_source(), session.ext)
    facts.save()
    _remembered = len(facts.claims.get("section_intents", {}))

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
        )
        turn_service = TurnService(
            config=config, llm=llm,
            coordinator=coordinator,
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

    index = _build_index(session)
    print(f"已载入 {source}（working: {session.working_path}）")
    print(f"抽取到 {len(facts.known_refs)} 条文献引用、{len(facts.known_numbers)} 个表格数字。")
    if _remembered:
        print(f"（已记住 {_remembered} 个此前章节的意图/结构，可复用）")
    _print_sections(index)
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
        if msg == ":sections":
            index = _build_index(session)
            _print_sections(index)
            continue
        if msg == ":save":
            def _do_save(bus, _ct):
                session.save_to_source(bus=bus)
                return None
            coordinator.run(_do_save)  # :save 是独立 run
            print(f"已保存到源文件：{session.source_path}")
            continue
        if msg == ":undo":
            print("已撤销。" if session.undo() else "没有可撤销的修改。")
            continue

        # 自然语言意图
        index = _build_index(session)
        route = Router(index).route(msg)

        # intent-planning 理解层（默认 off，行为与既有逐字节一致）：
        # - active：接管整条自然语言链路（多操作规划 → 澄清 → 未映射处置 → 确认 → 有序执行）
        # - shadow：只旁路分析并记录差异，随后仍走下面的旧链路（旧行为不变）
        if turn_service is not None and turn_service.mode != "off":
            if _handle_planned_turn(
                turn_service, msg, session=session, facts=facts,
                legacy_intent=route.intent.value,
            ):
                continue

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
                raise OperationCancelled()

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
