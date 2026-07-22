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


def _run_add_section(session, facts, llm, retrieval, verifier, title_hint, position_hint):
    """执行一次 add_section（供单章与批处理复用）。返回 result。"""
    index = _build_index(session)
    plan = interview_add_section(title_hint, position_hint, index, facts, ask=_ask)
    return execute_add_section(
        session, plan, facts, llm,
        retrieval_provider=retrieval, verifier=verifier, confirm=_confirm_number,
    )


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

    # Facts 按「论文」持久化（claims 跨 chat 复用）；refs/数字每次从源刷新
    facts = SessionFacts.load(project_facts_path(config.workspace_root, source))
    if session.ext in _TEXT_EXT:
        facts.known_refs = extract_refs(session.read_source(), session.ext)
        facts.known_numbers = extract_numbers(session.read_source(), session.ext)
    facts.save()
    _remembered = len(facts.claims.get("section_intents", {}))

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
            session.save_to_source()
            print(f"已保存到源文件：{session.source_path}")
            continue
        if msg == ":undo":
            print("已撤销。" if session.undo() else "没有可撤销的修改。")
            continue

        # 自然语言意图
        index = _build_index(session)
        route = Router(index).route(msg)

        if route.intent is Intent.UNCLEAR:
            print(route.note)
            continue

        if route.intent is Intent.FIX_BIBLIOGRAPHY:
            report = fix_bibliography(session, facts)
            print("\n" + report.message)
            continue

        if route.intent is Intent.FORMAT_DOCX:
            fresult = execute_format(session, msg)
            print("\n" + fresult.message)
            if fresult.ok and fresult.applied:
                print("操作清单：" + "；".join(fresult.applied))
                print("（:save 落盘到源文件，:undo 撤销）")
            continue

        if route.intent is Intent.ADD_SECTION:
            try:
                ares = _run_add_section(
                    session, facts, llm, retrieval, verifier, route.title, None
                )
            except InterviewError as exc:
                print(f"访谈中止：{exc}")
                continue
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
            cresult = execute_convert(session, cplan)
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

            rresult = execute_rewrite(
                session, rplan, facts, llm,
                retrieval_provider=retrieval, verifier=verifier,
                confirm=_confirm, max_retry=config.polish_max_retry,
            )
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

        result = execute_polish(
            session, plan, llm,
            max_char_change_ratio=config.polish_max_char_change_ratio,
            max_retry=config.polish_max_retry,
        )
        print("\n" + result.message)
        if result.ok and result.diff:
            print("\n--- diff ---")
            print(result.diff)
            print("（:save 落盘到源文件，:undo 撤销）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
