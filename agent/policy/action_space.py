"""Build one action space from one Observation.

Operations group elements by what the executor does with them: CLICK for
clickable roles, TYPE_TEXT for editable text fields, SELECT for native
dropdowns. Global controls (SCROLL_UP, SCROLL_DOWN, BACK, ENTER, WAIT) are
added when the page supports them. DONE and BLOCKED are always available.

Every offered choice maps to one concrete Action, so the executor never sees
a target the policy did not tie to a real element.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from agent.executor import Action
from agent.perception import Observation

CLICKABLE_ROLES = frozenset({
    "button", "link", "checkbox", "radio", "switch",
    "tab", "menuitem", "menuitemradio", "option", "gridcell",
})
TEXT_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})
SELECT_ROLE = "select"

SCROLL_STEP = 700  # pixels per scroll action

# A submit button that reports unmet requirements ("Make 1 required selection -
# CA$16.55"). Clicking it never meets them: on DoorDash it jumps the dialog back
# to its top, away from the missing group. It stays in the page text, so the
# policy still knows a choice is missing; it is just not offered as a click.
REQUIREMENT_STATUS = re.compile(r"\b\d+\s+required\s+selections?\b", re.I)
# An option group's heading ("Beans Required • Select 1", "Toppings (Optional) •
# Select up to 11"). It only opens or closes its group; runs clicked it instead
# of an option even with a prompt rule against it.
GROUP_HEADING = re.compile(r"\b(required|optional)\b.*\bselect\b", re.I)


@dataclass(frozen=True, slots=True)
class Target:
    """One offered choice under one operation."""

    id: str
    label: str
    action: Action
    # A dict of extras Jev sees; every value must be JSON-serialisable.
    hints: Mapping[str, str] = ()  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class Operation:
    """One operation the policy may execute. `targets` is empty when the
    operation is a self-contained control (SCROLL_UP, ENTER, WAIT, DONE)."""

    id: str
    label: str
    targets: tuple[Target, ...] = ()

    @property
    def is_direct(self) -> bool:
        return not self.targets


@dataclass(frozen=True, slots=True)
class ActionSpace:
    operations: tuple[Operation, ...]

    def by_id(self, operation_id: str) -> Operation:
        for operation in self.operations:
            if operation.id == operation_id:
                return operation
        raise KeyError(operation_id)

    def has(self, operation_id: str) -> bool:
        return any(operation.id == operation_id for operation in self.operations)


def _click_target(index: int, element) -> Target:  # noqa: ANN001
    """A CLICK target for one element."""
    label = element.name
    action = Action(id=f"click:{element.ref}", kind="click", label=label, locator=element.ref, role=element.role)
    hints = {"role": element.role}
    if element.section:
        hints["section"] = element.section
    if element.context:
        hints["context"] = element.context[:200]
    if element.visible:
        # Rendered on-screen text — set only when it differs from the
        # accessible name. The mismatch is the signal (autocomplete row
        # named "mouse" with visible "mouse pad" is different from a plain
        # link where visible == name).
        hints["visible"] = element.visible[:200]
    if element.checked is not None:
        hints["checked"] = str(element.checked).lower()
    if element.selected is not None:
        hints["selected"] = str(element.selected).lower()
    if element.expanded is not None:
        hints["expanded"] = str(element.expanded).lower()
    if element.covered_by:
        # Another layer (a suggestion list, a sticky bar, a banner) sits on the
        # control's centre. The executor tries to reach it (scroll, Escape),
        # but the uncovered controls are the surer pick.
        hints["covered_by"] = element.covered_by[:80]
    return Target(id=f"c{index}", label=label, action=action, hints=hints)


def _type_target(index: int, element) -> Target:  # noqa: ANN001
    action = Action(id=f"fill:{element.ref}", kind="fill", label=element.name, locator=element.ref, role=element.role)
    hints = {"role": element.role}
    if element.section:
        hints["section"] = element.section
    if element.value:
        hints["current_value"] = element.value[:80]
    return Target(id=f"t{index}", label=element.name or "text field", action=action, hints=hints)


def _select_targets(index: int, element) -> list[Target]:  # noqa: ANN001
    targets: list[Target] = []
    for option_index, option in enumerate(element.options):
        if option.disabled:
            continue
        action = Action(
            id=f"select:{element.ref}:{option.value}",
            kind="select",
            label=f"{element.name} → {option.label}",
            locator=element.ref,
            role=element.role,
            value=option.value,
        )
        hints = {"parent": element.name, "value": option.value}
        targets.append(Target(id=f"s{index}_{option_index}", label=option.label, action=action, hints=hints))
    return targets


def build(observation: Observation) -> ActionSpace:
    """Turn the observation's elements into a typed action space.

    Every element gets ONE index. If it supports multiple operations (an
    editable searchbox supports both TYPE_TEXT and CLICK-to-open), both
    groups reference that same index. This mirrors old jevis and lets the
    per-element operations list be read directly.
    """
    click_targets: list[Target] = []
    type_targets: list[Target] = []
    select_targets: list[Target] = []
    for index, element in enumerate(observation.elements):
        if element.role in CLICKABLE_ROLES:
            if REQUIREMENT_STATUS.search(element.name or "") or GROUP_HEADING.search(element.name or ""):
                continue
            click_targets.append(_click_target(index, element))
        elif element.role in TEXT_ROLES and element.editable:
            type_targets.append(_type_target(index, element))
        elif element.role == SELECT_ROLE:
            select_targets.extend(_select_targets(index, element))

    operations: list[Operation] = []
    if click_targets:
        operations.append(Operation(
            id="CLICK",
            label="Click an element, button, menu option, or link.",
            targets=tuple(click_targets),
        ))
    if type_targets:
        operations.append(Operation(
            id="TYPE_TEXT",
            label="Enter or replace text in an editable field. The text helper supplies the value.",
            targets=tuple(type_targets),
        ))
    if select_targets:
        operations.append(Operation(
            id="SELECT",
            label="Choose one dropdown value.",
            targets=tuple(select_targets),
        ))
    if observation.can_scroll_down:
        operations.append(Operation(
            id="SCROLL_DOWN",
            label="Reveal content below the viewport.",
        ))
    if observation.can_scroll_up:
        operations.append(Operation(
            id="SCROLL_UP",
            label="Return to content above the viewport.",
        ))
    if observation.can_go_back:
        operations.append(Operation(
            id="BACK",
            label="Return to the previous page.",
        ))
    operations.append(Operation(id="ENTER", label="Press Enter on the focused element."))
    operations.append(Operation(id="WAIT", label="Wait for a running request or animation to settle."))
    operations.append(Operation(id="DONE", label="Every requirement is visibly satisfied."))
    operations.append(Operation(id="BLOCKED", label="No supported operation can advance the goal."))
    return ActionSpace(operations=tuple(operations))


DIRECT_ACTIONS: dict[str, Action] = {
    "SCROLL_DOWN": Action(id="SCROLL_DOWN", kind="scroll", label="Scroll down", delta=SCROLL_STEP),
    "SCROLL_UP": Action(id="SCROLL_UP", kind="scroll", label="Scroll up", delta=-SCROLL_STEP),
    "BACK": Action(id="BACK", kind="back", label="Go back"),
    "ENTER": Action(id="ENTER", kind="enter", label="Press Enter"),
    "WAIT": Action(id="WAIT", kind="wait", label="Wait"),
}


def resolve(space: ActionSpace, operation_id: str, target_id: str | None) -> Action | None:
    """Translate a policy answer into a concrete Action. Returns None for
    DONE and BLOCKED — the supervisor handles those directly."""
    if operation_id in {"DONE", "BLOCKED"}:
        return None
    if operation_id in DIRECT_ACTIONS:
        return DIRECT_ACTIONS[operation_id]
    operation = space.by_id(operation_id)
    if target_id is None:
        raise ValueError(f"{operation_id} requires a target id")
    for target in operation.targets:
        if target.id == target_id:
            return target.action
    raise KeyError(f"Unknown target {target_id!r} for operation {operation_id!r}")


def scoped_ban(operation_id: str, label: str) -> str:
    """An `exclude` entry that bans `label` under one operation only:
    "TYPE_TEXT:Search" hides the Search field but not the Search button."""
    return f"{operation_id}:{label}"


def is_banned(operation_id: str, target: Target, banned: Iterable[str]) -> bool:
    """Whether `banned` withholds `target` under `operation_id`: by target id,
    by label, or by an operation-scoped label (`scoped_ban`)."""
    banned = banned if isinstance(banned, (set, frozenset)) else set(banned)
    return (target.id in banned or target.label in banned
            or scoped_ban(operation_id, target.label) in banned)


def summarise(space: ActionSpace, exclude: Iterable[str] = ()) -> dict[str, dict]:
    """The `questions` dict the Jev policy consumes. Each operation becomes
    one question. Targets under CLICK, TYPE_TEXT, SELECT each get their own
    question so unused heads never influence the winner.

    `exclude` may contain operation ids (CLICK, WAIT), target ids (c9),
    target labels ("Go"), or operation-scoped labels ("TYPE_TEXT:Search",
    see `scoped_ban`). Guards on ids only survive one observation because
    the reader re-stamps refs on every read; label bans are what carry across
    pages, and let `cycling_labels` / `repeated_label` / `inert_labels` block
    a dead action wherever it reappears. A scoped label is for a page where a
    field and its button share a name (Walmart's "Search" box and "Search"
    button): `refill_bans` hides the field without hiding the button.

    An operation whose targets are all banned is removed from the top-level
    choice too — otherwise Jev could pick it and hit the caller with an
    empty target head.
    """
    banned = set(exclude)

    def _idx_of(target_id: str) -> str:
        """Pull the element index out of a target id like 'c42', 't3', 's7_1'.
        This is the same index used in `state.elements[idx]` so the model
        can cross-reference the choice against per-element context.
        """
        stripped = target_id.lstrip("cts")
        return stripped.split("_", 1)[0]

    def surviving(operation: Operation) -> dict[str, dict]:
        return {
            target.id: {
                # `element: "[42] label"` mirrors old jevis's format: the
                # bracketed index is a visible pointer to state.elements[42]
                # so the model reads the per-element context alongside its
                # criteria hints without extra plumbing.
                "element": f"[{_idx_of(target.id)}] {target.label}",
                **dict(target.hints),
            }
            for target in operation.targets
            if not is_banned(operation.id, target, banned)
        }

    target_criteria: dict[str, dict[str, dict]] = {}
    for operation in space.operations:
        if operation.id in banned or not operation.targets:
            continue
        criteria = surviving(operation)
        if criteria:
            target_criteria[operation.id] = criteria

    top_level = {
        operation.id: operation.label
        for operation in space.operations
        if operation.id not in banned
        and (not operation.targets or operation.id in target_criteria)
    }
    questions: dict[str, dict] = {
        "operation": {"type": "choice", "criteria": top_level},
    }
    for operation_id, criteria in target_criteria.items():
        questions[f"{operation_id.lower()}_target"] = {"type": "choice", "criteria": criteria}
    return questions
