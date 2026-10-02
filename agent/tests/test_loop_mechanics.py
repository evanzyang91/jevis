"""Supervisor mechanics around one step: a refused click, an unrendered page,
an add that must move the cart, and the operation-scoped re-fill ban.

Stand-in executor and reader, no browser. The browser side of the same fixes
(reader reachability, click-through, search submit, cart count, blank wait)
is covered by the inline tests in `agent/perception/dom.py`.
"""

from __future__ import annotations

from typing import Any

import agent.supervisor.loop as loop_module
from agent.executor import Action, Occluded, Outcome
from agent.perception import Element, Observation, Rect
from agent.policy import Decision, build, summarise
from agent.supervisor import HistoryEntry, RunState, Supervisor, combined_ban
from agent.transport import Bus


def _observation(*, marker: str = "m", cart_count: int | None = None, blank: bool = False,
                 elements: tuple[Element, ...] = ()) -> Observation:
    return Observation(url="https://shop.test/", title="Shop", text="" if blank else "page", elements=elements,
                       marker=marker, fingerprint="f", guards={}, can_go_back=False, can_scroll_up=False,
                       can_scroll_down=False, viewport=(1280, 800), cart_count=cart_count, blank=blank)


def _supervisor(executor: Any) -> tuple[Supervisor, RunState]:
    supervisor = Supervisor(executor=executor, bus=Bus(), jev=None, text=None, goal="Order a burrito.")  # type: ignore[arg-type]
    return supervisor, RunState(run_id=supervisor.run_id, goal=supervisor.goal)


class _CoveringExecutor:
    """Refuses every click as covered, as the DoorDash store page did."""

    async def act(self, action: Action) -> Outcome:
        raise Occluded(f"Target {action.label!r} covered by div")


class _Page:
    def __init__(self) -> None:
        self.reloads = 0

    async def reload(self, **_: Any) -> None:
        self.reloads += 1


class _PageExecutor:
    def __init__(self) -> None:
        self.page = _Page()


def _reads(monkeypatch: Any, *observations: Observation) -> list[int]:
    """Make the loop's reader return `observations` in turn (the last one
    repeats). Returns a one-item list counting the reads."""
    queue = list(observations)
    count = [0]

    async def fake_observe(page: Any, **_: Any) -> Observation:
        count[0] += 1
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(loop_module, "observe", fake_observe)
    return count


async def test_covered_click_is_recorded_and_withheld_on_its_page_only() -> None:
    entree = Element(ref="[data-agent-ref='e0']", role="button", name="Entree", bounds=Rect(10, 10, 50, 20))
    observation = _observation(elements=(entree,))
    supervisor, state = _supervisor(_CoveringExecutor())
    action = Action(id="click:e0", kind="click", label="Entree", locator=entree.ref, role="button")
    decision = Decision(operation="CLICK", target="c0", action=action, confidence=0.9, probabilities={},
                        model="stand-in", latency_ms=1, usage={})
    await supervisor._act(state, observation, decision)  # noqa: SLF001
    last = state.history[-1]
    assert (last.error, last.page_changed, last.marker) == ("covered", False, "m")
    assert "Entree" not in state.covered  # no run-long ban
    assert "Entree" in combined_ban(state.history, "m", covered=state.covered)
    assert "Entree" not in combined_ban(state.history, "m2", covered=state.covered)


async def test_blank_read_is_reloaded_once_before_the_policy_sees_it(monkeypatch: Any) -> None:
    _reads(monkeypatch, _observation(marker="blank", blank=True), _observation(marker="full"))
    executor = _PageExecutor()
    supervisor, state = _supervisor(executor)
    observation = await supervisor._observe(state)  # noqa: SLF001
    assert observation.marker == "full"
    assert executor.page.reloads == 1


async def test_a_page_still_blank_after_its_reload_is_not_reloaded_again(monkeypatch: Any) -> None:
    blank = _observation(marker="blank", blank=True)
    _reads(monkeypatch, blank)
    executor = _PageExecutor()
    supervisor, state = _supervisor(executor)
    state.observation = blank  # the previous step already read this URL blank
    await supervisor._observe(state)  # noqa: SLF001
    assert executor.page.reloads == 0


async def test_add_click_waits_for_the_cart_badge_and_records_the_delta(monkeypatch: Any) -> None:
    # The button turned into a stepper (marker moved) before the badge updated.
    reads = _reads(monkeypatch, _observation(marker="m1", cart_count=0), _observation(marker="m1", cart_count=1))
    supervisor, state = _supervisor(_PageExecutor())
    state.observation = _observation(marker="m0", cart_count=0)
    state.history.append(HistoryEntry(step=1, operation="CLICK", target="c0", action_id="click:e0",
                                      action_label="Add to cart - Eggs", marker="m0", page_changed=True,
                                      url_changed=False))
    observation = await supervisor._observe(state)  # noqa: SLF001
    assert observation.cart_count == 1
    assert state.history[-1].cart_delta == 1
    assert state.history[-1].page_changed is True
    assert reads[0] == 2


async def test_refused_action_is_not_effect_polled(monkeypatch: Any) -> None:
    reads = _reads(monkeypatch, _observation(marker="m"))
    supervisor, state = _supervisor(_PageExecutor())
    state.observation = _observation(marker="m")
    state.history.append(HistoryEntry(step=1, operation="CLICK", target="c0", action_id="click:e0",
                                      action_label="Entree", marker="m", page_changed=False,
                                      url_changed=False, error="covered"))
    await supervisor._observe(state)  # noqa: SLF001
    assert reads[0] == 1


def test_refill_ban_keeps_the_same_named_search_button() -> None:
    """Walmart run 9ae93ef1: after a fill that did not submit, the next decision
    may not re-fill Search but may still click the Search button."""
    field = Element(ref="[data-agent-ref='e0']", role="combobox", name="Search", bounds=Rect(0, 0, 400, 30),
                    editable=True)
    button = Element(ref="[data-agent-ref='e1']", role="button", name="Search", bounds=Rect(400, 0, 40, 30))
    history = [HistoryEntry(step=1, operation="TYPE_TEXT", target="t0", action_id="fill:e0",
                            action_label="Search", marker="m0", page_changed=True, url_changed=False,
                            text="granulated sugar")]
    banned = combined_ban(history, "m1")
    questions = summarise(build(_observation(marker="m1", elements=(field, button))), exclude=banned)
    assert "TYPE_TEXT" not in questions["operation"]["criteria"]
    assert "CLICK" in questions["operation"]["criteria"]
    assert [entry["element"] for entry in questions["click_target"]["criteria"].values()] == ["[1] Search"]
