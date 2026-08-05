"""检索意图合成器（A2f-2，design-a2f D2/D3）。

把 `SearchContext`（用户显式 query + operation 语义 + 受控 project claims）收敛为一个
**冻结的 `SearchIntent`**：1 个语义主题 + 1–6 个检索 seed。

两种实现：
- `DeterministicSearchIntentSynthesizer`：**不调 LLM**。有显式 query → 直接构造；否则从
  operation/claims 的有界字段确定性投影出主题与 seed。信号不足 → 空结果（调用方判 NEEDS_INPUT）。
- `LLMSearchIntentSynthesizer`：structured tool call 产出主题/section_purpose/seeds/target；
  schema **不含** quota/filter/provider/fingerprint 字段（结构上杜绝越权）。失败/非法 → 空结果。

纪律：
- LLM 只产**内容**；kind/上限/校验由代码定（本模块与 search_intent 验证器）。
- 输出统一经 `search_intent` 的验证与 `build_seeds` 收敛：剔空/通用章节名/纯标点数字/超长/重复。
- 全部 seed 被删或主题不可用 → 返回 `None`（**不**偷偷恢复章节标题）。
- `tasking` 可依赖 `literature.retrieval.search_intent`（方向同 quota_materializer）；
  本模块不读全局 config（模型注入）。
"""

from __future__ import annotations

from typing import Protocol

from paper_agent.literature.retrieval.search_intent import (
    SearchContext,
    SearchIntent,
    SearchIntentError,
    SearchIntentOrigin,
    SearchSeed,
    SeedTarget,
    build_seeds,
    is_informative_title,
    normalize_text,
)
from paper_agent.providers.llm.base import LLMProvider, Message
from paper_agent.providers.llm.structured import CallStatus, structured_call

#: prompt 内容已变（加入论文上下文 + 可执行的语言提示）⇒ 版本号必须跟着变，
#: 否则跨版本的 golden 基线无法区分「模型变了」与「prompt 变了」。
SYNTH_PROMPT_VERSION = "search-intent-synth-a2f-v2"

#: 语言维度 → 人类可读标签（prompt 里不要出现内部枚举名）。
_LANG_LABEL = {"language.zh": "中文", "language.en": "英文"}


class SynthesisResult:
    """合成结果：intent 或 None（信号不足/失败）+ 是否**真的**发了 LLM 调用（计费用）。"""

    __slots__ = ("intent", "llm_called", "error")

    def __init__(self, intent: SearchIntent | None, *, llm_called: bool = False,
                 error: str = "") -> None:
        self.intent = intent
        self.llm_called = llm_called
        self.error = error


class SearchIntentSynthesizer(Protocol):
    def synthesize(self, context: SearchContext) -> SynthesisResult:
        ...


# ---------------------------------------------------------------- 确定性

def _explicit_intent(ctx: SearchContext) -> SearchIntent | None:
    """显式 query 有效 → 直接构造 USER_EXPLICIT intent（不调 LLM）。"""
    q = normalize_text(ctx.explicit_query, max_chars=300)
    seeds = build_seeds([SearchSeed(text=q, origin=SearchIntentOrigin.USER_EXPLICIT)]) if q else ()
    if not seeds:
        return None
    topic = _first_informative(ctx.explicit_query, ctx.purpose, ctx.direction, ctx.target_title) or q
    try:
        return SearchIntent(semantic_topic=topic, section_purpose=ctx.purpose,
                            seeds=seeds, origin=SearchIntentOrigin.USER_EXPLICIT)
    except SearchIntentError:
        return None


def _first_informative(*candidates: str) -> str:
    for c in candidates:
        norm = normalize_text(c, max_chars=500)
        if norm and is_informative_title(norm):
            return norm
    return ""


def _deterministic_projection(ctx: SearchContext) -> SearchIntent | None:
    """从有界字段确定性投影主题与 seed（无 LLM）。信号不足 → None。

    `paper_title` 排在最前：论文题目比「改写方向」更能说明**要检索什么**。
    章节标题进 seed 池：`2.1 跨视角图像匹配研究` 这类标题本身就是好检索词
    （通用章节名如「引言」会被 `build_seeds` 剔掉，无需在此过滤）。
    两者都是新增字段、默认空 ⇒ 不传时行为与既有逐字一致。
    """
    topic = _first_informative(
        ctx.paper_title,
        ctx.purpose, ctx.direction, ctx.details,
        ctx.project_gap, ctx.project_novelty, ctx.target_title,
    )
    if not topic:
        return None
    origin = (SearchIntentOrigin.OPERATION_DERIVED
              if _first_informative(ctx.purpose, ctx.direction, ctx.details, ctx.target_title)
              else SearchIntentOrigin.PROJECT_DERIVED)
    raw = [topic, *ctx.paper_section_titles, *ctx.structure_terms,
           *ctx.project_contributions, ctx.project_gap, ctx.project_novelty]
    # **必须传 dict 形态**：`build_seeds` 的过滤契约（剔通用章节名/超长/纯标点）只在
    # 它**自己构造** SearchSeed 时生效；先用列表推导构造好再传进去，异常会在进入
    # build_seeds 之前就抛出。原本这里是 `[SearchSeed(text=x) ...]`，属于既有的
    # 崩溃路径 —— `structure_terms` 里只要出现「Introduction」这类通用章节名就会
    # 整个请求 failed。加入 `paper_section_titles` 后这条路径必然被触发
    # （章节标题天然就是「引言」「Related Work」），故一并修正。
    seeds = build_seeds([{"text": str(x), "origin": origin.value} for x in raw if x])
    if not seeds:
        return None
    try:
        return SearchIntent(semantic_topic=topic, section_purpose=ctx.purpose,
                            seeds=seeds[:6], origin=origin)
    except SearchIntentError:
        return None


class DeterministicSearchIntentSynthesizer:
    """无 LLM：显式 query 优先，否则确定性投影。"""

    def synthesize(self, context: SearchContext) -> SynthesisResult:
        intent = _explicit_intent(context) or _deterministic_projection(context)
        return SynthesisResult(intent, llm_called=False)


# ---------------------------------------------------------------- LLM

_TOOL = {
    "type": "function",          # OpenAI 协议必填；见 relevance.py::_TOOL 的说明
    "function": {
        "name": "propose_search_intent",
        "description": (
            "据写作操作与论文上下文，提出**检索意图**：一个语义主题(semantic_topic) + "
            "1–6 个具体、可检索的关键词(seeds)。禁止把通用章节名(引言/相关工作/Introduction 等)"
            "当主题或关键词。只描述『检索什么』，不得输出篇数、语言/年份过滤、provider 或凭据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "semantic_topic": {"type": "string",
                                   "description": "这一章要引用哪方面文献的语义主题。"},
                "section_purpose": {"type": "string"},
                "seeds": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "具体检索关键词。"},
                            "target": {"type": "string",
                                       "enum": ["any", "language.zh", "language.en"],
                                       "description": "调度提示，仅在用户配额需要该语言时给。"},
                        },
                        "required": ["text"],
                    },
                },
            },
            "required": ["semantic_topic", "seeds"],
        },
    }
}

_SYSTEM = (
    "你是学术检索意图规划器。据写作操作与论文上下文，产出一个语义主题与 1–6 个具体关键词，"
    "用于检索切题文献。禁止用通用章节名作主题/关键词；只描述『检索什么』，"
    "不得输出篇数、语言/年份过滤、provider 或任何凭据。通过 propose_search_intent 返回。"
)


class LLMSearchIntentSynthesizer:
    """structured_call 合成。注入 writer 或专用 planner LLM（不复用相关性 reviewer）。"""

    def __init__(self, llm: LLMProvider, *, prompt_version: str = SYNTH_PROMPT_VERSION) -> None:
        self._llm = llm
        self.prompt_version = prompt_version

    def synthesize(self, context: SearchContext) -> SynthesisResult:
        # 显式 query 有效 → 不调 LLM（确定性优先，省钱）。
        explicit = _explicit_intent(context)
        if explicit is not None:
            return SynthesisResult(explicit, llm_called=False)

        messages = self._build_messages(context)
        call = structured_call(self._llm, messages, tool_schema=_TOOL)
        # 到这里确实发了一次模型调用（无论成功与否）→ 计费点。
        if call.call_status is not CallStatus.SUCCESS:
            det = _deterministic_projection(context)   # 有确定性信号则 fallback
            return SynthesisResult(det, llm_called=True,
                                   error=call.error_category or call.call_status.value)
        intent = self._to_intent(call.payload or {}, context)
        if intent is None:
            det = _deterministic_projection(context)
            return SynthesisResult(det, llm_called=True, error="empty_after_validation")
        return SynthesisResult(intent, llm_called=True)

    def _to_intent(self, payload: dict, ctx: SearchContext) -> SearchIntent | None:
        topic = normalize_text(str(payload.get("semantic_topic", "")), max_chars=500)
        if not topic or not is_informative_title(topic):
            return None
        seed_specs = []
        for s in payload.get("seeds") or []:
            if isinstance(s, dict):
                seed_specs.append({
                    "text": str(s.get("text", "")),
                    "target": s.get("target", "any"),
                    "origin": SearchIntentOrigin.OPERATION_DERIVED.value,
                })
        seeds = build_seeds(seed_specs)
        if not seeds:
            return None
        purpose = normalize_text(str(payload.get("section_purpose", "") or ctx.purpose),
                                 max_chars=500)
        try:
            return SearchIntent(semantic_topic=topic, section_purpose=purpose,
                                seeds=seeds[:6], origin=SearchIntentOrigin.OPERATION_DERIVED)
        except SearchIntentError:
            return None

    def _build_messages(self, ctx: SearchContext) -> list[Message]:
        """组装合成 prompt。

        **文档上下文必须在这里出现**（2026-08-01）：此前本函数只用 operation 槽位 +
        `facts.claims`，真机上对「完善我的引言部分」这类请求，整个 user 消息只有
        「写作操作：rewrite / 改写方向：完善引言部分」两行 —— 模型只能产出
        「引言这一章通常引什么」的通用模板（`research gap identification` 之类）。
        论文写的是什么，它无从得知。

        文档内容是**唯一**能说明「这篇论文在研究什么」的来源，故排在 operation
        槽位之前：论文主题决定检索什么，写作动作只决定这一章要哪个侧面。
        """
        parts: list[str] = []

        # --- 论文是什么（最重要，放最前）---
        title = normalize_text(ctx.paper_title, max_chars=300)
        if title:
            parts.append(f"论文题目：{title}")
        if ctx.paper_section_titles:
            titles = "、".join(
                normalize_text(t, max_chars=120)
                for t in ctx.paper_section_titles[:40] if t
            )
            if titles:
                parts.append(f"全文章节：{titles}")
        body = normalize_text(ctx.target_section_body, max_chars=2000)
        if body:
            parts.append(
                "目标章节的现有内容（可能是提纲或草稿，请据此判断这一章要引哪方面"
                "文献；其中的专业术语是重要线索）：\n" + body
            )

        # --- 这次要做什么 ---
        parts.append(f"写作操作：{ctx.operation_kind}")
        if is_informative_title(ctx.target_title):
            parts.append(f"章节标题：{normalize_text(ctx.target_title, max_chars=200)}")
        for label, val in (("章节目的", ctx.purpose), ("改写方向", ctx.direction),
                           ("补充说明", ctx.details), ("研究空白", ctx.project_gap),
                           ("创新点", ctx.project_novelty)):
            v = normalize_text(val, max_chars=500)
            if v:
                parts.append(f"{label}：{v}")
        if ctx.project_contributions:
            parts.append("贡献：" + "；".join(
                normalize_text(c, max_chars=200) for c in ctx.project_contributions if c))
        if ctx.requested_dimensions:
            # 措辞必须**可执行**：旧版只印内部枚举名（「配额涉及语言维度：language.zh」），
            # 模型无从知道该拿它做什么，于是 seed 全是英文、target 全 any，
            # 中文配额永远填不满。
            langs = "、".join(_LANG_LABEL.get(d, d) for d in ctx.requested_dimensions)
            parts.append(
                f"用户要求的文献语言包含：{langs}。请为每种语言分别给出 seed，"
                "并把该 seed 的 target 设成对应值（中文用 language.zh，英文用 "
                "language.en）；**中文 seed 的关键词本身要用中文**，不要给英文词再标中文。"
            )
        return [Message(role="system", content=_SYSTEM),
                Message(role="user", content="\n".join(parts))]


__all__ = [
    "SynthesisResult",
    "SearchIntentSynthesizer",
    "DeterministicSearchIntentSynthesizer",
    "LLMSearchIntentSynthesizer",
    "SYNTH_PROMPT_VERSION",
]
