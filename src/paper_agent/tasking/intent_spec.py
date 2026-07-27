"""IntentSpec 注册表（Task 4）：五意图的槽位规格。

作用：
- 声明每个意图有哪些槽位、哪些必填、缺失时是否追问（`must_ask`）、默认值（由
  **PlanBuilder** 按此填，**不由 LLM**）、规范化/校验钩子、能否复用历史值。
- 每个槽位声明 **结构化 ExecutionBinding**（落到现有 Plan 字段 / execute_* 参数 /
  ContextBuilder / Gate），杜绝「抽取了却没人执行」。字符串太难校验，故用枚举。

本模块只放**配置与查询**，不含 LLM/执行逻辑。绑定「是否真被 Handler 消费」的完整
校验在 Task 19（Handler 完成后）真验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from paper_agent.intents.interview import _FORMAT_ALIASES
from paper_agent.intents.router import Intent

# convert 目标格式的规范化与校验（复用 interview 的别名表，避免双份真相源）。
_CANONICAL_EXTS = set(_FORMAT_ALIASES.values())  # {"docx", "tex", "md"}


def _normalize_target_ext(value: object) -> object:
    """把用户口径（Word / latex / .md ...）规范化为规范扩展名（docx/tex/md）。

    无法识别时原样返回，交给 validator 标 invalid（不静默丢弃）。
    """
    key = str(value).strip().lower().lstrip(".")
    return _FORMAT_ALIASES.get(key, value)


def _valid_target_ext(value: object) -> bool:
    return value in _CANONICAL_EXTS


class BindingKind(str, Enum):
    """槽位最终落到系统哪一处。"""

    PLAN_FIELD = "plan_field"        # 现有 *Plan 的字段（如 PolishPlan.section_id）
    EXECUTE_ARG = "execute_arg"      # execute_* 的参数
    CONTEXT_FIELD = "context_field"  # ContextBuilder 的输入（如 constraint block）
    GATE_POLICY = "gate_policy"      # Gate 配置/策略


@dataclass(frozen=True)
class ExecutionBinding:
    kind: BindingKind
    target: str                      # 具体字段/参数名，如 "PolishPlan.section_id"


@dataclass(frozen=True)
class SlotSpec:
    name: str
    required: bool
    execution_binding: ExecutionBinding
    must_ask: bool = False           # 缺失时是否必须澄清
    default_factory: Callable[[], object] | None = None  # 系统默认值（非 LLM）
    normalizer: Callable[[object], object] | None = None
    validator: Callable[[object], bool] | None = None
    reuse_history: bool = False      # 是否允许复用 facts 历史值
    # --- 抽取指导（function calling 的字段级 description 与类型）---
    # `description` 是该槽位的**判定规矩**，会直接作为 JSON schema 的字段
    # description 交给模型；比堆在 system prompt 里更贴近字段本身。
    description: str = ""
    value_type: str = "string"       # string | integer | number | boolean | array | object

    def default(self):
        return self.default_factory() if self.default_factory is not None else None


@dataclass(frozen=True)
class IntentSpec:
    intent: Intent
    slots: dict[str, SlotSpec] = field(default_factory=dict)

    def required_slots(self) -> list[str]:
        return [n for n, s in self.slots.items() if s.required]

    def must_ask_slots(self) -> list[str]:
        return [n for n, s in self.slots.items() if s.must_ask]


def _b(kind: BindingKind, target: str) -> ExecutionBinding:
    return ExecutionBinding(kind=kind, target=target)


# --- 五意图规格（对应 design 附录表）---

POLISH_SPEC = IntentSpec(
    intent=Intent.POLISH,
    slots={
        "section_id": SlotSpec(
            "section_id", required=True, must_ask=True,
            description="要润色的章节名称或标识（用户指明的那一章）。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "PolishPlan.section_id"),
        ),
        "goal": SlotSpec(
            "goal", required=False, default_factory=lambda: "保守润色",
            description=(
                "润色要**达成什么**——程度或风格取向。只放「要变成什么样」的诉求；"
                "「不能做什么」属于 constraints，不要放这里。"
            ),
            execution_binding=_b(BindingKind.PLAN_FIELD, "PolishPlan.goal"),
        ),
        "constraints": SlotSpec(
            "constraints", required=False, default_factory=list, value_type="array",
            description=(
                "润色**不得越过的边界**，逐条独立列出，保留用户原话措辞。\n"
                "判定规则（按语义作用判断，不要匹配固定句式）：一条要求若限定"
                "「不能做什么」「必须维持原样什么」「不得超过什么量」，即为边界约束。\n"
                "表达方式不限：否定式、保持式、限量式、口语或倒装皆可，只要语义是边界约束。\n"
                "可被约束的对象包括但不限于：数字与数据、引用与文献、公式、表格与图、"
                "章节与段落结构及顺序、术语与专有名词、语气与断言强度、篇幅与字数、格式排版；"
                "未列出但语义同为边界约束的同样收录。\n"
                "一句话里有多个约束时**必须拆成多项**，不要合并成一条。"
            ),
            execution_binding=_b(BindingKind.CONTEXT_FIELD, "ContextBuilder.constraints"),
        ),
    },
)

REWRITE_SPEC = IntentSpec(
    intent=Intent.REWRITE,
    slots={
        "section_id": SlotSpec(
            "section_id", required=True, must_ask=True,
            description="要改写的章节名称或标识。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.section_id"),
        ),
        "direction": SlotSpec(
            "direction", required=True, must_ask=True,
            description=(
                "改动方向——用户希望往哪个方向改（如补充论证、调整结构、"
                "更换叙述角度等）。用用户自己的说法概括。"
            ),
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.direction"),
        ),
        "details": SlotSpec(
            "details", required=False, default_factory=lambda: "",
            description="对改动方向的具体补充说明（要补什么论据、怎么调整等）。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.details"),
        ),
        "search_query": SlotSpec(
            "search_query", required=False, default_factory=lambda: "",
            description=(
                "要检索的文献主题关键词。仅当用户明确表达了「查/搜文献」"
                "或「引用现有工作」的意图时给出；否则留空。"
            ),
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.search_query"),
        ),
        "want_search": SlotSpec(
            "want_search", required=False, default_factory=lambda: False,
            value_type="boolean",
            description="是否需要检索文献。不确定就不要给（由系统据 search_query 推导）。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.want_search"),
        ),
        "search_limit": SlotSpec(
            "search_limit", required=False, default_factory=lambda: 5,
            value_type="integer",
            description="检索条数上限；用户没说数量就不要给。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.search_limit"),
        ),
    },
)

ADD_SECTION_SPEC = IntentSpec(
    intent=Intent.ADD_SECTION,
    slots={
        "title": SlotSpec(
            "title", required=True, must_ask=True,
            description="新章节的标题/名称。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.title"),
        ),
        "purpose": SlotSpec(
            "purpose", required=True, must_ask=True,
            description="这一章要达成的目的或承担的作用（一句话）。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.purpose"),
        ),
        "structure_kind": SlotSpec(
            "structure_kind", required=True, must_ask=True, reuse_history=True,
            validator=lambda v: v in ("desc", "list", "mimic"),
            description=(
                "章节内部结构的组织方式，只能取三者之一："
                "desc（用一句话描述骨架）、list（给出若干小节标题）、"
                "mimic（模仿另一篇论文的某章结构）。判断不了就不要给。"
            ),
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.structure_kind"),
        ),
        "structure_desc": SlotSpec(
            "structure_desc", required=False, default_factory=lambda: "",
            description="structure_kind=desc 时，用一句话描述这一章的骨架。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.structure_desc"),
        ),
        "subsections": SlotSpec(
            "subsections", required=False, default_factory=list, value_type="array",
            description="structure_kind=list/mimic 时的小节标题列表，按顺序给出。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.subsections"),
        ),
        "position": SlotSpec(
            "position", required=False, default_factory=lambda: "end",
            description=(
                "插入位置，取值形如 start / end / after:<章节名> / before:<章节名>。"
                "用户说「放在X前/后」时用 before:X / after:X。没说就不要给。"
            ),
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.position"),
        ),
        "search_query": SlotSpec(
            "search_query", required=False, default_factory=lambda: "",
            description=(
                "要检索的文献主题关键词。仅当用户明确要求查/搜文献或引用现有工作时给出。"
            ),
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.search_query"),
        ),
        "want_search": SlotSpec(
            "want_search", required=False, default_factory=lambda: False,
            value_type="boolean",
            description="是否需要检索文献。不确定就不要给（系统会据 search_query 推导）。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.want_search"),
        ),
        "search_limit": SlotSpec(
            "search_limit", required=False, default_factory=lambda: 5,
            value_type="integer",
            description="检索条数上限；用户没说数量就不要给。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.search_limit"),
        ),
    },
)

CONVERT_SPEC = IntentSpec(
    intent=Intent.CONVERT_FORMAT,
    slots={
        "target_ext": SlotSpec(
            "target_ext", required=True, must_ask=True,
            normalizer=_normalize_target_ext, validator=_valid_target_ext,
            description=(
                "目标文件格式，按用户原话给出即可（如 Word / docx / latex / markdown），"
                "系统会自行归一。"
            ),
            execution_binding=_b(BindingKind.PLAN_FIELD, "ConvertPlan.target_ext"),
        ),
        "presets": SlotSpec(
            "presets", required=False, default_factory=list, value_type="array",
            description="转换预设/样式要求（如双栏、特定模板）；没提就不要给。",
            execution_binding=_b(BindingKind.PLAN_FIELD, "ConvertPlan.presets"),
        ),
    },
)

FORMAT_DOCX_SPEC = IntentSpec(
    intent=Intent.FORMAT_DOCX,
    slots={
        "operations": SlotSpec(
            "operations", required=True, must_ask=True, value_type="array",
            description=(
                "排版操作列表，每项形如 {name, value, scope}。\n"
                "name 只能取：set_font_family / set_font_size / set_bold / set_italic /"
                " set_alignment / set_line_spacing / set_first_line_indent /"
                " set_columns / set_margins / set_page_orientation /"
                " apply_three_line_tables。\n"
                "scope 只能取：all / body / headings / heading1。\n"
                "不在上述清单内的排版诉求不要臆造操作名。"
            ),
            execution_binding=_b(BindingKind.EXECUTE_ARG, "execute_format.ops"),
        ),
        "scope": SlotSpec(
            "scope", required=False, default_factory=lambda: "document",
            description="整体作用范围（一般为 document）；用户没特别说就不要给。",
            execution_binding=_b(BindingKind.EXECUTE_ARG, "execute_format.scope"),
        ),
    },
)


INTENT_SPECS: dict[Intent, IntentSpec] = {
    Intent.POLISH: POLISH_SPEC,
    Intent.REWRITE: REWRITE_SPEC,
    Intent.ADD_SECTION: ADD_SECTION_SPEC,
    Intent.CONVERT_FORMAT: CONVERT_SPEC,
    Intent.FORMAT_DOCX: FORMAT_DOCX_SPEC,
}


def spec_for(intent: Intent) -> IntentSpec | None:
    return INTENT_SPECS.get(intent)


__all__ = [
    "BindingKind", "ExecutionBinding", "SlotSpec", "IntentSpec",
    "INTENT_SPECS", "spec_for",
    "POLISH_SPEC", "REWRITE_SPEC", "ADD_SECTION_SPEC", "CONVERT_SPEC", "FORMAT_DOCX_SPEC",
]
