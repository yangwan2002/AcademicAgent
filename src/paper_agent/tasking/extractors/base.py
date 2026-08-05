"""PreExtractor 公共框架（Task 9）。

职责（design Component§4 / D3 / D4）：
- **调用态/字段态分离**：一次抽取调用的整体成败用 `ExtractionResult.call_status`
  （success/provider_failed/parse_failed/schema_failed）表达；单个槽位的状态用
  `SlotValue.field_status`（present/user_absent/ambiguous/invalid/unavailable）。
  两者永不折叠——「用户没说(user_absent)」≠「因抽取失败暂缺(unavailable)」。
- **结构化输出唯一收敛点**：`structured_call()` 是唯一与 LLM 交互并拿回结构化 payload
  的地方，统一走 **function calling**（字段规矩写在 JSON schema 的 description 里，
  见 `build_tool_schema`）。真机实测 FC 显著优于「prompt 约束 JSON」（约束召回
  73%→100%、澄清追问 7→1），故 prompt 模式已移除；模型未走工具而回正文时按 JSON
  兜底解析，不塌成失败。
- **requirement 唯一真相源**（D4）：
  - Analyzer 路径（`canonical_requirements` 非 None）：Analyzer 已产 canonical
    requirements，PreExtractor 只回 `consumed_requirement_ids`，**不重复造 req**。
  - Fast_Path（`canonical_requirements is None`）：PreExtractor 负责产
    `newly_discovered_requirements`（offset 由程序定位）+ 消费关系。
- **整体失败兜底**（D4 #7）：抽取调用失败时所有槽位记 `unavailable`；Fast_Path 下
  额外把整段用户原话记为一条 `category="unparsed"` 的 requirement，避免 provider
  失败造成不可观测的静默丢失。

**默认值不在此填**（D10 由 PlanBuilder 按 IntentSpec 填），故本框架只会产出
present/user_absent/ambiguous/invalid/unavailable，绝不产出 defaulted。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Any

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.base import LLMProvider, Message
from paper_agent.providers.llm.structured import (
    CallStatus,
    StructuredCallResult,
    structured_call,
)
from paper_agent.tasking.contracts import (
    AtomicRequirement,
    ExtractionResult,
    FieldStatus,
    RawOperation,
    SlotSource,
    SlotValue,
)
from paper_agent.tasking.intent_spec import IntentSpec, SlotSpec
from paper_agent.tasking.operation_analyzer import new_requirement_id
from paper_agent.tasking.spans import locate_requirement


# --- 结构化调用收敛点：已迁至 providers/llm/structured.py（A2a）---------------
# `CallStatus` / `StructuredCallResult` / `structured_call` 从中立模块导入并在下方
# __all__ re-export，既有 `from ...extractors.base import structured_call` 零改动。


# --- 抽取器基类 ---------------------------------------------------------------

_VALUED_STATUSES = (FieldStatus.PRESENT, FieldStatus.AMBIGUOUS, FieldStatus.INVALID)


def _unwrap_slot_payload(raw) -> dict:
    """把 payload 里的一项归一成「信封」形态 `{value, status, ...}`。

    模型有两种写法，都要容忍：

    ==========  ==================================================
    信封形态     ``{"value": X, "status": "present"}``
    裸值形态     ``X``
    ==========  ==================================================

    **历史缺陷**：判据原本是 `isinstance(raw, dict)` —— 裸值恰好是 dict 时
    （object 型槽位，如 `citation_quota` 的 ``{"filters": [...], "constraints": [...]}``）
    会被误认成信封，于是 `raw.get("value")` 得到 `None` → 静默判成 `user_absent`。
    而 `build_tool_schema` 生成的是**扁平** schema，模型必然发裸值形态，
    **object 型槽位因此从未被读到过**：路由修好、文本送到、模型正确填了配额，
    值仍会在这里被丢掉。

    正确判据是**看有没有信封的标志键**，而不是看类型。当前所有 object 型槽位的
    顶层属性都不含 `value` / `status`（`RAW_QUOTA_JSON_SCHEMA` 是
    `filters` / `constraints` / `explicit_scope`），故无歧义。
    """
    if not isinstance(raw, dict):
        return {"value": raw}
    if "value" in raw or "status" in raw:
        return raw
    return {"value": raw}


def _is_empty_shell(value) -> bool:
    """各层皆空的容器。递归。

    算「空」的：`None` / `[]` / `{}` / `""`（含纯空白）/ 各层皆空的嵌套组合。
    空字符串必须算空 —— 否则 ``{"filters": [], "constraints": [], "explicit_scope": ""}``
    这种「模型把可选字段填成空串」的写法会逃过判定。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    if isinstance(value, dict):
        return all(_is_empty_shell(v) for v in value.values())
    return False


def _is_empty_object(slot_spec: SlotSpec, value) -> bool:
    """仅对 **object 型**槽位判定空壳。

    **刻意不覆盖 array 型槽位**：`constraints` / `presets` / `subsections` 的空列表
    在现状下记 `present`（`source=user_explicit`），PlanBuilder 保留 `[]`；
    若改判 `user_absent`，最终值仍是 `[]`（由 `default_factory` 填）但
    `source` 会变成 `defaulted` —— 这是**可观测的行为变化**，会破坏保持性比对，
    且与本次缺陷无关。收窄到 object 型即可覆盖 `citation_quota`
    （当前唯一的 object 槽），且判据是**类型**而非槽位名，故不是单槽位特判。
    """
    return getattr(slot_spec, "value_type", "string") == "object" and _is_empty_shell(value)


class PreExtractor(ABC):
    """按 IntentSpec 抽槽的抽取器基类。

    具体意图抽取器（Task 10-12）只需实现 `intent` 与 `_spec()`——消息与 schema 都由
    框架据 IntentSpec 自动生成。需要特殊值处理（如 format_docx 的 `list[FormatOp]`）
    可覆写 `_coerce_slot()`；跨槽位推导可覆写 `_post_process()`。框架统一处理调用、
    失败态、schema 校验、requirement 消费与 Fast_Path 兜底。
    """

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm
        # 本次抽取的 canonical requirements（供程序推断消费关系）
        self._canonical: tuple[AtomicRequirement, ...] = ()

    # -- 子类实现 --

    @property
    @abstractmethod
    def intent(self) -> Intent:
        ...

    @abstractmethod
    def _spec(self) -> IntentSpec:
        ...

    def _tool_schema(self) -> dict | None:
        """function schema：由 IntentSpec 自动生成（字段级 description = 槽位规矩）。"""
        return build_tool_schema(self._spec(), self.intent.value)

    # -- 框架主流程 --

    def pre_extract(
        self,
        operation: RawOperation,
        canonical_requirements: tuple[AtomicRequirement, ...] | None = None,
    ) -> ExtractionResult:
        """抽取一个 Operation 的槽位。

        `canonical_requirements`：Analyzer 路径传入既有 canonical requirements；
        Fast_Path 传 None（由本抽取器负责发现 requirement）。
        """
        self._canonical = canonical_requirements or ()
        fast_path = canonical_requirements is None
        messages = self._build_tool_messages(operation, canonical_requirements)
        call = structured_call(self._llm, messages, tool_schema=self._tool_schema())
        if call.call_status is not CallStatus.SUCCESS:
            return self._failure_result(operation, call, fast_path)

        try:
            slots, consumed, discovered = self._parse_payload(
                call.payload or {}, operation, fast_path
            )
        except (KeyError, TypeError, ValueError) as exc:
            return self._failure_result(
                operation,
                StructuredCallResult(CallStatus.SCHEMA_FAILED, None, type(exc).__name__),
                fast_path,
            )

        slots = self._post_process(slots)

        return ExtractionResult(
            call_status=CallStatus.SUCCESS,
            slots=slots,
            consumed_requirement_ids=tuple(consumed),
            newly_discovered_requirements=tuple(discovered),
        )

    def _build_tool_messages(
        self,
        operation: RawOperation,
        canonical_requirements: tuple[AtomicRequirement, ...] | None,
    ) -> list[Message]:
        """tools 模式的精简消息：格式与字段规矩已在 schema 里，system 只讲任务与纪律。"""
        parts = [
            f"调用工具 extract_{self.intent.value} 返回抽取结果，不要输出正文。",
            "只抽用户确实表达过的内容；没提到的字段一律省略，不要臆造、不要填默认值。",
        ]
        if canonical_requirements is None:
            parts.append(
                "另外把这段话里每条可执行要求的原话填入 newly_discovered_requirements。"
            )
        else:
            lines = "\n".join(
                f"  - [{r.requirement_id}] {r.verbatim}" for r in canonical_requirements
            )
            if lines:
                parts.append(
                    "该操作已识别出以下要求，**请据此一并填充对应槽位**"
                    "（它们与上面的原话同等重要，不要漏抽；不要新造要求）：\n" + lines
                )
        return [
            Message(role="system", content="\n".join(parts)),
            Message(
                role="user",
                content=build_extractor_user_text(operation, canonical_requirements),
            ),
        ]

    def _post_process(self, slots: dict[str, SlotValue]) -> dict[str, SlotValue]:
        """成功抽取后的跨槽位收尾（默认无操作）。

        子类可覆写做跨字段推导（如 rewrite 由 search_query 推 want_search）。
        仍不填 IntentSpec 默认值——那是 PlanBuilder 的职责（D10）。
        """
        return slots

    # -- 解析与兜底 --

    def _parse_payload(
        self,
        payload: dict,
        operation: RawOperation,
        fast_path: bool,
    ) -> tuple[dict[str, SlotValue], list[str], list[AtomicRequirement]]:
        if "slots" in payload:
            raw_slots = payload.get("slots", {})
        else:
            # 容忍**扁平**形态（function calling 常见）：槽位直接在顶层。
            # `_coerce_slot` 已能把裸值包成 {"value": ...}，故两种形态统一处理。
            raw_slots = {
                k: v for k, v in payload.items()
                if k != "newly_discovered_requirements"
            }
        if not isinstance(raw_slots, dict):
            raise TypeError("slots must be a dict")

        spec = self._spec()
        slots: dict[str, SlotValue] = {}
        consumed: list[str] = []
        for name, slot_spec in spec.slots.items():
            raw = raw_slots.get(name)
            if raw is None:
                slots[name] = SlotValue(field_status=FieldStatus.USER_ABSENT)
                continue
            sv = self._coerce_slot(name, slot_spec, raw)
            slots[name] = sv
            consumed.extend(sv.requirement_ids)

        # requirement↔slot 消费关系：**由程序推断**（同「offset 不让 LLM 算」的哲学）。
        # function calling 的扁平 schema 不带 per-slot requirement_ids，若靠模型自报既
        # 增负担又易漏；这里用确定性文本匹配补全，模型已显式给出的引用优先保留。
        if self._canonical:
            slots, inferred = _infer_consumption(slots, self._canonical)
            consumed.extend(inferred)

        discovered: list[AtomicRequirement] = []
        if fast_path:
            for r in payload.get("newly_discovered_requirements", []) or []:
                req = AtomicRequirement(
                    requirement_id=new_requirement_id(),
                    verbatim=str(r["verbatim"]),
                    category=str(r.get("category", "")),
                )
                discovered.append(locate_requirement(operation.verbatim, req))

        return slots, consumed, discovered

    def _coerce_slot(self, name: str, slot_spec: SlotSpec, raw: Any) -> SlotValue:
        """把一条 payload slot 组装为 SlotValue：定状态、跑 validator/normalizer。

        抽取器**不填默认值**（D10），故只会产出 present/user_absent/ambiguous/
        invalid，绝不 defaulted。
        """
        raw = _unwrap_slot_payload(raw)

        status_str = str(raw.get("status", "present"))
        try:
            status = FieldStatus(status_str)
        except ValueError:
            status = FieldStatus.AMBIGUOUS
        # 抽取器不允许自称 defaulted/user_confirmed（那是 PlanBuilder/Clarifier 的事）
        if status not in (
            FieldStatus.PRESENT, FieldStatus.USER_ABSENT,
            FieldStatus.AMBIGUOUS, FieldStatus.INVALID,
        ):
            status = FieldStatus.AMBIGUOUS

        value = raw.get("value")
        if status is FieldStatus.PRESENT and value is None:
            status = FieldStatus.USER_ABSENT
        elif status is FieldStatus.PRESENT and _is_empty_object(slot_spec, value):
            # 空壳对象（如 {"filters": [], "constraints": []}）语义上等于「没有配额」。
            # 若记 present，下游「该要求是否被承载」的判定会对空壳返回 true ——
            # 验证器报「已修复」而实际没有配额。**验证器不能被哄骗。**
            #
            # 能到这一行说明模型**发出了这个键**（`_parse_payload` 里 key 缺失时
            # 直接记 USER_ABSENT、不进本函数），即「模型试过但产出空壳」：
            #   - 该槽声明了 clarify_if_user_invalid → INVALID，走澄清
            #   - 否则 → USER_ABSENT，由 PlanBuilder 填默认
            status = (
                FieldStatus.INVALID if slot_spec.clarify_if_user_invalid
                else FieldStatus.USER_ABSENT
            )

        source: SlotSource | None = None
        if status is FieldStatus.PRESENT:
            # 1) 先规范化（如 "word"→"docx"）：成功且有变化标 normalized，异常降 invalid
            if slot_spec.normalizer is not None:
                normalized, ok = self._safe_map(slot_spec.normalizer, value)
                if not ok:
                    status = FieldStatus.INVALID
                else:
                    if normalized != value:
                        value = normalized
                        source = SlotSource.NORMALIZED
                    else:
                        source = SlotSource.USER_EXPLICIT
            else:
                source = SlotSource.USER_EXPLICIT
            # 2) 再校验规范化后的值：非法降 invalid（保留原值供澄清参考）
            if status is FieldStatus.PRESENT and slot_spec.validator is not None:
                if not self._safe(slot_spec.validator, value):
                    status = FieldStatus.INVALID

        if status is not FieldStatus.PRESENT:
            source = None  # 非 present 的槽位不带来源

        req_ids = tuple(str(x) for x in (raw.get("requirement_ids") or ()))
        keep_value = value if status in _VALUED_STATUSES else None
        return SlotValue(
            value=keep_value,
            field_status=status,
            source=source,
            requirement_ids=req_ids,
            verbatim=raw.get("verbatim"),
        )

    def _failure_result(
        self,
        operation: RawOperation,
        call: StructuredCallResult,
        fast_path: bool,
    ) -> ExtractionResult:
        """抽取调用失败：所有槽位 unavailable；Fast_Path 额外产 unparsed 兜底。"""
        spec = self._spec()
        slots = {
            name: SlotValue(field_status=FieldStatus.UNAVAILABLE)
            for name in spec.slots
        }
        discovered: tuple[AtomicRequirement, ...] = ()
        if fast_path:
            fallback = AtomicRequirement(
                requirement_id=new_requirement_id(),
                verbatim=operation.verbatim,
                category="unparsed",
            )
            discovered = (locate_requirement(operation.verbatim, fallback),)
        return ExtractionResult(
            call_status=call.call_status,
            slots=slots,
            newly_discovered_requirements=discovered,
            error_category=call.error_category,
        )

    # -- 小工具 --

    @staticmethod
    def _safe(fn, value) -> bool:
        try:
            return bool(fn(value))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _safe_map(fn, value) -> tuple[Any, bool]:
        try:
            return fn(value), True
        except Exception:  # noqa: BLE001
            return value, False


# --- 抽取器看到的「用户说的话」------------------------------------------------

#: 句末标点：已有则不再补「。」，避免拼出「…部分。。要求：」这种。
_SENTENCE_END = ("。", "，", "；", "：", "!", "?", ".", ",", ";", ":", "！", "？")


def build_extractor_user_text(
    operation: RawOperation,
    canonical_requirements: tuple[AtomicRequirement, ...] | None,
) -> str:
    """组装抽取器的 user 消息内容 = 操作原话 + 归属于它的要求原话。

    **这里曾是真机缺陷的所在（2026-08-01）。** 此前只有 `operation.verbatim`：

    ==============  ==========================================================
    用户原话         …你能帮我完善我的引言部分吗，要求参考文献10，英文8，中文2
    analyzer 切片    完善我的引言部分
    抽取器看到       完善我的引言部分          ← 一个数字都没有
    ==============  ==========================================================

    于是模型按 `citation_quota` 槽位说明「仅当用户明确提出篇数/语言配额时给出；
    没提就不要给」**正确地**省略了该槽 —— 三条配额落 unmapped，计划里没有配额，
    执行会产出一篇零引用的引言。槽位、绑定、schema 全都在位，缺的只是输入。

    **为什么放 user 消息而不是继续留在 system**：要求列表此前**已经**在 system
    里（见 `_build_tool_messages` 的「该操作已识别出以下要求」），模型仍然没填
    —— 因为 user 消息与槽位 description 都在说「用户没提配额」，两个信号冲突时
    模型采信了后者。同一信息放在不同位置权重不同：**用户说了什么，就应当出现在
    user 消息里。**

    去重：要求原话已包含在 `verbatim` 内时不重复追加（analyzer 若把修饰语一起
    摘进了切片，这是正常且更好的情况，不该因此喂两遍）。
    """
    text = (operation.verbatim or "").strip()
    if not canonical_requirements:
        return text

    extras: list[str] = []
    seen: set[str] = set()
    for r in canonical_requirements:
        v = (r.verbatim or "").strip()
        if not v or v in text or v in seen:
            continue
        seen.add(v)
        extras.append(v)
    if not extras:
        return text
    joined = "要求：" + "；".join(extras)
    if not text:
        return joined
    sep = "" if text.endswith(_SENTENCE_END) else "。"
    return f"{text}{sep}{joined}"


# --- function calling schema（由 IntentSpec 自动生成）-------------------------

_FORMAT_OP_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "操作类型（见字段说明的白名单）"},
        "value": {"description": "该操作的取值（字号用数字、字体用字符串等）"},
        "scope": {"type": "string", "description": "作用范围：all/body/headings/heading1"},
    },
    "required": ["name"],
}


def _slot_property(spec_slot) -> dict:
    """把一个 SlotSpec 映射为 JSON schema 属性（类型 + 字段级规矩 description）。"""
    # 显式 override（嵌套结构，如配额）优先，原样使用（A2b D10）。
    override = getattr(spec_slot, "json_schema_override", None)
    if override is not None:
        prop = dict(override)
        desc = getattr(spec_slot, "description", "") or ""
        if desc and "description" not in prop:
            prop["description"] = desc
        return prop
    vt = getattr(spec_slot, "value_type", "string") or "string"
    prop: dict
    if vt == "array":
        if spec_slot.name == "operations":
            prop = {"type": "array", "items": _FORMAT_OP_SCHEMA}
        else:
            prop = {"type": "array", "items": {"type": "string"}}
    else:
        prop = {"type": vt}
    desc = getattr(spec_slot, "description", "") or ""
    if desc:
        prop["description"] = desc
    return prop


def build_tool_schema(spec: IntentSpec, label: str) -> dict:
    """据 IntentSpec 生成 OpenAI 风格 function schema。

    设计要点：
    - **扁平**结构（槽名直接是参数），对模型最友好；解析侧 `_parse_payload` 已容忍。
    - 每个字段的 `description` 直接取自 `SlotSpec.description`——「规矩」挂在字段上，
      而非堆在 system prompt 里。
    - **`required` 恒为空**（P0）：JSON schema 的 required 与业务 required 是两个层。
      业务必填的含义是「缺了要问用户」（由 PlanBuilder 记 missing_fields、Clarifier
      追问），而 schema 的 required 会**禁止模型省略**，逼它为没说的字段编造取值 —— 
      那样缺口永远不会暴露给 Clarifier，整套澄清机制被架空。故这里一律不设。
    """
    props = {name: _slot_property(s) for name, s in spec.slots.items()}
    props["newly_discovered_requirements"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": "（仅在未提供已识别要求时使用）这段话里各条可执行要求的原话。",
    }
    return {
        "type": "function",
        "function": {
            "name": f"extract_{spec.intent.value}",
            "description": (
                f"从用户这段话里抽取「{label}」操作的槽位。"
                "只抽用户**确实表达过**的内容；没提到的字段一律省略、不要臆造，"
                "也不要填系统默认值。"
            ),
            "parameters": {
                "type": "object",
                "properties": props,
                # 恒为空：业务必填由 PlanBuilder/Clarifier 处理，不在协议层强迫模型编造
                "required": [],
            },
        },
    }


# --- requirement 消费关系推断（程序侧，确定性）---------------------------------

def _norm_text(s: object) -> str:
    return re.sub(r"\s+", "", str(s)).lower()


def _collect_verbatims(v: object, out: list[str]) -> None:
    """递归收集嵌套结构里所有 ``verbatim`` 字段（结构化槽位的原话来源）。

    嵌套槽位（如 `citation_quota`）的 schema 已要求每个 clause 自带 `verbatim`
    （见 `intent_spec._RAW_COUNT_CLAUSE_SCHEMA`），模型也确实在填——**此前无人读**。
    """
    if isinstance(v, dict):
        for key, val in v.items():
            if key == "verbatim" and isinstance(val, str) and val.strip():
                out.append(val)
            else:
                _collect_verbatims(val, out)
    elif isinstance(v, (list, tuple)):
        for item in v:
            _collect_verbatims(item, out)


def _slot_texts(sv: SlotValue) -> list[str]:
    """一个槽位可用于认领 requirement 的**文本片段列表**（各自独立匹配）。

    分片而非拼成一坨,是 X-11 的修复要点：结构化槽位（dict/list）序列化后,
    用户原话里相邻的两句会被 dict 语法切开（`'英文8篇'},{'dimension':...`）,
    子串匹配要求连续 ⇒ **任何嵌套槽位恒定 100% 漏报**。
    改为逐 clause 的 `verbatim` 独立匹配后,「英文 8 篇」「中文 2 篇」各自可认领。
    """
    parts: list[str] = []
    if sv.verbatim:
        parts.append(str(sv.verbatim))
    v = sv.value
    if isinstance(v, str):
        parts.append(v)
    elif isinstance(v, dict):
        _collect_verbatims(v, parts)          # 结构化槽位：只认 verbatim，不吞 dict 语法
    elif isinstance(v, (list, tuple)):
        for item in v:
            if isinstance(item, str):
                parts.append(item)
            else:
                _collect_verbatims(item, parts)
    elif v is not None:
        parts.append(str(v))
    return [t for t in (_norm_text(p) for p in parts) if t]


def _slot_text(sv: SlotValue) -> str:
    """一个槽位承载的全部文本（值 + verbatim）。保留给既有调用方/测试。"""
    return "".join(_slot_texts(sv))


def _infer_consumption(
    slots: dict[str, SlotValue],
    canonical: tuple[AtomicRequirement, ...],
) -> tuple[dict[str, SlotValue], list[str]]:
    """据文本匹配推断哪些 requirement 被哪个槽位消费了。

    只对**已有取值**的槽位推断；模型已显式给出 `requirement_ids` 的槽位保持原样
    （显式优先）。匹配用归一化双向子串包含，与 golden 打分器同一口径。

    每个槽位贡献**多个**候选文本（`_slot_texts`）：结构化槽位按 clause 的 verbatim
    分片。任一片匹配即算该槽消费，修 X-11「嵌套槽位恒漏报」。

    返回 `(新的 slots, 被消费的 requirement id 列表)`。
    """
    texts = {
        name: _slot_texts(sv)
        for name, sv in slots.items()
        if sv.field_status in _VALUED_STATUSES and sv.value is not None
    }
    texts = {k: v for k, v in texts.items() if v}
    if not texts:
        return slots, []

    additions: dict[str, list[str]] = {}
    consumed: list[str] = []
    for req in canonical:
        needle = _norm_text(req.verbatim)
        if not needle:
            continue
        # 已被模型显式引用 → 无需推断
        if any(req.requirement_id in sv.requirement_ids for sv in slots.values()):
            consumed.append(req.requirement_id)
            continue
        best: str | None = None
        best_len: int | None = None
        for name, fragments in texts.items():
            for text in fragments:
                if needle in text or text in needle:
                    # 取匹配片段最短者（最贴切的那个槽位）
                    if best_len is None or len(text) < best_len:
                        best, best_len = name, len(text)
                    break
        if best is not None:
            additions.setdefault(best, []).append(req.requirement_id)
            consumed.append(req.requirement_id)

    if not additions:
        return slots, consumed

    new_slots = dict(slots)
    for name, ids in additions.items():
        sv = new_slots[name]
        merged = tuple(dict.fromkeys(sv.requirement_ids + tuple(ids)))
        new_slots[name] = replace(sv, requirement_ids=merged)
    return new_slots, consumed


# --- 跨槽位派生助手 ------------------------------------------------------------

def derive_want_search(slots: dict[str, SlotValue]) -> dict[str, SlotValue]:
    """search_query 非空而用户未明确 want_search 时，推导 want_search=True。

    这是对「用户表达出的检索意图」的规范化（source=normalized），不是填静态默认
    （静态默认 False 由 PlanBuilder 填）。仅当 want_search 处于 user_absent 时推导，
    不覆盖用户明确给出的取值。
    """
    query = slots.get("search_query")
    want = slots.get("want_search")
    if query is None or want is None:
        return slots
    has_query = (
        query.field_status is FieldStatus.PRESENT
        and isinstance(query.value, str)
        and query.value.strip() != ""
    )
    if has_query and want.field_status is FieldStatus.USER_ABSENT:
        new = dict(slots)
        new["want_search"] = SlotValue(
            value=True,
            field_status=FieldStatus.PRESENT,
            source=SlotSource.NORMALIZED,
            requirement_ids=query.requirement_ids,
        )
        return new
    return slots


__all__ = [
    "PreExtractor",
    "StructuredCallResult",
    "structured_call",
    "build_tool_schema",
    "build_extractor_user_text",
    "derive_want_search",
]
