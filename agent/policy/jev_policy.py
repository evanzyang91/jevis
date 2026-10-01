"""One decision: read the action space, ask Jev, return a typed Decision.

The policy stays pure. It never touches the browser, never mutates memory,
never emits events. The supervisor owns those. This module answers one
question: given this observation and this goal, what is the next action?
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from agent.perception import Observation
from agent.providers import JevClient, JevError, JevOversized

from .action_space import ActionSpace, build, resolve, summarise
from .prompts import DIALOG_KIND, DIALOG_KINDS, NEXT_ACTION, TARGET

MAX_ACTION_SPACE = 60
# Below this the dialog's diagnosis is not sent, and rule 1 applies as written.
DIALOG_KIND_MIN = 0.6


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
    # The open modal's diagnosis, when one was sent to the policy.
    dialog: str | None = None
    dialog_p: float = 0.0


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


async def diagnose_dialog(
    *, client: JevClient, observation: Observation, goal: str,
) -> tuple[str, float, dict[str, int]] | None:
    """Judge an open modal: a step of the goal, or an interruption?

    Its own request, before the decision, because questions in one request
    cannot see each other's answers. The state is the dialog alone: with
    the page behind it hidden, an item's options window looks like any
    popup, and rule 1 of NEXT_ACTION would close it. Returns (kind,
    probability, usage), or None when no modal is open or Jev fails —
    the decision then goes ahead without it."""
    if observation.scroll_area != "dialog":
        return None
    state = {
        "goal": goal,
        "page": {"url": observation.url, "title": observation.title},
        "dialog": {
            "text": observation.dialog_text or "",
            "controls": [element.name for element in observation.elements][:40],
        },
    }
    question = {"type": "choice", "instructions": DIALOG_KIND, "criteria": DIALOG_KINDS}
    try:
        result = await client.ask(state=state, questions={"dialog_kind": question})
    except JevError:
        return None
    answer = result.answers.get("dialog_kind")
    if answer is None:
        return None
    return answer.choice, answer.probabilities[answer.choice], dict(result.usage)


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
    diagnosis = await diagnose_dialog(client=client, observation=observation, goal=goal)
    dialog = diagnosis[0] if diagnosis and diagnosis[1] >= DIALOG_KIND_MIN else None
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
                # Only off the page: tells the policy that SCROLL moves the
                # open dialog or panel, not the page behind it.
                **({"scroll_area": observation.scroll_area} if observation.scroll_area != "page" else {}),
                # What the open dialog is, so rule 1 closes only interruptions.
                **({"dialog": dialog} if dialog else {}),
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
    if action is not None and action.kind == "scroll" and observation.scroll_point is not None:
        # Move the open dialog or panel, by most of its own height.
        step = observation.scroll_step or abs(action.delta)
        action = replace(action, point=observation.scroll_point, delta=step if action.delta > 0 else -step)
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
        usage=_usage_sum(result.usage, diagnosis[2] if diagnosis else {}),
        offered=offered,
        dialog=dialog,
        dialog_p=diagnosis[1] if dialog and diagnosis else 0.0,
    )


def _usage_sum(*usages: Mapping[str, int]) -> dict[str, int]:
    """Token usage of every Jev call behind one decision, so the run's budget counts them all."""
    total: dict[str, int] = {}
    for usage in usages:
        for key, value in usage.items():
            total[key] = total.get(key, 0) + int(value)
    return total


# ---- Inline tests: `uv run python -m agent.policy.jev_policy` ------------------------
# Unit tests use a stand-in Jev. The end-to-end tests call the live API on
# dialogs copied from real DoorDash runs (a fraction of a cent); they skip
# without TYPESAFE_API_KEY.


class DialogTestFailure(AssertionError):
    """An inline dialog-diagnosis test saw the wrong result."""


_BOWL_TEXT = (
    "Burrito Bowl CA$15.60 Your choice of freshly grilled meat or sofritas served in a delicious bowl "
    "with rice, beans, or fajita veggies, and topped with guac, salsa, queso blanco, sour cream or cheese. "
    "Choose Protein or Veggie Required • Select 1 Chicken 180 cal Steak 150 cal +CA$0.95 "
    "Beef Barbacoa 170 cal +CA$0.95 Carnitas 210 cal +CA$0.25 Sofritas 150 cal Veggie "
    "NEXT Make 1 required selection - CA$15.60"
)
_BOWL_CONTROLS = ("Close Burrito Bowl", "NEXT", "Make 1 required selection - CA$15.60")
_SIMILAR_TEXT = ("Hungry now? This store is closed. View similar stores open now. Taco Bell 0.9 mi • 45 min "
                 "Mary Brown's 1.2 mi • 40 min See All Close")
_SIMILAR_CONTROLS = ("Close", "Hungry now? View similar stores", "See All Hungry now?", "Taco Bell 0.9 mi • 45 min")
_GOAL = ("Search 'barbacoa bowl' at Chipotle. Add one barbacoa bowl to the cart. Do not place the order. "
         "Stop when the cart shows one barbacoa bowl.")


def _dialog_observation(text: str, controls: tuple[str, ...]) -> Observation:
    from agent.perception import Element, Rect

    elements = tuple(Element(ref=f"[data-agent-ref=e{i}]", role="button", name=name,
                             bounds=Rect(500, 80 + 40 * i, 200, 30), section="dialog")
                     for i, name in enumerate(controls))
    return Observation(url="https://www.doordash.com/store/chipotle-waterloo-36154775/81102878/", title="Chipotle",
                       text="DoorDash Chipotle menu " + text, elements=elements, marker="m", fingerprint="f",
                       guards={}, can_go_back=False, can_scroll_up=False, can_scroll_down=True,
                       viewport=(1280, 800), scroll_area="dialog", scroll_point=(735, 365), scroll_step=468,
                       dialog_text=text)


class _StandIn:
    """Answers the dialog question with `kind` at `p`, and the operation question with WAIT.
    Records how many calls were made and the state of the operation request."""

    def __init__(self, kind: str = "task", p: float = 0.9, fail: bool = False) -> None:
        self.kind, self.p, self.fail = kind, p, fail
        self.calls = 0
        self.state: dict[str, Any] | None = None

    async def ask(self, *, state: dict[str, Any], questions: dict[str, Any]):  # noqa: ANN201
        from agent.providers import ChoiceAnswer, JevResult

        self.calls += 1
        if "dialog_kind" in questions:
            if self.fail:
                raise JevError("stand-in failure")
            other = "interruption" if self.kind == "task" else "task"
            answer = ChoiceAnswer(choice=self.kind, probabilities={self.kind: self.p, other: 1 - self.p},
                                  confidence=self.p)
            return JevResult(answers={"dialog_kind": answer}, model="stand-in",
                             usage={"input_tokens": 100}, latency_ms=0)
        self.state = state
        options = questions["operation"]["criteria"]
        answer = ChoiceAnswer(choice="WAIT", probabilities={k: float(k == "WAIT") for k in options}, confidence=1.0)
        return JevResult(answers={"operation": answer}, model="stand-in", usage={"input_tokens": 1000}, latency_ms=0)


async def _unit_tests() -> None:
    bowl = _dialog_observation(_BOWL_TEXT, _BOWL_CONTROLS)
    # Gap 1: a confident "task" reaches the policy state, and its tokens reach the budget.
    jev = _StandIn("task", 0.9)
    decision = await decide(client=jev, observation=bowl, goal=_GOAL, history=[])  # type: ignore[arg-type]
    if (jev.state or {}).get("page", {}).get("dialog") != "task" or decision.dialog != "task":
        raise DialogTestFailure("confident task diagnosis did not reach the policy")
    if decision.usage.get("input_tokens") != 1100:
        raise DialogTestFailure(f"diagnosis tokens not counted: {decision.usage}")
    # Gap 2: an unsure diagnosis is not sent; rule 1 then applies as written.
    jev = _StandIn("task", 0.55)
    await decide(client=jev, observation=bowl, goal=_GOAL, history=[])  # type: ignore[arg-type]
    if "dialog" in (jev.state or {}).get("page", {}):
        raise DialogTestFailure("unsure diagnosis was sent")
    # Gap 3: no modal means no extra call; a failed diagnosis never stops the decision.
    page = replace(bowl, scroll_area="page", scroll_point=None, scroll_step=None, dialog_text=None)
    jev = _StandIn()
    await decide(client=jev, observation=page, goal=_GOAL, history=[])  # type: ignore[arg-type]
    if jev.calls != 1:
        raise DialogTestFailure(f"page without a modal made {jev.calls} calls")
    jev = _StandIn(fail=True)
    if (await decide(client=jev, observation=bowl, goal=_GOAL, history=[])).dialog is not None:  # type: ignore[arg-type]
        raise DialogTestFailure("failed diagnosis left a dialog kind")


async def _e2e_live() -> None:
    import os

    if not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: live dialog tests need TYPESAFE_API_KEY")
        return
    client = JevClient()
    bowl = _dialog_observation(_BOWL_TEXT, _BOWL_CONTROLS)
    similar = _dialog_observation(_SIMILAR_TEXT, _SIMILAR_CONTROLS)
    # End to end 1: the item's options window is a task; "similar stores" is an interruption.
    kinds = [await diagnose_dialog(client=client, observation=o, goal=_GOAL) for o in (bowl, similar)]
    print(f"live: bowl -> {kinds[0][:2] if kinds[0] else None}, similar stores -> {kinds[1][:2] if kinds[1] else None}")
    if not kinds[0] or kinds[0][0] != "task" or not kinds[1] or kinds[1][0] != "interruption":
        raise DialogTestFailure(f"live diagnosis wrong: {kinds}")
    # End to end 2: on the bowl dialog the policy no longer closes the window it needs.
    decision = await decide(client=client, observation=bowl, goal=_GOAL, history=[])
    print(f"live: bowl decision -> {decision.operation} {decision.action.label if decision.action else ''} "
          f"(dialog={decision.dialog} p={decision.dialog_p:.2f})")
    if decision.action is not None and decision.action.label.startswith("Close"):
        raise DialogTestFailure("policy closed the task dialog")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_unit_tests())
    asyncio.run(_e2e_live())
    print("jev_policy.py inline dialog tests passed")
