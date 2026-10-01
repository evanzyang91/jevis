"""Guard rules as pure functions."""

from __future__ import annotations

from agent.supervisor import (
    HistoryEntry,
    StaleTracker,
    already_taken,
    cycling_labels,
    inert_labels,
    is_blocked_tail,
    stale_over_limit,
    url_cycling,
)


def _entry(step: int, *, marker: str = "m1", label: str = "Buy", action_id: str = "a1",
           page_changed: bool = True, url_changed: bool | None = None,
           operation: str = "CLICK", url: str = "") -> HistoryEntry:
    return HistoryEntry(
        step=step,
        operation=operation,
        target=None,
        action_id=action_id,
        action_label=label,
        marker=marker,
        page_changed=page_changed,
        # url_changed defaults to mirror page_changed so pre-existing tests
        # that only track page-level progress read as "real navigation".
        url_changed=page_changed if url_changed is None else url_changed,
        url=url,
    )


def test_cycling_flags_a_two_way_rotation() -> None:
    history = [
        _entry(i, label="A" if i % 2 == 0 else "B", action_id=f"a{i}")
        for i in range(6)
    ]
    assert cycling_labels(history) == frozenset({"A", "B"})


def test_cycling_ignores_a_single_repeated_label() -> None:
    history = [_entry(i, label="Add to cart", action_id=f"a{i}") for i in range(6)]
    assert cycling_labels(history) == frozenset()


def test_inert_marks_labels_with_three_no_change_hits_in_a_row() -> None:
    history = [
        _entry(1, label="Scroll", page_changed=False, action_id="a1"),
        _entry(2, label="Scroll", page_changed=False, action_id="a2"),
        _entry(3, label="Scroll", page_changed=False, action_id="a3"),
    ]
    assert "Scroll" in inert_labels(history)


def test_inert_resets_counter_after_page_change() -> None:
    history = [
        _entry(1, label="Scroll", page_changed=False, action_id="a1"),
        _entry(2, label="Scroll", page_changed=True, url_changed=True, action_id="a2"),
        _entry(3, label="Scroll", page_changed=False, action_id="a3"),
    ]
    assert "Scroll" not in inert_labels(history)


def test_repeated_label_exempts_type_text_so_multi_item_search_reuses_survive() -> None:
    """A multi-item shopping run fills Search once per item and clicks the
    Search button to submit. Grouping fill Search with click Search (both
    labelled 'Search') under a repeats=3 threshold banned Search after the
    second egg add — the run then couldn't submit 'butter' and clicked a
    stray Gray Ridge Eggs product from the still-visible eggs page."""
    history = [
        _entry(1, operation="TYPE_TEXT", label="Search", page_changed=True, url_changed=False),
        _entry(2, operation="CLICK", label="Search", page_changed=True, url_changed=False),
        _entry(3, operation="TYPE_TEXT", label="Search", page_changed=True, url_changed=False),
    ]
    from agent.supervisor import repeated_label
    assert "Search" not in repeated_label(history)


def test_inert_exempts_type_text_because_fill_never_url_changes() -> None:
    """A multi-item shopping run reuses the Search field once per item.
    Every fill is url_changed=False (fill only preps the input; the followup
    click or Enter navigates). Counting fills as inert would ban Search
    after the second search — the very regression that stranded a run on
    Walmart's post-add-to-cart modal."""
    history = [
        _entry(1, operation="TYPE_TEXT", label="Search", page_changed=True, url_changed=False),
        _entry(2, operation="TYPE_TEXT", label="Search", page_changed=True, url_changed=False),
        _entry(3, operation="TYPE_TEXT", label="Search", page_changed=True, url_changed=False),
    ]
    assert "Search" not in inert_labels(history)


def test_inert_bans_a_label_whose_page_changes_but_url_does_not() -> None:
    """A dropdown that toggles open/closed bumps the marker without moving
    the URL. Old jevis missed this class and my rewrite did too — the rule
    is now URL-based so a toggling label is caught after three uses."""
    history = [
        _entry(1, label="Language English", page_changed=True, url_changed=False),
        _entry(2, label="Language English", page_changed=True, url_changed=False),
        _entry(3, label="Language English", page_changed=True, url_changed=False),
    ]
    assert "Language English" in inert_labels(history)


def test_already_taken_returns_streak_of_unchanged_marker_entries() -> None:
    # Tail streak of same-marker, page_changed=False entries. Matches old
    # jevis's `streak` — bans only what the run has just tried at this
    # exact state, resets the moment the page moves.
    history = [
        _entry(1, marker="m1", label="Buy", action_id="x", page_changed=True),
        _entry(2, marker="m1", label="Save", action_id="z", page_changed=False),
        _entry(3, marker="m1", label="Cart", action_id="y", page_changed=False),
    ]
    assert already_taken(history, "m1") == frozenset({"z", "y", "Save", "Cart"})


def test_already_taken_resets_when_marker_moves() -> None:
    # A different marker breaks the streak: the run has moved on and the
    # tail of same-marker unchanged entries starts empty.
    history = [
        _entry(1, marker="m1", label="Buy", action_id="x", page_changed=False),
    ]
    assert already_taken(history, "m2") == frozenset()


def test_stale_tracker_counts_repeats_and_resets_on_progress() -> None:
    tracker = StaleTracker()
    for _ in range(3):
        tracker = tracker.bump("m1", 0)
    assert tracker.streak == 3
    tracker = tracker.bump("m2", 0)
    assert tracker.streak == 1


def test_stale_tracker_flags_over_limit() -> None:
    tracker = StaleTracker()
    for _ in range(8):
        tracker = tracker.bump("m1", 0)
    assert stale_over_limit(tracker) is True


def test_is_blocked_tail_detects_a_dead_run() -> None:
    history = [
        _entry(i, marker="m", label=f"a{i}", action_id=f"a{i}", page_changed=False, operation="CLICK")
        for i in range(1, 5)
    ]
    assert is_blocked_tail(history) is True


def test_is_blocked_tail_lets_a_wait_extend_a_streak() -> None:
    history = [
        _entry(1, page_changed=False, operation="WAIT"),
        _entry(2, page_changed=False, operation="CLICK"),
        _entry(3, page_changed=False, operation="CLICK"),
        _entry(4, page_changed=False, operation="CLICK"),
    ]
    assert is_blocked_tail(history) is False


def test_url_cycling_flags_a_two_url_bounce() -> None:
    # Model bouncing between /cart and /search — the exact pattern that
    # ate 48 turns of the cake-ingredients run without any other guard
    # noticing (marker moved every step, labels varied).
    history = [
        _entry(i, url=("/cart" if i % 2 == 0 else "/search"), url_changed=True, action_id=f"a{i}")
        for i in range(12)
    ]
    assert url_cycling(history) is True


def test_url_cycling_ignores_progress_across_new_urls() -> None:
    # Six URL-changing steps that keep reaching new pages is real progress,
    # not a cycle.
    history = [
        _entry(i, url=f"/page-{i}", url_changed=True, action_id=f"a{i}")
        for i in range(6)
    ]
    assert url_cycling(history) is False


def test_url_cycling_ignores_url_inert_activity() -> None:
    # Fills and clicks that don't change the URL don't count toward the
    # cycle detection — that's what other guards handle.
    history = [_entry(i, page_changed=False, url_changed=False, action_id=f"a{i}") for i in range(12)]
    assert url_cycling(history) is False
