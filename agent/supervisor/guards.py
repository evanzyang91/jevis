"""Loop guards as pure functions.

Every rule that ends a run early or refuses an action lives here. Pure inputs,
pure outputs — the supervisor calls each in turn. Tests can construct any
history and assert on the outcome without running a browser.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    step: int
    operation: str
    target: str | None
    action_id: str
    action_label: str
    marker: str
    page_changed: bool
    url_changed: bool
    # What was typed, for TYPE_TEXT actions. The policy needs it to remember
    # earlier searches on multi-step goals ("I typed 'flour' at step 3, so at
    # step 12 I should not search 'flour' again").
    text: str | None = None
    # URL after the action landed. Used by `url_cycling` to detect a run
    # that keeps bouncing between the same two or three pages.
    url: str = ""


# ---- Cycle detection --------------------------------------------------------


def cycling_labels(history: Sequence[HistoryEntry], window: int = 8, repeats: int = 5) -> frozenset[str]:
    """Labels stuck in a two- or three-way rotation.

    A page whose markers change every step but keeps repeating the same two or
    three actions is what the fingerprint-keyed history cannot see. Requiring
    two-plus distinct labels excludes real progress on Add-to-cart or Next-page.

    `repeats=5` catches shorter oscillations (open/close/open/close/open) that
    an 8-window at 6 repeats would miss.
    """
    tail = list(history[-window:])
    if len(tail) < repeats:
        return frozenset()
    distinct = {entry.action_label for entry in tail}
    return frozenset(distinct) if 2 <= len(distinct) <= 3 else frozenset()


def repeated_label(history: Sequence[HistoryEntry], window: int = 5, repeats: int = 3) -> frozenset[str]:
    """One label re-executed too many times inside a short window, without
    any of those uses moving the URL forward.

    Catches a mono-label loop — clicking Search Go on a results page that just
    resubmits, hammering an inert Add button, or clicking a dropdown toggle
    that opens and closes the same menu. Legitimate re-use is fine: a
    multi-item shopping goal hits Search once per item and each click changes
    the URL, so the ban does not fire.

    URL change is the productive signal — a page_changed=True dropdown toggle
    is DOM churn without progress. Using page_changed here immunises a
    toggling label and lets an open/close/open/close loop run untouched.

    TYPE_TEXT, Scroll, and Wait are exempt: fill inherently never URL-changes
    (the follow-up click/Enter navigates), and grouping fill Search with
    click Search (button) under the same label banned Search after two
    searches on the same input — a multi-item shopping run needs Search
    reusable across every item.
    """
    tail = [
        entry for entry in history[-window:]
        if entry.operation not in {"TYPE_TEXT", "WAIT", "SCROLL_UP", "SCROLL_DOWN"}
    ]
    if len(tail) < repeats:
        return frozenset()
    counts: dict[str, int] = {}
    productive: set[str] = set()
    for entry in tail:
        counts[entry.action_label] = counts.get(entry.action_label, 0) + 1
        if entry.url_changed:
            productive.add(entry.action_label)
    return frozenset(
        label for label, count in counts.items()
        if count >= repeats and label not in productive
    )


# ---- Inert actions ----------------------------------------------------------


def inert_labels(
    history: Sequence[HistoryEntry], threshold: int = 2, window: int = 20,
) -> frozenset[str]:
    """Labels with `threshold` url-unchanged uses inside `window` steps,
    with no url-moving use in between to reset the count.

    URL change is the productive signal, not page_changed — a dropdown that
    toggles open/closed changes the marker without progressing the task, and
    counting that as "changed" hides the loop from every guard.

    Window (20) is decoupled from threshold so a label reused every few steps
    still gets caught. A short window at threshold=2 missed labels spaced
    ~5 steps apart because only one hit fell inside the window at a time.

    TYPE_TEXT, SCROLL_*, WAIT are all exempt from scoring: fill inherently
    never URL-changes (it's the follow-up click/Enter that navigates), and
    counting fills as inert bans the same Search field a multi-item shopping
    run has to reuse. Scroll and Wait have the same trait — never navigate
    on their own — and the ban would only stop legitimate use.
    """
    exempt = {"TYPE_TEXT", "WAIT", "SCROLL_UP", "SCROLL_DOWN"}
    counts: dict[str, int] = {}
    for entry in history[-window:]:
        if entry.operation in exempt:
            continue
        if entry.url_changed:
            counts[entry.action_label] = 0
        else:
            counts[entry.action_label] = counts.get(entry.action_label, 0) + 1
    return frozenset(label for label, count in counts.items() if count >= threshold)


# ---- Covered / taken on this exact page -------------------------------------


def already_taken(history: Sequence[HistoryEntry], marker: str) -> frozenset[str]:
    """Labels and ids from the tail streak of unchanged-page actions at the
    current marker. Reset by any page_changed=True entry.

    Matches old jevis's `streak` behaviour: only bans actions the run just
    tried on this exact state and got no response from. Once the page
    changes (marker moves), the streak resets and the ban lifts — the
    model gets its full choice set back. This is a much narrower ban than
    a URL-keyed variant, which would over-ban legitimate re-uses of the
    same button on a multi-item shopping page.
    """
    streak: list[HistoryEntry] = []
    for entry in reversed(history):
        if entry.page_changed:
            break
        if entry.marker != marker:
            break
        streak.append(entry)
    ids = {entry.action_id for entry in streak}
    labels = {entry.action_label for entry in streak}
    return frozenset(ids | labels)


# ---- Stale streak on unchanged page -----------------------------------------


@dataclass(frozen=True, slots=True)
class StaleTracker:
    """Counts consecutive discarded decisions against an unchanged page marker.

    The supervisor updates one instance per run. `bump` returns a new tracker;
    the caller writes the new value back to its state so the type stays
    frozen.
    """

    marker: str | None = None
    streak: int = 0
    known: int = 0

    def bump(self, marker: str, learned: int) -> "StaleTracker":
        same = marker == self.marker and learned <= self.known
        streak = self.streak + 1 if same else 1
        return StaleTracker(marker=marker, streak=streak, known=learned)

    def reset(self) -> "StaleTracker":
        return StaleTracker()


STALE_STREAK_LIMIT = 8


def stale_over_limit(tracker: StaleTracker) -> bool:
    return tracker.streak >= STALE_STREAK_LIMIT


# ---- Blocked-run detection --------------------------------------------------


def is_blocked_tail(history: Sequence[HistoryEntry], span: int = 4) -> bool:
    """Every action in the last `span` steps changed nothing and stayed on
    one URL. That is a run that has run out of moves; the supervisor blocks."""
    tail = list(history[-span:])
    if len(tail) < span:
        return False
    if any(entry.page_changed for entry in tail):
        return False
    if any(entry.operation == "WAIT" for entry in tail):
        return False
    return len({entry.marker for entry in tail}) <= 1


def url_cycling(
    history: Sequence[HistoryEntry],
    window: int = 12,
    min_repeats: int = 3,
    max_urls: int = 3,
) -> bool:
    """The run is bouncing between the same few URLs without breaking out.

    Every URL-changing step in the last `window` entries only reaches one
    of at most `max_urls` distinct URLs, and each of those URLs has been
    visited at least `min_repeats` times. That is a mechanical cycle — the
    model is verifying, hesitating, or exploring the same loop, and every
    other guard (marker-based, label-based) misses it because the marker
    and labels legitimately change each step.

    Fires on any task, not just shopping: a research run that keeps flipping
    between an article and its search page hits the same pattern.
    """
    nav = [entry for entry in history[-window:] if entry.url_changed and entry.url]
    if len(nav) < min_repeats * 2:
        return False
    urls: dict[str, int] = {}
    for entry in nav:
        urls[entry.url] = urls.get(entry.url, 0) + 1
    if len(urls) > max_urls:
        return False
    return all(count >= min_repeats for count in urls.values())


# ---- Ban set ----------------------------------------------------------------


def combined_ban(
    history: Sequence[HistoryEntry],
    marker: str,
    *,
    covered: Iterable[str] = (),
) -> set[str]:
    """Everything the supervisor should hide from the next decision.

    Combines: streak-of-unchanged on this marker (already_taken),
    executor-refused (`covered`), labels stuck in a cycle, and labels
    marked inert. The policy will not receive any of them.
    """
    return (
        set(already_taken(history, marker))
        | set(covered)
        | set(cycling_labels(history))
        | set(repeated_label(history))
        | set(inert_labels(history))
    )


# ---- Purchase guard ---------------------------------------------------------

# A checkout or payment page. Reaching one is how money gets spent, and the
# plan's "Do not place the order" is only a prompt: a generic "Continue" in a
# DoorDash cart drawer led straight to /consumer/checkout/ (2026-10-01).
PURCHASE_URL = re.compile(r"/(checkout|payment|pay|place-?order)(/|\?|$)", re.I)
_ASKS_TO_BUY = re.compile(r"\b(check ?out|place the order|complete the purchase|pay)\b", re.I)
_FORBIDS_BUYING = re.compile(r"\bdo not (place|submit|complete|check ?out|pay)\b", re.I)


def purchase_allowed(goal: str) -> bool:
    """Only a goal that asks to buy, and does not forbid it, may reach checkout."""
    return bool(_ASKS_TO_BUY.search(goal)) and not _FORBIDS_BUYING.search(goal)


def reached_purchase(url: str, goal: str) -> bool:
    """True when an action landed on a checkout or payment page the goal forbids."""
    return bool(PURCHASE_URL.search(url)) and not purchase_allowed(goal)


# ---- Inline tests: `uv run python -m agent.supervisor.guards` ------------------------


class GuardTestFailure(AssertionError):
    """An inline guard test saw the wrong result."""


def _test_purchase_guard() -> None:
    """Unit: the checkout page a "Continue" reached is stopped; a goal that asks
    to buy may proceed; a store page is never flagged."""
    checkout = "https://www.doordash.com/consumer/checkout/?lat=43.47&order_cart_id=364a"
    store = "https://www.doordash.com/store/chipotle-waterloo-36154775/81102878/"
    plan = "Search 'salad'. Choose steak. Add the salad to the cart. Do not place the order."
    if not reached_purchase(checkout, plan):
        raise GuardTestFailure("checkout page not stopped for a do-not-order goal")
    if reached_purchase(store, plan):
        raise GuardTestFailure("store page flagged as a purchase")
    if reached_purchase(checkout, "Add the salad to the cart, then check out and pay."):
        raise GuardTestFailure("a goal that asks to buy was stopped")


if __name__ == "__main__":
    _test_purchase_guard()
    print("guards.py inline tests passed")
