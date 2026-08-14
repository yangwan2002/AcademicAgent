"""只读问答循环（P-4 第一步）——本项目第一条 react 范式通路。

与五个写作意图的区别：写作意图是 **workflow**（访谈 → 生成 → Gate → 重试 → 写入，
阶段由代码写死）；这里是 **react**（模型自己决定下一步调哪个工具、什么时候答完）。

**为什么这里可以让 LLM 判停，而写作意图不行**：写作的目标可验证（Gate 过不过、配额
够不够），程序判断严格优于模型判断；"这个问题答好了吗"没有程序判据，LLM 判停不是
选择而是唯一出路。所以这条通路只读、不产计划、不碰 Gate 体系。

四层护栏（缺一不可）：
1. 内层：模型不再请求工具 → 停（正常出口）
2. 硬迭代上限 `max_iterations`
3. 超限是**失败终态** `ITERATION_LIMIT`，绝不报"回答完成"
4. LLM 调用次数上限；工具集只读 ⇒ 不需要 permission prompter 那一整套

第 3 条要特别强调：把超限伪装成正常结束，会让用户拿到一个基于半截推理的答案却以为
是完整结论。停下来必须说清为什么停。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from enum import Enum

from paper_agent.application.ask_context import (
    ASK_TOOLS,
    AskContext,
    ToolDispatchError,
    dispatch,
)
from paper_agent.observability.events import EventKind
from paper_agent.observability.sanitize import digest
from paper_agent.providers.llm.base import LLMProvider, Message

_SYSTEM = """你是学术论文写作助手的问答模块。用户在问关于他这篇论文的问题。

你只能读，不能改。想改动文档时不要假装改了，而要告诉用户可以用哪个功能
（润色 / 改写 / 新增章节 / 转格式 / 修参考文献）。

工作方式：
- 需要信息就调工具。不确定章节 id 时先调 list_sections。
- 能用章节摘要回答的，不要去读正文（读正文很贵）。
- 拿到足够信息后直接用自然语言回答，不要再调工具。

纪律：
- 只根据工具返回的内容回答。没有的信息就说"文档里看不出来"，不要推测、不要编。
- 工具返回标注了"已截断"时，明确告诉用户你只看了一部分。
- 数字要按给出的口径回答，不要把不同口径的数相加或混用：
  "正文引用了几个编号"和"文献表有几条"是两个数；行内 $x$ 是变量符号不是公式；
  谈"正文有几章"时不要算进标了"（非正文）"的参考文献/致谢/附录。
  计数是正则近似值，但这不等于可以给错口径的数 —— 先选对口径，再说明是近似。
- 问到"刚才改了什么""改过几次"这类历史问题，调 edit_history，不要说自己看不到历史。
- 问到"哪一章引了哪些文献""某篇在哪引过""哪章引用最多"，调 section_citations，
  不要去读正文自己数方括号（数不准，且贵）。它报的是**正文实际引用**，
  与 doc_stats 的"文献表条目数"不是一个口径。
- 用中文回答，简洁直接。"""


class AskStopReason(str, Enum):
    """循环为什么停。**每个终态都必须能对用户解释清楚。**"""

    ANSWERED = "answered"                  # 模型不再要工具，给出了回答
    ITERATION_LIMIT = "iteration_limit"    # 护栏 2/3：撞迭代上限
    BUDGET_EXHAUSTED = "budget_exhausted"  # 护栏 4：撞 LLM 调用上限
    PROVIDER_FAILED = "provider_failed"    # LLM 调用异常
    CANCELLED = "cancelled"                # 用户取消


#: 非 ANSWERED 终态给用户看的说明。措辞要让人知道"这不是完整答案"。
_STOP_NOTE = {
    AskStopReason.ITERATION_LIMIT: (
        "我查了多轮仍没收敛，已到迭代上限停下。上面是已经查到的部分，不是完整结论。"
        "把问题拆小一点问会更好。"
    ),
    AskStopReason.BUDGET_EXHAUSTED: (
        "这次问答的模型调用次数用完了，已停下。上面是已经查到的部分，不是完整结论。"
    ),
    AskStopReason.PROVIDER_FAILED: "模型调用失败了，这次没能答完。",
    AskStopReason.CANCELLED: "已取消。",
}


def _publish(bus, kind: EventKind, **data) -> None:
    """发一个观测事件。无 bus / 无该入口 / 发布抛异常一律静默。

    观测**永不**改变业务可见行为：`run_ask` 不传 bus 时（测试、库调用）行为
    与埋点前逐字节一致。
    """
    if bus is None:
        return
    pub = getattr(bus, "publish", None)
    if not callable(pub):
        return
    try:
        pub(kind, **data)
    except Exception:  # noqa: BLE001 - 观测失败不拖垮问答
        pass


@contextlib.contextmanager
def _span(bus, name: str, **attrs):
    """给这次问答开一个 span，让三个事件挂在自己的子树上而不是散在 run 根上。

    没有 span 入口时退化为零成本空上下文（同 `current.ambient_span`）。
    """
    span = getattr(bus, "span", None) if bus is not None else None
    if span is None:
        yield None
        return
    try:
        scope = span(name, **attrs)
    except Exception:  # noqa: BLE001
        yield None
        return
    with scope as entered:
        yield entered


@dataclass
class AskResult:
    stop_reason: AskStopReason
    answer: str = ""
    llm_calls: int = 0
    iterations: int = 0
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """只有 ANSWERED 算成功。护栏触发一律不算。"""
        return self.stop_reason is AskStopReason.ANSWERED

    def display(self) -> str:
        """给终端看的文本：回答 + （非正常终态时）为什么停。"""
        note = _STOP_NOTE.get(self.stop_reason)
        if self.ok:
            return self.answer
        parts = [self.answer.strip()] if self.answer.strip() else []
        if note:
            parts.append(f"（{note}）")
        if self.error:
            parts.append(f"错误：{self.error}")
        return "\n\n".join(parts) or "这次没能给出回答。"


def run_ask(
    question: str,
    ctx: AskContext,
    llm: LLMProvider,
    *,
    max_iterations: int = 10,
    max_llm_calls: int = 12,
    cancel_token=None,
    bus=None,
) -> AskResult:
    """跑一次只读问答。

    `ctx` 是 frozen 快照，**不含 session** —— 循环内没有任何写入路径。

    `bus`：观测总线（可选）。给了就把这次问答埋成一个 `ask` span，内含
    ASK_TURN_START / ASK_TOOL_CALL / ASK_TURN_END；不给则行为逐字节不变。

    **ASK_TURN_END 只在这里发一次**（同 `Coordinator` 对 WORKFLOW_END 的口径）：
    内层 `_loop` 有 5 个 return 分支，逐个补发必然漏，且漏掉的恰是异常路径 ——
    而"为什么停"正是要观测的东西。
    """
    with _span(bus, "ask"):
        _publish(
            bus, EventKind.ASK_TURN_START,
            # 只出 hash 与长度：用户问句是整句原话，绝不进 trace（同 A2f 主题/查询口径）。
            question_hash=digest(question),
            question_len=len(question),
            max_iterations=max_iterations,
            max_llm_calls=max_llm_calls,
            section_count=len(ctx.sections),
        )
        try:
            res = _loop(
                question, ctx, llm,
                max_iterations=max_iterations, max_llm_calls=max_llm_calls,
                cancel_token=cancel_token, bus=bus,
            )
        except BaseException as exc:
            # `_loop` 已把 provider 异常收成 PROVIDER_FAILED 终态，走到这里说明是
            # 预料外的（工具 dispatch 抛了非 ToolDispatchError 等）。**异常照常抛出**，
            # 但终态必须留痕：不然 trace 里只剩一个没有结尾的 ask span，
            # 而"崩在哪一步"恰是这时最需要的信息。
            _publish(
                bus, EventKind.ASK_TURN_END,
                stop_reason="crashed", ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        _publish(
            bus, EventKind.ASK_TURN_END,
            stop_reason=res.stop_reason.value,
            ok=res.ok,
            iterations=res.iterations,
            llm_calls=res.llm_calls,
            tool_call_count=len(res.tool_calls),
            # 工具名是固定词表（5 个），不含用户数据，可原样落盘 —— 这是"它查了什么"
            # 唯一的可追溯记录。
            tool_calls=list(res.tool_calls),
            answer_len=len(res.answer),
            error=res.error,
        )
    return res


def _loop(
    question: str,
    ctx: AskContext,
    llm: LLMProvider,
    *,
    max_iterations: int,
    max_llm_calls: int,
    cancel_token,
    bus,
) -> AskResult:
    """react 循环本体。终态由调用方统一上报，这里只管跑和发工具事件。"""
    messages = [
        Message(role="system", content=_SYSTEM),
        Message(role="system", content="当前文档情况：\n" + ctx.facts_digest()),
        Message(role="user", content=question),
    ]

    llm_calls = 0
    tool_names: list[str] = []
    last_text = ""

    for iteration in range(1, max_iterations + 1):
        if cancel_token is not None and getattr(cancel_token, "cancelled", False):
            return AskResult(
                AskStopReason.CANCELLED, answer=last_text,
                llm_calls=llm_calls, iterations=iteration - 1, tool_calls=tool_names,
            )

        # 护栏 4：先检查预算，再花钱。
        if llm_calls >= max_llm_calls:
            return AskResult(
                AskStopReason.BUDGET_EXHAUSTED, answer=last_text,
                llm_calls=llm_calls, iterations=iteration - 1, tool_calls=tool_names,
            )

        try:
            resp = llm.complete(list(messages), tools=list(ASK_TOOLS))
            llm_calls += 1
        except Exception as exc:  # noqa: BLE001 - 失败要如实分类，不能塌成空回答
            return AskResult(
                AskStopReason.PROVIDER_FAILED, answer=last_text,
                llm_calls=llm_calls, iterations=iteration, tool_calls=tool_names,
                error=f"{type(exc).__name__}: {exc}",
            )

        if resp.content:
            last_text = resp.content

        # 护栏 1（内层）：不再要工具 ⇒ 这是答案。
        if not resp.tool_calls:
            return AskResult(
                AskStopReason.ANSWERED, answer=resp.content or last_text,
                llm_calls=llm_calls, iterations=iteration, tool_calls=tool_names,
            )

        # 回灌 assistant 的工具请求，再逐个回灌结果。
        messages.append(
            Message(role="assistant", content=resp.content or "", tool_calls=list(resp.tool_calls))
        )
        for tc in resp.tool_calls:
            tool_names.append(tc.name)
            failed = False
            try:
                result = dispatch(ctx, tc.name, tc.arguments or {})
            except ToolDispatchError as exc:
                # 参数/工具名错不是终态：把错误回灌，让模型自己改。这就是
                # X-7 记的"拒绝里要带上正确做法"——dispatch 的报错含可用 id 列表。
                result = f"调用失败：{exc}"
                failed = True
            _publish(
                bus, EventKind.ASK_TOOL_CALL,
                iteration=iteration,
                tool=tc.name,
                # 参数键名（不出值）：`read_section` 的 section_id 值是文档内容派生物。
                # 键名足以看出模型是按 id 查还是全量查。
                argument_keys=sorted((tc.arguments or {}).keys()),
                failed=failed,
                result_len=len(result),
                # 截断是**降级**，必须在 trace 里可见：模型据半章正文下的断言，
                # 事后要能从这里看出它只看了一部分。
                truncated="[已截断" in result,
            )
            messages.append(
                Message(role="tool", content=result, tool_call_id=tc.id)
            )

    # 护栏 2+3：循环自然跑完 = 撞上限。**不是** ANSWERED。
    return AskResult(
        AskStopReason.ITERATION_LIMIT, answer=last_text,
        llm_calls=llm_calls, iterations=max_iterations, tool_calls=tool_names,
    )
