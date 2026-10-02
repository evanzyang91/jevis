"""Planner uses a stub adapter to test parsing without hitting the network."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agent.planner import (
    PlannerError,
    SubGoal,
    VerifyError,
    build_plan,
    parse_plan,
    repair,
    suggest_url,
    verify,
)
from agent.providers import ImageInput, TextResult, TokenUsage


@dataclass
class StubAdapter:
    reply: str

    async def complete(
        self,
        system: str,
        user: str,
        *,
        images: list[ImageInput] | None = None,
        json_object: bool = False,
        max_tokens: int = 1024,
    ) -> TextResult:
        del system, user, images, json_object, max_tokens
        return TextResult(
            text=self.reply,
            model="stub",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            latency_ms=1,
        )


@pytest.mark.asyncio
async def test_build_plan_parses_a_well_formed_response() -> None:
    stub = StubAdapter(reply=json.dumps({
        "goal": "Search 'flour' and add one bag. Stop.",
        "subgoals": [
            {"text": "Search 'flour' and add one bag.", "check": "Cart shows one bag of flour."},
            {"text": "Stop.", "check": "Cart shows one bag of flour."},
        ],
    }))
    plan = await build_plan(adapter=stub, goal="Get flour", url="https://example.com")
    assert plan.refined_goal.startswith("Search")
    assert [sg.text for sg in plan.subgoals][0].startswith("Search")
    assert plan.active is plan.subgoals[0]
    assert not plan.completed()


@pytest.mark.asyncio
async def test_build_plan_rejects_missing_fields() -> None:
    stub = StubAdapter(reply=json.dumps({"goal": "x"}))
    with pytest.raises(PlannerError):
        await build_plan(adapter=stub, goal="x")


@pytest.mark.asyncio
async def test_verify_returns_a_boolean_result() -> None:
    stub = StubAdapter(reply=json.dumps({"met": True, "reason": "one bag visible"}))
    outcome = await verify(
        adapter=stub,
        subgoal_text="Add one bag of flour",
        check="Cart shows one bag of flour",
        page_text="Cart: 1x Bag of flour",
        url="https://example.com/cart",
    )
    assert outcome.met is True
    assert "bag" in outcome.reason


@pytest.mark.asyncio
async def test_verify_rejects_non_boolean_met() -> None:
    stub = StubAdapter(reply=json.dumps({"met": "yes"}))
    with pytest.raises(VerifyError):
        await verify(
            adapter=stub,
            subgoal_text="x",
            check="y",
            page_text="",
            url="",
        )


@pytest.mark.asyncio
async def test_repair_can_skip_a_blocked_step() -> None:
    step = SubGoal(text="Search 'vanilla extract' and add one bottle.", check="Cart shows vanilla.",
                   search_term="vanilla extract", done_when="add")
    stub = StubAdapter(reply=json.dumps({"action": "skip", "reason": "not sold here"}))
    fix = await repair(adapter=stub, goal="cake", rules=[], step=step, reason="no results")
    assert fix.step is None
    assert fix.reason == "not sold here"
    assert fix.usage == {"prompt_tokens": 10, "completion_tokens": 5}


@pytest.mark.asyncio
async def test_repair_can_rewrite_a_blocked_step() -> None:
    step = SubGoal(text="Search 'caster sugar' and add one bag.", check="Cart shows sugar.",
                   search_term="caster sugar", done_when="add")
    stub = StubAdapter(reply=json.dumps({"action": "rewrite", "text": "Search 'sugar' and add one bag.",
                                         "check": "Cart shows sugar.", "search_term": "sugar"}))
    fix = await repair(adapter=stub, goal="cake", rules=["Do not place the order."], step=step, reason="none")
    assert fix.step is not None
    assert (fix.step.text, fix.step.search_term, fix.step.done_when) == ("Search 'sugar' and add one bag.",
                                                                         "sugar", "add")


@pytest.mark.asyncio
async def test_repair_rejects_an_unknown_action() -> None:
    stub = StubAdapter(reply=json.dumps({"action": "concede"}))
    with pytest.raises(PlannerError):
        await repair(adapter=stub, goal="x", rules=[], step=SubGoal(text="a", check="A"), reason="r")


def test_parse_plan_keeps_rules_out_of_the_tracked_steps() -> None:
    """The older flat format put rules and the stop sentence among the
    subgoals (the 2026-10-01 pasta plan). Only actionable steps are tracked."""
    raw = json.dumps({
        "goal": "Search 'pasta' and add one pack of spaghetti. Do not place the order.",
        "subgoals": [
            {"text": "Search 'pasta' on Walmart and add one pack of spaghetti.", "check": "Cart shows spaghetti.",
             "search_term": "pasta"},
            {"text": "Search 'pasta sauce' and add one jar.", "check": "Cart shows sauce.",
             "search_term": "pasta sauce"},
            {"text": "If an item is out of stock, substitute the closest equivalent and continue.",
             "check": "Substitute in cart.", "search_term": None},
            {"text": "Do not place the order.", "check": "Order not placed.", "search_term": None},
            {"text": "Stop when the cart shows spaghetti and pasta sauce.", "check": "Both in cart.",
             "search_term": None},
        ],
    })
    plan = parse_plan(raw, goal="buy me ingredients for some pasta", url="https://www.walmart.ca/")
    assert [s.search_term for s in plan.subgoals] == ["pasta", "pasta sauce"]
    assert [s.done_when for s in plan.subgoals] == ["add", "add"]
    assert plan.constraints == ["If an item is out of stock, substitute the closest equivalent and continue.",
                                "Do not place the order."]
    assert plan.stop_when == "Stop when the cart shows spaghetti and pasta sauce."
    assert "Do not place the order." in plan.refined_goal  # the purchase guard reads it


def test_parse_plan_reads_the_current_format() -> None:
    raw = json.dumps({
        "goal": "Search 'transformer attention' on Google Scholar. Open the top result. Stop when it is open.",
        "steps": [
            {"text": "Search 'transformer attention' on Google Scholar.", "check": "Results shown.",
             "search_term": "transformer attention", "done_when": "search"},
            {"text": "Open the top result.", "check": "Paper page is open.", "search_term": None,
             "done_when": "page"},
        ],
        "constraints": [],
        "stop_when": "Stop when it is open.",
    })
    plan = parse_plan(raw, goal="find a paper", url=None)
    assert [s.done_when for s in plan.subgoals] == ["search", "page"]
    assert plan.stop_when == "Stop when it is open."
    assert plan.start_url == "https://www.google.com/"


def test_a_step_that_adds_completes_on_the_add_whatever_the_planner_called_it() -> None:
    raw = json.dumps({"goal": "g", "steps": [
        {"text": "Search 'eggs' and add one carton.", "check": "c", "search_term": "eggs", "done_when": "search"},
    ]})
    assert parse_plan(raw, goal="g", url=None).subgoals[0].done_when == "add"


def test_parse_plan_rejects_a_plan_of_rules_only() -> None:
    raw = json.dumps({"goal": "g", "subgoals": [{"text": "Do not place the order.", "check": "c"}]})
    with pytest.raises(PlannerError):
        parse_plan(raw, goal="g", url=None)


@pytest.mark.asyncio
async def test_suggest_url_returns_a_parsed_url() -> None:
    stub = StubAdapter(reply=json.dumps({"url": "https://www.example.com/"}))
    url = await suggest_url(adapter=stub, goal="buy something")
    assert url.startswith("https://")


@pytest.mark.asyncio
async def test_suggest_url_rejects_a_non_http_scheme() -> None:
    stub = StubAdapter(reply=json.dumps({"url": "ftp://example.com"}))
    with pytest.raises(PlannerError):
        await suggest_url(adapter=stub, goal="x")
