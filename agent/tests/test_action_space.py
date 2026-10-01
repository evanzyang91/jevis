"""Action-space construction and resolution rules."""

from __future__ import annotations

from datetime import datetime, timezone

from agent.executor import Action
from agent.perception import Element, Observation, Rect, SelectOption
from agent.policy import Operation, build, resolve, summarise


def _observation(elements: list[Element], **overrides) -> Observation:
    base = dict(
        url="https://example.com/",
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
    base.update(overrides)
    return Observation(**base)


def _rect() -> Rect:
    return Rect(0, 0, 10, 10)


def test_click_targets_are_grouped_under_one_operation() -> None:
    obs = _observation([
        Element(ref="[data-agent-ref='e0']", role="button", name="Add", bounds=_rect()),
        Element(ref="[data-agent-ref='e1']", role="link", name="Home", bounds=_rect()),
    ])
    space = build(obs)
    click = space.by_id("CLICK")
    assert isinstance(click, Operation)
    assert [target.label for target in click.targets] == ["Add", "Home"]


def test_only_editable_text_fields_receive_type_text_operation() -> None:
    obs = _observation([
        Element(ref="[data-agent-ref='e0']", role="textbox", name="Query", bounds=_rect(), editable=True),
        Element(ref="[data-agent-ref='e1']", role="textbox", name="Readonly", bounds=_rect(), editable=False),
    ])
    space = build(obs)
    type_op = space.by_id("TYPE_TEXT")
    assert len(type_op.targets) == 1
    assert type_op.targets[0].label == "Query"


def test_select_expands_options_into_individual_targets() -> None:
    element = Element(
        ref="[data-agent-ref='e0']",
        role="select",
        name="Size",
        bounds=_rect(),
        editable=True,
        options=(
            SelectOption(label="Small", value="s"),
            SelectOption(label="Large", value="l"),
            SelectOption(label="Sold out", value="x", disabled=True),
        ),
    )
    obs = _observation([element])
    space = build(obs)
    select = space.by_id("SELECT")
    assert [target.label for target in select.targets] == ["Small", "Large"]


def test_scroll_and_back_operations_appear_only_when_applicable() -> None:
    obs = _observation([], can_scroll_down=True)
    space = build(obs)
    assert space.has("SCROLL_DOWN")
    assert not space.has("SCROLL_UP")
    assert not space.has("BACK")


def test_resolve_returns_a_concrete_action_for_a_click_target() -> None:
    obs = _observation([Element(ref="[data-agent-ref='e0']", role="button", name="Buy", bounds=_rect())])
    space = build(obs)
    click = space.by_id("CLICK")
    action = resolve(space, "CLICK", click.targets[0].id)
    assert isinstance(action, Action)
    assert action.kind == "click"
    assert action.locator == "[data-agent-ref='e0']"


def test_resolve_returns_none_for_terminal_operations() -> None:
    obs = _observation([])
    space = build(obs)
    assert resolve(space, "DONE", None) is None
    assert resolve(space, "BLOCKED", None) is None


def test_resolve_returns_direct_action_for_scroll() -> None:
    obs = _observation([], can_scroll_down=True)
    space = build(obs)
    action = resolve(space, "SCROLL_DOWN", None)
    assert action is not None
    assert action.kind == "scroll"
    assert action.delta > 0


def test_summarise_produces_one_question_per_operation_with_targets() -> None:
    obs = _observation([
        Element(ref="[data-agent-ref='e0']", role="button", name="Buy", bounds=_rect()),
    ])
    space = build(obs)
    questions = summarise(space)
    assert set(questions) >= {"operation", "click_target"}
    assert "DONE" in questions["operation"]["criteria"]


def test_summarise_drops_targets_named_in_exclude() -> None:
    obs = _observation([
        Element(ref="[data-agent-ref='e0']", role="button", name="Buy", bounds=_rect()),
        Element(ref="[data-agent-ref='e1']", role="button", name="Skip", bounds=_rect()),
    ])
    space = build(obs)
    space_targets = {target.id for target in space.by_id("CLICK").targets}
    banned = next(iter(space_targets))
    questions = summarise(space, exclude={banned})
    assert banned not in questions["click_target"]["criteria"]
