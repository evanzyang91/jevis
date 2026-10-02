"""Turn one goal into an ordered plan.

The planner runs once at start. It emits a `Plan`: the actionable steps the
supervisor tracks one by one (`subgoals`), the standing rules that hold on
every step (`constraints`, such as "Do not place the order."), the end state
(`stop_when`), and the URL to start on. The supervisor's ledger
(`planner/progress.py`) marks each step done or skipped as the run goes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from agent.providers import TextAdapter

from .prompts import PLAN_GOAL, SUGGEST_URL

# How a step's completion shows. "add": a committing add (Add to cart / order
# / bag) lands. "search": a typed query is submitted. "page": the policy says
# DONE and a cheap verifier confirms it from the page.
DONE_WHEN = ("add", "search", "page")

# A planner sentence that is a rule for every step, not a step of its own:
# "If an item is out of stock, ...", "Do not place the order.", "Never ...".
_CONSTRAINT = re.compile(r"^\s*(if|do not|don't|dont|never|only|keep|avoid|make sure|ensure|unless)\b", re.I)
_STOP = re.compile(r"^\s*stop\b", re.I)
_ADDS = re.compile(r"\badd\b.*\b(cart|order|bag|basket|trolley)\b|\badd (one|a|an|two|three|four|five|\d+)\b", re.I)
_SEARCHES = re.compile(r"^\s*search\b", re.I)


@dataclass(frozen=True, slots=True)
class SubGoal:
    """One actionable step. `check` is the phrase the verifier reads out of
    the page — kept short so the verifier does not weigh unrelated text.

    `search_term` is the exact string to type when this step reaches a
    search field, or None when the step does not involve typing a search.
    The planner emits it so the text helper does not have to re-infer the
    query on every fill.

    `done_when` is how the supervisor recognises the step as finished: one
    of DONE_WHEN.
    """

    text: str
    check: str
    search_term: str | None = None
    done_when: str = "page"


@dataclass(slots=True)
class Plan:
    """A plan is a list of steps, the rules that hold on every step, and the
    URL to start on.

    Mutable on purpose: the supervisor's ledger rewrites a blocked step in
    place, and keeps `active_index` in step with its own state for readers
    that only know the index.
    """

    original_goal: str
    refined_goal: str
    start_url: str
    subgoals: list[SubGoal] = field(default_factory=list)
    active_index: int = 0
    # Standing rules sent to the policy with every step, never tracked.
    constraints: list[str] = field(default_factory=list)
    # The planner's end-state sentence ("Stop when the cart shows ..."). The
    # supervisor ends the run when every step is done; this is for display.
    stop_when: str | None = None

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


def done_when_for(text: str, search_term: str | None, claimed: object = None) -> str:
    """The planner's `done_when` when it is valid, else one read from the text:
    a sentence that adds something is "add", a bare search is "search"."""
    if _ADDS.search(text):
        # A step that plainly adds an item completes on the add, whatever the
        # planner called it: "search" would end it before anything is added.
        return "add"
    if isinstance(claimed, str) and claimed.strip().lower() in DONE_WHEN:
        return claimed.strip().lower()
    if search_term and _SEARCHES.match(text):
        return "search"
    return "page"


def is_constraint(text: str) -> bool:
    """Whether a planner sentence is a standing rule rather than a step."""
    return bool(_CONSTRAINT.match(text))


def _parse_step(item: object) -> SubGoal:
    if not isinstance(item, dict):
        raise ValueError("step is not an object")
    text = str(item["text"]).strip()
    if not text:
        raise ValueError("empty step")
    check = str(item.get("check") or "").strip() or text
    raw_term = item.get("search_term")
    # Accept missing, null, or an empty string as "no term for this step". A
    # non-string value is a planner mistake — drop it to the helper instead of
    # typing a dict into a search field.
    search_term: str | None = None
    if isinstance(raw_term, str) and raw_term.strip():
        search_term = raw_term.strip()
    return SubGoal(text=text, check=check, search_term=search_term,
                   done_when=done_when_for(text, search_term, item.get("done_when")))


def parse_plan(raw: str, *, goal: str, url: str | None) -> Plan:
    """Parse the planner's JSON into a Plan.

    Reads `steps` and `constraints` (current format) or a flat `subgoals`
    list (older format). Either way a sentence that reads as a rule ("If ...",
    "Do not ...") moves to the constraints and a "Stop when ..." sentence to
    `stop_when`, so only actionable steps are tracked.
    """
    try:
        parsed = json.loads(raw)
        refined = parsed["goal"]
        if not isinstance(refined, str) or not refined.strip():
            raise ValueError("empty refined goal")
        items = parsed.get("steps")
        if items is None:
            items = parsed["subgoals"]
        if not isinstance(items, list) or not items:
            raise ValueError("empty steps")
        constraints = [str(c).strip() for c in parsed.get("constraints") or [] if str(c).strip()]
        stop_when = str(parsed.get("stop_when") or "").strip() or None
        steps: list[SubGoal] = []
        for item in items:
            step = _parse_step(item)
            if _STOP.match(step.text):
                stop_when = stop_when or step.text
            elif is_constraint(step.text):
                constraints.append(step.text)
            else:
                steps.append(step)
        if not steps:
            raise ValueError("no actionable step")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
        raise PlannerError(f"Planner returned an unusable plan: {raw[:200]}") from err
    return Plan(
        original_goal=goal,
        refined_goal=refined.strip(),
        start_url=(url or _fallback_url(goal)),
        subgoals=steps,
        constraints=list(dict.fromkeys(constraints)),
        stop_when=stop_when,
    )


async def build_plan(*, adapter: TextAdapter, goal: str, url: str | None = None) -> Plan:
    """Refine the goal and split it into steps and constraints in one call.

    The refined goal keeps every sentence, steps and rules both: the purchase
    guard and the UI read it. The policy reads the ledger's per-step view
    instead (`planner/progress.py`).
    """
    context = {"goal": goal, "site": url or ""}
    result = await adapter.complete(
        system=PLAN_GOAL,
        user=json.dumps(context),
        json_object=True,
    )
    return parse_plan(result.text, goal=goal, url=url)


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


# Regional storefronts, by AGENT_REGION (an ISO country code). Unset means no
# region: URLs pass through unchanged. Redirect on both the raw and
# `www.`-prefixed forms. Only sites with a real regional equivalent belong
# here — adding a bogus one would send the run to a 404.
_REGIONAL_HOSTS: dict[str, dict[str, str]] = {
    "CA": {
        "amazon.com": "www.amazon.ca",
        "walmart.com": "www.walmart.ca",
        "bestbuy.com": "www.bestbuy.ca",
        "costco.com": "www.costco.ca",
        "homedepot.com": "www.homedepot.ca",
        "staples.com": "www.staples.ca",
        "newegg.com": "www.newegg.ca",
        "ebay.com": "www.ebay.ca",
        "indeed.com": "ca.indeed.com",
    },
}


def localise(url: str, region: str | None = None) -> str:
    """Rewrite a global retailer URL to its storefront in `region` (default:
    AGENT_REGION) when one exists; with no region, return the URL unchanged."""
    if region is None:
        region = os.environ.get("AGENT_REGION", "")
    hosts = _REGIONAL_HOSTS.get(region.strip().upper(), {})
    parts = urlparse(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    regional = hosts.get(host)
    if not regional:
        return url
    return parts._replace(netloc=regional).geturl()


# ---- Inline tests: `uv run python -m agent.planner.plan` ------------------------------
# Live: they call the configured text model (TEXT_MODEL in .env), a fraction of a cent.


class PlannerTestFailure(AssertionError):
    """An inline planner test saw the wrong result."""


async def _e2e_site_routing(adapter: TextAdapter) -> None:
    """Restaurant food goes to DoorDash, never the restaurant's own site; other
    kinds of goal keep their usual sites. Checked under region CA."""
    cases = {
        "order me a barbacoa bowl from chipotle": "doordash.com",
        "get me a large iced coffee from starbucks": "doordash.com",
        "can you get me ingredients for a cake": "walmart.ca",
        "buy a wireless mouse from amazon": "amazon.ca",
        "open the wikipedia article on gödel's incompleteness theorems": "wikipedia.org",
    }
    for goal, host in cases.items():
        url = localise(await suggest_url(adapter=adapter, goal=goal), region="CA")
        if host not in (urlparse(url).hostname or ""):
            raise PlannerTestFailure(f"{goal!r} started on {url}, wanted {host}")
        print(f"  {goal[:50]:50} -> {url}")


async def _e2e_delivery_plan(adapter: TextAdapter) -> None:
    """On DoorDash's home page the plan finds the restaurant first and keeps the
    shown address; it never asks the agent to type one."""
    plan = await build_plan(adapter=adapter, goal="order me a barbacoa bowl from chipotle",
                            url="https://www.doordash.com")
    goal = plan.refined_goal.lower()
    first = plan.subgoals[0].text.lower()
    if "chipotle" not in first:
        raise PlannerTestFailure(f"the plan does not open the restaurant first: {first!r}")
    if any(word in goal for word in ("type the address", "enter an address", "enter the address", "new address")):
        raise PlannerTestFailure(f"the plan asks for an address: {plan.refined_goal!r}")
    print(f"  plan: {plan.refined_goal}")


if __name__ == "__main__":
    import asyncio

    from agent.cli import load_dotenv
    from agent.providers import adapter_for
    from agent.providers.registry import get

    load_dotenv()
    model = os.environ.get("TEXT_MODEL", "")
    if not model or not os.environ.get("OPENAI_API_KEY"):
        print("skip: live planner tests need TEXT_MODEL and OPENAI_API_KEY")
    else:
        text = adapter_for(model, get(model).provider)

        async def _all() -> None:  # one event loop: the adapter's client is bound to it
            await _e2e_site_routing(text)
            await _e2e_delivery_plan(text)

        asyncio.run(_all())
        print("plan.py inline tests passed")
