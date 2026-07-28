"""A1 wave 8：search_and_verify 接线 Ledger + config 项。"""

from __future__ import annotations

from paper_agent.config import Config, load_config
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.session.facts import SessionFacts
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.literature import search_and_verify


def _fresh():
    return SessionFacts(path="x"), MockRetrievalProvider(), CitationVerifier(MockRetrievalProvider())


def test_search_and_verify_preserves_behavior():
    f, prov, ver = _fresh()
    out = search_and_verify("transformers", 5, prov, ver, f)
    assert out.verified == 2 and len(out.added) == 2
    assert all(r.verified for r in f.known_refs)
    assert len(f.bibkeys()) == 2


def test_search_and_verify_idempotent_dedup():
    f, prov, ver = _fresh()
    search_and_verify("t", 5, prov, ver, f)
    out2 = search_and_verify("t", 5, prov, ver, f)   # 同 arxiv id → 合并不新增
    assert out2.verified == 2 and len(out2.added) == 0
    assert len(f.bibkeys()) == 2


def test_search_and_verify_bibkeys_stable():
    f, prov, ver = _fresh()
    search_and_verify("t", 5, prov, ver, f)
    assert f.bibkeys() == {"vaswani2017attention", "kinney2023semantic"}


def test_config_literature_defaults_preserve_behavior():
    c = Config()
    assert c.literature_strict_verification is False
    assert c.literature_dedup_suggestions_enabled is True
    assert c.literature_require_final_verification is False
    assert c.literature_title_similarity == 0.85
    assert c.literature_source_priority == "openalex,semantic_scholar,arxiv"


def test_load_config_literature_env(monkeypatch):
    monkeypatch.setenv("PAPER_LIT_STRICT_VERIFY", "true")
    monkeypatch.setenv("PAPER_LIT_REQUIRE_FINAL_VERIFY", "1")
    monkeypatch.setenv("PAPER_LIT_TITLE_SIM", "0.9")
    c = load_config()
    assert c.literature_strict_verification is True
    assert c.literature_require_final_verification is True
    assert c.literature_title_similarity == 0.9
