"""Race two decisions when the policy is ambiguous.

When the winning operation's probability is close to the second-best, the
supervisor forks a sandboxed page (a fresh Playwright context that starts from
the same URL), runs the second-best action there, and observes the resulting
page. Whichever tab looks more like progress toward the goal is kept.

Cost: one extra observation + policy call per speculation. Payoff: many
formerly stuck runs make it past the ambiguous step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

AMBIGUITY_MARGIN = 0.15
"""Trigger speculation when p(first) - p(second) is below this margin."""


@dataclass(frozen=True, slots=True)
class Candidate:
    operation: str
    target: str | None
    probability: float


def should_speculate(probabilities: dict[str, float]) -> tuple[Candidate, Candidate] | None:
    """Return the pair to race, or None when the policy is decisive.

    Speculation is expensive; only trigger when the winning margin is thin
    enough that a wrong choice is likely.
    """
    sorted_pairs = sorted(probabilities.items(), key=lambda pair: pair[1], reverse=True)
    if len(sorted_pairs) < 2:
        return None
    first_id, first_p = sorted_pairs[0]
    second_id, second_p = sorted_pairs[1]
    if first_p - second_p >= AMBIGUITY_MARGIN:
        return None
    return (
        Candidate(operation=first_id, target=None, probability=first_p),
        Candidate(operation=second_id, target=None, probability=second_p),
    )


def choose_winner(*, primary: dict[str, Any], secondary: dict[str, Any]) -> str:
    """Given two post-action observations plus their guard signals, pick the
    tab that looks more advanced.

    Heuristic: whichever tab reports more page change on the last step wins.
    If tied, the primary tab wins by default — speculation is a tiebreak, not
    a preference.
    """
    if secondary.get("page_changed") and not primary.get("page_changed"):
        return "secondary"
    if primary.get("page_changed") and not secondary.get("page_changed"):
        return "primary"
    if secondary.get("element_count", 0) > primary.get("element_count", 0) * 1.5:
        # A page that grew markedly is likelier to be a productive result page.
        return "secondary"
    return "primary"
