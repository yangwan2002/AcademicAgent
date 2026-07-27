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

import json
import re
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Any

from paper_agent.intents.router import Intent
from paper_agent.providers.llm.base import LLMProvider, Message
from paper_agent.tasking.contracts import (
    AtomicRequirement,
    CallStatus,
    ExtractionResult,
    FieldStatus,
    RawOperation,
    SlotSource,
    SlotValue,
)
from paper_agent.tasking.intent_spec import IntentSpec, SlotSpec
from paper_agent.tasking.operation_analyzer import new_requirement_id
from paper_agent.tasking.spans import locate_requirement


# --- 结构化调用收敛点 ---------------------------------------------------------

class StructuredCallResult:
    """一次结构化调用的结果：调用态 + payload（成功时）+ 错误类别。"""

    __slots__ = ("call_status", "payload", "error_category")

    def __init__(
        self,
        call_status: CallStatus,
        payload: dict | None = None,
        error_category: str | None = None,
    ) -> None:
        self.call_status = call_status
        self.payload = payload
        self.error_category = error_category


def _extract_json_object(text: str) -> dict:
    """从模型文本输出里取第一段 JSON 对象；失败抛 ValueError/JSONDecodeError。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no json object")
    obj = json.loads(s[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("not a json object")
    return obj


def _forced_tool_name(tool_schema: dict) -> str:
    """要强制调用的工具名。**协议形态由各 provider 自行适配**（#8）——
    统一层只传 `forced_tool_name`，不拼 Anthropic/OpenAI 各异的 `tool_choice` 结构。
    """
    fn = tool_schema.get("function", tool_schema) if isinstance(tool_schema, dict) else {}
    return str(fn.get("name", "") or "")


# 只有「参数不被支持」才值得去掉 forced_tool_name 重试一次；
# 超时/限流/网络错误重试会造成重复请求与延迟翻倍（由 Resilient 层负责退避）。
_UNSUPPORTED_HINTS = (
    "forced_tool_name", "tool_choice", "unexpected keyword", "unknown parameter",
    "unsupported parameter", "invalid_request_error", "extra fields not permitted",
)


def _looks_unsupported(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    if isinstance(exc, TypeError):
        return True          # provider 签名不认识该参数
    return any(h in text for h in _UNSUPPORTED_HINTS)


def structured_call(
    llm: LLMProvider,
    messages: list[Message],
    *,
    tool_schema: dict | None = None,
) -> StructuredCallResult:
    """调 LLM 拿结构化 payload；四态失败分类，绝不塌成空。

    统一走 function calling（`tool_schema` 必给）：`tool_choice` 强制调用，读
    `tool_calls[0].arguments`；模型若未走工具而回了正文，则按 JSON 兜底解析。
    未提供 `tool_schema` 时退化为「解析正文 JSON」（仅供无 schema 的调用方）。

    这是全项目结构化输出的唯一收敛点；换实现只改此函数。
    """
    # 1) provider 调用
    use_tools = tool_schema is not None
    try:
        if use_tools:
            # 只传中立的 forced_tool_name；具体 tool_choice 形态由 provider 适配。
            resp = llm.complete(
                messages, tools=[tool_schema],
                forced_tool_name=_forced_tool_name(tool_schema),
            )
        else:
            resp = llm.complete(messages)
    except Exception as exc:  # noqa: BLE001
        # **只**在「该参数不被支持」时降级重试一次；超时/限流等一律直接失败，
        # 交由 Resilient 层退避，避免重复请求与延迟翻倍（#8）。
        if use_tools and _looks_unsupported(exc):
            try:
                resp = llm.complete(messages, tools=[tool_schema])
            except Exception as exc2:  # noqa: BLE001
                return StructuredCallResult(
                    CallStatus.PROVIDER_FAILED, None, type(exc2).__name__
                )
        else:
            return StructuredCallResult(
                CallStatus.PROVIDER_FAILED, None, type(exc).__name__
            )

    # 2) 取 payload
    if use_tools:
        calls = getattr(resp, "tool_calls", None) or []
        if calls:
            args = calls[0].arguments
            if isinstance(args, dict):
                return StructuredCallResult(CallStatus.SUCCESS, args, None)
            return StructuredCallResult(
                CallStatus.SCHEMA_FAILED, None, "tool_args_not_dict"
            )
        # 模型没走工具而是回了正文 → 兜底按 JSON 解析（不塌成失败）；
        # 正文也不是 JSON → parse_failed（确实"解析"失败，非结构不符）。
        try:
            payload = _extract_json_object((resp.content or "").strip())
        except (ValueError, json.JSONDecodeError) as exc:
            return StructuredCallResult(
                CallStatus.PARSE_FAILED, None, type(exc).__name__
            )
        return StructuredCallResult(CallStatus.SUCCESS, payload, None)

    content = (resp.content or "").strip()
    try:
        payload = _extract_json_object(content)
    except (ValueError, json.JSONDecodeError) as exc:
        return StructuredCallResult(CallStatus.PARSE_FAILED, None, type(exc).__name__)
    return StructuredCallResult(CallStatus.SUCCESS, payload, None)


# --- 抽取器基类 ---------------------------------------------------------------

_VALUED_STATUSES = (FieldStatus.PRESENT, FieldStatus.AMBIGUOUS, FieldStatus.INVALID)


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
            Message(role="user", content=operation.verbatim),
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
        if not isinstance(raw, dict):
            raw = {"value": raw}

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


def _slot_text(sv: SlotValue) -> str:
    """一个槽位承载的全部文本（值 + verbatim），用于匹配 requirement 原话。"""
    parts: list[str] = []
    if sv.verbatim:
        parts.append(str(sv.verbatim))
    v = sv.value
    if isinstance(v, str):
        parts.append(v)
    elif isinstance(v, (list, tuple)):
        parts.extend(str(x) for x in v)
    elif v is not None:
        parts.append(str(v))
    return _norm_text(" ".join(parts))


def _infer_consumption(
    slots: dict[str, SlotValue],
    canonical: tuple[AtomicRequirement, ...],
) -> tuple[dict[str, SlotValue], list[str]]:
    """据文本匹配推断哪些 requirement 被哪个槽位消费了。

    只对**已有取值**的槽位推断；模型已显式给出 `requirement_ids` 的槽位保持原样
    （显式优先）。匹配用归一化双向子串包含，与 golden 打分器同一口径。

    返回 `(新的 slots, 被消费的 requirement id 列表)`。
    """
    texts = {
        name: _slot_text(sv)
        for name, sv in slots.items()
        if sv.field_status in _VALUED_STATUSES and sv.value is not None
    }
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
        for name, text in texts.items():
            if not text:
                continue
            if needle in text or text in needle:
                # 取匹配文本最短者（最贴切的那个槽位）
                if best is None or len(text) < len(texts[best]):
                    best = name
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
    "derive_want_search",
]
