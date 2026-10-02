"""The run loop with a plan: step-by-step tracking, completion, skips.

A scripted store stands in for the browser (search box, Search button, add
buttons, cart badge) and a stand-in policy reads the per-step goal the
supervisor composes. The verifier and repair calls are stand-ins too.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any
from urllib.parse import quote_plus

import pytest

import agent.supervisor.loop as loop
from agent.executor import Action, FakeExecutor, Outcome
from agent.perception import Element, Observation, Rect
from agent.planner import Plan, Repair, SubGoal, VerifyResult
from agent.policy import Decision
from agent.supervisor import Supervisor
from agent.transport import Bus, PlanEvent, StatusEvent

STOCK = {
    "all-purpose flour": ["Great Value Original All-Purpose Flour"],
    "granulated sugar": ["Great Value Organic Pure Golden Sugar, 900 g"],
    "eggs": ["Great Value Large 12 Eggs"],
}


class Store:
    """A store page the executor drives. Every action that does something
    moves the marker, as a real page would."""

    def __init__(self, stock: dict[str, list[str]], *, cart: int = 11, adds_land: bool = True,
                 adds_inert: bool = False) -> None:
        self.stock, self.cart, self.adds_land = stock, cart, adds_land
        self.adds_inert = adds_inert  # an add click the page ignores: nothing moves
        self.query = ""  # what the search box holds
        self.searched = ""  # what the results page shows
        self.added: list[str] = []
        self.ticks = 0

    def observation(self) -> Observation:
        names = [("searchbox", "Search"), ("button", "Search"),
                 ("link", f"Cart contains {self.cart} items Total Amount $1.00")]
        names += [("button", f"Add to cart - {product}") for product in self.stock.get(self.searched, [])]
        elements = tuple(Element(ref=f"[data-agent-ref=e{i}]", role=role, name=name,
                                 bounds=Rect(10, 10 + 30 * i, 200, 24), editable=role == "searchbox",
                                 value=self.query if role == "searchbox" else None)
                         for i, (role, name) in enumerate(names))
        url = f"https://store.test/search?q={quote_plus(self.searched)}" if self.searched else "https://store.test/"
        return Observation(url=url, title="Store", text=f"Results for {self.searched}", elements=elements,
                           marker=f"m{self.ticks}", fingerprint="f", guards={}, can_go_back=True,
                           can_scroll_up=False, can_scroll_down=False, viewport=(1280, 800))

    def act(self, action: Action) -> Outcome:
        if self.adds_inert and action.label.startswith("Add to cart - "):
            # The executor's coarse signal still says the page changed.
            return Outcome(page_changed=True, url_changed=False, load_ms=0, final_url=self.observation().url)
        self.ticks += 1
        url_changed = False
        if action.kind == "fill":
            self.query = action.value or ""
        elif action.label == "Search":
            self.searched, url_changed = self.query, True
        elif action.label.startswith("Add to cart - "):
            self.added.append(action.label.removeprefix("Add to cart - "))
            if self.adds_land:
                self.cart += 1
        return Outcome(page_changed=True, url_changed=url_changed, load_ms=0, final_url=self.observation().url)


class StoreExecutor(FakeExecutor):
    def __init__(self, store: Store) -> None:
        super().__init__(url="https://store.test/")
        self.page = store

    async def act(self, action: Action) -> Outcome:
        self.calls.append(("act", action))
        return self.page.act(action)


def _decision(operation: str, element: Element | None = None, kind: str = "click") -> Decision:
    action = None
    if element is not None:
        action = Action(id=f"{kind}:{element.ref}", kind=kind, label=element.name, locator=element.ref)  # type: ignore[arg-type]
    return Decision(operation=operation, target=element.ref if element else None, action=action, confidence=0.9,
                    probabilities={}, model="stand-in", latency_ms=1, usage={"input_tokens": 1, "output_tokens": 0})


class Shopper:
    """A stand-in policy that does exactly the current step: type the step's
    item, submit, add the first result. Records every goal it was given."""

    def __init__(self, *, block_on: set[str] | None = None, done_on: set[str] | None = None,
                 thrash: bool = False) -> None:
        self.goals: list[str] = []
        self.banned: list[set[str]] = []
        self.block_on, self.done_on, self.thrash = block_on or set(), done_on or set(), thrash

    async def __call__(self, *, observation: Observation, goal: str, banned: Any, **_: Any) -> Decision:
        self.goals.append(goal)
        self.banned.append(set(banned))
        match = re.search(r"Current step: Search '([^']+)'", goal)
        if match is None:
            return _decision("DONE")
        term = match.group(1)
        if term in self.block_on and "BLOCKED" not in banned:
            return _decision("BLOCKED")
        if term in self.done_on and "DONE" not in banned:
            self.done_on.discard(term)  # a premature claim, once
            return _decision("DONE")
        by_name = {(e.role, e.name): e for e in observation.elements}
        box = by_name[("searchbox", "Search")]
        if (box.value or "") != term and term not in observation.url.replace("+", " "):
            return _decision("TYPE_TEXT", box, kind="fill")
        if term not in observation.url.replace("+", " "):
            return _decision("CLICK", by_name[("button", "Search")])
        adds = [e for e in observation.elements if e.name.startswith("Add to cart - ")]
        if adds:
            return _decision("CLICK", adds[0])
        if self.thrash:  # keep looking, never admit BLOCKED (live run 27280578)
            scroll = Action(id="SCROLL_DOWN", kind="scroll", label="Scroll down", delta=600)
            return replace(_decision("SCROLL_DOWN"), action=scroll)
        return _decision("BLOCKED")


def _plan(terms: list[str]) -> Plan:
    return Plan(original_goal="Buy me cake ingredients from walmart",
                refined_goal="(full goal). Do not place the order.",
                start_url="https://store.test/",
                subgoals=[SubGoal(text=f"Search '{t}' and add one.", check=f"Cart shows {t}.", search_term=t,
                                  done_when="add") for t in terms],
                constraints=["If an item is out of stock, substitute the closest equivalent and continue.",
                             "Do not place the order."])


async def _run(store: Store, plan: Plan | None, policy: Shopper, monkeypatch: pytest.MonkeyPatch,
               verdicts: list[bool] | None = None, repairs: list[SubGoal | None] | None = None,
               ) -> tuple[Any, list[Any], Store, list[str]]:
    async def fake_observe(page: Store) -> Observation:
        return page.observation()

    verify_calls: list[str] = []

    async def fake_verify(**kwargs: Any) -> VerifyResult:
        verify_calls.append(kwargs["subgoal_text"])
        met = (verdicts or [True]).pop(0) if verdicts else True
        return VerifyResult(met=met, reason="stand-in", latency_ms=1, model="stand-in",
                            usage={"prompt_tokens": 1, "completion_tokens": 1})

    async def fake_repair(**kwargs: Any) -> Repair:
        step = (repairs or [None]).pop(0) if repairs else None
        return Repair(step=step, reason="not sold here", model="stand-in",
                      usage={"prompt_tokens": 1, "completion_tokens": 1}, latency_ms=1)

    async def no_hint(**_: Any) -> None:
        return None

    monkeypatch.setattr(loop, "observe", fake_observe)
    monkeypatch.setattr(loop, "decide", policy)
    monkeypatch.setattr(loop, "verify", fake_verify)
    monkeypatch.setattr(loop, "repair", fake_repair)
    monkeypatch.setattr(loop, "reinstruct", no_hint)
    monkeypatch.setattr(loop, "CART_SETTLE_S", 0.0)
    published: list[Any] = []
    bus = Bus(sink=published.append)
    supervisor = Supervisor(executor=StoreExecutor(store), bus=bus, jev=None, text=None,  # type: ignore[arg-type]
                            goal=plan.refined_goal if plan else "Buy flour.", plan=plan)
    state = await supervisor.run("https://store.test/")
    return state, published, store, verify_calls


@pytest.mark.asyncio
async def test_a_multi_item_run_ends_done_once_every_step_is_done(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = Shopper()
    state, published, store, checked = await _run(Store(STOCK), _plan(list(STOCK)), policy, monkeypatch)
    assert state.status == "done"
    # Each item added once; the run ends after the last add, with no DONE asked for.
    assert store.added == ["Great Value Original All-Purpose Flour", "Great Value Organic Pure Golden Sugar, 900 g",
                           "Great Value Large 12 Eggs"]
    assert state.budget.steps == 9  # type, submit, add per item
    assert len(policy.goals) == 9
    status = [e for e in published if isinstance(e, StatusEvent)][-1]
    assert status.status == "done" and status.reason.startswith("all 3 steps done")
    assert "cart 11 -> 14 items" in status.reason
    # The policy saw one step at a time, with what was finished.
    assert "Current step: Search 'all-purpose flour'" in policy.goals[0]
    assert "Current step: Search 'eggs'" in policy.goals[-1]
    assert "granulated sugar (added Great Value Organic Pure Golden Sugar, g)" not in policy.goals[-1]
    assert "granulated sugar (added Great Value Organic Pure Golden Sugar)" in policy.goals[-1]
    plans = [e for e in published if isinstance(e, PlanEvent)]
    assert [s.status for s in plans[-1].steps] == ["done", "done", "done"]
    assert plans[-1].active_index == 3
    assert plans[-1].constraints[-1] == "Do not place the order."


@pytest.mark.asyncio
async def test_an_impossible_step_is_rewritten_once_then_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baking powder is not stocked: the step is rewritten once, blocks again,
    is skipped, and the run goes on to vanilla and ends done, naming the skip."""
    stock = {"all-purpose flour": ["Five Roses Flour"], "vanilla extract": ["Club House Pure Vanilla Extract"]}
    policy = Shopper(block_on={"baking powder", "double-acting baking powder"})
    rewritten = SubGoal(text="Search 'double-acting baking powder' and add one.", check="c",
                        search_term="double-acting baking powder", done_when="add")
    state, published, store, checked = await _run(Store(stock), _plan(["all-purpose flour", "baking powder",
                                                               "vanilla extract"]), policy, monkeypatch,
                                          repairs=[rewritten])
    assert state.status == "done"
    assert store.added == ["Five Roses Flour", "Club House Pure Vanilla Extract"]
    status = [e for e in published if isinstance(e, StatusEvent)][-1]
    assert status.reason.startswith("2 of 3 steps done")
    assert "skipped double-acting baking powder (no way forward on" in status.reason
    notes = [e.note for e in published if isinstance(e, PlanEvent)]
    assert any("rewritten" in note for note in notes) and any("skipped" in note for note in notes)


@pytest.mark.asyncio
async def test_a_step_that_stalls_is_handled_as_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Green onions are not stocked and the policy keeps scrolling without
    ever saying BLOCKED. After STEP_ACTION_LIMIT actions the step goes to
    repair (here: skip) and the run finishes the oil."""
    stock = {"rice": ["Great Value Long Grain White Rice"], "vegetable oil": ["Mazola Corn Oil"]}
    state, published, store, _ = await _run(Store(stock), _plan(["rice", "green onions", "vegetable oil"]),
                                            Shopper(thrash=True), monkeypatch)
    assert state.status == "done"
    assert store.added == ["Great Value Long Grain White Rice", "Mazola Corn Oil"]
    status = [e for e in published if isinstance(e, StatusEvent)][-1]
    assert status.reason.startswith("2 of 3 steps done") and "skipped green onions (not sold here)" in status.reason
    # rice: type, submit, add; onions: type, submit, then scrolls up to the limit; oil: type, submit, add.
    assert state.budget.steps == 3 + loop.STEP_ACTION_LIMIT + 3


@pytest.mark.asyncio
async def test_a_done_claim_is_confirmed_before_the_step_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    """The policy claims DONE on sugar before adding it. The verifier rejects
    the claim, the policy decides again without DONE and adds the sugar."""
    policy = Shopper(done_on={"granulated sugar"})
    state, published, store, checked = await _run(Store(STOCK), _plan(list(STOCK)), policy, monkeypatch,
                                                  verdicts=[False])
    assert state.status == "done"
    assert checked == ["Search 'granulated sugar' and add one."]
    assert sum("DONE" in banned for banned in policy.banned) == 1  # only the re-decision after the rejection
    assert store.added[1] == "Great Value Organic Pure Golden Sugar, 900 g"
    assert any("not finished yet" in e.note for e in published if isinstance(e, PlanEvent))


@pytest.mark.asyncio
async def test_an_add_that_never_reaches_the_cart_reopens_its_step(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = Shopper()
    state, published, store, checked = await _run(Store({"eggs": ["Large Eggs"]}, adds_land=False), _plan(["eggs"]),
                                         policy, monkeypatch)
    # Added, badge flat twice -> reopened, added again, then trusted.
    assert store.added == ["Large Eggs", "Large Eggs"]
    assert state.status == "done"
    assert any("did not reach the cart" in e.note for e in published if isinstance(e, PlanEvent))


@pytest.mark.asyncio
async def test_without_a_plan_done_ends_the_run_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = Shopper()  # no "Current step" in the goal: says DONE at once
    state, _, _, _ = await _run(Store(STOCK), None, policy, monkeypatch)
    assert state.status == "done"
    assert policy.goals == ["Buy flour."]


@pytest.mark.asyncio
async def test_an_add_the_page_ignored_finishes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The executor reports a change, the reader's marker does not move: the
    corrected history entry, not the executor's guess, feeds the ledger."""

    class FastClock:  # the effect poll waits up to 3 s for the marker; skip the wait
        now = 0.0

        @classmethod
        def monotonic(cls) -> float:
            cls.now += 1.0
            return cls.now

    monkeypatch.setattr(loop, "time", FastClock)
    state, published, store, _ = await _run(Store({"eggs": ["Large Eggs"]}, adds_inert=True), _plan(["eggs"]),
                                            Shopper(), monkeypatch)
    assert store.added == []
    assert state.status == "blocked"
    assert all(step.status != "done" for event in published if isinstance(event, PlanEvent) for step in event.steps)
    status = [e for e in published if isinstance(e, StatusEvent)][-1]
    assert "0 of 1 steps done" in status.reason and "not reached eggs" in status.reason
