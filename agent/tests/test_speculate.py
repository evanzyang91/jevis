"""Speculative parallelism triggers and tiebreak rules."""

from __future__ import annotations

from agent.supervisor.speculate import choose_winner, should_speculate


def test_no_speculation_when_the_leader_is_clear() -> None:
    assert should_speculate({"CLICK": 0.9, "SCROLL_DOWN": 0.05, "WAIT": 0.05}) is None


def test_speculate_when_two_operations_are_close() -> None:
    pair = should_speculate({"CLICK": 0.5, "SCROLL_DOWN": 0.45, "WAIT": 0.05})
    assert pair is not None
    first, second = pair
    assert first.operation == "CLICK"
    assert second.operation == "SCROLL_DOWN"


def test_winner_prefers_a_tab_that_actually_changed() -> None:
    winner = choose_winner(
        primary={"page_changed": False, "element_count": 30},
        secondary={"page_changed": True, "element_count": 28},
    )
    assert winner == "secondary"


def test_winner_prefers_a_tab_that_grew_when_both_changed() -> None:
    winner = choose_winner(
        primary={"page_changed": True, "element_count": 10},
        secondary={"page_changed": True, "element_count": 40},
    )
    assert winner == "secondary"


def test_winner_defaults_to_primary_on_ties() -> None:
    winner = choose_winner(
        primary={"page_changed": True, "element_count": 20},
        secondary={"page_changed": True, "element_count": 20},
    )
    assert winner == "primary"
