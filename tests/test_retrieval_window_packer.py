"""Task 2 deterministic WindowPacker tests."""

from __future__ import annotations

import ast
import dataclasses
import random
from pathlib import Path

import pytest

from paper_agent.literature.relevance_runtime import (
    RelevanceTextPolicy,
    prepare_relevance_text,
)
from paper_agent.literature.retrieval.window_packer import (
    BatchInputBudget,
    BatchInputCost,
    WindowConfigurationError,
    WindowItem,
    WindowItemTooLargeError,
    WindowLimits,
    WindowPacker,
    WindowPackingError,
    render_window_item,
)


def _item(index: int, *, title_len: int = 5, abstract_len: int = 10) -> WindowItem:
    prepared = prepare_relevance_text(
        "T" * title_len,
        "A" * abstract_len,
        RelevanceTextPolicy(
            version="packer-test-v1",
            max_title_chars=100,
            max_abstract_chars=200,
        ),
    )
    return WindowItem.from_prepared(
        candidate_key=f"c{index}", reference_id=f"r{index}", prepared=prepared
    )


def _limits(*, batch_size: int = 8, chars: int = 100_000, tokens=None):
    return WindowLimits(
        batch_size=batch_size,
        input_budget=BatchInputBudget(
            max_input_chars=chars,
            max_input_tokens=tokens,
            max_output_chars=20_000,
            max_output_tokens=4_000,
        ),
    )


def _keys(windows):
    return [[item.candidate_key for item in window.items] for window in windows]


def test_empty_input_produces_no_window():
    assert WindowPacker(limits=_limits()).pack([], base_prompt_cost=10) == []


def test_exact_character_boundary_fits_and_one_more_splits():
    first, second = _item(0), _item(1)
    base = 37
    exact = base + len(render_window_item(first)) + len(render_window_item(second))
    packed = WindowPacker(limits=_limits(chars=exact)).pack(
        [first, second], base_prompt_cost=base
    )
    assert _keys(packed) == [["c0", "c1"]]
    assert packed[0].estimated_input_chars == exact

    split = WindowPacker(limits=_limits(chars=exact - 1)).pack(
        [first, second], base_prompt_cost=base
    )
    assert _keys(split) == [["c0"], ["c1"]]


def test_batch_size_and_input_order_are_hard_constraints():
    items = [_item(i) for i in range(5)]
    windows = WindowPacker(limits=_limits(batch_size=2)).pack(
        items, base_prompt_cost=0
    )
    assert _keys(windows) == [["c0", "c1"], ["c2", "c3"], ["c4"]]
    assert all(len(window.items) <= 2 for window in windows)


def test_batch_size_one_is_deterministic_per_item_mode():
    items = [_item(i) for i in range(4)]
    windows = WindowPacker(limits=_limits(batch_size=1)).pack(
        items, base_prompt_cost=0
    )
    assert _keys(windows) == [["c0"], ["c1"], ["c2"], ["c3"]]


def test_token_and_character_limits_are_both_enforced():
    items = [_item(i, abstract_len=30) for i in range(3)]

    def fake_tokens(text: str) -> int:
        return len(text) // 10 + 1

    item_tokens = [fake_tokens(render_window_item(item)) for item in items]
    token_cap = 3 + item_tokens[0] + item_tokens[1] - 1
    windows = WindowPacker(
        limits=_limits(batch_size=3, chars=100_000, tokens=token_cap),
        token_estimator=fake_tokens,
    ).pack(items, base_prompt_cost=BatchInputCost(chars=20, tokens=3))
    assert _keys(windows) == [["c0"], ["c1"], ["c2"]]
    assert all(window.estimated_input_tokens <= token_cap for window in windows)


def test_character_fallback_works_without_tokenizer():
    items = [_item(0), _item(1)]
    char_cap = len(render_window_item(items[0])) + 1
    windows = WindowPacker(
        limits=_limits(chars=char_cap, tokens=1), token_estimator=None
    ).pack(items, base_prompt_cost=0)
    assert _keys(windows) == [["c0"], ["c1"]]
    assert all(window.estimated_input_tokens is None for window in windows)


def test_token_mode_requires_base_prompt_cost_in_both_units():
    packer = WindowPacker(
        limits=_limits(tokens=1000), token_estimator=lambda text: len(text)
    )
    with pytest.raises(WindowConfigurationError, match="base prompt token cost"):
        packer.pack([_item(0)], base_prompt_cost=10)


def test_prepared_item_is_never_truncated_or_mutated_by_packer():
    prepared = prepare_relevance_text(
        "123456",
        "abcdefgh",
        RelevanceTextPolicy(
            version="truncate-v1", max_title_chars=5, max_abstract_chars=7
        ),
    )
    item = WindowItem.from_prepared(
        candidate_key="c0", reference_id="r0", prepared=prepared
    )
    before = dataclasses.asdict(item)
    window = WindowPacker(limits=_limits()).pack(
        [item], base_prompt_cost=0
    )[0]
    assert dataclasses.asdict(item) == before
    assert window.items[0] is item
    assert window.truncated_items == 1


def test_single_oversized_item_raises_typed_error_instead_of_overflowing():
    item = _item(0, abstract_len=100)
    cap = len(render_window_item(item)) - 1
    with pytest.raises(WindowItemTooLargeError) as exc:
        WindowPacker(limits=_limits(chars=cap)).pack(
            [item], base_prompt_cost=0
        )
    assert exc.value.candidate_key == "c0"
    assert exc.value.dimensions == ("max_input_chars",)


def test_base_prompt_alone_cannot_exceed_capacity():
    with pytest.raises(WindowConfigurationError, match="base prompt"):
        WindowPacker(limits=_limits(chars=10)).pack(
            [_item(0)], base_prompt_cost=11
        )


def test_duplicate_candidate_keys_are_rejected_before_packing():
    first = _item(0)
    second = dataclasses.replace(first, reference_id="r-other")
    with pytest.raises(WindowPackingError, match="duplicate candidate_key"):
        WindowPacker(limits=_limits()).pack(
            [first, second], base_prompt_cost=0
        )


def test_randomized_packing_is_repeatable_and_preserves_all_items():
    rng = random.Random(20260731)
    items = [
        _item(i, title_len=rng.randint(1, 40), abstract_len=rng.randint(1, 100))
        for i in range(100)
    ]
    largest = max(len(render_window_item(item)) for item in items)
    limits = _limits(batch_size=7, chars=largest * 3 + 50)
    first = WindowPacker(limits=limits).pack(items, base_prompt_cost=17)
    second = WindowPacker(limits=limits).pack(items, base_prompt_cost=17)
    expected = [f"c{i}" for i in range(100)]
    assert [key for window in _keys(first) for key in window] == expected
    assert _keys(first) == _keys(second)
    assert all(len(window.items) <= 7 for window in first)
    assert all(window.estimated_input_chars <= limits.input_budget.max_input_chars for window in first)


def test_budget_hash_is_canonical_and_changes_with_capacity():
    first = _limits(chars=1000).input_budget
    same = _limits(chars=1000).input_budget
    changed = _limits(chars=1001).input_budget
    assert first.canonical_hash() == same.canonical_hash()
    assert first.canonical_hash() != changed.canonical_hash()
    assert len(first.canonical_hash()) == 64


def test_invalid_token_estimator_fails_explicitly():
    packer = WindowPacker(
        limits=_limits(tokens=1000), token_estimator=lambda text: -1
    )
    with pytest.raises(WindowConfigurationError, match="token_estimator"):
        packer.pack(
            [_item(0)], base_prompt_cost=BatchInputCost(chars=0, tokens=0)
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: BatchInputBudget(max_input_chars=0, max_output_chars=1),
        lambda: BatchInputBudget(max_input_chars=1, max_output_chars=0),
        lambda: BatchInputBudget(
            max_input_chars=1, max_output_chars=1, max_input_tokens=0
        ),
        lambda: WindowLimits(
            batch_size=0,
            input_budget=BatchInputBudget(
                max_input_chars=1, max_output_chars=1
            ),
        ),
    ],
)
def test_invalid_limits_fail_at_construction(factory):
    with pytest.raises(ValueError):
        factory()


def test_window_packer_obeys_literature_architecture_boundary():
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "paper_agent"
        / "literature"
        / "retrieval"
        / "window_packer.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    forbidden = (
        "paper_agent.tasking",
        "paper_agent.application",
        "paper_agent.session",
        "paper_agent.config",
    )
    assert not any(name.startswith(forbidden) for name in imports)

