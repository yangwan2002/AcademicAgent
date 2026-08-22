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
from dataclasses import fields as dc_fields
from dataclasses import is_dataclass, replace
from typing import Any

from paper_agent.intents.intent import Intent
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


#: 扁平 payload 里**不是槽位**的顶层键（元信息信封）。
#:
#: 今天漏登记不会出错 —— 建槽的循环是 spec 驱动的（遍历 `spec.slots` 后
#: `raw_slots.get(name)`），多余的键根本不会被读到。集中登记是为可读性与将来：
#: 一旦有人改成遍历 payload 键（比如加「未知槽位」告警），漏登记就会立刻变成 bug。
_ENVELOPE_KEYS = frozenset({
    "newly_discovered_requirements",
    "slot_verbatims",
    "slot_consumed_requirements",
})


def _merge_ids(envelope: Any, declared: tuple[str, ...]) -> tuple[str, ...]:
    """合并信封内 `requirement_ids` 与旁路 `slot_consumed_requirements` 的声明。

    两者都是模型的显式声明，取并集（保序去重）。前者是嵌套形态下模型可直接挂在
    槽位上的字段，后者是扁平 schema 的旁路通道 —— 同一意图的两种表达，不该二选一。
    """
    out: dict[str, None] = {}
    for x in envelope or ():
        rid = str(x).strip()
        if rid:
            out.setdefault(rid, None)
    for rid in declared:
        out.setdefault(rid, None)
    return tuple(out)


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

    def _declared_ids(self, raw: Any) -> tuple[str, ...]:
        """校验模型自报的 requirement id：只保留**本次 canonical 集合里存在**的。

        模型自造/记错的 id 一律丢弃而非报错 —— 消费关系是记账信息，一个坏 id 不该
        让整次抽取失败（槽值本身可能完全正确）。Fast_Path 下 `_canonical` 为空，
        此时无从校验，返回空元组（该路径的 requirement 由抽取器自己发现）。
        """
        if not isinstance(raw, (list, tuple)) or not self._canonical:
            return ()
        known = {r.requirement_id for r in self._canonical}
        seen: dict[str, None] = {}
        for x in raw:
            rid = str(x).strip()
            if rid in known:
                seen.setdefault(rid, None)
        return tuple(seen)

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
        # 每槽原话的旁路映射（见 `build_tool_schema` 的 `slot_verbatims`）：它是
        # **元信息**而非槽位，必须在建槽前摘掉，否则会被当成一个未知槽。
        raw_verbatims = payload.get("slot_verbatims")
        if not isinstance(raw_verbatims, dict):
            raw_verbatims = {}
        # 模型自报的消费关系（见 `build_tool_schema` 的 `slot_consumed_requirements`）。
        # 同 `slot_verbatims`，是**元信息**而非槽位，必须在建槽前摘掉。
        raw_consumed = payload.get("slot_consumed_requirements")
        if not isinstance(raw_consumed, dict):
            raw_consumed = {}

        if "slots" in payload:
            raw_slots = payload.get("slots", {})
        else:
            # 容忍**扁平**形态（function calling 常见）：槽位直接在顶层。
            # `_coerce_slot` 已能把裸值包成 {"value": ...}，故两种形态统一处理。
            raw_slots = {
                k: v for k, v in payload.items()
                if k not in _ENVELOPE_KEYS
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
            sv = self._coerce_slot(
                name, slot_spec, raw, raw_verbatims.get(name),
                declared_ids=self._declared_ids(raw_consumed.get(name)),
            )
            slots[name] = sv
            consumed.extend(sv.requirement_ids)

        # requirement↔slot 消费关系：**模型声明优先，文本匹配兜底**。
        #
        # 权威来源是模型自报的 `slot_consumed_requirements`（理由见
        # `build_tool_schema`：消费关系是语义事实，不在原文里，程序无从反推）。
        # `_infer_consumption` 退为兜底，只处理**没有任何声明**的槽位 —— 模型漏报
        # 一两个槽时不至于直接倒退，且 `constraints` 这类 `list[str]` 槽的槽值本身
        # 就是原话（实测 `['不要改数字', '保留引用']`），字面匹配在它们身上仍有效。
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

    def _coerce_slot(
        self,
        name: str,
        slot_spec: SlotSpec,
        raw: Any,
        reported_verbatim: str | None = None,
        declared_ids: tuple[str, ...] = (),
    ) -> SlotValue:
        """把一条 payload slot 组装为 SlotValue：定状态、跑 validator/normalizer。

        抽取器**不填默认值**（D10），故只会产出 present/user_absent/ambiguous/
        invalid，绝不 defaulted。

        `reported_verbatim`：模型在 `slot_verbatims` 里自报的该槽用户原话（可空）。
        优先于「规范化前原值」这个兜底 —— 它能覆盖模型**重述**的情形，而兜底只能
        覆盖 normalizer 改写的情形。

        `declared_ids`：模型在 `slot_consumed_requirements` 里自报的该槽消费的
        requirement id（已由 `_declared_ids` 校验过存在性）。与信封内的
        `requirement_ids` 合并 —— 两者都是模型的显式声明，取并集不丢信息。
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
        # 规范化会**销毁原像**：`"Word"→"docx"` 之后，用户原话在这个槽里就不存在了。
        # 而 `_infer_consumption` 要靠原话做字面匹配来认领 requirement ⇒ 归一化过的槽
        # 恒定漏报（`转成word` 匹配不上 `docx`），requirement 落 UNMAPPED，弹窗说
        # 「这条要求没能安排进任何操作」——而那个操作就在计划里。
        # 故覆盖前把原值留作 verbatim 兜底（模型自报的 verbatim 优先，不覆盖它）。
        pre_normalized: str | None = None
        if status is FieldStatus.PRESENT:
            # 1) 先规范化（如 "word"→"docx"）：成功且有变化标 normalized，异常降 invalid
            if slot_spec.normalizer is not None:
                normalized, ok = self._safe_map(slot_spec.normalizer, value)
                if not ok:
                    status = FieldStatus.INVALID
                else:
                    if normalized != value:
                        if isinstance(value, str) and value.strip():
                            pre_normalized = value
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

        req_ids = _merge_ids(raw.get("requirement_ids"), declared_ids)
        keep_value = value if status in _VALUED_STATUSES else None
        # 优先级：信封自带 > 模型旁路自报 > 规范化前原值（兜底）
        verbatim = raw.get("verbatim") or reported_verbatim or pre_normalized
        if isinstance(verbatim, str) and not verbatim.strip():
            verbatim = None
        return SlotValue(
            value=keep_value,
            field_status=status,
            source=source,
            requirement_ids=req_ids,
            verbatim=verbatim,
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
        # per-op 原话：与 `_RAW_COUNT_CLAUSE_SCHEMA.verbatim` 同一做法。`operations`
        # 在结构上逐元素对应用户的每条排版要求（「双栏」「正文小四」「标题四号」），
        # 却此前没有逐元素锚点 ⇒ 兜底匹配对它恒定失效。
        "verbatim": {"type": "string",
                     "description": "这一条操作对应的用户原话片段（照抄，不要改写）。"},
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
    # 每槽的**用户原话**。schema 是扁平的（槽名直接是参数、模型发裸值），没法给
    # 每个槽挂同级 `verbatim`，故收在一个映射里。
    #
    # 为什么需要它：槽值经规范化（`"Word"→"docx"`）或模型重述（「读起来有点别扭」
    # → `消除别扭感，使语句通顺自然`）之后，字面锚点就断了，而
    # `_infer_consumption` 靠字面匹配认领 requirement ⇒ 恒定漏报、误弹 unmapped。
    # 嵌套 clause 的 schema 早已要求 `verbatim`（`_RAW_COUNT_CLAUSE_SCHEMA`）且模型
    # 填得准，配额槽的归属因此从不丢；这里只是把同一做法补给顶层槽。
    props["slot_verbatims"] = {
        "type": "object",
        "description": (
            "每个已填槽位对应的**用户原话片段**，键为槽位名。"
            "照抄用户的说法，不要改写、不要规范化（用户说「转成 Word」就写"
            "「转成 Word」，即使 target_ext 填的是 docx）。没填的槽位不必出现。"
        ),
        "additionalProperties": {"type": "string"},
    }
    # 消费关系的**权威来源**：哪个槽位吃掉了哪几条要求。
    #
    # 为什么由模型报而不是程序推：这是**语义事实**，只在模型做转换的那一刻产生，
    # 原文里并不存在答案。「双栏格式」→ `FormatOp(set_columns, 2)` 之后，原话与
    # 槽值再无字面交集（实测 `_slot_texts` 对 dataclass 列表返回 `[]`），靠字面
    # 反推等于拿有损变换的输出去反推输入。此前三层锚点补丁（`pre_normalized` /
    # `slot_verbatims` / per-clause `verbatim`）都只是提高猜中概率，没有一层触及
    # 「反推」本身 —— `format_docx.operations` 上误报率恒定 100%。
    #
    # 与「offset 不让 LLM 算」的区别（那条哲学在这里**不适用**）：offset 的答案
    # 客观存在于原文，程序能精确定位、可验证；消费关系不在原文里。两件事性质不同。
    #
    # 成本近零：requirement id 已在 system 消息里逐条列给模型（见
    # `_build_tool_messages` 的「该操作已识别出以下要求」），这里只是开一条回信通道。
    # 报 id 也比 `slot_verbatims` 报字符串更可靠 —— id 是闭集（选错概率低），
    # 字符串是自由文本（抄错一字即失配）。
    props["slot_consumed_requirements"] = {
        "type": "object",
        "description": (
            "每个已填槽位**消费了哪些要求**，键为槽位名、值为要求编号数组"
            "（如 {\"operations\": [\"r1\", \"r2\"]}）。编号只能取上面「已识别出"
            "以下要求」里给出的那些，不要自造。一个槽位可消费多条要求；"
            "一条要求也可被多个槽位消费。没填的槽位不必出现。"
        ),
        "additionalProperties": {"type": "array", "items": {"type": "string"}},
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

    **dataclass 分支是 2026-08-06 补的**：此前只处理 dict 与 list/tuple，遇到
    dataclass（`format_docx.operations` 的 `list[FormatOp]`）两个 isinstance 都不
    匹配，函数体走完直接返回 —— 没有 else、没有告警、没有异常。静默产出空列表，
    下游 `_infer_consumption` 的 `if not texts: return` 同样静默 ⇒ 该意图上
    unmapped 误报率恒定 100%，而 1604 个测试全绿。**失败得完全无声是这个 bug 能
    活下来的唯一原因。**
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
    elif is_dataclass(v) and not isinstance(v, type):
        for f in dc_fields(v):
            val = getattr(v, f.name, None)
            if f.name == "verbatim" and isinstance(val, str) and val.strip():
                out.append(val)
            else:
                _collect_verbatims(val, out)


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
    """**兜底**：对模型未声明消费方的 requirement，据文本匹配推断归属。

    权威来源是模型自报的 `slot_consumed_requirements`（见 `build_tool_schema`）。
    本函数只处理**漏网的那些** —— 跳过粒度是 **per-requirement 而非 per-slot**：
    某条 requirement 只要被任一槽位声明过就跳过，但一个已有声明的槽位仍可通过
    匹配再认领别的 requirement。模型漏报一两条时不至于整体倒退。

    为什么保留而不删：`constraints`/`subsections`/`presets` 这类 `list[str]` 槽的
    槽值本身就是原话（实测 `['不要改数字', '保留引用']`），字面匹配对它们有效。
    但对转换彻底的槽（`operations` 的 `list[FormatOp]`，实测 `_slot_texts` 返回
    `[]`）本函数**无能为力**，那正是它必须退居兜底的原因。

    匹配用归一化双向子串包含，与 golden 打分器同一口径。每个槽位贡献**多个**候选
    文本（`_slot_texts`）：结构化槽位按 clause 的 verbatim 分片，任一片匹配即算该
    槽消费（修 X-11「嵌套槽位恒漏报」）。

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
