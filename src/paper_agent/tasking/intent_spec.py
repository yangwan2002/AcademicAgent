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
            execution_binding=_b(BindingKind.PLAN_FIELD, "PolishPlan.section_id"),
        ),
        "goal": SlotSpec(
            "goal", required=False, default_factory=lambda: "保守润色",
            execution_binding=_b(BindingKind.PLAN_FIELD, "PolishPlan.goal"),
        ),
        "constraints": SlotSpec(
            "constraints", required=False, default_factory=list,
            execution_binding=_b(BindingKind.CONTEXT_FIELD, "ContextBuilder.constraints"),
        ),
    },
)

REWRITE_SPEC = IntentSpec(
    intent=Intent.REWRITE,
    slots={
        "section_id": SlotSpec(
            "section_id", required=True, must_ask=True,
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.section_id"),
        ),
        "direction": SlotSpec(
            "direction", required=True, must_ask=True,
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.direction"),
        ),
        "details": SlotSpec(
            "details", required=False, default_factory=lambda: "",
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.details"),
        ),
        "search_query": SlotSpec(
            "search_query", required=False, default_factory=lambda: "",
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.search_query"),
        ),
        "want_search": SlotSpec(
            "want_search", required=False, default_factory=lambda: False,
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.want_search"),
        ),
        "search_limit": SlotSpec(
            "search_limit", required=False, default_factory=lambda: 5,
            execution_binding=_b(BindingKind.PLAN_FIELD, "RewritePlan.search_limit"),
        ),
    },
)

ADD_SECTION_SPEC = IntentSpec(
    intent=Intent.ADD_SECTION,
    slots={
        "title": SlotSpec(
            "title", required=True, must_ask=True,
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.title"),
        ),
        "purpose": SlotSpec(
            "purpose", required=True, must_ask=True,
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.purpose"),
        ),
        "structure_kind": SlotSpec(
            "structure_kind", required=True, must_ask=True, reuse_history=True,
            validator=lambda v: v in ("desc", "list", "mimic"),
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.structure_kind"),
        ),
        "structure_desc": SlotSpec(
            "structure_desc", required=False, default_factory=lambda: "",
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.structure_desc"),
        ),
        "subsections": SlotSpec(
            "subsections", required=False, default_factory=list,
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.subsections"),
        ),
        "position": SlotSpec(
            "position", required=False, default_factory=lambda: "end",
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.position"),
        ),
        "search_query": SlotSpec(
            "search_query", required=False, default_factory=lambda: "",
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.search_query"),
        ),
        "want_search": SlotSpec(
            "want_search", required=False, default_factory=lambda: False,
            execution_binding=_b(BindingKind.PLAN_FIELD, "AddSectionPlan.want_search"),
        ),
        "search_limit": SlotSpec(
            "search_limit", required=False, default_factory=lambda: 5,
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
            execution_binding=_b(BindingKind.PLAN_FIELD, "ConvertPlan.target_ext"),
        ),
        "presets": SlotSpec(
            "presets", required=False, default_factory=list,
            execution_binding=_b(BindingKind.PLAN_FIELD, "ConvertPlan.presets"),
        ),
    },
)

FORMAT_DOCX_SPEC = IntentSpec(
    intent=Intent.FORMAT_DOCX,
    slots={
        "operations": SlotSpec(
            "operations", required=True, must_ask=True,
            execution_binding=_b(BindingKind.EXECUTE_ARG, "execute_format.ops"),
        ),
        "scope": SlotSpec(
            "scope", required=False, default_factory=lambda: "document",
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
