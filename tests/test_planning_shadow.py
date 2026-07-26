"""intent-planning Task 16 验收：Shadow Mode（只读不接管）。

- off：不跑新层（analyze 零调用），legacy 照常执行
- shadow：legacy 照常执行；shadow 基于**执行前快照**分析、只记录
- Working_Copy 字节不变（shadow 不触碰写入）
- shadow 失败/异常被隔离，legacy 不受影响
- shadow 用快照而非改动后的文件（legacy 改了文件也不影响 shadow 的章节视图）
- 差异被记录（operation_intents / validation_result / matches_legacy）
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.application.turn_service import TurnService, capture_snapshot
from paper_agent.config import Config
from paper_agent.coordinator import Coordinator
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.providers.llm.mock import MockLLMProvider


# --- 假 session（只读接口 + 可变 working 用于验证快照隔离）---

class _FakeSession:
    def __init__(self, working: str, ext: str = "tex"):
        self._working = working
        self.ext = ext
        self.working_path = "<mem>"
        self.write_calls = 0

    def read_working(self) -> str:
        return self._working

    def write_working(self, text, **kw):  # 若被 shadow 调用即视为污染
        self.write_calls += 1
        self._working = text


_TEX = (
    "\\section{Introduction}\nintro body\n"
    "\\section{Method}\nmethod body\n"
    "\\section{Conclusion}\nconclusion body\n"
)


class _FailingLLM(LLMProvider):
    def complete(self, messages, **opts):
        raise RuntimeError("shadow provider down")

    def stream(self, messages, *, cancel_token=None, **opts):
        yield from ()


def _analyze_json(intent="polish", verbatim="润色引言"):
    return json.dumps({
        "operations": [{"intent": intent, "verbatim": verbatim, "requirement_ids": ["r1"]}],
        "requirements": [{"requirement_id": "r1", "verbatim": verbatim, "category": "target"}],
        "unmapped_text": [],
    }, ensure_ascii=False)


def _extract_json(slot="section_id", value="introduction"):
    return json.dumps({
        "slots": {slot: {"value": value, "status": "present", "requirement_ids": ["r1"]}},
    }, ensure_ascii=False)


def _cfg(mode: str) -> Config:
    return Config(intent_planning_mode=mode)


def _coord() -> Coordinator:
    return Coordinator()  # 无下游 → NullEventBus，零开销


# --- off ---

def test_off_does_not_run_new_layer():
    llm = MockLLMProvider(scripted=[_analyze_json(), _extract_json()])
    svc = TurnService(config=_cfg("off"), llm=llm, shadow_coordinator=_coord())
    sess = _FakeSession(_TEX)
    calls = {"legacy": 0}

    def legacy():
        calls["legacy"] += 1
        return "legacy-done"

    out = svc.handle_turn("润色引言", session=sess, facts=None, legacy=legacy)
    assert out.mode == "off"
    assert out.legacy_result == "legacy-done"
    assert out.shadow is None
    assert calls["legacy"] == 1
    assert llm.calls == []          # 理解层未被调用


# --- shadow 基本 ---

def test_shadow_runs_and_records_without_touching_working_copy():
    llm = MockLLMProvider(scripted=[_analyze_json(), _extract_json()])
    svc = TurnService(config=_cfg("shadow"), llm=llm, shadow_coordinator=_coord())
    sess = _FakeSession(_TEX)

    out = svc.handle_turn("润色引言", session=sess, facts=None,
                          legacy=lambda: "legacy-done", legacy_intent="polish")
    assert out.mode == "shadow"
    assert out.legacy_result == "legacy-done"
    assert out.shadow is not None
    assert out.shadow.ok is True
    assert out.shadow.operation_intents == ("polish",)
    assert out.shadow.validation_result == "ready"
    assert out.shadow.matches_legacy is True
    # shadow 绝不写 Working_Copy
    assert sess.write_calls == 0


def test_shadow_working_copy_bytes_unchanged():
    llm = MockLLMProvider(scripted=[_analyze_json(), _extract_json()])
    svc = TurnService(config=_cfg("shadow"), llm=llm, shadow_coordinator=_coord())
    sess = _FakeSession(_TEX)
    before = sess.read_working()
    svc.handle_turn("润色引言", session=sess, facts=None, legacy=lambda: None)
    assert sess.read_working() == before


# --- 失败隔离 ---

def test_shadow_failure_isolated_from_legacy():
    svc = TurnService(config=_cfg("shadow"), llm=_FailingLLM(),
                      shadow_coordinator=_coord())
    sess = _FakeSession(_TEX)
    calls = {"legacy": 0}

    def legacy():
        calls["legacy"] += 1
        return "legacy-done"

    out = svc.handle_turn("润色引言", session=sess, facts=None, legacy=legacy)
    # legacy 正常返回，未被 shadow 失败波及
    assert out.legacy_result == "legacy-done"
    assert calls["legacy"] == 1
    # shadow 记录了失败（provider_failed，analyze 兜底），但 ok=False、不抛
    assert out.shadow is not None
    assert out.shadow.ok is False
    assert out.shadow.analyze_status == "provider_failed"


def test_shadow_without_coordinator_still_safe():
    # 不传 shadow_coordinator 也应工作（直接跑）
    llm = MockLLMProvider(scripted=[_analyze_json(), _extract_json()])
    svc = TurnService(config=_cfg("shadow"), llm=llm, shadow_coordinator=None)
    sess = _FakeSession(_TEX)
    out = svc.handle_turn("润色引言", session=sess, facts=None, legacy=lambda: "x")
    assert out.shadow is not None and out.shadow.ok is True


# --- 快照隔离：legacy 改了文件，shadow 仍用执行前快照 ---

def test_shadow_uses_presnapshot_not_modified_working_copy():
    # analyze 让 shadow 去校验 "Method" 章节；legacy 执行时把 Method 删掉。
    # 若 shadow 误读改动后的文件 → 找不到 Method → rejected；
    # 用执行前快照 → Method 仍在 → ready。
    llm = MockLLMProvider(scripted=[
        _analyze_json(intent="polish", verbatim="润色方法"),
        _extract_json(slot="section_id", value="method"),
    ])
    svc = TurnService(config=_cfg("shadow"), llm=llm, shadow_coordinator=_coord())
    sess = _FakeSession(_TEX)

    def legacy():
        # legacy 改动 working copy：删掉 Method 章节
        sess.write_working("\\section{Introduction}\nintro only\n")
        return "legacy-mutated"

    out = svc.handle_turn("润色方法部分", session=sess, facts=None,
                          legacy=legacy, legacy_intent="polish")
    assert out.legacy_result == "legacy-mutated"
    # 快照在 legacy 前捕获，含 Method → 校验通过
    assert out.shadow.ok is True
    assert out.shadow.validation_result == "ready"


def test_capture_snapshot_reads_sections_and_hash():
    sess = _FakeSession(_TEX)
    snap = capture_snapshot(sess, facts=None)
    assert snap.file_type == "tex"
    titles = [s.title for s in snap.sections]
    assert titles == ["Introduction", "Method", "Conclusion"]
    assert len(snap.working_hash) == 64  # sha256 hex


# --- 差异记录：shadow 与 legacy 意图不一致时可见 ---

def test_shadow_records_mismatch_with_legacy():
    # shadow 判成 convert_format，legacy 说是 polish → matches_legacy False
    llm = MockLLMProvider(scripted=[
        _analyze_json(intent="convert_format", verbatim="转成 docx"),
        _extract_json(slot="target_ext", value="docx"),
    ])
    svc = TurnService(config=_cfg("shadow"), llm=llm, shadow_coordinator=_coord())
    sess = _FakeSession(_TEX)
    out = svc.handle_turn("转成 docx", session=sess, facts=None,
                          legacy=lambda: None, legacy_intent="polish")
    assert out.shadow.operation_intents == ("convert_format",)
    assert out.shadow.matches_legacy is False
