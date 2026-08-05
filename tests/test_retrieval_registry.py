"""A2c-2 wave 1 / Task 2：ProviderRegistry 能力一致性校验 + 类型选源。"""

from __future__ import annotations

import pytest

from paper_agent.literature.retrieval.registry import (
    ProviderRegistry,
    RegistryError,
    named,
)
from paper_agent.providers.retrieval.capabilities import FetchResult, FetchStatus


class _Search:
    def search(self, query, *, limit=10, filters=()):
        return []


class _Verify:
    def fetch_metadata(self, identifier_type, identifier):
        return FetchResult(FetchStatus.NOT_FOUND_AT_SOURCE)

    def search_bibliographic(self, query, *, limit=5):
        return []


# ---------------------------------------------------------------- 能力一致性

def test_valid_registry():
    reg = ProviderRegistry(
        search_sources=(named("openalex", _Search()),),
        verify_sources=(named("crossref", _Verify()), named("openalex", _Verify())),
    )
    assert len(reg.search_sources) == 1
    assert reg.discovery_sources == ()


def test_search_source_without_search_capability_rejected():
    # crossref profile 只有 VERIFY 角色 → 放进 search_sources 应报错
    with pytest.raises(RegistryError):
        ProviderRegistry(search_sources=(named("crossref", _Search()),))


def test_verify_source_not_conforming_protocol_rejected():
    with pytest.raises(RegistryError):
        ProviderRegistry(verify_sources=(named("crossref", _Search()),))  # _Search 无 fetch_metadata


def test_unknown_source_has_no_verify_role():
    # 未知源兜底档案无 VERIFY 角色 → 放进 verify_sources 报错（不默认授信）
    with pytest.raises(RegistryError):
        ProviderRegistry(verify_sources=(named("mystery_source", _Verify()),))


# ---------------------------------------------------------------- 类型选源

def test_verify_providers_for_doi_final_first():
    reg = ProviderRegistry(
        verify_sources=(named("openalex", _Verify()), named("crossref", _Verify())),
    )
    picks = reg.verify_providers_for("doi")
    names = [n for n, _ in picks]
    # crossref(final) 排在 openalex(corroboration) 之前
    assert names[0] == "crossref"
    assert "openalex" in names


def test_verify_providers_for_arxiv():
    reg = ProviderRegistry(verify_sources=(named("arxiv", _Verify()),))
    picks = reg.verify_providers_for("arxiv")
    assert [n for n, _ in picks] == ["arxiv"]        # arxiv 对自身 id final
