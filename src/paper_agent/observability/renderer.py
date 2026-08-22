"""ConsoleRenderer：把运行状态与流式候选显示到控制台的下游 EventSink。

职责=呈现（"streaming" 只是它处理的一类事件）。核心约束：
- 流式 `LLM_DELTA` 显示到「候选区」，且明确标示为**待 gate 校验的候选**（该次
  LLM 调用首个增量前打 `[候选·校验中…]`），杜绝用户误以为已写入文档（Req 2.2）。
- `LLM_THINKING` **永不显示原文**，仅显示「正在分析…」一次（Req 5.1）。
- Gate 失败呈现 violations；`GENERATION_ATTEMPT_START` 用分隔线把新候选与上次
  失败候选分区（Req 3.4/3.5）；候选作废/取消给出明确提示（Req 4.4/6.x）。
- `escape_terminal_controls()` 过滤 ANSI 转义 / 回车覆盖 / 清屏等控制序列，只保留
  普通换行/制表/Unicode——防止模型输出伪造「✓ 已通过」之类的终端假象。

渲染器是纯下游消费者：只读事件、只写 stdout，不触碰任何业务状态。任何自身异常由
上游 FanOutSink 隔离（置 renderer_failed），不拖垮业务。
"""

from __future__ import annotations

import re
import sys

from paper_agent.observability.events import Event, EventKind

# 允许保留的控制字符：制表、换行（\n 会被规范化）。其余 C0/C1 控制符一律剔除，
# 尤其是 ESC(0x1b，ANSI 序列前导)、CR(0x0d，回车覆盖)、BEL、退格等。
_ALLOWED_CONTROL = {"\t", "\n"}
_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_OTHER_ESC = re.compile(r"\x1b.")


def escape_terminal_controls(text: str) -> str:
    """剔除可用于伪造终端显示的控制序列，保留普通换行/制表/Unicode。"""
    if not text:
        return text
    # 先整体去掉 ANSI 序列（CSI / OSC / 其它 ESC 引导）。
    s = _ANSI_CSI.sub("", text)
    s = _ANSI_OSC.sub("", s)
    s = _OTHER_ESC.sub("", s)
    # 回车统一并入换行语义（防止 \r 回退到行首覆盖已输出内容）。
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # 逐字符过滤剩余控制码（保留 \t \n 与所有可打印/Unicode 字符）。
    out = []
    for ch in s:
        if ch in _ALLOWED_CONTROL:
            out.append(ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            continue  # 其它 C0 控制符 / DEL
        elif 0x80 <= ord(ch) <= 0x9F:
            continue  # C1 控制符
        else:
            out.append(ch)
    return "".join(out)


class ConsoleRenderer:
    """实现 EventSink：把事件渲染到控制台。"""

    def __init__(self, stream=None, show_thinking: bool = False) -> None:
        self._out = stream if stream is not None else sys.stdout
        self._show_thinking = show_thinking
        # 每个 llm_call 首个增量前是否已打过候选标识。
        self._delta_started_calls: set[str] = set()
        # 每个 llm_call 是否已提示过「正在分析…」。
        self._thinking_notified_calls: set[str] = set()

    # --- EventSink ---

    def emit(self, event: Event) -> None:
        kind = event.kind
        if kind is EventKind.LLM_DELTA:
            self._on_delta(event)
        elif kind is EventKind.LLM_THINKING:
            self._on_thinking(event)
        elif kind is EventKind.GENERATION_ATTEMPT_START:
            self._on_attempt_start(event)
        elif kind is EventKind.GENERATION_ATTEMPT_END:
            self._on_attempt_end(event)
        elif kind is EventKind.GATE_RESULT:
            self._on_gate_result(event)
        elif kind is EventKind.GATE_RETRY:
            self._on_gate_retry(event)
        elif kind is EventKind.RUN_CANCELLED:
            self._line("⚠ 本次运行已取消")
        elif kind is EventKind.RETRIEVAL_START:
            self._line("· 检索中…")
        elif kind is EventKind.RETRIEVAL_END:
            self._line(f"· 检索完成：{self._get(event, 'count', 0)} 条")
        elif kind is EventKind.GATE_START:
            self._line("· 校验候选中…")

    # --- 具体渲染 ---

    def _on_delta(self, event: Event) -> None:
        call_id = event.llm_call_id or ""
        if call_id not in self._delta_started_calls:
            self._delta_started_calls.add(call_id)
            self._line("[候选·校验中…]")
        text = escape_terminal_controls(str(self._get(event, "text", "")))
        self._write(text)

    def _on_thinking(self, event: Event) -> None:
        # 永不显示原文；仅当显式开启时提示一次「正在分析…」。
        if not self._show_thinking:
            return
        call_id = event.llm_call_id or ""
        if call_id in self._thinking_notified_calls:
            return
        self._thinking_notified_calls.add(call_id)
        self._line("[正在分析…]")

    def _on_attempt_start(self, event: Event) -> None:
        attempt = self._get(event, "attempt", event.attempt)
        self._line("─" * 40)
        self._line(f"▶ 生成候选（第 {attempt} 次尝试）")

    def _on_attempt_end(self, event: Event) -> None:
        status = self._get(event, "status", "")
        if status in ("rejected", "cancelled", "error"):
            self._line("")
            self._line("[该候选已作废，未写入文档]")

    def _on_gate_result(self, event: Event) -> None:
        passed = self._get(event, "passed", None)
        if passed:
            self._line("✓ 候选通过校验")
            return
        self._line("✗ 候选未通过校验：")
        violations = self._get(event, "violations", []) or []
        for v in violations:
            self._line(f"  - {escape_terminal_controls(str(v))}")

    def _on_gate_retry(self, event: Event) -> None:
        attempt = self._get(event, "attempt", event.attempt)
        self._line(f"↻ 校验未过，重新生成（第 {attempt} 次尝试）")

    # --- 输出原语 ---

    @staticmethod
    def _get(event: Event, key: str, default=None):
        try:
            return event.data.get(key, default)
        except Exception:  # noqa: BLE001 - 渲染绝不抛
            return default

    def _write(self, text: str) -> None:
        self._out.write(text)
        flush = getattr(self._out, "flush", None)
        if callable(flush):
            flush()

    def _line(self, text: str) -> None:
        self._write(text + "\n")
