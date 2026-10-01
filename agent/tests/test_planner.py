"""Planner uses a stub adapter to test parsing without hitting the network."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agent.planner import (
    Plan,
    PlannerError,
    SubGoal,
    VerifyError,
    build_plan,
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
async def test_repair_can_drop_the_active_subgoal() -> None:
    plan = Plan(
        original_goal="x",
        refined_goal="x",
        start_url="https://x",
        subgoals=[SubGoal(text="a", check="A"), SubGoal(text="b", check="B")],
    )
    stub = StubAdapter(reply=json.dumps({"action": "drop"}))
    repaired = await repair(adapter=stub, plan=plan, reason="not available")
    assert [sg.text for sg in repaired.subgoals] == ["b"]


@pytest.mark.asyncio
async def test_repair_can_rewrite_the_active_subgoal() -> None:
    plan = Plan(
        original_goal="x",
        refined_goal="x",
        start_url="https://x",
        subgoals=[SubGoal(text="a", check="A")],
    )
    stub = StubAdapter(reply=json.dumps({"action": "rewrite", "text": "a2", "check": "A2"}))
    repaired = await repair(adapter=stub, plan=plan, reason="wrong search term")
    assert repaired.subgoals[0].text == "a2"


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
