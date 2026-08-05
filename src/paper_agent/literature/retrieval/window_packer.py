"""Deterministic, capacity-aware packing for batch relevance windows.

The packer is deliberately a pure mechanism: it receives final immutable
judge text, accounts for it, and groups it.  It never cleans, truncates, or
rehashes an item.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Sequence

from paper_agent.literature.relevance_runtime import PreparedRelevanceText
from paper_agent.literature.untrusted import wrap_as_data


TokenEstimator = Callable[[str], int]
_CANDIDATE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class WindowPackingError(ValueError):
    """Base error for invalid capacity or an item that cannot fit."""


class WindowConfigurationError(WindowPackingError):
    """The declared limits/cost inputs cannot form a valid window."""


class WindowItemTooLargeError(WindowPackingError):
    """A prepared item exceeds a window and must be re-prepared upstream."""

    def __init__(self, candidate_key: str, dimensions: tuple[str, ...]) -> None:
        self.candidate_key = candidate_key
        self.dimensions = dimensions
        super().__init__(
            f"prepared item {candidate_key!r} exceeds window budget: "
            + ", ".join(dimensions)
        )


@dataclass(frozen=True, kw_only=True)
class BatchInputBudget:
    max_input_chars: int
    max_output_chars: int
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_input_chars", "max_output_chars"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("max_input_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when provided")

    def canonical_payload(self) -> dict[str, int | None]:
        return {
            "max_input_chars": self.max_input_chars,
            "max_input_tokens": self.max_input_tokens,
            "max_output_chars": self.max_output_chars,
            "max_output_tokens": self.max_output_tokens,
        }

    def canonical_hash(self) -> str:
        raw = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, kw_only=True)
class WindowLimits:
    batch_size: int
    input_budget: BatchInputBudget

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")


@dataclass(frozen=True, kw_only=True)
class BatchInputCost:
    """Cost of the non-item prompt material in both active units."""

    chars: int
    tokens: int | None = None

    def __post_init__(self) -> None:
        if self.chars < 0:
            raise ValueError("chars must be non-negative")
        if self.tokens is not None and self.tokens < 0:
            raise ValueError("tokens must be non-negative when provided")


@dataclass(frozen=True, kw_only=True)
class WindowItem:
    candidate_key: str
    reference_id: str
    clean_title: str
    clean_abstract: str
    has_abstract: bool
    content_hash: str
    truncated: bool = False

    def __post_init__(self) -> None:
        if not _CANDIDATE_KEY.fullmatch(self.candidate_key):
            raise ValueError(
                "candidate_key must be 1-64 stable ASCII identifier characters"
            )
        if not self.reference_id:
            raise ValueError("reference_id must be non-empty")
        if self.has_abstract != bool(self.clean_abstract):
            raise ValueError("has_abstract must match clean_abstract")
        if not self.content_hash:
            raise ValueError("content_hash must be non-empty")

    @classmethod
    def from_prepared(
        cls,
        *,
        candidate_key: str,
        reference_id: str,
        prepared: PreparedRelevanceText,
    ) -> "WindowItem":
        return cls(
            candidate_key=candidate_key,
            reference_id=reference_id,
            clean_title=prepared.clean_title,
            clean_abstract=prepared.clean_abstract,
            has_abstract=prepared.has_abstract,
            content_hash=prepared.content_hash,
            truncated=prepared.truncated,
        )


@dataclass(frozen=True, kw_only=True)
class RelevanceWindow:
    items: tuple[WindowItem, ...]
    estimated_input_chars: int
    estimated_input_tokens: int | None
    truncated_items: int

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("RelevanceWindow.items must not be empty")


def render_window_item(item: WindowItem) -> str:
    """Render the canonical item fragment used for capacity accounting.

    BatchRelevanceJudge should reuse this function when constructing its user
    message so accounting cannot drift from the actual item envelope.
    """

    parts = [
        f"candidate_key={item.candidate_key}",
        wrap_as_data("title", item.clean_title),
    ]
    if item.has_abstract:
        parts.append(wrap_as_data("abstract", item.clean_abstract))
    else:
        parts.append("(no abstract; verdict is capped at weakly_relevant)")
    return "\n".join(parts) + "\n"


class WindowPacker:
    def __init__(
        self,
        *,
        limits: WindowLimits,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self._limits = limits
        self._token_estimator = token_estimator

    def pack(
        self,
        items: Sequence[WindowItem],
        *,
        base_prompt_cost: BatchInputCost | int,
    ) -> list[RelevanceWindow]:
        """Greedily pack items in input order under count and input limits.

        An integer base cost is the character-only convenience form.  When a
        token estimator and token limit are both active, callers must provide
        ``BatchInputCost`` with the token cost of system prompt, criteria,
        schema, and the results envelope.
        """

        if not items:
            return []
        base = self._normalize_base_cost(base_prompt_cost)
        self._validate_unique_keys(items)
        self._validate_base_fits(base)

        windows: list[RelevanceWindow] = []
        current: list[WindowItem] = []
        current_chars = base.chars
        current_tokens = base.tokens if self._token_limit_active else None

        for item in items:
            rendered = render_window_item(item)
            item_chars = len(rendered)
            item_tokens = self._estimate_tokens(rendered)

            if current and not self._fits(
                count=len(current) + 1,
                chars=current_chars + item_chars,
                tokens=self._add_tokens(current_tokens, item_tokens),
            ):
                windows.append(
                    self._make_window(current, current_chars, current_tokens)
                )
                current = []
                current_chars = base.chars
                current_tokens = base.tokens if self._token_limit_active else None

            proposed_chars = current_chars + item_chars
            proposed_tokens = self._add_tokens(current_tokens, item_tokens)
            if not self._fits(
                count=1,
                chars=proposed_chars,
                tokens=proposed_tokens,
            ):
                raise WindowItemTooLargeError(
                    item.candidate_key,
                    self._overflow_dimensions(proposed_chars, proposed_tokens),
                )

            current.append(item)
            current_chars = proposed_chars
            current_tokens = proposed_tokens

        if current:
            windows.append(self._make_window(current, current_chars, current_tokens))
        return windows

    @property
    def _token_limit_active(self) -> bool:
        return (
            self._token_estimator is not None
            and self._limits.input_budget.max_input_tokens is not None
        )

    def _normalize_base_cost(self, value: BatchInputCost | int) -> BatchInputCost:
        if isinstance(value, int):
            value = BatchInputCost(chars=value)
        if not isinstance(value, BatchInputCost):
            raise TypeError("base_prompt_cost must be BatchInputCost or int")
        if self._token_limit_active and value.tokens is None:
            raise WindowConfigurationError(
                "base prompt token cost is required when token capacity is active"
            )
        return value

    def _validate_base_fits(self, base: BatchInputCost) -> None:
        dimensions = self._overflow_dimensions(base.chars, base.tokens)
        if dimensions:
            raise WindowConfigurationError(
                "base prompt exceeds window budget: " + ", ".join(dimensions)
            )

    @staticmethod
    def _validate_unique_keys(items: Sequence[WindowItem]) -> None:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for item in items:
            if item.candidate_key in seen:
                duplicates.add(item.candidate_key)
            seen.add(item.candidate_key)
        if duplicates:
            raise WindowPackingError(
                "duplicate candidate_key: " + ", ".join(sorted(duplicates))
            )

    def _estimate_tokens(self, text: str) -> int | None:
        if not self._token_limit_active:
            return None
        assert self._token_estimator is not None
        value = self._token_estimator(text)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WindowConfigurationError(
                "token_estimator must return a non-negative integer"
            )
        return value

    @staticmethod
    def _add_tokens(left: int | None, right: int | None) -> int | None:
        if left is None or right is None:
            return None
        return left + right

    def _fits(self, *, count: int, chars: int, tokens: int | None) -> bool:
        budget = self._limits.input_budget
        return (
            count <= self._limits.batch_size
            and chars <= budget.max_input_chars
            and (
                not self._token_limit_active
                or (tokens is not None and tokens <= budget.max_input_tokens)
            )
        )

    def _overflow_dimensions(
        self, chars: int, tokens: int | None
    ) -> tuple[str, ...]:
        budget = self._limits.input_budget
        out: list[str] = []
        if chars > budget.max_input_chars:
            out.append("max_input_chars")
        if (
            self._token_limit_active
            and tokens is not None
            and tokens > budget.max_input_tokens
        ):
            out.append("max_input_tokens")
        return tuple(out)

    @staticmethod
    def _make_window(
        items: Sequence[WindowItem], chars: int, tokens: int | None
    ) -> RelevanceWindow:
        return RelevanceWindow(
            items=tuple(items),
            estimated_input_chars=chars,
            estimated_input_tokens=tokens,
            truncated_items=sum(item.truncated for item in items),
        )


__all__ = [
    "TokenEstimator",
    "BatchInputBudget",
    "BatchInputCost",
    "WindowLimits",
    "WindowItem",
    "RelevanceWindow",
    "WindowPacker",
    "WindowPackingError",
    "WindowConfigurationError",
    "WindowItemTooLargeError",
    "render_window_item",
]
