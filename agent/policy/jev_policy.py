"""One decision: read the action space, ask Jev, return a typed Decision.

The policy stays pure. It never touches the browser, never mutates memory,
never emits events. The supervisor owns those. This module answers one
question: given this observation and this goal, what is the next action?
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agent.perception import Observation
from agent.providers import JevClient, JevOversized

from .action_space import ActionSpace, build, resolve, summarise
from .prompts import NEXT_ACTION, TARGET

MAX_ACTION_SPACE = 60


def _operations_for(element) -> list[str]:  # noqa: ANN001
    """Which operations apply to this element — same rule as `build()`."""
    from .action_space import CLICKABLE_ROLES, SELECT_ROLE, TEXT_ROLES
    ops: list[str] = []
    if element.role in CLICKABLE_ROLES:
        ops.append("CLICK")
    if element.role in TEXT_ROLES and element.editable:
        ops.append("TYPE_TEXT")
    if element.role == SELECT_ROLE:
        ops.append("SELECT")
    return ops


def _element_state(idx: int, element) -> dict:  # noqa: ANN001 — Element imported at top-level would cycle
    """Rich per-element metadata for the Jev state payload.

    `idx` matches the target id suffix (c9 = click on state.elements with
    idx=9, t3 = TYPE_TEXT on state.elements with idx=3). This gives the
    policy a page-wide overview it can cross-reference with the per-target
    criteria without duplicating context info.

    `operations` lists which ops apply — old jevis put this on every element
    so the model reads at a glance that element 42 supports both CLICK and
    TYPE_TEXT, instead of inferring from role alone.
    """
    out: dict = {
        "idx": idx,
        "role": element.role,
        "label": element.name,
        "operations": _operations_for(element),
    }
    if element.section:
        out["section"] = element.section
    if element.context:
        out["context"] = element.context[:200]
    if element.opens:
        out["opens"] = element.opens
    if element.visible:
        out["visible"] = element.visible[:200]
    if element.value:
        out["value"] = element.value[:80]
    if element.checked is not None:
        out["checked"] = element.checked
    if element.selected is not None:
        out["selected"] = element.selected
    if element.expanded is not None:
        out["expanded"] = element.expanded
    return out


def _progress(history: list[dict], window: int = 20, limit: int = 60) -> list[dict]:
    """Recent history filtered like old jevis's progress(): every action that
    changed the page, plus the last `window` steps, capped at `limit`.

    On a long multi-item goal a naive tail hides completed items and the
    policy restarts them. This keeps completed work visible.
    """
    if len(history) <= window:
        return history[-limit:]
    older_kept = [entry for entry in history[:-window] if entry.get("page_changed")]
    kept = older_kept + history[-window:]
    return kept[-limit:]


@dataclass(frozen=True, slots=True)
class Decision:
    """One turn's answer. `action` is `None` for DONE and BLOCKED."""

    operation: str
    target: str | None
    action: Any  # Action from executor.kinds; typed loosely to avoid import cycles.
    confidence: float
    probabilities: Mapping[str, float]
    model: str
    latency_ms: int
    usage: Mapping[str, int]
    # Labels that reached the model per operation (post-ban). Diagnostic —
    # tells post-hoc analysis whether the expected action was even in the
    # choice set when the model picked something else.
    offered: Mapping[str, tuple[str, ...]] = ()  # type: ignore[assignment]


def _reduced(space: ActionSpace, keep: int) -> ActionSpace:
    """Drop element-scoped targets past `keep` while preserving global controls.

    Straight DOM-order truncation. Viewport-only enumeration in reader.js
    keeps the initial element count small enough that reduction rarely
    triggers — and when it does, DOM order is a safer default than any
    section-based heuristic (Amazon and others put filter chips inside a
    `<form>` landmark while leaving products as bare divs, which fooled a
    section-priority sort into preferring filters over products).
    """
    from .action_space import Operation  # local to keep module import order clean

    new_ops: list[Operation] = []
    for operation in space.operations:
        if operation.is_direct:
            new_ops.append(operation)
            continue
        new_ops.append(Operation(
            id=operation.id,
            label=operation.label,
            targets=operation.targets[:keep],
        ))
    return ActionSpace(operations=tuple(new_ops))


async def decide(
    *,
    client: JevClient,
    observation: Observation,
    goal: str,
    history: list[dict[str, Any]],
    banned: Iterable[str] = (),
) -> Decision:
    """Ask Jev for the next operation and target. Retries with a shrunken
    action space when the server reports the request oversized."""
    space = build(observation)
    attempts = 0
    max_targets = MAX_ACTION_SPACE
    while True:
        attempts += 1
        questions = summarise(space, exclude=banned)
        for question in questions.values():
            if question["criteria"] == {}:
                # Nothing left to answer; caller must not have a choice at all.
                raise RuntimeError("Action space collapsed after banning; nothing to decide")
        questions["operation"]["instructions"] = {"goal": goal, "rules": NEXT_ACTION}
        for operation in space.operations:
            key = f"{operation.id.lower()}_target"
            if key in questions:
                questions[key]["instructions"] = {
                    "goal": goal,
                    "operation": operation.id,
                    "rules": [NEXT_ACTION, TARGET],
                }
        state = {
            "page": {
                "url": observation.url,
                "title": observation.title,
                "text": observation.text[:1500 if attempts > 1 else 6000],
                # Loading flag is advisory — lets the policy pick Wait when the
                # page is mid-load rather than betting on a stale action.
                "loading": observation.loading,
            },
            "elements": [
                _element_state(idx, element) for idx, element in enumerate(observation.elements)
            ],
            # progress() keeps every page-changing action plus the recent tail,
            # capped, mirroring what old jevis sent. A naive tail loses the
            # completed items on multi-step goals and the policy restarts them.
            "recent_actions": _progress(history),
        }
        try:
            result = await client.ask(state=state, questions=questions)
            break
        except JevOversized:
            if max_targets < 20:
                raise
            max_targets //= 2
            space = _reduced(space, max_targets)

    operation_answer = result.answers["operation"]
    operation = operation_answer.choice
    target: str | None = None
    probabilities = operation_answer.probabilities
    if space.by_id(operation).targets:
        target_answer = result.answers.get(f"{operation.lower()}_target")
        if target_answer is None:
            raise RuntimeError(f"Missing target head for operation {operation}")
        target = target_answer.choice
        probabilities = target_answer.probabilities
    action = resolve(space, operation, target)
    offered: dict[str, tuple[str, ...]] = {}
    banned_set = set(banned)
    for op in space.operations:
        if not op.targets or op.id in banned_set:
            continue
        surviving = tuple(
            target.label for target in op.targets
            if target.id not in banned_set and target.label not in banned_set
        )
        if surviving:
            offered[op.id] = surviving
    return Decision(
        operation=operation,
        target=target,
        action=action,
        confidence=operation_answer.confidence,
        probabilities=probabilities,
        model=result.model,
        latency_ms=result.latency_ms,
        usage=result.usage,
        offered=offered,
    )
