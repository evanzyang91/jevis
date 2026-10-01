"""Memory: shape function, session recall, and the in-memory playbook."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.executor import Action
from agent.memory import (
    InMemoryPlaybook,
    SessionMemory,
    action_shape,
    record_outcome,
    situation,
    suggested_action,
)
from agent.perception import Element, Observation, Rect


def _rect() -> Rect:
    return Rect(0, 0, 10, 10)


def _observation(elements: list[Element], url: str = "https://shop.example/products") -> Observation:
    return Observation(
        url=url,
        title="",
        text="",
        elements=tuple(elements),
        marker="m",
        fingerprint="fp",
        guards={element.ref: "g" for element in elements},
        can_go_back=False,
        can_scroll_up=False,
        can_scroll_down=False,
        viewport=(1280, 800),
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_action_shape_trims_item_specific_suffixes() -> None:
    a = Action(id="a", kind="click", label="Add to cart · Robin Hood Flour")
    b = Action(id="b", kind="click", label="Add to cart · Redpath Sugar")
    assert action_shape(a) == action_shape(b)


def test_situation_uses_path_only() -> None:
    path, previous = situation("https://x/products?item=1", None)
    assert path == "/products"
    assert previous == "start"


def test_session_recall_waits_for_second_confirmation() -> None:
    memory = SessionMemory()
    action = Action(id="c1", kind="click", label="Add to cart · A", locator="[data-agent-ref='e0']")
    memory.learn("https://x/products", None, action)
    element = Element(ref="[data-agent-ref='e0']", role="button", name="Add to cart · A", bounds=_rect())
    obs = _observation([element])
    assert memory.recall(obs, None) is None  # one confirmation only
    memory.learn("https://x/products", None, action)
    recalled = memory.recall(obs, None)
    assert recalled is not None
    assert recalled.kind == "click"


def test_session_recall_declines_ambiguous_pages() -> None:
    memory = SessionMemory()
    action = Action(id="c1", kind="click", label="Add to cart", locator="[data-agent-ref='e0']")
    memory.learn("https://x/products", None, action)
    memory.learn("https://x/products", None, action)
    elements = [
        Element(ref="[data-agent-ref='e0']", role="button", name="Add to cart", bounds=_rect()),
        Element(ref="[data-agent-ref='e1']", role="button", name="Add to cart", bounds=_rect()),
    ]
    assert memory.recall(_observation(elements), None) is None


@pytest.mark.asyncio
async def test_playbook_records_and_recalls_a_stable_move() -> None:
    store = InMemoryPlaybook()
    action = Action(id="c1", kind="click", label="Add to cart", locator="[data-agent-ref='e0']")
    await record_outcome(store, url="https://x.com/products", previous=None, action=action)
    await record_outcome(store, url="https://x.com/products", previous=None, action=action)
    element = Element(ref="[data-agent-ref='e0']", role="button", name="Add to cart", bounds=_rect())
    obs = _observation([element], url="https://x.com/products")
    recalled = await suggested_action(store, observation=obs, previous=None)
    assert recalled is not None
    assert recalled.locator == "[data-agent-ref='e0']"


@pytest.mark.asyncio
async def test_playbook_declines_ambiguous_pages() -> None:
    store = InMemoryPlaybook()
    action = Action(id="c1", kind="click", label="Add to cart", locator="[data-agent-ref='e0']")
    await record_outcome(store, url="https://x.com/products", previous=None, action=action)
    await record_outcome(store, url="https://x.com/products", previous=None, action=action)
    elements = [
        Element(ref="[data-agent-ref='e0']", role="button", name="Add to cart", bounds=_rect()),
        Element(ref="[data-agent-ref='e1']", role="button", name="Add to cart", bounds=_rect()),
    ]
    obs = _observation(elements, url="https://x.com/products")
    assert await suggested_action(store, observation=obs, previous=None) is None


@pytest.mark.asyncio
async def test_playbook_success_and_failure_confirms_shift_recall() -> None:
    store = InMemoryPlaybook()
    action = Action(id="c1", kind="click", label="Add to cart", locator="[data-agent-ref='e0']")
    await record_outcome(store, url="https://x.com/products", previous=None, action=action)
    await record_outcome(store, url="https://x.com/products", previous=None, action=action)
    # Two failures drag success rate below 50%.
    await store.confirm("x.com", "/products", "start", action_shape(action), success=False)
    await store.confirm("x.com", "/products", "start", action_shape(action), success=False)
    element = Element(ref="[data-agent-ref='e0']", role="button", name="Add to cart", bounds=_rect())
    obs = _observation([element], url="https://x.com/products")
    assert await suggested_action(store, observation=obs, previous=None) is None
