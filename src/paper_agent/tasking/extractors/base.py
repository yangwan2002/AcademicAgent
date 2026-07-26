"""PreExtractor 公共框架（Task 9）。

职责（design Component§4 / D3 / D4）：
- **调用态/字段态分离**：一次抽取调用的整体成败用 `ExtractionResult.call_status`
  （success/provider_failed/parse_failed/schema_failed）表达；单个槽位的状态用
  `SlotValue.field_status`（present/user_absent/ambiguous/invalid/unavailable）。
  两者永不折叠——「用户没说(user_absent)」≠「因抽取失败暂缺(unavailable)」。
- **结构化输出可插拔收敛点**：`structured_call()` 是唯一与 LLM 交互并拿回 JSON
  payload 的地方，支持 `mode="prompt"`（约束模型只输出 JSON）与 `mode="tools"`
  （function calling，读 tool_calls[0].arguments）。两协议均支持 tools；真机验证
  推迟，默认走 prompt。换实现只需改这一处。
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
from abc import ABC, abstractmethod
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


def structured_call(
    llm: LLMProvider,
    messages: list[Message],
    *,
    mode: str = "prompt",
    tool_schema: dict | None = None,
) -> StructuredCallResult:
    """调 LLM 拿结构化 payload；四态失败分类，绝不塌成空。

    - `mode="prompt"`：约束模型只输出 JSON，从正文里抽第一段 JSON 对象。
    - `mode="tools"`：function calling，读 `tool_calls[0].arguments`（须传
      `tool_schema`）；无 tool_calls 视为 schema_failed。

    这是「prompt / tools 可插拔」的唯一收敛点；后续换实现只改此函数。
    """
    # 1) provider 调用
    try:
        if mode == "tools" and tool_schema is not None:
            resp = llm.complete(messages, tools=[tool_schema])
        else:
            resp = llm.complete(messages)
    except Exception as exc:  # noqa: BLE001
        return StructuredCallResult(CallStatus.PROVIDER_FAILED, None, type(exc).__name__)

    # 2) 取 payload
    if mode == "tools" and tool_schema is not None:
        calls = getattr(resp, "tool_calls", None) or []
        if not calls:
            return StructuredCallResult(CallStatus.SCHEMA_FAILED, None, "no_tool_call")
        args = calls[0].arguments
        if not isinstance(args, dict):
            return StructuredCallResult(CallStatus.SCHEMA_FAILED, None, "tool_args_not_dict")
        return StructuredCallResult(CallStatus.SUCCESS, args, None)

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

    具体意图抽取器（Task 10-12）只需实现 `_spec()` 与 `_build_messages()`；
    需要特殊值处理（如 format_docx 的 `list[FormatOp]`、add_section 的结构化
    structure）可覆写 `_coerce_slot()`。框架统一处理调用、失败态、schema 校验、
    requirement 消费与 Fast_Path 兜底。
    """

    def __init__(self, llm: LLMProvider, *, structured_mode: str = "prompt") -> None:
        self._llm = llm
        self._mode = structured_mode

    # -- 子类实现 --

    @property
    @abstractmethod
    def intent(self) -> Intent:
        ...

    @abstractmethod
    def _spec(self) -> IntentSpec:
        ...

    @abstractmethod
    def _build_messages(
        self,
        operation: RawOperation,
        canonical_requirements: tuple[AtomicRequirement, ...] | None,
    ) -> list[Message]:
        ...

    def _tool_schema(self) -> dict | None:
        """tools 模式下的 function schema；默认无（走 prompt）。"""
        return None

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
        fast_path = canonical_requirements is None
        messages = self._build_messages(operation, canonical_requirements)
        call = structured_call(
            self._llm, messages, mode=self._mode, tool_schema=self._tool_schema()
        )
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
        raw_slots = payload.get("slots", {})
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


# --- 提示词构造助手（各意图抽取器复用）--------------------------------------

def build_extraction_messages(
    operation: RawOperation,
    canonical_requirements: tuple[AtomicRequirement, ...] | None,
    *,
    intent_label: str,
    slot_lines: list[str],
) -> list[Message]:
    """构造 prompt 模式的抽取消息：说明输出 JSON 结构 + 槽位清单 + requirement 引用。

    - `intent_label`：给模型的人类可读意图名（如「润色」）。
    - `slot_lines`：每个槽位一行的说明（名称 + 含义 + 取值约束）。
    - Analyzer 路径（canonical 非 None）：列出 canonical requirement 及其 id，要求
      模型在对应槽位的 `requirement_ids` 里引用；**不要新造 requirement**。
    - Fast_Path（canonical 为 None）：要求模型额外输出
      `newly_discovered_requirements`（只给 verbatim + category，**不算字符位置**）。
    """
    fast_path = canonical_requirements is None
    slots_block = "\n".join(f"  - {line}" for line in slot_lines)

    sys_parts = [
        f"你是学术论文写作助手的「{intent_label}」槽位抽取器。"
        "从用户这段话里抽取以下槽位，只输出 JSON，不要解释。",
        "槽位：",
        slots_block,
        "",
        "输出格式：",
        '{"slots":{"<slot>":{"value":<值>,"status":"present|user_absent|ambiguous|invalid",'
        '"verbatim":"对应的用户原话","requirement_ids":["r1"]}}'
        + (',"newly_discovered_requirements":[{"verbatim":"一条要求原话","category":"target|constraint|goal|other"}]'
           if fast_path else "")
        + "}",
        "",
        "规则：",
        "- 用户没提到的槽位：status=user_absent，不要臆造值、不要填默认值。",
        "- 不确定的：status=ambiguous。值明显非法：status=invalid。",
        "- 不要计算字符位置；verbatim 只给用户原话片段。",
    ]

    if fast_path:
        sys_parts.append(
            "- 把这段话里每条可执行要求记入 newly_discovered_requirements，"
            "并在消费它的槽位 requirement_ids 里引用（用你在本次输出里自定的临时序号亦可留空）。"
        )
    else:
        lines = "\n".join(
            f"  - [{r.requirement_id}] {r.verbatim}" for r in (canonical_requirements or ())
        )
        sys_parts.append(
            "- 已识别的要求如下；把每个槽位消费到的要求 id 填进该槽的 requirement_ids，"
            "不要新造要求：\n" + (lines or "  （无）")
        )

    return [
        Message(role="system", content="\n".join(sys_parts)),
        Message(role="user", content=operation.verbatim),
    ]


__all__ = [
    "PreExtractor",
    "StructuredCallResult",
    "structured_call",
    "build_extraction_messages",
    "derive_want_search",
]
