"""One decision: read the action space, ask Jev, return a typed Decision.

The policy stays pure. It never touches the browser, never mutates memory,
never emits events. The supervisor owns those. This module answers one
question: given this observation and this goal, what is the next action?
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Any

from agent.executor import CLOSE_LABEL
from agent.perception import Observation
from agent.providers import JevClient, JevError, JevOversized

from .action_space import ActionSpace, build, resolve, summarise
from .prompts import DIALOG_KIND, DIALOG_KINDS, NEXT_ACTION, STEP_CHECK, TARGET

MAX_ACTION_SPACE = 60
# Below this the dialog's diagnosis is not sent, and rule 1 applies as written.
DIALOG_KIND_MIN = 0.6
# The step check runs when the grouped signal is below CHECK_TRIGGER, or below
# CHECK_TERMINAL for DONE and BLOCKED, which end the run. A candidate passes at
# CHECK_PASS. Tuned offline on 84 labelled steps (2026-10-01).
CHECK_TRIGGER = 0.5
CHECK_TERMINAL = 0.8
CHECK_PASS = 0.7
# A product's add control commits: a wrong product, or a second product for an
# item already in the cart, cannot be undone by the next step. Unfamiliar
# grocery runs added "Golden Sugar" for granulated sugar at 0.50 and a second
# carton of eggs, both below this bar and above CHECK_TRIGGER. A failed check
# here escalates at once (see should_escalate).
COMMIT_TRIGGER = 0.8
CHECK_CANDIDATES = 3
# Act on the check's best candidate when it clears and is not the policy's pick
# ("open X" -> "Add to cart - X"). Only within the same operation's targets, or
# to an operation with no target: an operation alone names no element to click.
CHECK_SWITCH = True
# Route words that do not change which item a control acts on: "Eggs" and
# "Add to cart - Eggs" are one decision. A favourites or sign-in control is not.
_ROUTE_PREFIX = re.compile(r"^(add to cart|add item to cart|add to order|loading)\b\s*-?\s*", re.I)
_TRAILING_PRICE = re.compile(r"\s*(ca)?\$[\d.,]+$", re.I)
# Quantity controls ("Current quantity is 1", "Increase quantity by 1"), and the
# words that ask for more than one of an item. Without such words a quantity
# control can only overshoot: one live run typed into it and ordered three.
_QUANTITY_CONTROL = re.compile(r"\bquantity\b", re.I)
# An option label's details after its name: " VG 210 cal", " +CA$0.95", " 150 cal".
_OPTION_DETAILS = re.compile(r"\s(?:VG|VT|GF)\b|\s\+|\s\d", re.I)
NAME_MATCH = 0.85


def option_core(label: str) -> str:
    """An option's own name: "Brown Rice VG 210 cal Brown Rice" -> "brown rice"."""
    return _OPTION_DETAILS.split(label.strip(), maxsplit=1)[0].strip().lower()


def goal_names(goal: str, label: str) -> bool:
    """Whether the goal names this option, loosely: "pinto bean" names "Pinto
    Beans", "brown-rice" names "Brown Rice", "white rice" does not."""
    core = option_core(label)
    if not core:
        return False
    words = re.sub(r"[^a-z0-9 ]+", " ", goal.lower()).split()
    if core in " ".join(words):
        return True
    size = len(core.split())
    return any(SequenceMatcher(None, " ".join(words[i:i + size]), core).ratio() >= NAME_MATCH
               for i in range(max(0, len(words) - size + 1)))


def settled_options(observation: Observation, goal: str) -> set[str]:
    """Option labels not to offer: a radio already chosen, and the other radios
    of a group that already has a choice, unless the goal names that option.
    Prompt rules alone left runs re-choosing inside finished groups (Brown Rice
    chosen, then White Rice at 0.54, below the step check's reach)."""
    radios = [e for e in observation.elements if e.role == "radio"]
    finished = {e.group for e in radios if e.checked and e.group}
    return {e.name for e in radios
            if e.checked or (e.group in finished and not goal_names(goal, e.name))}


_COUNT = re.compile(r"\b(?:[2-9]|[1-9]\d+|two|three|four|five|six|seven|eight|nine|ten|several|"
                    r"pair|couple)\b", re.I)
# Counts that do not ask for several units of one item: "for two" (people),
# "all six" and "seven cake ingredients" (how many different items), and a
# size ("2 L", "4 kg"). Planner goals end with "Stop when the cart shows all
# seven cake ingredients", which used to offer every quantity control.
_COUNT_OF_PEOPLE_OR_ITEMS = re.compile(r"\b(?:for|all|serves?|feeds?|of the|these|those)\s*$", re.I)
_COUNT_THEN_NOT_UNITS = re.compile(
    r"^\s*(?:(?:[a-z-]+\s+){0,2}(?:items?|ingredients?|things?|products?|people|persons?|guests?|servings?|"
    r"kinds?|types?|categories|groceries|sides?|toppings?)\b|(?:kg|g|l|ml|lbs?|oz|%|litres?|liters?|pounds?|"
    r"grams?|kilograms?|ounces?|pack|count|ct)\b|-)", re.I)


def asks_several(goal: str) -> bool:
    """Whether the goal asks for more than one unit of an item. A spelled-out
    count in title case before another capitalised word is part of an item's
    name ("Three Tacos"); "for two", "all six items" and "2 L" are not
    quantities; any other count is ("3 Coca-Cola cans", "two bags of flour")."""
    for match in _COUNT.finditer(goal):
        after = goal[match.end():]
        if match.group()[0].isupper() and re.match(r"\s+[A-Z]", after):
            continue
        if _COUNT_OF_PEOPLE_OR_ITEMS.search(goal[:match.start()]) or _COUNT_THEN_NOT_UNITS.match(after):
            continue
        return True
    return False


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
    offered: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # The open modal's diagnosis, when one was sent to the policy.
    dialog: str | None = None
    dialog_p: float = 0.0
    # Confidence with "X" and "Add to cart - X" counted as one decision.
    signal: float = 1.0
    # The step check, when it ran: "cleared" or "failed". `check_p` is the best
    # candidate's score; `check_switch` names it when it is not the policy's pick.
    # With CHECK_SWITCH on, a cleared check may replace the policy's pick.
    check: str | None = None
    check_p: float = 0.0
    check_switch: str | None = None
    # True when the action above is the check's candidate, not the policy's pick.
    switched: bool = False
    # True when the action adds a named product (see `commits_product`).
    committing: bool = False
    # The product add refused before this answer (see `decide`), if any.
    vetoed: str | None = None


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
    guidance: str | None = None,
    hint_control: str | None = None,
) -> Decision:
    """Ask Jev for the next operation and target. Retries with a shrunken
    action space when the server reports the request oversized.

    A product add whose step check is hopeless (below CHECK_HOPELESS, no
    better candidate cleared) is not executed: the policy is asked once more
    without it. The check scored 0.01 for a mayonnaise dip as pasta sauce and
    0.23 for a second rice after rice was added, both picks a live run made.
    The answer then carries `vetoed`, which escalates the step for a hint."""
    ask = dict(client=client, observation=observation, goal=goal, history=history, guidance=guidance,
               hint_control=hint_control)
    first = await _decide_once(banned=banned, **ask)
    refused = first.action.label if first.action is not None else ""
    if not (first.committing and first.check == "failed" and first.check_p < CHECK_HOPELESS and not first.switched):
        return first
    second = await _decide_once(banned={*banned, refused}, **ask)
    return replace(second, vetoed=refused, usage=_usage_sum(first.usage, second.usage),
                   latency_ms=first.latency_ms + second.latency_ms)


async def _decide_once(
    *,
    client: JevClient,
    observation: Observation,
    goal: str,
    history: list[dict[str, Any]],
    banned: Iterable[str] = (),
    guidance: str | None = None,
    hint_control: str | None = None,
) -> Decision:
    space = build(observation)
    banned = {*banned, *settled_options(observation, goal)}
    # Hide a product-title link whose "Add to cart - <item>" sibling is right
    # beside it in the action space, so a commit intent cannot route through
    # the product page. Nothing fires when no add control is offered.
    banned = {*banned, *_title_sibling_bans(space)}
    # Controls the prompt already forbids but a small model still took on
    # unfamiliar pages: a favourites detour whose label names the item
    # ("Sign in to add to Favourites list, Great Value Soya Sauce", chosen at
    # 0.74), and a second product for an item already in the cart.
    banned = {*banned, *_detour_bans(observation, goal), *_duplicate_add_bans(space, goal, history)}
    if not asks_several(goal):
        banned = {*banned, *(element.name for element in observation.elements
                             if _QUANTITY_CONTROL.search(element.name or ""))}
    diagnosis = await diagnose_dialog(client=client, observation=observation, goal=goal)
    dialog = diagnosis[0] if diagnosis and diagnosis[1] >= DIALOG_KIND_MIN else None
    if dialog == "task" and not (hint_control and CLOSE_LABEL.match(hint_control)):
        # A task dialog holds what the goal needs; with its finished options
        # withheld, "Close" was the policy's pick at 0.55 and lost every choice.
        # Only a hint that names the close control (a wrong item) may close it.
        banned = {*banned, *(element.name for element in observation.elements
                             if CLOSE_LABEL.match(element.name or ""))}
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
                # While a modal is open the page behind it is inert, and its text
                # fills the 6000 characters before the dialog's own text begins
                # (DoorDash: all navigation). The dialog's headings say which
                # option groups are required, so they are what the policy reads.
                "text": (observation.dialog_text if observation.scroll_area == "dialog" and observation.dialog_text
                         else observation.text)[:1500 if attempts > 1 else 6000],
                # Loading flag is advisory — lets the policy pick Wait when the
                # page is mid-load rather than betting on a stale action.
                "loading": observation.loading,
                # Only off the page: tells the policy that SCROLL moves the
                # open dialog or panel, not the page behind it.
                **({"scroll_area": observation.scroll_area} if observation.scroll_area != "page" else {}),
                # What the open dialog is, so rule 1 closes only interruptions.
                **({"dialog": dialog} if dialog else {}),
                # Which required option groups still lack a choice, and whether
                # the add control is ready (computed by the reader, not inferred).
                **({"dialog_status": observation.dialog_status} if observation.dialog_status else {}),
            },
            "elements": [
                _element_state(idx, element) for idx, element in enumerate(observation.elements)
            ],
            # progress() keeps every page-changing action plus the recent tail,
            # capped, mirroring what old jevis sent. A naive tail loses the
            # completed items on multi-step goals and the policy restarts them.
            "recent_actions": _progress(history),
        }
        if guidance:
            # A text model's hint for this step (see policy/reinstruct.py).
            state["guidance"] = guidance
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
    # Candidates as the check reads them: the policy's pick first, then the
    # next most likely, from whichever head decided this step.
    # Ids travel with labels: a page can hold several controls with one label.
    labels: dict[str, str] = {}
    if target is not None:
        labels = {t.id: t.label for t in space.by_id(operation).targets}
        ranked = sorted(probabilities.items(), key=lambda item: -item[1])
        keyed = [(key, labels.get(key, key), p) for key, p in ranked]
        chosen_key, chosen_label = target, labels.get(target, target)
    else:
        op_labels = {op.id: op.label for op in space.operations}
        ranked = sorted(operation_answer.probabilities.items(), key=lambda item: -item[1])
        keyed = [(key, f"{key}: {op_labels.get(key, key)}", p) for key, p in ranked]
        # The check may only clear what can be done: "CLICK" alone names no
        # element. A best idea of "click something" means the policy does not
        # know what to click, so the step must fail and escalate.
        executable = [entry for entry in keyed if not space.by_id(entry[0]).targets]
        chosen_key, chosen_label = operation, f"{operation}: {op_labels.get(operation, operation)}"
    candidates = [(label, p) for _, label, p in keyed]
    signal = grouped_signal(operation_answer.confidence, chosen_label if target else None, candidates)
    check = None
    switched = False
    terminal = operation in {"DONE", "BLOCKED"}
    committing = target is not None and commits_product(chosen_label)
    if target is None and not terminal and signal < CHECK_TRIGGER:
        # Unsure between kinds of action (scroll, wait, back...): the check has
        # nothing concrete to judge. Each scroll of a loop looks reasonable alone
        # (0.69-0.73 in the 2026-10-01 replay), so it counts as failed, with no
        # call; the streak in should_escalate keeps a one-off WAIT from escalating.
        check = StepCheck(verdict="failed", best_p=0.0, switch=None, scores=(), usage={})
    elif (signal < CHECK_TRIGGER or (terminal and signal < CHECK_TERMINAL)
          or (committing and signal < COMMIT_TRIGGER)):
        pool = keyed if target is not None else executable
        shortlist = ([(chosen_key, chosen_label)]
                     + [(key, label) for key, label, _ in pool if key != chosen_key])[:CHECK_CANDIDATES]
        screen = None
        if terminal:
            screen = {"text": state["page"]["text"][:2000],
                      "controls": [element.name for element in observation.elements][:60]}
        check = await check_step(client=client, goal=goal, page=state["page"],
                                 recent_actions=state["recent_actions"],
                                 candidates=[label for _, label in shortlist], screen=screen)
        if CHECK_SWITCH and check is not None and check.switch_index:
            new_key = shortlist[check.switch_index][0]
            if target is not None:
                target, switched = new_key, True
            else:
                operation, switched = new_key, True  # the pool held only target-free operations
    if hint_control:
        # The hint named a control and the policy did not take it, while its own
        # pick failed the check or stops the run. Take the named control, if it
        # is offered and Jev's own check finds it reasonable: the text model
        # still cannot invent an action.
        chosen_now = labels.get(target, "") if target is not None else ""
        unsure = (check is not None and check.verdict == "failed") or operation in {"DONE", "BLOCKED"}
        if unsure and not _same_control(chosen_now, hint_control):
            named = _offered_control(space, set(banned), hint_control)
            if named is not None:
                follow = await check_step(client=client, goal=goal, page=state["page"],
                                          recent_actions=state["recent_actions"], candidates=[named[2]])
                if follow is not None and follow.best_p >= CHECK_PASS:
                    operation, target, switched = named[0], named[1], True
                    check = StepCheck(verdict="cleared", best_p=follow.best_p, switch=named[2],
                                      scores=follow.scores, usage=_usage_sum(check.usage if check else {},
                                                                             follow.usage))
    action = resolve(space, operation, target)
    if action is not None and action.kind == "scroll" and observation.scroll_point is not None:
        # Move the open dialog or panel, by most of its own height.
        step = observation.scroll_step or abs(action.delta)
        action = replace(action, point=observation.scroll_point, delta=step if action.delta > 0 else -step)
    return Decision(
        operation=operation,
        target=target,
        action=action,
        confidence=operation_answer.confidence,
        probabilities=probabilities,
        model=result.model,
        latency_ms=result.latency_ms,
        usage=_usage_sum(result.usage, diagnosis[2] if diagnosis else {}, check.usage if check else {}),
        offered=offered,
        dialog=dialog,
        dialog_p=diagnosis[1] if dialog and diagnosis else 0.0,
        signal=signal,
        check=check.verdict if check else None,
        check_p=check.best_p if check else 0.0,
        check_switch=check.switch if check else None,
        switched=switched,
        committing=action is not None and commits_product(action.label or ""),
    )


def _item_of(label: str) -> str:
    """The item a control acts on, with route words and a trailing price removed."""
    return _TRAILING_PRICE.sub("", _ROUTE_PREFIX.sub("", label.strip())).strip().lower()


def _routed(label: str) -> bool:
    """Whether a label is a committing route-prefixed control (`Add to cart - X`)."""
    return bool(_ROUTE_PREFIX.match(label.strip()))


def commits_product(label: str) -> bool:
    """Whether a control adds a named product: "Add to cart - Great Value Flour",
    not a dialog's own "Add to cart - CA$15.60", whose item the dialog shows."""
    return _routed(label) and bool(re.search(r"[a-z]{2}", _item_of(label)))


# Controls that leave a shopping task for an account: sign-in, a favourites or
# wish list. Withheld unless the goal itself is about an account or a list.
_DETOUR = re.compile(r"^(?:sign in|log ?in|create (?:an )?account)\b|\b(?:add|save) to (?:my )?"
                     r"(?:favou?rites|wish ?list|registry)\b", re.I)
_ACCOUNT_GOAL = re.compile(r"sign in|log ?in|account|favou?rite|wish ?list|registry", re.I)


def _detour_bans(observation: Observation, goal: str) -> set[str]:
    if _ACCOUNT_GOAL.search(goal):
        return set()
    return {element.name for element in observation.elements if element.name and _DETOUR.search(element.name)}


# The planner writes one "Search '<term>'" per item. A product's add control
# belongs to the most specific term whose words it holds ("Great Value Frozen
# Mixed Vegetables" -> 'mixed vegetables'; "Green Onion, Sold in bunches" ->
# 'green onions'). Goals without quoted terms get no item bookkeeping here.
_QUOTED_TERM = re.compile(r"\bsearch (?:for )?['\"‘“]([^'\"’”]{2,60})['\"’”]", re.I)


def goal_items(goal: str) -> list[str]:
    seen: list[str] = []
    for match in _QUOTED_TERM.finditer(goal):
        term = match.group(1).strip().lower()
        if term and term not in seen:
            seen.append(term)
    return seen


def _words(text: str) -> set[str]:
    """Lower-case words, plural 's' dropped, so "eggs" holds "egg"."""
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
            for w in re.findall(r"[a-z0-9]+", text.lower())}


def item_for(label: str, items: list[str]) -> str | None:
    """The goal item a product's add control serves, or None."""
    product = _words(_item_of(label))
    fits = [item for item in items if _words(item) and _words(item) <= product]
    return max(fits, key=lambda item: len(_words(item)), default=None)


def finished_items(goal: str, history: Iterable[Mapping[str, Any]]) -> set[str]:
    """Goal items whose product the history shows added (an add that changed the page)."""
    items = goal_items(goal)
    done: set[str] = set()
    for entry in history:
        label = str(entry.get("label") or "")
        if entry.get("operation") == "CLICK" and entry.get("page_changed") and commits_product(label):
            item = item_for(label, items)
            if item is not None:
                done.add(item)
    return done


def _duplicate_add_bans(space: ActionSpace, goal: str, history: Iterable[Mapping[str, Any]]) -> set[str]:
    """Add controls for an item already added. One cake run put three cartons
    of eggs in the cart; NEXT_ACTION forbade it in words. A goal that wants
    several of an item raises the quantity on the product already added."""
    done = finished_items(goal, history)
    if not done:
        return set()
    items = goal_items(goal)
    return {target.label for operation in space.operations if operation.id == "CLICK"
            for target in operation.targets
            if commits_product(target.label) and item_for(target.label, items) in done}


def _title_sibling_bans(space: ActionSpace) -> set[str]:
    """Labels of plain title links that share an item with a route-prefixed add
    control on the same page. The policy's grouping already treats "X" and
    "Add to cart - X" as one decision, but the model is free to pick either —
    and sometimes picks the title link, which opens the product page and makes
    the "add" subgoal a two-step journey. Banning the plain siblings when the
    direct add is present leaves one obvious control for a commit intent, and
    does nothing on pages where no add exists (then the title link is the
    right thing to click). Site-agnostic: it reads only the route-prefix
    marker already in `_ROUTE_PREFIX`.
    """
    labels: list[str] = []
    for operation in space.operations:
        if operation.id != "CLICK":
            continue
        for target in operation.targets:
            if target.label:
                labels.append(target.label)
    if not labels:
        return set()
    routed_items = {_item_of(label) for label in labels if _routed(label)}
    return {
        label for label in labels
        if not _routed(label) and _item_of(label) in routed_items
    }


def _same_control(a: str, b: str) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    return bool(a) and (a == b or a.startswith(b) or b.startswith(a)
                        or SequenceMatcher(None, a, b).ratio() >= NAME_MATCH)


def _offered_control(space: ActionSpace, banned: set[str], name: str) -> tuple[str, str, str] | None:
    """(operation, target id, label) of the offered target that `name` names."""
    for operation in space.operations:
        for candidate in operation.targets:
            if candidate.id in banned or candidate.label in banned:
                continue
            if _same_control(candidate.label, name):
                return operation.id, candidate.id, candidate.label
    return None


def grouped_signal(op_confidence: float, chosen: str | None, candidates: list[tuple[str, float]]) -> float:
    """How sure the policy is of its decision, not of one control.

    Probability split between "Eggs" and "Add to cart - Eggs" is one decision
    taken two ways, so their probabilities add. Probability split between
    different items, or different operations, stays split. `chosen` is None
    for an operation with no target: then only the operation head counts."""
    if chosen is None:
        return op_confidence
    item = _item_of(chosen)
    same = sum(p for label, p in candidates if _item_of(label) == item)
    return min(op_confidence, same)


@dataclass(frozen=True, slots=True)
class StepCheck:
    """One step check. `verdict` is "cleared" when the best candidate scores at
    least CHECK_PASS, else "failed". `switch` names the best candidate when it is
    not the policy's pick (candidates[0])."""

    verdict: str
    best_p: float
    switch: str | None
    scores: tuple[float, ...]
    usage: Mapping[str, int]
    # Index of `switch` in the candidates, or None.
    switch_index: int | None = None


async def check_step(
    *, client: JevClient, goal: str, page: Mapping[str, Any], recent_actions: list[dict[str, Any]],
    candidates: list[str], screen: Mapping[str, Any] | None = None,
) -> StepCheck | None:
    """Ask Jev whether each candidate is a reasonable next step: one Noul each,
    in one request. A Choice is relative and says which option is best; a Noul
    is absolute and says whether an option is acceptable at all, so it tells
    "several good options" apart from "no good option". None when Jev fails —
    the check is advisory and must never stop a decision."""
    state = {
        "goal": goal,
        "page": {"url": page.get("url", ""), "title": page.get("title", "")},
        "recent_actions": recent_actions,
        "candidates": candidates,
    }
    if screen:
        # DONE and BLOCKED only: what is on screen now. "Add to cart" in the
        # history is not proof; a cart badge reading 0 items is proof against.
        state["screen"] = dict(screen)
    questions = {
        f"c{j}": {"type": "noul", "instructions": {
            "question": STEP_CHECK["question"].replace("{candidate}", f"`candidates[{j}]`"),
            "rules": STEP_CHECK["rules"]}}
        for j in range(len(candidates))
    }
    try:
        result = await client.ask(state=state, questions=questions)
    except JevError:
        return None
    scores = tuple(result.nouls[f"c{j}"].noul if f"c{j}" in result.nouls else 0.0 for j in range(len(candidates)))
    best = max(range(len(scores)), key=lambda j: scores[j])
    passed = scores[best] >= CHECK_PASS
    return StepCheck(
        verdict="cleared" if passed else "failed",
        best_p=scores[best],
        switch=candidates[best] if passed and best != 0 else None,
        scores=scores,
        usage=dict(result.usage),
        switch_index=best if passed and best != 0 else None,
    )


# Below this score, the check's best candidate is no better than a weak guess —
# the policy is genuinely lost, not just a hair under the trigger. Escalate at
# once instead of waiting for a second failure in a row, because a committing
# action (add-to-cart, submit, send) taken at this level usually sticks and the
# model never recovers inside the same subgoal. 0.45 sits well below the pass
# bar (0.7) and above the "nothing scored" floor (0.0) in logged failures.
CHECK_HOPELESS = 0.45


def should_escalate(decision: Decision, previous_check_failed: bool) -> bool:
    """Escalate a failed check when it is not a one-off: the previous step
    failed too, this step ends the run (DONE, BLOCKED, which leaves no next step
    to wait for), this step adds a product (a wrong add sticks), or the check
    itself is hopeless (even the best candidate scored below `CHECK_HOPELESS`,
    so the streak-wait would spend another step on a wrong commit). A refused
    add (`vetoed`) escalates too: the policy wanted a product the check says
    is wrong, so it needs a hint more than another guess. One-off mild doubts
    on good runs stay cheap."""
    if decision.vetoed:
        return True
    if decision.check != "failed":
        return False
    if decision.check_p < CHECK_HOPELESS or decision.committing:
        return True
    return previous_check_failed or decision.operation in {"DONE", "BLOCKED"}


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


class CheckTestFailure(AssertionError):
    """An inline step-check test saw the wrong result."""


# The third control was a favourites detour; `_detour_bans` now withholds those,
# so the wrong pick here is another product of a different kind.
_SUGAR = ("Rogers Fine Granulated Sugar 4kg", "Add to cart - Rogers Fine Granulated Sugar 4kg",
          "Add to cart - Rogers Golden Yellow Sugar 2kg")


def _sugar_page() -> Observation:
    from agent.perception import Element, Rect

    elements = tuple(Element(ref=f"[data-agent-ref=e{i}]", role="button", name=name,
                             bounds=Rect(100, 100 + 50 * i, 300, 40), section="main")
                     for i, name in enumerate(_SUGAR))
    return Observation(url="https://www.walmart.ca/en/search?q=granulated+sugar", title="granulated sugar",
                       text="Rogers Fine Granulated Sugar 4kg $7.47", elements=elements, marker="m",
                       fingerprint="f", guards={}, can_go_back=True, can_scroll_up=False, can_scroll_down=True,
                       viewport=(1280, 800))


class _SplitJev:
    """Answers CLICK at `op_conf`, the targets with `target_probs`, and any step
    check with `check_scores`. Counts the check requests."""

    def __init__(self, op_conf: float, target_probs: list[float], check_scores: list[float]) -> None:
        self.op_conf, self.target_probs, self.check_scores = op_conf, target_probs, check_scores
        self.checks = 0
        self.checked: list[str] = []

    async def ask(self, *, state: dict[str, Any], questions: dict[str, Any]):  # noqa: ANN201
        from agent.providers import ChoiceAnswer, JevResult, NoulAnswer

        if "c0" in questions:
            self.checks += 1
            self.checked = list(state["candidates"])
            nouls = {f"c{j}": NoulAnswer(noul=v) for j, v in enumerate(self.check_scores)}
            return JevResult(answers={}, model="stand-in", usage={"input_tokens": 50}, latency_ms=0, nouls=nouls)
        ops = questions["operation"]["criteria"]
        rest = (1 - self.op_conf) / max(1, len(ops) - 1)
        op = ChoiceAnswer(choice="CLICK", probabilities={k: self.op_conf if k == "CLICK" else rest for k in ops},
                          confidence=self.op_conf)
        ids = list(questions["click_target"]["criteria"])
        probs = dict(zip(ids, self.target_probs))
        tgt = ChoiceAnswer(choice=max(probs, key=probs.get), probabilities=probs, confidence=max(probs.values()))
        return JevResult(answers={"operation": op, "click_target": tgt}, model="stand-in",
                         usage={"input_tokens": 1000}, latency_ms=0)


class _OpJev(_SplitJev):
    """Picks SCROLL_DOWN unsurely (0.40, CLICK 0.35); the check prefers CLICK."""

    def __init__(self) -> None:
        super().__init__(0.4, [1.0, 0.0, 0.0], [0.1, 0.9, 0.2])

    async def ask(self, *, state: dict[str, Any], questions: dict[str, Any]):  # noqa: ANN201
        from agent.providers import ChoiceAnswer, JevResult

        if "c0" in questions:
            return await super().ask(state=state, questions=questions)
        ops = list(questions["operation"]["criteria"])
        probs = {k: 0.40 if k == "SCROLL_DOWN" else 0.35 if k == "CLICK" else 0.25 / (len(ops) - 2) for k in ops}
        op = ChoiceAnswer(choice="SCROLL_DOWN", probabilities=probs, confidence=0.40)
        return JevResult(answers={"operation": op}, model="stand-in", usage={}, latency_ms=0)


class _HintJev(_SplitJev):
    """Policy unsure between the sugar controls (picks the golden sugar); the
    check scores every candidate low except "Add to cart"."""

    async def ask(self, *, state: dict[str, Any], questions: dict[str, Any]):  # noqa: ANN201
        from agent.providers import JevResult, NoulAnswer

        if "c0" in questions:
            self.checks += 1
            nouls = {f"c{j}": NoulAnswer(noul=0.9 if c == _SUGAR[1] else 0.1)
                     for j, c in enumerate(state["candidates"])}
            return JevResult(answers={}, model="stand-in", usage={}, latency_ms=0, nouls=nouls)
        return await super().ask(state=state, questions=questions)


def _radio_page(checked: str | None) -> Observation:
    from agent.perception import Element, Rect

    names = ("White Rice VG 210 cal White Rice", "Brown Rice VG 210 cal Brown Rice", "No Rice 0 cal",
             "Rice Required • Select 1")
    elements = tuple(Element(ref=f"[data-agent-ref=e{i}]", role="button" if "Select" in n else "radio", name=n,
                             bounds=Rect(500, 100 + 40 * i, 200, 30), checked=None if "Select" in n else n == checked,
                             group=None if "Select" in n else "g0", section="dialog")
                     for i, n in enumerate(names))
    return replace(_sugar_page(), elements=elements, scroll_area="dialog", dialog_text="Rice Required • Select 1")


async def _option_unit_tests() -> None:
    """Units for the option guard, the fuzzy naming, the heading filter, and the hint's control."""
    goal = "Open a bowl and choose sofritas, brown-rice, and pinto bean. Add one bowl."
    # Fuzzy naming: small wording changes still name the option; other options do not.
    named = {label: goal_names(goal, label) for label in ("Brown Rice VG 210 cal Brown Rice",
             "Pinto Beans VG 130 cal Pinto Beans", "White Rice VG 210 cal White Rice", "No Beans 0 cal")}
    if named != {"Brown Rice VG 210 cal Brown Rice": True, "Pinto Beans VG 130 cal Pinto Beans": True,
                 "White Rice VG 210 cal White Rice": False, "No Beans 0 cal": False}:
        raise CheckTestFailure(f"fuzzy naming wrong: {named}")
    # Settled options: a chosen radio and its finished group are withheld, unless named.
    settled = settled_options(_radio_page("White Rice VG 210 cal White Rice"), goal)
    if settled != {"White Rice VG 210 cal White Rice", "No Rice 0 cal"}:
        raise CheckTestFailure(f"settled options wrong (named Brown Rice must stay open): {settled}")
    if settled_options(_radio_page(None), goal):
        raise CheckTestFailure("an open group was withheld")
    # Heading filter: an option group's heading is not a click target.
    labels = [t.label for op in build(_radio_page(None)).operations for t in op.targets]
    if "Rice Required • Select 1" in labels:
        raise CheckTestFailure("group heading offered as a click")
    # Close labels: a bare "x" or "×" closes; a size option that starts with X does not.
    closes = {label: bool(CLOSE_LABEL.match(label)) for label in ("Close Salad", "×", "x", "X-Large", "X Small")}
    if closes != {"Close Salad": True, "×": True, "x": True, "X-Large": False, "X Small": False}:
        raise CheckTestFailure(f"close labels wrong: {closes}")
    # Task dialog: its close control is not offered, unless a hint names it.
    closing =replace(_radio_page("White Rice VG 210 cal White Rice"), elements=(
        *_radio_page("White Rice VG 210 cal White Rice").elements,
        replace(_radio_page(None).elements[0], ref="[data-agent-ref=e9]", role="button", name="Close Salad",
                checked=None, group=None)))

    class _TaskJev(_StandIn):
        async def ask(self, *, state: dict[str, Any], questions: dict[str, Any]):  # noqa: ANN201
            if "operation" in questions:
                self.offered = list(questions.get("click_target", {}).get("criteria", {}).values())
            return await super().ask(state=state, questions=questions)

    jev = _TaskJev("task", 0.9)
    await decide(client=jev, observation=closing, goal=goal, history=[])  # type: ignore[arg-type]
    if any("Close Salad" in str(c) for c in jev.offered):
        raise CheckTestFailure("close control offered in a task dialog")
    jev = _TaskJev("task", 0.9)
    await decide(client=jev, observation=closing, goal=goal, history=[], hint_control="Close Salad")  # type: ignore[arg-type]
    if not any("Close Salad" in str(c) for c in jev.offered):
        raise CheckTestFailure("a hint naming the close control could not close")
    # Hint control: the policy ignores the hint and fails its check; the named control is taken.
    # `_title_sibling_bans` hides the plain "Rogers Fine Granulated Sugar 4kg" title because its
    # "Add to cart - …" sibling is also offered. Only the two adds reach the model. Target_probs
    # are ordered to the surviving ids: granulated first, golden second — keep golden at 0.45 so
    # its signal stays below CHECK_TRIGGER and the check runs.
    jev = _HintJev(0.9, [0.10, 0.45], [])
    decision = await decide(client=jev, observation=_sugar_page(), goal="Add one bag of sugar to the cart.",
                            history=[], guidance="Click Add to cart.", hint_control=_SUGAR[1])  # type: ignore[arg-type]
    if decision.action is None or decision.action.label != _SUGAR[1] or not decision.switched:
        raise CheckTestFailure(f"hint control not taken: {decision.action.label if decision.action else None}")


async def _check_unit_tests() -> None:
    goal = "Search 'granulated sugar' and add one bag to the cart. Do not place the order."
    # Gap 1: one item's routes count as one decision; another product does not.
    signal = grouped_signal(0.9, _SUGAR[0], [(_SUGAR[0], 0.40), (_SUGAR[1], 0.35), (_SUGAR[2], 0.25)])
    if abs(signal - 0.75) > 1e-9:
        raise CheckTestFailure(f"routes not grouped: {signal}")
    if grouped_signal(0.9, _SUGAR[2], [(_SUGAR[0], 0.40), (_SUGAR[1], 0.35), (_SUGAR[2], 0.25)]) != 0.25:
        raise CheckTestFailure("another product was grouped with the item")
    # Gap 2: no check when the grouped signal is high. `_title_sibling_bans` hides the plain
    # title because its "Add to cart - …" sibling is offered, so only the two adds reach the
    # model. With the add at 0.85 — above CHECK_TRIGGER and COMMIT_TRIGGER — the check never runs.
    jev = _SplitJev(0.9, [0.85, 0.15], [0.9, 0.9, 0.1])
    decision = await decide(client=jev, observation=_sugar_page(), goal=goal, history=[])  # type: ignore[arg-type]
    if jev.checks or decision.check is not None:
        raise CheckTestFailure("check ran on a confident decision")
    # Gap 2: an unsure pick (the golden sugar at 0.45) is checked and logged; the
    # better candidate — the add control — is named, and the action switches to it.
    jev = _SplitJev(0.9, [0.10, 0.45], [0.10, 0.85, 0.90])  # pick first: granulated, golden
    decision = await decide(client=jev, observation=_sugar_page(), goal=goal, history=[])  # type: ignore[arg-type]
    if jev.checks != 1 or decision.check != "cleared" or decision.check_switch != _SUGAR[1]:
        raise CheckTestFailure(f"check not logged: {decision.check} {decision.check_switch}")
    if decision.action is None or decision.action.label != _SUGAR[1] or not decision.switched:
        raise CheckTestFailure(f"switch not applied: {decision.action.label if decision.action else None}")
    if decision.usage.get("input_tokens") != 1050:
        raise CheckTestFailure(f"check tokens not counted: {decision.usage}")
    cleared = decision
    # Gap 4: unsure between kinds of action (here SCROLL_DOWN 0.40 vs CLICK 0.35)
    # counts as a failed check with no Jev call, and the action stays the pick.
    jev = _OpJev()
    decision = await decide(client=jev, observation=_sugar_page(), goal=goal, history=[])  # type: ignore[arg-type]
    if jev.checks or decision.check != "failed" or decision.switched or decision.operation != "SCROLL_DOWN":
        raise CheckTestFailure(f"operation-level doubt wrong: calls={jev.checks} check={decision.check}")
    # Gap 5: quantity controls stay offered only when the goal asks for several;
    # a count inside an item's name ("Three Tacos") is not such a request.
    for text, several in (("Add two bags of flour.", True), ("Add 3 cartons of milk.", True),
                          ("Open Three Tacos and choose chicken.", False),
                          ("Add 3 Coca-Cola cans.", True),
                          ("Search chips and guacamole and add one to the cart.", False)):
        if asks_several(text) != several:
            raise CheckTestFailure(f"count detection wrong for {text!r}")
    # Gap 3: escalate on the second failure in a row, or at once for DONE/BLOCKED.
    # `cleared` switched to an add; the streak rule is for steps that commit nothing.
    failed = replace(cleared, check="failed", committing=False)
    if should_escalate(failed, previous_check_failed=False) or not should_escalate(failed, True):
        raise CheckTestFailure("streak rule wrong")
    if not should_escalate(replace(failed, committing=True), previous_check_failed=False):
        raise CheckTestFailure("failed add did not escalate at once")
    if not should_escalate(replace(failed, operation="DONE"), previous_check_failed=False):
        raise CheckTestFailure("failed DONE did not escalate at once")
    if should_escalate(cleared, previous_check_failed=True):
        raise CheckTestFailure("cleared check escalated")
    # Hopeless check (best candidate below CHECK_HOPELESS) escalates at once,
    # before a second-failure streak would let the committing action land.
    hopeless = replace(failed, check_p=CHECK_HOPELESS - 0.01)
    if not should_escalate(hopeless, previous_check_failed=False):
        raise CheckTestFailure("hopeless check did not escalate at once")


async def _check_e2e_live() -> None:
    import os

    if not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: live check tests need TYPESAFE_API_KEY")
        return
    client = JevClient()
    # End to end 1: a Walmart step unsure between opening and adding the same sugar clears.
    sugar = await check_step(
        client=client, goal="Search 'granulated sugar' and add one bag to the cart. Do not place the order.",
        page={"url": "https://www.walmart.ca/en/search?q=granulated+sugar", "title": "granulated sugar"},
        recent_actions=[{"label": "Search", "page_changed": True}], candidates=list(_SUGAR))
    print(f"live: sugar -> {sugar.verdict if sugar else None} best={sugar.best_p if sugar else 0:.2f} "
          f"switch={sugar.switch if sugar else None!r}")
    if sugar is None or sugar.verdict != "cleared":
        raise CheckTestFailure("harmless split did not clear")
    # End to end 2: DONE after a plain bowl and a background "Add item to cart" (the
    # first Chipotle run: no barbacoa was ever chosen) fails the check.
    done = await check_step(
        client=client, goal=_GOAL,
        page={"url": "https://www.doordash.com/store/chipotle-waterloo-36154775/81102878/", "title": "Chipotle"},
        recent_actions=[{"label": "Essential only", "page_changed": True},
                        {"label": "Item Search", "page_changed": True},
                        {"label": "Burrito Bowl CA$15.60", "page_changed": True},
                        {"label": "Loading Add to cart - CA$15.60", "page_changed": False},
                        {"label": "Add item to cart", "page_changed": True}],
        candidates=["DONE: Every requirement is visibly satisfied.",
                    "SCROLL_DOWN: Reveal content below the viewport.",
                    "WAIT: Wait for a running request or animation to settle."])
    print(f"live: chipotle wrong DONE -> {done.verdict if done else None} best={done.best_p if done else 0:.2f}")
    if done is None or done.verdict != "failed":
        raise CheckTestFailure("DONE without barbacoa passed the check")


def _test_title_sibling_bans() -> None:
    """Non-route title siblings get banned when a route-prefix sibling exists;
    nothing is banned on a page that only offers titles (an item configurator)
    or only offers add buttons."""
    from agent.executor import Action
    from agent.policy.action_space import ActionSpace, Operation, Target

    def tgt(label: str, idx: int) -> Target:
        action = Action(id=f"click:e{idx}", kind="click", label=label, locator=f"e{idx}")
        return Target(id=f"c{idx}", label=label, action=action)

    # Both title and add offered — the title is banned.
    mixed = ActionSpace(operations=(Operation(id="CLICK", label="Click", targets=(
        tgt("Gay Lea Salted Butter", 1),
        tgt("Add to cart - Gay Lea Salted Butter", 2),
        tgt("Gay Lea Unsalted Butter", 3),
        tgt("Add to cart - Gay Lea Unsalted Butter", 4),
        tgt("See all butter", 5),
    )),))
    bans = _title_sibling_bans(mixed)
    if bans != {"Gay Lea Salted Butter", "Gay Lea Unsalted Butter"}:
        raise AssertionError(f"wrong bans on a mixed results page: {bans}")

    # Only titles offered (item configurator, no direct add) — nothing banned.
    titles_only = ActionSpace(operations=(Operation(id="CLICK", label="Click", targets=(
        tgt("Burrito Bowl", 1),
        tgt("Barbacoa", 2),
    )),))
    if _title_sibling_bans(titles_only):
        raise AssertionError("titles-only page should not ban anything")

    # Only add buttons offered — nothing to ban.
    adds_only = ActionSpace(operations=(Operation(id="CLICK", label="Click", targets=(
        tgt("Add to cart - Flour", 1),
    )),))
    if _title_sibling_bans(adds_only):
        raise AssertionError("adds-only page should not ban anything")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_unit_tests())
    asyncio.run(_check_unit_tests())
    asyncio.run(_option_unit_tests())
    _test_title_sibling_bans()
    asyncio.run(_e2e_live())
    asyncio.run(_check_e2e_live())
    print("jev_policy.py inline dialog and step-check tests passed")
