"""重试循环层面的原地打转:LLM 调用次数必须真的减少。

`test_retry_feedback.py` 测的是判定函数;这里测**循环真的提前停了** ——
数 LLM 调用次数。这两件事分开测很重要:`is_stagnant` 全对但没接进循环,
或者接进去了却没 break,都只有这个测试能发现。
"""
from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.intents.plans import RewritePlan
from paper_agent.intents.rewrite import execute_rewrite
from paper_agent.providers.llm.base import LLMResponse
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.session.facts import SessionFacts
from paper_agent.session.file_session import FileBackedSession
from paper_agent.tools.citation import CitationVerifier

SAMPLE_MD = """# Introduction

原有内容一段。这里有一句论述。

# Results

结果章节保持不动。
"""


class _CountingLLM:
    """每次都返回同一段违规文本 —— 模拟「模型已尽力但约束不可满足」。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def complete(self, messages, **kw):
        self.calls += 1
        return LLMResponse(content=self.reply)

    def stream(self, messages, **kw):
        raise NotImplementedError


def _session(tmp_path):
    src = tmp_path / "paper.md"
    src.write_text(SAMPLE_MD, encoding="utf-8")
    return FileBackedSession(str(src), workspace_root=str(tmp_path / "ws"))


def _run(tmp_path, reply, *, max_retry=2):
    session = _session(tmp_path)
    facts = SessionFacts(path=str(tmp_path / "facts.json"))
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="")
    llm = _CountingLLM(reply)
    provider = MockRetrievalProvider()
    result = execute_rewrite(
        session, plan, facts, llm,
        retrieval_provider=provider, verifier=CitationVerifier(provider),
        confirm=lambda _v, _c: False, max_retry=max_retry,
    )
    return result, llm


def test_stagnant_retry_stops_early(tmp_path):
    """违规逐轮相同 ⇒ 第 2 次调用后就该停，而不是跑满 3 次。

    原行为:`for _ in range(max_retry + 1)` 无条件跑满,三次相同输入换三次相同失败。
    """
    # 引一个账本里不存在的标记 ⇒ 每轮都产生同一条「无法解析」违规
    result, llm = _run(tmp_path, "改写后的内容[nosuchref]。", max_retry=2)

    assert not result.ok
    assert llm.calls == 2, f"应在第 2 次调用后识别打转，实际调用 {llm.calls} 次"


def test_stagnation_message_is_diagnostic(tmp_path):
    """失败消息要说清「约束无法满足」，而不是笼统的「尝试 N 次未通过」。"""
    result, _ = _run(tmp_path, "改写后的内容[nosuchref]。", max_retry=2)
    assert "无法满足" in result.message
    assert "提前停止" in result.message


def test_working_copy_untouched_on_failure(tmp_path):
    """提前停止不得留下半成品:working copy 必须逐字不变。"""
    session = _session(tmp_path)
    before = session.read_working()

    facts = SessionFacts(path=str(tmp_path / "facts.json"))
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="")
    provider = MockRetrievalProvider()
    result = execute_rewrite(
        session, plan, facts, _CountingLLM("改写后的内容[nosuchref]。"),
        retrieval_provider=provider, verifier=CitationVerifier(provider),
        confirm=lambda _v, _c: False, max_retry=2,
    )
    assert not result.ok
    assert session.read_working() == before
    assert session.read_source() == SAMPLE_MD


def test_progressing_retry_uses_full_budget(tmp_path):
    """违规**在变化**时必须跑满预算 —— 打转检测不能顺带砍掉正常重试。

    这是防止「修复过度」的对照实验:每轮回复不同 ⇒ 违规不同 ⇒ 不该判打转。
    """
    session = _session(tmp_path)
    facts = SessionFacts(path=str(tmp_path / "facts.json"))
    plan = RewritePlan(section_id="introduction", direction="补充论证", details="")

    class _VaryingLLM(_CountingLLM):
        def complete(self, messages, **kw):
            self.calls += 1
            # 每轮引不同的坏标记 ⇒ 违规文本逐轮不同
            return LLMResponse(content=f"内容[bad{self.calls}]。")

    llm = _VaryingLLM("")
    provider = MockRetrievalProvider()
    result = execute_rewrite(
        session, plan, facts, llm,
        retrieval_provider=provider, verifier=CitationVerifier(provider),
        confirm=lambda _v, _c: False, max_retry=2,
    )
    assert not result.ok
    assert llm.calls == 3, f"违规在变化时应跑满 3 次，实际 {llm.calls} 次"
    assert "无法满足" not in result.message
