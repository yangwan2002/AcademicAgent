"""Runtime-neutral relevance text preparation for throughput paths.

The legacy per-item judge owns the canonical cleaning and content-hash
semantics.  This module makes the field limits explicit and versioned for
batch runtimes while preserving the legacy defaults exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from paper_agent.literature.relevance import relevance_content_hash
from paper_agent.literature.untrusted import clean_external_text


LEGACY_TITLE_MAX_CHARS = 1000
LEGACY_ABSTRACT_MAX_CHARS = 4000
LEGACY_TEXT_POLICY_VERSION = "relevance-text-legacy-v1"


@dataclass(frozen=True)
class RelevanceTextPolicy:
    """Versioned limits applied before a relevance content hash is made.

    Batch policies may be stricter than the per-item legacy limits, but may
    not silently enlarge them.  Enlarging would make the same content hash
    mean different judge input on the two paths.
    """

    version: str = LEGACY_TEXT_POLICY_VERSION
    max_title_chars: int = LEGACY_TITLE_MAX_CHARS
    max_abstract_chars: int = LEGACY_ABSTRACT_MAX_CHARS

    def __post_init__(self) -> None:
        if not self.version or not self.version.strip():
            raise ValueError("RelevanceTextPolicy.version must be non-empty")
        if not 1 <= self.max_title_chars <= LEGACY_TITLE_MAX_CHARS:
            raise ValueError(
                f"max_title_chars must be in [1, {LEGACY_TITLE_MAX_CHARS}]"
            )
        if not 1 <= self.max_abstract_chars <= LEGACY_ABSTRACT_MAX_CHARS:
            raise ValueError(
                f"max_abstract_chars must be in [1, {LEGACY_ABSTRACT_MAX_CHARS}]"
            )


LEGACY_RELEVANCE_TEXT_POLICY = RelevanceTextPolicy()


@dataclass(frozen=True)
class PreparedRelevanceText:
    """Final immutable text that a relevance judge is allowed to receive."""

    clean_title: str
    clean_abstract: str
    basis: str
    content_hash: str
    truncated: bool

    @property
    def has_abstract(self) -> bool:
        return bool(self.clean_abstract)


def prepare_relevance_text(
    title: str,
    abstract: str,
    policy: RelevanceTextPolicy = LEGACY_RELEVANCE_TEXT_POLICY,
) -> PreparedRelevanceText:
    """Clean and truncate text, then hash the exact final judge input.

    ``relevance_content_hash`` remains the single canonical hash function.
    Re-cleaning the already-clean fields is idempotent, and policy limits are
    never larger than the legacy limits, so no second truncation can occur.
    """

    full_title = clean_external_text(title or "", max_chars=None)
    full_abstract = clean_external_text(abstract or "", max_chars=None)
    clean_title = full_title[: policy.max_title_chars]
    clean_abstract = full_abstract[: policy.max_abstract_chars]
    has_abstract = bool(clean_abstract)
    return PreparedRelevanceText(
        clean_title=clean_title,
        clean_abstract=clean_abstract,
        basis="title_and_abstract" if has_abstract else "title_only",
        content_hash=relevance_content_hash(clean_title, clean_abstract),
        truncated=(
            len(full_title) > policy.max_title_chars
            or len(full_abstract) > policy.max_abstract_chars
        ),
    )


@dataclass(frozen=True)
class ActiveRelevanceRuntime:
    """Frozen operation policy shared by attempt, preflight and bundle reads."""

    evaluator: object
    text_policy: RelevanceTextPolicy
    window_limits: object
    token_estimator: Callable[[str], int] | None = None

    def __post_init__(self) -> None:
        if getattr(self.evaluator, "text_policy", None) != self.text_policy:
            raise ValueError("evaluator and runtime must share one text policy")
        evaluator_budget = getattr(self.evaluator, "input_budget", None)
        limits_budget = getattr(self.window_limits, "input_budget", None)
        if evaluator_budget != limits_budget:
            raise ValueError("evaluator and runtime must share one input budget")

    @property
    def judge_key(self) -> str:
        return self.evaluator.judge_version.key()

    def prepare(self, title: str, abstract: str) -> PreparedRelevanceText:
        return prepare_relevance_text(title, abstract, self.text_policy)

    def content_hash(self, title: str, abstract: str) -> str:
        return self.prepare(title, abstract).content_hash

    def make_window_item(
        self,
        *,
        candidate_key: str,
        reference_id: str,
        title: str,
        abstract: str,
    ):
        from paper_agent.literature.retrieval.window_packer import WindowItem

        return WindowItem.from_prepared(
            candidate_key=candidate_key,
            reference_id=reference_id,
            prepared=self.prepare(title, abstract),
        )

    def pack(self, items: Sequence, criteria) -> list:
        from paper_agent.literature.relevance_batch import batch_base_prompt_material
        from paper_agent.literature.retrieval.window_packer import (
            BatchInputCost,
            WindowPacker,
        )

        material = batch_base_prompt_material(criteria)
        token_cost = (
            self.token_estimator(material) if self.token_estimator is not None else None
        )
        return WindowPacker(
            limits=self.window_limits,
            token_estimator=self.token_estimator,
        ).pack(
            items,
            base_prompt_cost=BatchInputCost(
                chars=len(material),
                tokens=token_cost,
            ),
        )

    def assess_window_with_stats(self, window, criteria, *, call_control, **kwargs):
        return self.evaluator.assess_window_with_stats(
            window, criteria, call_control=call_control, **kwargs
        )


__all__ = [
    "LEGACY_TITLE_MAX_CHARS",
    "LEGACY_ABSTRACT_MAX_CHARS",
    "LEGACY_TEXT_POLICY_VERSION",
    "LEGACY_RELEVANCE_TEXT_POLICY",
    "RelevanceTextPolicy",
    "PreparedRelevanceText",
    "prepare_relevance_text",
    "ActiveRelevanceRuntime",
]
