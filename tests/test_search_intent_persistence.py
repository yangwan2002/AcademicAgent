"""A2f-3：语义主题接入 + 冻结持久化（criteria 映射 / SearchIntent 序列化 / checkpoint / facts）。"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.application.literature_ports import (
    PreflightCheckpoint,
    checkpoint_from_dict,
    checkpoint_to_dict,
)
from paper_agent.literature.retrieval.search_intent import (
    SearchIntent,
    SearchSeed,
    SeedTarget,
    search_intent_from_dict,
    search_intent_to_dict,
    to_relevance_criteria,
)
from paper_agent.session.facts import SessionFacts


def _intent(topic="大语言模型代码生成", seeds=("large language model code generation",)):
    return SearchIntent(semantic_topic=topic,
                        seeds=tuple(SearchSeed(text=s) for s in seeds))


# ---------------------------------------------------------------- criteria 只吃语义

def test_criteria_ignores_seed_changes():
    a = to_relevance_criteria(_intent(seeds=("kw one",)), query_intent="综述")
    b = to_relevance_criteria(_intent(seeds=("kw two", "kw three")), query_intent="综述")
    assert a.fingerprint() == b.fingerprint()          # seed 变，TaskFingerprint 不变


def test_criteria_changes_with_topic():
    a = to_relevance_criteria(_intent(topic="大语言模型代码生成"), query_intent="综述")
    b = to_relevance_criteria(_intent(topic="图神经网络推荐"), query_intent="综述")
    assert a.fingerprint() != b.fingerprint()


def test_criteria_never_uses_joined_seeds_as_query_intent():
    it = _intent(seeds=("alpha keyword", "beta keyword"))
    crit = to_relevance_criteria(it, query_intent="章节语义目标")
    assert crit.query_intent == "章节语义目标"
    assert "alpha keyword" not in crit.query_intent


# ---------------------------------------------------------------- SearchIntent 序列化

def test_search_intent_dict_roundtrip_preserves_hashes():
    it = SearchIntent(semantic_topic="大语言模型代码生成", section_purpose="综述背景",
                      seeds=(SearchSeed(text="code generation", target=SeedTarget.LANGUAGE_EN),
                             SearchSeed(text="程序合成", target=SeedTarget.LANGUAGE_ZH)))
    back = search_intent_from_dict(search_intent_to_dict(it))
    assert back.semantic_fingerprint() == it.semantic_fingerprint()
    assert back.strategy_hash() == it.strategy_hash()
    assert [(s.text, s.target) for s in back.seeds] == [(s.text, s.target) for s in it.seeds]


# ---------------------------------------------------------------- checkpoint 新字段

def test_checkpoint_roundtrip_with_search_intent_fields():
    cp = PreflightCheckpoint(
        flow_id="f1", operation_id="op1", task_fingerprint="tf", quota_hash="qh",
        state="needs_search_intent_confirmation",
        search_intent_id="si_1", semantic_fingerprint="sem", strategy_hash="strat")
    back = checkpoint_from_dict(checkpoint_to_dict(cp))
    assert back.search_intent_id == "si_1"
    assert back.semantic_fingerprint == "sem"
    assert back.strategy_hash == "strat"


def test_old_checkpoint_dict_defaults_empty():
    back = checkpoint_from_dict({"flow_id": "f", "operation_id": "o",
                                 "task_fingerprint": "t", "quota_hash": "q", "state": "s"})
    assert back.search_intent_id == "" and back.semantic_fingerprint == ""


# ---------------------------------------------------------------- facts pending_search_intents

def test_facts_put_get_drop_search_intent(tmp_path):
    f = SessionFacts(path=str(tmp_path / "facts.json"))
    it = _intent()
    f.put_search_intent("si_1", search_intent_to_dict(it))
    assert f.get_search_intent("si_1") is not None
    assert f.get_search_intent("nope") is None
    assert f.drop_search_intent("si_1") is True
    assert f.get_search_intent("si_1") is None


def test_facts_persist_and_reload_search_intent(tmp_path):
    path = str(tmp_path / "facts.json")
    f = SessionFacts(path=path)
    f.put_search_intent("si_1", search_intent_to_dict(_intent()))
    f.save()
    reloaded = SessionFacts.load(path)
    row = reloaded.get_search_intent("si_1")
    assert row is not None
    back = search_intent_from_dict(row)
    assert back.semantic_topic == "大语言模型代码生成"


def test_facts_isolates_corrupt_search_intents(tmp_path):
    import json

    path = tmp_path / "facts.json"
    payload = {
        "schema_version": 4, "references": [], "known_numbers": [], "claims": {},
        "pending_search_intents": [
            {"search_intent_id": "ok", "semantic_topic": "x"},
            {"no_id": True},           # 损坏：缺 id
            "not a dict",              # 损坏：非 dict
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    f = SessionFacts.load(str(path))
    assert len(f.pending_search_intents) == 1
    assert f.corrupt_search_intent_count == 2


def test_old_envelope_without_search_intents_ok(tmp_path):
    import json

    path = tmp_path / "facts.json"
    path.write_text(json.dumps({"schema_version": 4, "references": []}), encoding="utf-8")
    f = SessionFacts.load(str(path))
    assert f.pending_search_intents == []
    assert f.corrupt_search_intent_count == 0
