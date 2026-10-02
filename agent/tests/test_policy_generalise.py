"""Policy rules that keep unfamiliar shopping goals on track (no network calls).

Each rule came from a grocery run the prompts were not tuned on: a favourites
detour taken at 0.74, a second product for an item already in the cart, a
wrong-kind product added below the step-check bar, quantity controls offered
because the goal said "all seven ingredients", hints and typed queries that
could not see what the run had already done.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from agent.perception import Element, Observation, Rect
from agent.policy import decide, field_value, should_escalate
from agent.policy.jev_policy import (
    asks_several,
    commits_product,
    finished_items,
    goal_items,
    item_for,
)
from agent.policy.reinstruct import progress, reinstruct
from agent.providers import ChoiceAnswer, JevResult, NoulAnswer, get

GOAL = ("Search 'rice' on Walmart and add one bag of rice. Search 'eggs' and add one carton. "
        "Search 'soy sauce' and add one bottle. Do not place the order. "
        "Stop when the cart shows rice, eggs, and soy sauce.")


def _page(names: list[str], **overrides: Any) -> Observation:
    elements = tuple(Element(ref=f"[data-agent-ref=e{i}]", role="button", name=name, bounds=Rect(0, 40 * i, 200, 30))
                     for i, name in enumerate(names))
    base = dict(url="https://www.walmart.ca/en/search?q=rice", title="rice", text="rice results", elements=elements,
                marker="m", fingerprint="f", guards={}, can_go_back=True, can_scroll_up=False, can_scroll_down=True,
                viewport=(1280, 800), captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    base.update(overrides)
    return Observation(**base)


class _Jev:
    """Picks CLICK on the first offered target with `p`; any step check scores `noul`."""

    def __init__(self, p: float = 0.95, noul: float = 0.9) -> None:
        self.p, self.noul = p, noul
        self.offered: list[str] = []
        self.checks = 0

    async def ask(self, *, state: dict[str, Any], questions: dict[str, Any]) -> JevResult:
        if "c0" in questions:
            self.checks += 1
            return JevResult(answers={}, model="stand-in", usage={}, latency_ms=0,
                             nouls={key: NoulAnswer(noul=self.noul) for key in questions})
        ops = list(questions["operation"]["criteria"])
        targets = questions["click_target"]["criteria"]
        self.offered = [criteria["element"].split("] ", 1)[1] for criteria in targets.values()]
        ids = list(targets)
        rest = (1 - self.p) / max(1, len(ids) - 1)
        probs = {key: (self.p if j == 0 else rest) for j, key in enumerate(ids)}
        op = ChoiceAnswer(choice="CLICK", probabilities={k: float(k == "CLICK") for k in ops}, confidence=1.0)
        target = ChoiceAnswer(choice=ids[0], probabilities=probs, confidence=self.p)
        return JevResult(answers={"operation": op, "click_target": target}, model="stand-in", usage={}, latency_ms=0)


@pytest.mark.parametrize(("goal", "several"), [
    ("Add two bags of flour.", True),
    ("Add 3 Coca-Cola cans.", True),
    ("Search black beans and add two cans of black beans to the cart.", True),
    ("Open Three Tacos and choose chicken.", False),
    ("Stop when the cart shows all seven cake ingredients.", False),
    ("Search 'eggs' and add one carton for breakfast for two.", False),
    ("Add one 2 L bottle of milk.", False),
    ("Add a dozen eggs.", False),
])
def test_asks_several_reads_quantities_not_item_counts(goal: str, several: bool) -> None:
    assert asks_several(goal) is several


def test_add_controls_map_to_the_most_specific_goal_item() -> None:
    items = goal_items("Search 'cheese' and add one. Search 'parmesan cheese' and add one. "
                       "Search 'green onions' and add one bunch.")
    assert items == ["cheese", "parmesan cheese", "green onions"]
    assert item_for("Add to cart - Kraft Parmesan Cheese", items) == "parmesan cheese"
    assert item_for("Add to cart - Black Diamond Cheddar Cheese", items) == "cheese"
    assert item_for("Add to cart - Green Onion, Sold in bunches", items) == "green onions"
    assert item_for("Add to cart - PAM Olive Oil", items) is None


def test_commits_product_names_a_product_not_a_dialog_price() -> None:
    assert commits_product("Add to cart - Great Value Large 12 Eggs")
    assert not commits_product("Add to cart - CA$15.60")
    assert not commits_product("Great Value Large 12 Eggs")


async def test_no_second_product_for_an_item_already_added() -> None:
    history = [{"operation": "CLICK", "label": "Add to cart - Sitara Basmati Rice", "page_changed": True}]
    assert finished_items(GOAL, history) == {"rice"}
    jev = _Jev()
    page = _page(["Add to cart - Minute Rice Long Grain", "Add to cart - Great Value Large 12 Eggs", "Search"])
    decision = await decide(client=jev, observation=page, goal=GOAL, history=history)  # type: ignore[arg-type]
    assert "Add to cart - Minute Rice Long Grain" not in jev.offered
    assert decision.action is not None and decision.action.label == "Add to cart - Great Value Large 12 Eggs"


async def test_an_item_the_page_shows_in_the_cart_is_finished() -> None:
    jev = _Jev()
    page = _page(["Decrease quantity Great Value Long Grain Rice, Current Quantity 1",
                  "Increase quantity Great Value Long Grain Rice, Current Quantity 1",
                  "Add to cart - Minute Rice Long Grain", "Add to cart - Great Value Large 12 Eggs"])
    await decide(client=jev, observation=page, goal=GOAL, history=[])  # type: ignore[arg-type]
    assert "Add to cart - Minute Rice Long Grain" not in jev.offered
    assert "Add to cart - Great Value Large 12 Eggs" in jev.offered


async def test_an_add_that_did_not_change_the_page_does_not_finish_the_item() -> None:
    history = [{"operation": "CLICK", "label": "Add to cart - Sitara Basmati Rice", "page_changed": False}]
    jev = _Jev()
    page = _page(["Add to cart - Minute Rice Long Grain", "Search"])
    await decide(client=jev, observation=page, goal=GOAL, history=history)  # type: ignore[arg-type]
    assert "Add to cart - Minute Rice Long Grain" in jev.offered


async def test_favourites_and_sign_in_detours_are_not_offered() -> None:
    jev = _Jev()
    page = _page(["Sign in to add to Favourites list, Great Value Soya Sauce", "Sign In Account", "Language English",
                  "Legal", "Add to cart - Silver Swan Soy Sauce"])
    await decide(client=jev, observation=page, goal=GOAL, history=[])  # type: ignore[arg-type]
    assert jev.offered == ["Add to cart - Silver Swan Soy Sauce"]
    jev = _Jev()
    await decide(client=jev, observation=page, goal="Sign in to my account.", history=[])  # type: ignore[arg-type]
    assert "Sign In Account" in jev.offered


async def test_an_unsure_add_is_checked_and_a_failed_check_escalates_at_once() -> None:
    page = _page(["Add to cart - Great Value Organic Golden Sugar", "Search"])
    jev = _Jev(p=0.7, noul=0.6)  # above CHECK_TRIGGER, below COMMIT_TRIGGER; a doubtful, not hopeless, check
    decision = await decide(client=jev, observation=page, goal="Search 'granulated sugar' and add one bag.",
                            history=[])  # type: ignore[arg-type]
    assert jev.checks == 1 and decision.check == "failed" and decision.committing
    assert should_escalate(decision, previous_check_failed=False)
    jev = _Jev(p=0.95)
    decision = await decide(client=jev, observation=page, goal="Search 'sugar' and add one bag.",
                            history=[])  # type: ignore[arg-type]
    assert jev.checks == 0 and decision.check is None


class _VetoJev(_Jev):
    """Prefers the mayonnaise add; the check scores it 0.01 and anything else 0.6 (doubtful, not hopeless)."""

    async def ask(self, *, state: dict[str, Any], questions: dict[str, Any]) -> JevResult:
        if "c0" in questions:
            self.checks += 1
            return JevResult(answers={}, model="stand-in", usage={}, latency_ms=0, nouls={
                key: NoulAnswer(noul=0.01 if "Mayonnaise" in state["candidates"][int(key[1:])] else 0.6)
                for key in questions})
        return await super().ask(state=state, questions=questions)


async def test_a_hopeless_add_is_refused_and_the_policy_asked_again() -> None:
    page = _page(["Add to cart - Hellmann's Mayonnaise Dip", "Add to cart - Great Value Tomato Basil Pasta Sauce"])
    jev = _VetoJev(p=0.6)
    decision = await decide(client=jev, observation=page, goal="Search 'pasta sauce' and add one jar.",
                            history=[])  # type: ignore[arg-type]
    assert decision.vetoed == "Add to cart - Hellmann's Mayonnaise Dip"
    assert decision.action is not None and decision.action.label == "Add to cart - Great Value Tomato Basil Pasta Sauce"
    assert should_escalate(decision, previous_check_failed=False)


def test_jev_and_dated_snapshots_are_priced() -> None:
    assert get("jev-1.13.0").id == "jev-latest"
    assert get("gpt-4.1-2025-04-14").id == "gpt-4.1"
    assert get("gpt-4.1-mini-2025-04-14").id == "gpt-4.1-mini"
    with pytest.raises(KeyError):
        get("gpt-4")


class _Adapter:
    """Records the user payload and answers with `reply`."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.context: dict[str, Any] = {}

    async def complete(self, *, system: str, user: str, **_: Any) -> Any:
        del system
        self.context = json.loads(user)
        return SimpleNamespace(text=json.dumps(self.reply), model="stand-in", latency_ms=1,
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


async def test_hints_see_the_whole_run_and_the_controls_the_policy_cannot_use() -> None:
    history = [{"operation": "TYPE_TEXT", "label": "Search", "text": "rice", "page_changed": True},
               {"operation": "CLICK", "label": "Add to cart - Sitara Basmati Rice", "page_changed": True},
               *({"operation": "SCROLL_DOWN", "label": "Scroll down", "page_changed": True} for _ in range(30))]
    assert progress(history) == {"added": ["Add to cart - Sitara Basmati Rice"], "searched": ["rice"]}
    adapter = _Adapter({"guidance": "Search eggs.", "evidence": [], "control": "Search"})
    page = _page(["Add to cart - Minute Rice", "Search"])
    hint = await reinstruct(adapter=adapter, goal=GOAL, observation=page, history=history,  # type: ignore[arg-type]
                            policy_pick="CLICK Add to cart - Minute Rice", banned={"Add to cart - Minute Rice", "c0"})
    assert hint is not None and hint.control == "Search"
    assert adapter.context["added"] == ["Add to cart - Sitara Basmati Rice"]
    assert adapter.context["unavailable"] == ["Add to cart - Minute Rice"]


async def test_the_field_helper_sees_the_hint_and_every_query_typed() -> None:
    adapter = _Adapter({"text": "eggs"})
    history = [{"step": 1, "label": "Search", "page_changed": True, "text": "rice"}]
    value = await field_value(adapter=adapter, goal=GOAL, field_name="Search", field_role="searchbox",  # type: ignore[arg-type]
                              current_value=None, page_text="", history=history, guidance="Search eggs next.")
    assert value.text == "eggs"
    assert adapter.context["guidance"] == "Search eggs next."
    assert adapter.context["typed_before"] == ["rice"]
