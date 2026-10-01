"""Turn one goal into an ordered plan.

The planner runs once at start. It emits a `Plan` — an ordered list of
sub-goals with a starting URL suggestion — that the supervisor uses to break
one long run into checkable pieces. Verification confirms each sub-goal is met
before the plan advances.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import urlparse

from agent.providers import TextAdapter

from .prompts import PLAN_GOAL, SUGGEST_URL


@dataclass(frozen=True, slots=True)
class SubGoal:
    """One checkable step. `check` is the phrase the verifier reads out of
    the page — kept short so the verifier does not weigh unrelated text."""

    text: str
    check: str


@dataclass(slots=True)
class Plan:
    """A plan is a list of sub-goals plus the URL to start on.

    Mutable on purpose: `repair` may rewrite it and the supervisor tracks the
    active index directly on this object.
    """

    original_goal: str
    refined_goal: str
    start_url: str
    subgoals: list[SubGoal] = field(default_factory=list)
    active_index: int = 0

    @property
    def active(self) -> SubGoal | None:
        if 0 <= self.active_index < len(self.subgoals):
            return self.subgoals[self.active_index]
        return None

    def advance(self) -> None:
        self.active_index = min(self.active_index + 1, len(self.subgoals))

    def completed(self) -> bool:
        return self.active_index >= len(self.subgoals)


class PlannerError(RuntimeError):
    """The planner could not produce a usable plan."""


async def build_plan(*, adapter: TextAdapter, goal: str, url: str | None = None) -> Plan:
    """Refine the goal and split it into sub-goals in one call.

    The planner responds with `{goal, subgoals: [{text, check}, ...]}`. The
    refined goal is what the policy reads on every step; the sub-goals let
    the verifier acknowledge progress without re-reading the whole goal.
    """
    context = {"goal": goal, "site": url or ""}
    result = await adapter.complete(
        system=PLAN_GOAL,
        user=json.dumps(context),
        json_object=True,
    )
    try:
        parsed = json.loads(result.text)
        refined = parsed["goal"]
        subgoals_raw = parsed["subgoals"]
        if not isinstance(refined, str) or not refined.strip():
            raise ValueError("empty refined goal")
        if not isinstance(subgoals_raw, list) or not subgoals_raw:
            raise ValueError("empty subgoals")
        subgoals: list[SubGoal] = []
        for item in subgoals_raw:
            text = item["text"].strip()
            check = item["check"].strip()
            if not text or not check:
                raise ValueError("empty subgoal")
            subgoals.append(SubGoal(text=text, check=check))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
        raise PlannerError(f"Planner returned an unusable plan: {result.text[:200]}") from err
    return Plan(
        original_goal=goal,
        refined_goal=refined.strip(),
        start_url=(url or _fallback_url(goal)),
        subgoals=subgoals,
    )


async def suggest_url(*, adapter: TextAdapter, goal: str) -> str:
    """Ask the model to name a starting URL when the user did not."""
    result = await adapter.complete(
        system=SUGGEST_URL,
        user=json.dumps({"goal": goal}),
        json_object=True,
    )
    try:
        parsed = json.loads(result.text)
        url = parsed["url"]
        parts = urlparse(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("bad url")
        return url
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
        raise PlannerError(f"Site suggestion invalid: {result.text[:200]}") from err


def _fallback_url(goal: str) -> str:
    del goal
    return "https://www.google.com/"


# Retailers with a distinct Canadian storefront. Redirect on both the raw and
# `www.`-prefixed forms. Only sites with a real .ca equivalent belong here —
# adding a bogus one would send the run to a 404.
_CANADIAN_HOSTS: dict[str, str] = {
    "amazon.com": "www.amazon.ca",
    "walmart.com": "www.walmart.ca",
    "bestbuy.com": "www.bestbuy.ca",
    "costco.com": "www.costco.ca",
    "homedepot.com": "www.homedepot.ca",
    "staples.com": "www.staples.ca",
    "newegg.com": "www.newegg.ca",
    "ebay.com": "www.ebay.ca",
    "indeed.com": "ca.indeed.com",
}


def localise(url: str) -> str:
    """Rewrite a US retailer URL to its Canadian storefront when one exists."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    canadian = _CANADIAN_HOSTS.get(host)
    if not canadian:
        return url
    return parts._replace(netloc=canadian).geturl()
