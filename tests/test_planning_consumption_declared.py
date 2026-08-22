"""X-12 回归：消费关系改由模型声明，文本匹配退为兜底。

真机场景（2026-08-06）：`format_docx` 一句「双栏格式，正文字体小四，标题四号」
被连问三次「这条要求我没能安排进任何操作」，而执行器随后报「已执行 3 项格式操作」。
**账本与现实完全相反，且系统内部无任何机制能发现这个矛盾。**

根因不在账本，在账本的数据来源：

- 消费关系是**语义事实** —— 只在模型做「原话 → FormatOp」转换的那一刻产生，
  原文里并不存在答案。此前却当成**文本事实**由程序反推（`_infer_consumption`），
  等于拿有损变换的输出去反推输入。
- `operations` 转成 `list[FormatOp]`（dataclass）后与原话再无字面交集，且
  `_collect_verbatims` 当年只认 dict/list ⇒ 遇 dataclass 静默返回空列表。
  两级静默叠加，该意图上误报率**恒定 100%**，而 1604 个测试全绿。
- 该槽 `must_ask=True` 且是唯一实质槽位 ⇒ 每条排版要求必然挤进它，一条也逃不掉。

修法：schema 开 `slot_consumed_requirements` 出口（id 早已在 system 消息里列给
模型，只是没有回信通道），`_infer_consumption` 降为兜底。
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.intent import Intent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tasking.contracts import (
    AtomicRequirement,
    CallStatus,
    FieldStatus,
    RawOperation,
    SlotSource,
    SlotValue,
)
from paper_agent.tasking.extractors.base import (
    _merge_ids,
    _slot_texts,
    build_tool_schema,
)
from paper_agent.tasking.extractors.format_docx import FormatDocxExtractor
from paper_agent.tasking.intent_spec import FORMAT_DOCX_SPEC, INTENT_SPECS
from paper_agent.tools.docx_format import FormatOp

# 真机原话与 analyzer 的切分结果。三条要求同属一个 format_docx 操作
# （operation_analyzer 明确要求「同一目标上的多个细节要合并」）。
_VERBATIM = "帮我把这个论文改一下格式，要求双栏格式，正文字体小四，标题四号"
_OP = RawOperation(intent=Intent.FORMAT_DOCX, verbatim=_VERBATIM,
                   requirement_ids=("r1", "r2", "r3"))
_REQS = (
    AtomicRequirement(requirement_id="r1", verbatim="双栏格式", category="format"),
    AtomicRequirement(requirement_id="r2", verbatim="正文字体小四", category="format"),
    AtomicRequirement(requirement_id="r3", verbatim="标题四号", category="format"),
)

# 模型的实际返回：三个 FormatOp + 消费声明。这是修复后期望模型走的路径。
_OPS_PAYLOAD = [
    {"name": "set_columns", "value": 2, "scope": "all"},
    {"name": "set_font_size", "value": "小四", "scope": "body"},
    {"name": "set_font_size", "value": "四号", "scope": "headings"},
]


def _mock(obj) -> MockLLMProvider:
    return MockLLMProvider(scripted=[json.dumps(obj, ensure_ascii=False)])


def _extract(payload):
    return FormatDocxExtractor(_mock(payload)).pre_extract(
        _OP, canonical_requirements=_REQS,
    )


# --- 主证据：真机那三条要求全部被认领 -----------------------------------------

def test_declared_consumption_claims_all_three_requirements():
    """模型声明消费关系 → 三条要求全认领，unmapped 为空。

    这是 X-12 的主证据：修复前此处必然漏 3 条（`_slot_texts` 对 dataclass 列表返回
    `[]`，`_infer_consumption` 因 `texts` 为空直接返回）。
    """
    res = _extract({
        "operations": _OPS_PAYLOAD,
        "slot_consumed_requirements": {"operations": ["r1", "r2", "r3"]},
    })
    assert res.call_status is CallStatus.SUCCESS
    sv = res.slots["operations"]
    assert sv.field_status is FieldStatus.PRESENT
    assert set(sv.requirement_ids) == {"r1", "r2", "r3"}
    # 账本口径：三条全被消费 ⇒ 不该有任何 unmapped
    assert set(res.consumed_requirement_ids) == {"r1", "r2", "r3"}


def test_execution_value_unaffected_by_bookkeeping():
    """记账修好的同时，执行读的那条路（`SlotValue.value`）必须一字不变。

    执行读 `.value`、账本读 `.requirement_ids`，两者挂在同一对象上却互不校验 ——
    这正是 X-12 能「账本全错而执行全对」的结构原因。修账本不得动执行。
    """
    res = _extract({
        "operations": _OPS_PAYLOAD,
        "slot_consumed_requirements": {"operations": ["r1", "r2", "r3"]},
    })
    ops = res.slots["operations"].value
    assert all(isinstance(o, FormatOp) for o in ops)
    assert [(o.name, o.value, o.scope) for o in ops] == [
        ("set_columns", 2, "all"),
        ("set_font_size", "小四", "body"),
        ("set_font_size", "四号", "headings"),
    ]


# --- 兜底：模型漏报时仍要工作 --------------------------------------------------

def test_fallback_matching_still_works_when_model_omits_declaration():
    """模型完全不报 `slot_consumed_requirements` → 靠 per-op verbatim 兜底认领。

    per-op `verbatim` 是给兜底路径用的锚点（`set_columns(value=2)` 与「双栏格式」
    没有任何字面交集，没有它兜底对本槽恒定失效）。
    """
    res = _extract({
        "operations": [
            {"name": "set_columns", "value": 2, "scope": "all", "verbatim": "双栏格式"},
            {"name": "set_font_size", "value": "小四", "scope": "body",
             "verbatim": "正文字体小四"},
            {"name": "set_font_size", "value": "四号", "scope": "headings",
             "verbatim": "标题四号"},
        ],
    })
    assert set(res.slots["operations"].requirement_ids) == {"r1", "r2", "r3"}


def test_partial_declaration_lets_fallback_fill_the_rest():
    """模型只声明一条 → 另两条走兜底。

    跳过粒度是 per-requirement 而非 per-slot：已有声明的槽位仍可通过匹配再认领
    别的 requirement，否则模型漏报一条会连带牺牲同槽其余各条。
    """
    res = _extract({
        "operations": [
            {"name": "set_columns", "value": 2, "scope": "all", "verbatim": "双栏格式"},
            {"name": "set_font_size", "value": "小四", "scope": "body",
             "verbatim": "正文字体小四"},
            {"name": "set_font_size", "value": "四号", "scope": "headings",
             "verbatim": "标题四号"},
        ],
        "slot_consumed_requirements": {"operations": ["r1"]},
    })
    assert set(res.slots["operations"].requirement_ids) == {"r1", "r2", "r3"}


# --- 健壮性：模型报错 id 不能崩 ------------------------------------------------

def test_unknown_declared_id_is_discarded_not_raised():
    """模型自造 id（`r9`）→ 丢弃，不报错、不污染账本。

    消费关系是记账信息，一个坏 id 不该让整次抽取失败 —— 槽值本身可能完全正确。
    """
    res = _extract({
        "operations": _OPS_PAYLOAD,
        "slot_consumed_requirements": {"operations": ["r1", "r9", "nonexistent"]},
    })
    assert res.call_status is CallStatus.SUCCESS
    assert set(res.slots["operations"].requirement_ids) == {"r1"}


def test_malformed_declaration_shapes_are_tolerated():
    """声明字段类型不对（字符串 / 数组 / null）→ 当作没报，不崩。"""
    for bad in ("r1", ["r1"], None, 42):
        res = _extract({
            "operations": _OPS_PAYLOAD,
            "slot_consumed_requirements": bad,
        })
        assert res.call_status is CallStatus.SUCCESS
        assert res.slots["operations"].field_status is FieldStatus.PRESENT
    # 值不是数组（键对了、值错了）
    res = _extract({
        "operations": _OPS_PAYLOAD,
        "slot_consumed_requirements": {"operations": "r1"},
    })
    assert res.slots["operations"].requirement_ids == ()


def test_envelope_key_not_mistaken_for_slot():
    """`slot_consumed_requirements` 是元信息，不能被当成未知槽位。"""
    res = _extract({
        "operations": _OPS_PAYLOAD,
        "slot_consumed_requirements": {"operations": ["r1"]},
    })
    assert set(res.slots) == set(FORMAT_DOCX_SPEC.slots)


# --- 机制层：dataclass 洞 ------------------------------------------------------

def test_collect_verbatims_recurses_into_dataclasses():
    """`_collect_verbatims` 认 dataclass。

    修复前：只处理 dict 与 list/tuple，遇 dataclass 两个 isinstance 都不匹配、
    函数体走完直接返回 —— 没有 else、没有告警、没有异常。**静默产出空列表**，
    这是该 bug 能在 1604 个测试下存活的唯一原因。
    """
    @dataclasses.dataclass
    class Clause:
        dimension: str
        verbatim: str = ""

    sv = SlotValue(value=[Clause("total", "中文2篇"), Clause("en", "英文8篇")],
                   field_status=FieldStatus.PRESENT, source=SlotSource.USER_EXPLICIT)
    assert _slot_texts(sv) == ["中文2篇", "英文8篇"]


def test_format_op_carries_verbatim_through_coercion():
    """`_to_format_ops` 把 per-op verbatim 落进 `FormatOp.verbatim`（兜底锚点）。"""
    res = _extract({
        "operations": [
            {"name": "set_columns", "value": 2, "scope": "all", "verbatim": "双栏格式"},
        ],
    })
    ops = res.slots["operations"].value
    assert ops[0].verbatim == "双栏格式"
    # 且该锚点确实能被兜底读到
    assert "双栏格式" in _slot_texts(res.slots["operations"])


def test_merge_ids_unions_envelope_and_declaration():
    """信封内 `requirement_ids` 与旁路声明取并集、保序去重。"""
    assert _merge_ids(("r1", "r2"), ("r2", "r3")) == ("r1", "r2", "r3")
    assert _merge_ids(None, ("r1",)) == ("r1",)
    assert _merge_ids((), ()) == ()
    assert _merge_ids((" r1 ", "", "r1"), ()) == ("r1",)


# --- 契约层：所有意图的 schema 都要有这条出口 ---------------------------------

def test_every_intent_schema_exposes_consumption_channel():
    """消费声明通道是**全意图契约**，不是 format_docx 的一次性修补。

    X-11 当年给 `citation_quota` 打的 per-clause verbatim 补丁没有被制度化，
    别人加 `operations` 时无从知晓 ⇒ 重蹈覆辙。这条测试把它钉成契约：新增意图
    若绕过 `build_tool_schema`，此处即红。
    """
    for intent, spec in INTENT_SPECS.items():
        props = build_tool_schema(spec, intent.value)["function"]["parameters"]["properties"]
        assert "slot_consumed_requirements" in props, intent
        decl = props["slot_consumed_requirements"]
        assert decl["type"] == "object", intent
        assert decl["additionalProperties"]["type"] == "array", intent
