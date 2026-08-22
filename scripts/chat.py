"""Chat REPL：对一篇论文做润色 / 改写 / 新增章节 / 转格式 / 排版。

用法：
    python scripts/chat.py path/to/paper.tex

环境变量（可选，完整清单见 paper_agent/config.py）：
    PAPER_LLM=mock                      # 无 API key 时用 mock provider 走通流程
    PAPER_LLM=deepseek                  # 或任意预设厂商
    PAPER_LLM_MODEL=...
    PAPER_TRACE=true                    # 落 runs/<run_id>/trace.json
    PAPER_LANGFUSE=true                 # 额外上传 Langfuse（需 PAPER_TRACE 同开）
                                        # 另需 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
                                        # 自托管再加 LANGFUSE_HOST=http://localhost:3000

命令：
    :sections   列出章节
    :refs       回填参考文献著录字段（刊名/卷/期/页，按 DOI 查 Crossref）
    :save       把 working copy 落盘到源文件。docx 的引用会存成 Word 交叉引用域
                （编号可在 Word 里 F9 自动更新）；加 --plain 存纯文本 [n]
    :export     另存一份交叉引用版到**别的路径**（源文件不动），如投稿版
                可选路径：:export D:\\out.docx；默认在源文件旁加 _crossref 后缀
    :undo       撤销上一次修改
    :stats      本次会话的 Planning 结果统计
    :ask <问题>  只读问答：问文档的情况（有哪些章节、哪章还没写、某章写了什么）。
                只读不改，不走写作意图；模型自己决定查什么，查完直接回答。
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
from paper_agent.coordinator import Coordinator
from paper_agent.errors import OperationCancelled
from paper_agent.application.console_interactor import ConsolePlanningInteractor
from paper_agent.application.turn_service import TurnService
from paper_agent.runtime.handlers import HandlerContext
from paper_agent.observability.renderer import ConsoleRenderer
from paper_agent.observability.turn_metrics import TurnMetrics
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


def _run_ask_command(
    question: str, session, llm, config=None, facts=None, coordinator=None
) -> None:
    """`:ask` —— 只读问答循环。

    非 ANSWERED 终态一律带上"为什么停"，绝不把撞上限的半截推理当完整答案展示。

    `facts` 供 `section_citations` 工具回答「哪章引了哪篇」；仍是只读 ——
    `capture` 不写回 facts、不 save。

    `coordinator`：给了就把这次问答跑成一次独立 run（同 `:save` 的做法），
    这样 ASK_* 事件有 trace 可落。不给则不埋点，行为不变 —— 只读路径不能
    因为观测装配缺失而失效。
    """
    from paper_agent.application.ask_context import capture
    from paper_agent.application.ask_loop import run_ask

    ctx = capture(session, facts)
    kwargs = {}
    if config is not None:      # 护栏上限走 config，不写死在调用点
        kwargs = {
            "max_iterations": config.agent_loop_max_iterations,
            "max_llm_calls": config.agent_loop_max_llm_calls,
        }
    if coordinator is None:
        result = run_ask(question, ctx, llm, **kwargs)
    else:
        # 问答的取消语义走 run_ask 自己的 CANCELLED 终态。
        report = coordinator.run(
            lambda bus, ct: run_ask(question, ctx, llm, bus=bus, cancel_token=ct, **kwargs)
        )
        result = report.value
        if result is None:
            # run 自身失败（观测装配异常等）。问答本身的失败走 PROVIDER_FAILED，
            # 不会走到这里。
            cat = report.error.get("category") if report.error else "未知"
            print(f"\n这次没能给出回答（{cat}）。")
            return
    _print_ask_result(result)


def _print_ask_result(result) -> None:
    """统一呈现显式 ``:ask`` 与自然语言自动只读交接的结果。"""
    print("\n" + result.display())
    if result.tool_calls:
        print(
            f"\n（查了 {len(result.tool_calls)} 次："
            f"{'、'.join(result.tool_calls)}；{result.llm_calls} 次模型调用）"
        )


def _note_save_crossref(session, attempted: bool) -> None:
    """报告 `:save` 的升域结果 —— 成功、退化、或断链警告。"""
    if not attempted:
        return
    err = getattr(session, "last_save_crossref_error", "")
    if err:
        print(f"⚠ 升成交叉引用失败（{err}），已按纯文本 [n] 保存，正文内容完整。")
        return
    rep = getattr(session, "last_save_crossref", None)
    if rep is None:
        return
    if getattr(rep, "already_converted", False):
        return
    if rep.fields:
        print(f"· 引用已存为交叉引用域（{rep.fields} 处，{rep.bookmarks} 条书签）。"
              f"Word 里 Ctrl+A → F9 可重算编号。")
    if rep.dangling:
        print(f"⚠ 这些编号正文引用了但文献表没有，保留为纯文本："
              f"{'、'.join(rep.dangling)}")
    for w in getattr(rep, "warnings", []) or []:
        print(f"⚠ {w}")


def _note_crossref_import(session) -> None:
    """告知「你的引用本来是交叉引用，已摊平成文本以便编辑，导出时会还原」。

    不说的话，用户会看到 `:refs` 里凭空出现引用编号，却不知道文档被动过。
    """
    rep = getattr(session, "crossref_import", None)
    err = getattr(session, "_crossref_error", "")
    if err:
        print(f"⚠ 交叉引用检测失败（{err}）；若原文引用是域，编辑可能错位。")
        return
    if rep is None:
        return
    if getattr(rep, "flattened", 0):
        print(f"· 原文 {rep.flattened} 处引用是交叉引用域，已在副本里摊平成文本"
              f"（源文件未动）。编辑后 `:export` 会还原成域并复用原书签。")
    for w in getattr(rep, "warnings", []) or []:
        print(f"⚠ {w}")


def _cmd_export(session, msg: str) -> None:
    """`:export [路径]` —— 另存一份引用为交叉引用域的 docx 到**别的路径**。

    `:save` 现在默认也存域版（`import_crossrefs` 让带域文件能安全重新载入），
    所以这条命令的用途收窄成「想留一份副本、不动源文件」，比如投稿版。
    """
    src = session.working_path
    if not src.lower().endswith(".docx"):
        print(":export 只支持 docx（tex 的 \\cite 由 LaTeX 自己排，md 无域概念）。")
        return

    arg = msg[len(":export"):].strip().strip('"')
    if arg:
        dest = os.path.abspath(arg)
    else:
        base, ext = os.path.splitext(session.source_path)
        dest = f"{base}_crossref{ext}"

    if os.path.exists(dest):
        ans = input(f"目标已存在，覆盖？{dest} [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return

    # 原文引用本来就是域 → 复用原书签，避免别处引用旧书签的域断链。
    imported = getattr(session, "crossref_import", None)
    bmap = dict(getattr(imported, "bookmark_map", {}) or {})

    from paper_agent.tools.docx_crossref import export_crossrefs
    try:
        rep = export_crossrefs(src, dest, superscript=True, bookmark_map=bmap)
    except ValueError as exc:            # 目标 == 源文件等硬约束
        print(f"未导出：{exc}")
        return
    except Exception as exc:             # noqa: BLE001 —— 导出失败不该拖垮会话
        print(f"导出失败：{type(exc).__name__}: {exc}")
        return

    if rep.already_converted:
        print(f"已导出（原文已含交叉引用域，未重复转换）：{rep.dest_path}")
    else:
        print(f"已导出：{rep.dest_path}")
        print(f"  文献表书签 {rep.bookmarks} 条，正文交叉引用域 {rep.fields} 处")
    if rep.dangling:
        print(f"  ⚠ 正文引用了但文献表无对应条目（保留为纯文本）：{', '.join(rep.dangling)}")
    if rep.uncited:
        print(f"  · 文献表有但正文未引用：{', '.join(rep.uncited)}")
    for w in rep.warnings:
        print(f"  · {w}")
    if rep.fields:
        print("  提示：Word 打开后按 Ctrl+A → F9 可刷新域编号（Word 不会自动重算）。")


def _note_dangling_refs(session) -> None:
    """载入时报一次悬空引用（只读）。

    这是 `fix_bibliography`（已删，B-2）唯一剩下的价值：用户原稿里的悬空引用系统
    补不了（facts 里没那篇文献的元数据），但不说他就永远不知道 —— LaTeX 那处编译
    出来是 `[?]`。系统自己写的引用不需要这一步，commit 时 `apply_bibliography`
    已经对齐了。
    """
    from paper_agent.literature.dangling_refs import diagnose_dangling
    try:
        text = session.read_working()
    except Exception:  # noqa: BLE001 —— 诊断失败不该挡住会话启动
        return
    rep = diagnose_dangling(text, session.ext)
    if not rep.has_findings:
        return
    print(f"\n[注意] 正文有 {len(rep.dangling)} 处引用在参考文献区找不到条目："
          f"{', '.join(rep.dangling[:8])}"
          + ("…" if len(rep.dangling) > 8 else ""))
    print("       这些引用在编译/导出后会显示成问号。系统补不了（缺元数据），"
          "可让我搜文献补齐，或自行补条目。")
    if rep.low_confidence:
        print("       （docx 的文献表形态多样，若上面这些其实有条目，可忽略此提示。）")


def _verify_draft_refs(facts, facts_path: str) -> None:
    """载入时核验初稿手写的文献（Crossref 书目反查），结果按项目缓存。

    `extract_refs` 只做文本扫描，抽出的条目 `verified` 恒为 False；在此之前系统
    既不核验用户原稿的文献，也不说自己没核验，而「抽取到 N 条文献引用」很容易被
    读成「这 N 条已验过」。

    缓存（`draft_refs_cache.json`，与 facts.json 同目录）让反复载入只付一次网络
    成本；键是归一化题名，因此换个编号或改动标点都还能命中。缓存只存稳定结论，
    UNCHECKED 不入盘 —— 否则一次网络故障会被永久固化成结论。

    三态措辞见 `literature.draft_refs` 模块文档：「Crossref 查不到」绝不表述为
    「疑似虚假」，否则中文文献会大面积误报，用户学会无视警告后连真造假也一起漏掉。
    """
    from paper_agent.literature import draft_refs as dr

    refs = [r for r in facts.known_refs if not r.verified]
    if not refs:
        return

    cpath = dr.cache_path_for(facts_path)
    cache = dr.load_cache(cpath)
    fresh = sum(1 for r in refs
                if dr.parse_citation(r.title or "").usable
                and _dedup_key(r.title or "") not in cache)
    # 实测单条反查约 2s（Crossref，顺序发），36 条初稿 ≈ 73s。首次载入不能白等这么久
    # 又不给交代，所以先说清要多久、可以 Ctrl-C 跳过；已验过的项目走缓存则近乎零延迟。
    if fresh:
        print(f"       正在核验 {fresh} 条初稿文献（Crossref 书目反查，"
              f"约 {max(1, round(fresh * 2 / 5)) * 5} 秒；Ctrl-C 可跳过）…")

    from paper_agent.providers.retrieval.crossref import CrossrefVerifyProvider
    try:
        report = dr.verify_draft_refs(refs, CrossrefVerifyProvider(), cache=cache)
    except KeyboardInterrupt:
        # 已完成的部分仍落盘：下次载入不必重跑，中断不会白费。
        dr.save_cache(cpath, cache)
        print("\n       （已跳过初稿文献核验；已验部分已缓存，下次载入继续）")
        return
    except Exception as exc:  # noqa: BLE001 —— 核验失败不能挡住会话启动
        print(f"       （初稿文献核验未完成：{type(exc).__name__}，不影响其他功能）")
        return
    dr.save_cache(cpath, cache)

    for line in dr.format_report(report):
        print("       " + line)


def _dedup_key(title: str) -> str:
    from paper_agent.literature import dedup as _d
    from paper_agent.literature.draft_refs import parse_citation
    return _d.normalize_title(parse_citation(title).title)


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


def _confirm_number(value: str, ctx: str) -> bool:
    ans = input(
        f"\n出现新数字 {value}（{ctx}）。\n"
        "它是真实来源的吗？(y=确认可用 / 其他=视为编造) > "
    ).strip().lower()
    return ans in ("y", "yes", "是")


def _run_planning_turn(turn_service, msg, *, session, facts, metrics=None):
    """运行唯一的 Planning 管线；失败时不再降级到旧写作管线。"""
    try:
        out = turn_service.handle_turn(msg, session=session, facts=facts)
    except OperationCancelled:
        print("\n已取消。")
        if metrics is not None:
            metrics.record_cancelled()
        return None
    except Exception as exc:  # noqa: BLE001 - 单个 Turn 异常不炸退 REPL
        category = type(exc).__name__
        print(f"\nPlanning 出错（{category}），本轮未执行。")
        if metrics is not None:
            metrics.record_planning_failed(category)
        return None

    if metrics is not None:
        _absorb_planning_metrics(out, metrics)
    return out


def _absorb_planning_metrics(out, metrics) -> None:
    """把 Planning 结果的终态归入计数（结构化字段，不解析展示文本）。"""
    if getattr(out, "run_error_category", ""):
        metrics.record_planning_failed(out.run_error_category)
    if out.planning_exit is not None:
        metrics.record_planning_exit(out.planning_exit.status.value)
    elif out.report is not None:
        metrics.record_plan_status(out.report.plan_status.value)


def _print_plan_outcome(out) -> None:
    """打印 Planning 结果（计划出口或执行报告）。"""
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


def _force_utf8_output() -> None:
    """让 stdout/stderr 按 UTF-8 输出，且编不出的字符退化成 `?` 而不是崩。

    交互式终端本就是 UTF-8，但输出一旦被**重定向**（管道、`> log.txt`、CI、
    后台跑），Python 会回退到系统默认编码；简体 Windows 下是 GBK，而提示里的
    `⚠`(U+26A0) 不在 GBK 中 → `UnicodeEncodeError` 直接中断整个会话。

    统一在入口处 reconfigure，而不是把符号逐个换成 ASCII —— 后者治不了以后
    新加的符号。`errors="replace"` 是兜底：真遇到编不出的字符，宁可显示 `?`
    也不能让写作过程崩在一个装饰性字符上。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):       # 非常规流（被接管/已关闭）
            pass


def main(argv: list[str]) -> int:
    _force_utf8_output()
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
            print(f"引用配额写作未能启用：{exc} 带配额请求将由 Planning 明确拒绝或澄清。")

    # Planning 是自然语言的唯一入口；无受支持写作操作时固定交给 AskLoop。
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
        config=config, llm=llm, coordinator=coordinator,
        handler_context=handler_ctx,
        interactor=ConsolePlanningInteractor(),
    )
    print("自然语言管线：Planning（未命中写作操作时转只读 AskLoop）")

    # 每轮落盘一次，使崩溃/强杀后仍能看到已发生的 Planning 终态。
    metrics_path = os.path.join(config.runs_dir, "turn_metrics.json")
    # 读回既有累积值：计数跨会话累积。
    # 文件不存在/损坏 → 全零，不阻塞启动。
    metrics = TurnMetrics.load(metrics_path)

    index = _build_index(session)
    print(f"已载入 {source}（working: {session.working_path}）")
    print(f"抽取到 {len(facts.known_refs)} 条文献引用、{len(facts.known_numbers)} 个表格数字。")
    _verify_draft_refs(facts, project_facts_path(config.workspace_root, source))
    if _remembered:
        print(f"（已记住 {_remembered} 个此前章节的意图/结构，可复用）")
    _print_sections(index)
    _warn_if_no_sections(session, index)
    _note_docx_scope(session)
    _note_crossref_import(session)
    # 必须排在 _note_crossref_import 之后：docx 的交叉引用域在载入时才被摊平成
    # 纯文本，摊平前投影层看不见域内文字 ⇒ 那些引用会被误判成"正文没引用"。
    _note_dangling_refs(session)
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
        if msg.startswith(":ask"):
            # 只读问答（react 循环）。刻意用显式命令而不是接进 Router：
            # 现有五个写作意图一行不动，零回归面、零新增 flag。
            question = msg[len(":ask"):].strip()
            if not question:
                print("用法：:ask 你的问题（例如 :ask 哪几章还没写）")
                continue
            _run_ask_command(question, session, llm, config, facts, coordinator)
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
        if msg == ":save" or msg == ":save --plain":
            # docx 与 md/tex 走同一条路：working 本身就是同格式副本，直接拷回。
            # 就地编辑不再有格式转换，因此不需要"另存以防丢排版"那套额外问答。
            # docx 默认**存成交叉引用域版**（`--plain` 存纯文本 `[n]`）：编号能在
            # Word 里自动更新，且下次载入会自动摊平，不必记额外命令。
            want_xref = (
                session.save_crossrefs
                and bool(session.docx_source)
                and msg != ":save --plain"
            )
            saved: list[str] = []

            def _do_save(bus, _ct):
                saved.append(session.save_to_source(bus=bus, crossrefs=want_xref))
                return None
            coordinator.run(_do_save)  # :save 是独立 run
            print(f"已保存到：{saved[0] if saved else session.source_path}")
            _note_save_crossref(session, want_xref)
            continue
        if msg == ":export" or msg.startswith(":export "):
            _cmd_export(session, msg)
            continue
        if msg == ":undo":
            if session.undo():
                # 正文与账本别名同回退：只回正文会让旧号解析到别的文献。
                aliases = session.take_undo_state()
                if aliases:
                    facts.ledger.restore_numeric_aliases(aliases)
                    facts.save()
                print("已撤销。")
            else:
                print("没有可撤销的修改。")
            continue

        # 所有自然语言先经 OperationAnalyzer；零个受支持写作操作时转 AskLoop。
        metrics.record_turn()
        out = _run_planning_turn(
            turn_service, msg, session=session, facts=facts, metrics=metrics,
        )
        metrics.save(metrics_path)
        if out is None:
            continue
        if (
            out.planning_exit is not None
            and out.planning_exit.route_to_readonly
            and out.readonly_result is not None
        ):
            metrics.record_readonly_fallback()
            metrics.save(metrics_path)
            print("（未识别到受支持的写作操作，转为只读回答）")
            # AskLoop 已由 TurnService 在同一个 Coordinator run 内完成；这里只呈现，
            # 不能再调用 `_run_ask_command`，否则一条消息会生成第二份 trace。
            _print_ask_result(out.readonly_result)
            continue
        _print_plan_outcome(out)

    # 退出时给出 Planning 终态与只读交接摘要。
    if metrics.turns:
        metrics.save(metrics_path)
        print("\n" + metrics.summary())
        print(f"（明细：{metrics_path}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
