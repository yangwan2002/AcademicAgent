"""intent-planning Task 4 验收：IntentSpec 注册表。

- 五意图 slot / 默认 / must_ask 与 design 附录表一致
- 每个 slot 有结构化 ExecutionBinding（非空 target）
- 默认值可由系统取得（不依赖 LLM）
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.intent import Intent
from paper_agent.tasking.intent_spec import (
    INTENT_SPECS,
    BindingKind,
    spec_for,
)


# design 附录表：intent -> {slot: (required, default, must_ask)}
_EXPECTED = {
    Intent.POLISH: {
        "section_id": (True, None, True),
        "goal": (False, "保守润色", False),
        "constraints": (False, [], False),
    },
    Intent.REWRITE: {
        "section_id": (True, None, True),
        "direction": (True, None, True),
        "details": (False, "", False),
        "search_query": (False, "", False),
        "want_search": (False, False, False),
        "search_limit": (False, 5, False),
        # 按 A2d-5 预期变更新增：引用配额槽原子激活（与 Handler 消费同批）
        "citation_quota": (False, None, False),
    },
    Intent.ADD_SECTION: {
        "title": (True, None, True),
        "purpose": (True, None, True),
        "structure_kind": (True, None, True),
        "structure_desc": (False, "", False),
        "subsections": (False, [], False),
        "position": (False, "end", False),
        "search_query": (False, "", False),
        "want_search": (False, False, False),
        "search_limit": (False, 5, False),
        # 按 A2d-5 预期变更新增：引用配额槽原子激活
        "citation_quota": (False, None, False),
    },
    Intent.CONVERT_FORMAT: {
        "target_ext": (True, None, True),
        "presets": (False, [], False),
    },
    Intent.FORMAT_DOCX: {
        "operations": (True, None, True),
        "scope": (False, "document", False),
    },
}


def test_five_intents_registered():
    assert set(INTENT_SPECS.keys()) == {
        Intent.POLISH, Intent.REWRITE, Intent.ADD_SECTION,
        Intent.CONVERT_FORMAT, Intent.FORMAT_DOCX,
    }


def test_specs_match_appendix_table():
    for intent, slots in _EXPECTED.items():
        spec = spec_for(intent)
        assert spec is not None, intent
        assert set(spec.slots.keys()) == set(slots.keys()), intent
        for name, (required, default, must_ask) in slots.items():
            sl = spec.slots[name]
            assert sl.required is required, (intent, name)
            assert sl.must_ask is must_ask, (intent, name)
            # 必填字段无默认；可选字段默认值与表一致
            if required:
                assert sl.default_factory is None, (intent, name)
            else:
                assert sl.default() == default, (intent, name)


def test_every_slot_has_execution_binding():
    for spec in INTENT_SPECS.values():
        for name, sl in spec.slots.items():
            assert sl.execution_binding is not None, (spec.intent, name)
            assert isinstance(sl.execution_binding.kind, BindingKind)
            assert sl.execution_binding.target, (spec.intent, name)  # 非空


def test_required_and_must_ask_helpers():
    polish = spec_for(Intent.POLISH)
    assert polish.required_slots() == ["section_id"]
    assert polish.must_ask_slots() == ["section_id"]


def test_defaults_are_independent_instances():
    # default_factory=list 每次返回新 list（不共享可变默认）
    spec = spec_for(Intent.POLISH)
    a = spec.slots["constraints"].default()
    b = spec.slots["constraints"].default()
    assert a == [] and b == [] and a is not b


def test_every_public_intent_has_a_spec():
    assert set(INTENT_SPECS) == set(Intent)
    assert all(spec_for(intent) is not None for intent in Intent)
