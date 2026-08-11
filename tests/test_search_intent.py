"""A2f-1：SearchIntent 值对象契约 + 两种 hash 分离 + generic-title 过滤。"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.literature.retrieval.search_intent import (
    SearchIntent,
    SearchIntentError,
    SearchIntentPreparationOutcome,
    SearchIntentPreparationStatus,
    SearchSeed,
    SeedTarget,
    build_seeds,
    is_generic_title,
    is_informative_title,
    normalize_text,
)


def _seed(text, target=SeedTarget.ANY):
    return SearchSeed(text=text, target=target)


def _intent(topic="大语言模型代码生成", seeds=("large language model code generation",),
            purpose=""):
    return SearchIntent(semantic_topic=topic, section_purpose=purpose,
                        seeds=tuple(_seed(s) for s in seeds))


# ---------------------------------------------------------------- 规范化

def test_normalize_collapses_whitespace_and_nfc():
    assert normalize_text("  deep   learning\n\ttools ", max_chars=100) == "deep learning tools"


# ---------------------------------------------------------------- generic-title

def test_generic_titles_rejected():
    for t in ("引言", "Introduction", "相关工作", "Related Work", "discussion", " 方法 "):
        assert is_generic_title(t) is True
        assert is_informative_title(t) is False


def test_informative_title_kept():
    assert is_informative_title("基于对比学习的图像分割") is True
    assert is_generic_title("引言中的深度学习综述") is False   # 含实义，不算纯通用标题


# ---------------------------------------------------------------- SearchSeed 验证

def test_seed_too_short_rejected():
    with pytest.raises(SearchIntentError):
        SearchSeed(text="a")


def test_seed_generic_title_rejected():
    with pytest.raises(SearchIntentError):
        SearchSeed(text="Introduction")


def test_seed_normalized():
    s = SearchSeed(text="  code   generation ")
    assert s.text == "code generation"


# ---------------------------------------------------------------- SearchIntent 验证

def test_intent_empty_topic_rejected():
    with pytest.raises(SearchIntentError):
        SearchIntent(semantic_topic="", seeds=(_seed("code generation"),))


def test_intent_generic_topic_rejected():
    with pytest.raises(SearchIntentError):
        SearchIntent(semantic_topic="引言", seeds=(_seed("code generation"),))


def test_intent_zero_seeds_rejected():
    with pytest.raises(SearchIntentError):
        SearchIntent(semantic_topic="大语言模型代码生成", seeds=())


def test_intent_too_many_seeds_rejected():
    with pytest.raises(SearchIntentError):
        SearchIntent(semantic_topic="topic here",
                     seeds=tuple(_seed(f"keyword number {i}") for i in range(7)))


def test_intent_dedup_seeds_case_insensitive():
    it = SearchIntent(semantic_topic="大语言模型代码生成",
                      seeds=(_seed("Code Generation"), _seed("code generation"),
                             _seed("code   generation")))
    assert len(it.seeds) == 1                        # 大小写/空白差异视为同一 seed


# ---------------------------------------------------------------- 两种 hash 分离（核心）

def test_semantic_fingerprint_excludes_seeds():
    a = _intent(seeds=("large language model code generation",))
    b = _intent(seeds=("LLM program synthesis", "code completion transformers"))
    assert a.semantic_fingerprint() == b.semantic_fingerprint()    # seed 变，语义指纹不变


def test_semantic_fingerprint_changes_with_topic():
    a = _intent(topic="大语言模型代码生成")
    b = _intent(topic="图神经网络推荐系统")
    assert a.semantic_fingerprint() != b.semantic_fingerprint()


def test_strategy_hash_changes_with_seeds():
    a = _intent(seeds=("keyword one", "keyword two"))
    b = _intent(seeds=("keyword two", "keyword one"))   # 顺序不同
    assert a.semantic_fingerprint() == b.semantic_fingerprint()
    assert a.strategy_hash() != b.strategy_hash()       # seed 顺序影响 strategy hash


def test_strategy_hash_includes_semantic():
    a = _intent(topic="topic alpha", seeds=("same keyword",))
    b = _intent(topic="topic beta", seeds=("same keyword",))
    assert a.strategy_hash() != b.strategy_hash()


# ---------------------------------------------------------------- outcome 契约

def test_ready_requires_intent():
    with pytest.raises(SearchIntentError):
        SearchIntentPreparationOutcome(status=SearchIntentPreparationStatus.READY)


def test_non_ready_forbids_intent():
    with pytest.raises(SearchIntentError):
        SearchIntentPreparationOutcome(
            status=SearchIntentPreparationStatus.NEEDS_INPUT, intent=_intent())


def test_error_outcome_carries_issue_code():
    o = SearchIntentPreparationOutcome(
        status=SearchIntentPreparationStatus.ERROR, issue_code="synth_failed")
    assert o.intent is None and o.issue_code == "synth_failed"


# ---------------------------------------------------------------- build_seeds 收敛

def test_build_seeds_drops_invalid_and_generic():
    seeds = build_seeds([
        {"text": "code generation"},
        {"text": "Introduction"},          # 通用标题 → 剔
        {"text": "a"},                     # 太短 → 剔
        {"text": "code generation"},       # 重复 → 去
        {"text": "program synthesis", "target": "language.en"},
    ])
    texts = [s.text for s in seeds]
    assert texts == ["code generation", "program synthesis"]
    assert seeds[1].target is SeedTarget.LANGUAGE_EN


def test_build_seeds_all_invalid_returns_empty():
    assert build_seeds([{"text": "引言"}, {"text": "x"}]) == ()


# ---------------------------------------------------------------- raw_topic（召回辅助字段）

def test_raw_topic_excluded_from_both_hashes():
    """`raw_topic` 只影响召回、不影响「什么算切题」⇒ 不得进任何 hash。

    否则同一课题因原始表述措辞不同就换指纹，白丢相关性缓存。
    """
    base = SearchIntent(semantic_topic="cross-view geo-localization", seeds=(_seed("cvgl"),))
    with_raw = SearchIntent(semantic_topic="cross-view geo-localization",
                            raw_topic="跨视角图像地理定位", seeds=(_seed("cvgl"),))
    assert with_raw.semantic_fingerprint() == base.semantic_fingerprint()
    assert with_raw.strategy_hash() == base.strategy_hash()


def test_raw_topic_invalid_value_does_not_raise():
    """非法 raw_topic（通用章节名）退化为无原文可用，不该让整个请求失败。"""
    it = SearchIntent(semantic_topic="code generation", raw_topic="引言",
                      seeds=(_seed("code generation"),))
    assert it.raw_topic == "引言"          # 只归一化，不校验
    assert it.semantic_fingerprint()       # 仍可用


def test_raw_topic_defaults_empty_and_normalized():
    it = SearchIntent(semantic_topic="code generation", seeds=(_seed("code generation"),))
    assert it.raw_topic == ""
    padded = SearchIntent(semantic_topic="code generation", raw_topic="  大模型代码生成  ",
                          seeds=(_seed("code generation"),))
    assert padded.raw_topic == "大模型代码生成"
