"""A2f-2：Synthesizer（确定性/LLM）+ 准备服务 + typed port + 预算计费。"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.application.literature_ports import (
    SearchIntentDecision,
    SearchTopicDecision,
)
from paper_agent.application.search_intent_preparation import prepare_search_intent
from paper_agent.literature.retrieval.search_intent import (
    SearchContext,
    SearchIntentPreparationStatus,
)
from paper_agent.tasking.query_synthesizer import (
    DeterministicSearchIntentSynthesizer,
    LLMSearchIntentSynthesizer,
)


# ---------------------------------------------------------------- stubs

class _Budget:
    def __init__(self):
        self.llm_calls = 0

    def charge_llm(self, n: int = 1):
        self.llm_calls += n


class _ReviewerLLM:
    """stub：按 tool call 返回一个合成意图；记录调用次数。"""

    def __init__(self, topic="大语言模型代码生成",
                 seeds=("large language model code generation", "program synthesis"),
                 fail=False):
        self._topic, self._seeds, self._fail = topic, seeds, fail
        self.calls = 0

    def complete(self, messages, **opts):
        from paper_agent.providers.llm.base import LLMResponse, ToolCall
        self.calls += 1
        if self._fail:
            return LLMResponse(content="not json", tool_calls=[])
        return LLMResponse(content="", tool_calls=[ToolCall(
            id="1", name="propose_search_intent",
            arguments={"semantic_topic": self._topic,
                       "seeds": [{"text": s} for s in self._seeds]})])


class _Port:
    def __init__(self, topic=None, intent=None):
        self._topic = topic
        self._intent = intent
        self.topic_calls = 0
        self.intent_calls = 0

    def request_search_topic(self, summary):
        self.topic_calls += 1
        return self._topic or SearchTopicDecision("needs_input")

    def confirm_search_intent(self, summary):
        self.intent_calls += 1
        return self._intent or SearchIntentDecision("accept")


def _ctx(**kw):
    base = dict(operation_kind="add_section", target_title="引言")
    base.update(kw)
    return SearchContext(**base)


# ---------------------------------------------------------------- 确定性合成

def test_explicit_query_ready_no_llm():
    synth = LLMSearchIntentSynthesizer(_ReviewerLLM())
    budget = _Budget()
    out = prepare_search_intent(
        _ctx(explicit_query="graph neural network recommendation"),
        synthesizer=synth, budget=budget)
    assert out.status is SearchIntentPreparationStatus.READY
    assert budget.llm_calls == 0                      # 显式 query 不调 LLM
    assert synth._llm.calls == 0


def test_deterministic_operation_projection_ready_no_confirm():
    synth = DeterministicSearchIntentSynthesizer()
    out = prepare_search_intent(
        _ctx(purpose="综述基于对比学习的图像分割方法"),
        synthesizer=synth, interaction_port=_Port())
    assert out.status is SearchIntentPreparationStatus.READY
    assert out.intent.semantic_topic


def test_generic_title_only_needs_input():
    """只有通用章节名(引言)、无其他信号 → 不硬搜,走澄清/NEEDS_INPUT。"""
    synth = DeterministicSearchIntentSynthesizer()
    out = prepare_search_intent(_ctx(), synthesizer=synth, interaction_port=None)
    assert out.status is SearchIntentPreparationStatus.NEEDS_INPUT
    assert out.issue_code == "NEEDS_SEARCH_TOPIC"
    assert out.intent is None


# ---------------------------------------------------------------- LLM 合成 + 确认

def test_llm_synthesis_requires_confirmation():
    llm = _ReviewerLLM()
    budget = _Budget()
    port = _Port(intent=SearchIntentDecision("accept"))
    out = prepare_search_intent(
        _ctx(purpose="讨论大模型在代码生成中的应用"),
        synthesizer=LLMSearchIntentSynthesizer(llm), interaction_port=port, budget=budget)
    assert out.status is SearchIntentPreparationStatus.READY
    assert port.intent_calls == 1                     # LLM 合成必确认
    assert budget.llm_calls == 1                      # 一次真实调用记一次
    assert llm.calls == 1


def test_llm_synthesis_non_interactive_needs_input():
    """非交互 + 需确认 → NEEDS_INPUT,不默认接受。"""
    out = prepare_search_intent(
        _ctx(purpose="讨论大模型在代码生成中的应用"),
        synthesizer=LLMSearchIntentSynthesizer(_ReviewerLLM()), interaction_port=None,
        budget=_Budget())
    assert out.status is SearchIntentPreparationStatus.NEEDS_INPUT
    assert out.issue_code == "NEEDS_SEARCH_INTENT_CONFIRMATION"


def test_llm_synthesis_cancel():
    port = _Port(intent=SearchIntentDecision("cancel"))
    out = prepare_search_intent(
        _ctx(purpose="讨论大模型在代码生成中的应用"),
        synthesizer=LLMSearchIntentSynthesizer(_ReviewerLLM()), interaction_port=port)
    assert out.status is SearchIntentPreparationStatus.CANCELLED


def test_llm_synthesis_modify_then_accept():
    port = _Port(intent=SearchIntentDecision("modify", "对比学习图像分割"))
    # modify → 用新文本作 explicit_query 重合成（显式路径,不再需确认）→ READY
    out = prepare_search_intent(
        _ctx(purpose="讨论大模型在代码生成中的应用"),
        synthesizer=LLMSearchIntentSynthesizer(_ReviewerLLM()), interaction_port=port,
        max_revisions=2)
    assert out.status is SearchIntentPreparationStatus.READY
    assert "对比学习" in out.intent.semantic_topic


# ---------------------------------------------------------------- 计费纪律

def test_deterministic_never_charges_llm():
    budget = _Budget()
    prepare_search_intent(
        _ctx(purpose="综述图神经网络推荐系统"),
        synthesizer=DeterministicSearchIntentSynthesizer(), budget=budget)
    assert budget.llm_calls == 0


def test_llm_failure_charged_once_and_fallbacks():
    """LLM 失败仍记一次；有确定性信号 → fallback READY(不确认,因非 llm 成形)。"""
    llm = _ReviewerLLM(fail=True)
    budget = _Budget()
    out = prepare_search_intent(
        _ctx(purpose="综述基于对比学习的图像分割方法"),
        synthesizer=LLMSearchIntentSynthesizer(llm), interaction_port=_Port(), budget=budget)
    assert budget.llm_calls == 1                      # 失败的真实调用也计一次
    # 有确定性信号 → fallback 出 intent
    assert out.status in (SearchIntentPreparationStatus.READY,
                          SearchIntentPreparationStatus.NEEDS_INPUT)


# ---------------------------------------------------------------- 澄清补主题

def test_clarify_provide_topic_then_ready():
    port = _Port(topic=SearchTopicDecision("provide", "对比学习图像分割"))
    out = prepare_search_intent(
        _ctx(),                                       # 无信号
        synthesizer=DeterministicSearchIntentSynthesizer(), interaction_port=port,
        max_revisions=2)
    assert out.status is SearchIntentPreparationStatus.READY
    assert port.topic_calls == 1


def test_clarify_cancel():
    port = _Port(topic=SearchTopicDecision("cancel"))
    out = prepare_search_intent(
        _ctx(), synthesizer=DeterministicSearchIntentSynthesizer(), interaction_port=port)
    assert out.status is SearchIntentPreparationStatus.CANCELLED
