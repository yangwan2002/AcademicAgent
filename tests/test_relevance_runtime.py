"""Task 2 relevance text policy tests."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from paper_agent.literature.relevance import (
    clean_relevance_fields,
    relevance_content_hash,
)
from paper_agent.literature.relevance_runtime import (
    LEGACY_RELEVANCE_TEXT_POLICY,
    RelevanceTextPolicy,
    prepare_relevance_text,
)
from paper_agent.literature.untrusted import clean_external_text


def test_legacy_policy_is_byte_compatible_with_existing_clean_and_hash():
    samples = [
        ("<b>Title &amp; Method</b>", "First\tline.\n\n\nSecond line."),
        ("e\u0301" * 1200, "摘要" * 2500),
        ("plain title", ""),
        ("control\x00 title", "  whitespace   abstract  "),
    ]
    for title, abstract in samples:
        prepared = prepare_relevance_text(
            title, abstract, LEGACY_RELEVANCE_TEXT_POLICY
        )
        old_title, old_abstract, old_has_abstract = clean_relevance_fields(
            title, abstract
        )
        assert (prepared.clean_title, prepared.clean_abstract) == (
            old_title,
            old_abstract,
        )
        assert prepared.has_abstract is old_has_abstract
        assert prepared.content_hash == relevance_content_hash(title, abstract)


def test_custom_policy_truncates_before_hash_and_reports_basis():
    policy = RelevanceTextPolicy(
        version="batch-short-v1", max_title_chars=5, max_abstract_chars=7
    )
    prepared = prepare_relevance_text("<b>123456</b>", "abcdefghij", policy)
    assert prepared.clean_title == "12345"
    assert prepared.clean_abstract == "abcdefg"
    assert prepared.basis == "title_and_abstract"
    assert prepared.truncated is True
    assert prepared.content_hash == relevance_content_hash("12345", "abcdefg")


def test_truncation_is_measured_after_cleaning_not_from_raw_markup_length():
    policy = RelevanceTextPolicy(
        version="clean-first-v1", max_title_chars=2, max_abstract_chars=10
    )
    prepared = prepare_relevance_text("<strong>A</strong>", "", policy)
    assert prepared.clean_title == "A"
    assert prepared.basis == "title_only"
    assert prepared.truncated is False


def test_policy_output_is_clean_nfc_and_immutable():
    policy = RelevanceTextPolicy(
        version="nfc-v1", max_title_chars=10, max_abstract_chars=10
    )
    prepared = prepare_relevance_text("e\u0301", "<i>x</i>", policy)
    assert prepared.clean_title == clean_external_text("e\u0301", max_chars=10)
    assert prepared.clean_abstract == "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        prepared.clean_title = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"version": ""},
        {"max_title_chars": 0},
        {"max_title_chars": 1001},
        {"max_abstract_chars": 0},
        {"max_abstract_chars": 4001},
    ],
)
def test_policy_rejects_ambiguous_or_legacy_incompatible_limits(kwargs):
    with pytest.raises(ValueError):
        RelevanceTextPolicy(**kwargs)


def test_relevance_runtime_obeys_literature_architecture_boundary():
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "paper_agent"
        / "literature"
        / "relevance_runtime.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    forbidden = ("paper_agent.tasking", "paper_agent.application", "paper_agent.config")
    assert not any(name.startswith(forbidden) for name in imports)

