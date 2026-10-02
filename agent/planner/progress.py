"""Plan progress: which steps are done, which one is active, what remains.

The supervisor keeps one `Progress` per planned run. It feeds it cheap
signals, and no model call is needed for them:

- a committing add that changed the page (`Add to cart - X`, `Add to order`),
- a typed query that was submitted (the URL changed after the fill),
- the cart badge's count (`Cart contains 12 items`, `View cart (15)`),
- the policy's DONE, once the supervisor has confirmed it.

It reads back the per-step goal the policy sees (`goal_for_policy`), the step
the search fast path types for (`active`), and whether the run is over
(`finished`).

A completion is bound to the ACTIVE step, never to a product name that has to
contain the search term: "Great Value Organic Pure Golden Sugar" finishes the
"granulated sugar" step. Names only redirect an add that plainly belongs to
another step (butter added while eggs are active) or repeats a finished one.

Pure: no browser, no model, no events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote_plus, urlparse

from .plan import Plan, SubGoal

PENDING, ACTIVE, DONE, SKIPPED = "pending", "active", "done", "skipped"
_OPEN = (PENDING, ACTIVE)

# Badge reads after an add that may show no increase before the add counts
# as failed. Walmart's badge lagged one observation in 1 of 7 adds (run
# 9ae93ef1); two reads cover that.
CONFIRM_READS = 2

_CART = r"(?:cart|bag|basket|trolley)"
# A control that commits an item: "Add to cart - X", "Add item to cart",
# "Add X to cart", "Add to order". Not "Add to favourites" or "Add to list".
_COMMIT = re.compile(
    rf"^\s*(?:add(?:\s+item)?\s+to\s+(?:your\s+|my\s+)?(?:{_CART}|order)\b"
    rf"|add\s+.{{1,80}}?\s+to\s+(?:your\s+|my\s+)?(?:{_CART}|order)\b)",
    re.I,
)
_PRODUCT_AFTER = re.compile(rf"^\s*add(?:\s+item)?\s+to\s+(?:your\s+|my\s+)?(?:{_CART}|order)\s*[-–—:,]?\s*(.*)$", re.I)
_PRODUCT_INSIDE = re.compile(rf"^\s*add\s+(.+?)\s+to\s+(?:your\s+|my\s+)?(?:{_CART}|order)\b", re.I)
_PRICE_TAIL = re.compile(r"\s*[-–—,]?\s*(?:current price\s*)?(?:ca|us|c)?\$\s?[\d.,]+.*$", re.I)
_QUANTITY = re.compile(r"quantity|increase|decrease|remove|delete|^\s*[+\-−]\s*$", re.I)
_CART_COUNTS = (
    re.compile(rf"\b{_CART}\b\D{{0,30}}?(\d{{1,3}})\s*items?\b", re.I),  # "Cart contains 12 items"
    re.compile(rf"(\d{{1,3}})\s*items?\s+in\s+(?:your\s+|the\s+)?{_CART}\b", re.I),  # "1 item in cart"
    re.compile(rf"\b{_CART}\s*\(\s*(\d{{1,3}})\s*\)", re.I),  # "View cart (15)"
    re.compile(rf"^\s*(?:view\s+|shopping\s+|my\s+)?{_CART}\s*[:,-]?\s*(\d{{1,3}})\s*$", re.I),  # "Cart 3"
)
_QUOTED = re.compile(r"['\"‘’“”]([^'\"‘’“”]{2,60})['\"‘’“”]")
_WEAK = {"a", "an", "the", "of", "and", "or", "for", "with", "to", "in", "on", "one", "all", "some"}
# Words that read as "several": product names carry them ("Large 12 Eggs",
# "Sugar, 900 g") and the policy treats any of them in its goal as a request
# for more than one, which un-hides quantity controls.
_COUNTISH = re.compile(
    r"\b(?:\S*\d\S*(?:\s*(?:g|kg|mg|ml|l|lb|lbs|oz|ct|pk|pack|count)\b)?"
    r"|dozen|pair|couple|several|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.I,
)


def is_committing(label: str) -> bool:
    """Whether a control's label commits an item to a cart, bag, or order."""
    return bool(_COMMIT.match(label or ""))


def product_of(label: str) -> str:
    """The item a committing label names: "Add to cart - Great Value Flour" ->
    "Great Value Flour". Empty when the label names only a price."""
    match = _PRODUCT_AFTER.match(label or "") or _PRODUCT_INSIDE.match(label or "")
    name = match.group(1) if match else ""
    name = _PRICE_TAIL.sub("", name).strip(" -–—:,")
    return name if re.search(r"[a-z]", name, re.I) else ""


def cart_count(names: list[str] | tuple[str, ...]) -> int | None:
    """The item count a cart badge or cart link shows, from control names in
    page order; None when no control shows one."""
    for name in names:
        if not name or is_committing(name) or name.strip().lower().startswith("add"):
            continue
        for pattern in _CART_COUNTS:
            match = pattern.search(name)
            if match:
                return int(match.group(1))
    return None


def _stem(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("sses", "shes", "ches", "xes", "oes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def words(text: str) -> set[str]:
    """Lower-case content words, plural folded: "Large 12 Eggs" -> {large, 12, egg}."""
    return {_stem(token) for token in re.findall(r"[a-z0-9]+", (text or "").lower())
            if token not in _WEAK and len(token) > 1}


def plain(text: str, limit: int = 60) -> str:
    """A name without counts or sizes, for the policy's goal (see _COUNTISH)."""
    out = re.sub(r"\s{2,}", " ", _COUNTISH.sub(" ", text or "")).strip(" ,;.-")
    return out[:limit].rstrip(" ,;.-")


def searched_for(url: str, term: str) -> bool:
    """Whether the URL's query already holds every word of the term."""
    query = unquote_plus(urlparse(url or "").query).lower()
    wanted = words(term)
    return bool(wanted) and wanted <= words(query)


@dataclass(slots=True)
class StepState:
    index: int
    step: SubGoal
    status: str = PENDING
    # What finished it: the product added, "verified: ...", or why it was skipped.
    satisfied_by: str = ""
    # The product an add credited to this step, if any.
    product: str = ""
    # Badge count before the add credited here, while the add waits for the
    # badge to rise. None: nothing to confirm.
    cart_before: int | None = None
    flat_reads: int = 0
    reverted: int = 0
    # Policy DONE claims the verifier rejected, and rewrites after BLOCKED.
    rejected_done: int = 0
    repairs: int = 0
    # Actions taken while this step was active, since it became active or
    # was last rewritten. The supervisor treats a step that stays open too
    # long as blocked, even when the policy never says BLOCKED.
    actions: int = 0
    # A query was typed while this step was active.
    typed: bool = False

    @property
    def term(self) -> str:
        """The step's item: its search term, else its first quoted phrase."""
        if self.step.search_term:
            return self.step.search_term
        quoted = _QUOTED.search(self.step.text)
        return quoted.group(1).strip() if quoted else ""

    @property
    def name(self) -> str:
        return self.term or self.step.text.rstrip(".")[:60]


@dataclass(frozen=True, slots=True)
class Change:
    """One ledger transition, for the event stream and the run log."""

    index: int
    status: str
    note: str


class Progress:
    """The per-step ledger for one planned run."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.steps = [StepState(index=i, step=step) for i, step in enumerate(plan.subgoals)]
        self.cart_start: int | None = None
        self.cart_last: int | None = None
        self.notes: list[str] = []  # adds that credited no step (duplicates), for the trace
        self._sync()

    # ---- Reading ---------------------------------------------------------

    @property
    def active(self) -> StepState | None:
        return next((s for s in self.steps if s.status in _OPEN), None)

    def finished(self) -> bool:
        return all(s.status in (DONE, SKIPPED) for s in self.steps)

    def awaiting_cart(self) -> bool:
        """An add was credited and the badge has not confirmed it yet."""
        return any(s.status == DONE and s.cart_before is not None for s in self.steps)

    def done_count(self) -> int:
        return sum(s.status == DONE for s in self.steps)

    def goal_for_policy(self) -> str:
        """What the policy, the text helper, and the hint writer read as the
        goal: the user's words, what is finished (and with what), the ONE
        active step, what comes later, and the standing rules. Finished and
        later items carry no counts or sizes (see `plain`)."""
        active = self.active
        lines = [self.plan.original_goal.strip().rstrip(".") + "."]
        done = [s for s in self.steps if s.status == DONE]
        if done:
            lines.append("Finished steps, never redo them: " + "; ".join(
                plain(s.name) + (f" (added {plain(s.product, 50)})" if plain(s.product, 50) else "")
                for s in done) + ".")
        skipped = [s for s in self.steps if s.status == SKIPPED]
        if skipped:
            lines.append("Skipped steps, never retry them: " + "; ".join(plain(s.name) for s in skipped) + ".")
        if active is not None:
            lines.append(f"Current step: {active.step.text.strip()}")
            later = [s for s in self.steps if s.status == PENDING and s is not active]
            if later:
                lines.append("Later steps, not now: " + "; ".join(plain(s.name) for s in later) + ".")
        if self.plan.constraints:
            lines.append("Rules: " + " ".join(c.strip() for c in self.plan.constraints))
        if active is not None:
            lines.append("Work only on the current step. DONE means the current step is finished; "
                         "the next step follows.")
        return "\n".join(lines)

    def snapshot(self) -> list[dict[str, str | None]]:
        return [{"text": s.step.text, "status": s.status, "satisfied_by": s.satisfied_by,
                 "search_term": s.step.search_term, "done_when": s.step.done_when} for s in self.steps]

    def summary(self) -> str:
        """One line for the run's final status reason."""
        total = len(self.steps)
        done = [s for s in self.steps if s.status == DONE]
        skipped = [s for s in self.steps if s.status == SKIPPED]
        head = f"all {total} steps done" if len(done) == total else f"{len(done)} of {total} steps done"
        parts = [head]
        if done:
            parts.append("; ".join(f"{s.name}: {s.product or s.satisfied_by}"[:90] for s in done))
        if skipped:
            parts.append("skipped " + "; ".join(f"{s.name} ({s.satisfied_by})"[:120] for s in skipped))
        open_steps = [s for s in self.steps if s.status in _OPEN]
        if open_steps:
            parts.append("not reached " + "; ".join(s.name for s in open_steps))
        if self.cart_start is not None and self.cart_last is not None:
            parts.append(f"cart {self.cart_start} -> {self.cart_last} items")
        return ". ".join(parts)[:600]

    # ---- Transitions -------------------------------------------------------

    def complete(self, state: StepState, by: str, *, product: str = "") -> Change:
        state.status, state.satisfied_by, state.product = DONE, by[:160], product[:120]
        state.cart_before, state.flat_reads = None, 0
        self._sync()
        return Change(state.index, DONE, state.satisfied_by)

    def skip(self, state: StepState, reason: str) -> Change:
        state.status, state.satisfied_by, state.cart_before = SKIPPED, reason[:160], None
        self._sync()
        return Change(state.index, SKIPPED, state.satisfied_by)

    def rewrite(self, state: StepState, step: SubGoal) -> Change:
        state.step, state.typed, state.actions = step, False, 0
        state.repairs += 1
        self.plan.subgoals[state.index] = step
        self._sync()
        return Change(state.index, state.status, f"rewritten: {step.text}")

    def _sync(self) -> None:
        """Exactly one active step, the first unfinished one; mirror it on the plan."""
        active = self.active
        for s in self.steps:
            if s.status == ACTIVE and s is not active:
                s.status = PENDING
        if active is not None:
            if active.status != ACTIVE:
                active.typed, active.actions = False, 0
            active.status = ACTIVE
        self.plan.active_index = active.index if active is not None else len(self.steps)

    # ---- Signals -----------------------------------------------------------

    def on_add(self, label: str, *, fallback_name: str = "", confirmed: bool = False) -> list[Change]:
        """A committing add changed the page. Credit the active add step, or
        the step the product plainly names; a repeat of a finished item
        credits nothing. Steps before the credited one that are not adds
        (open the store, open the item) are its prerequisites: done too."""
        product = product_of(label) or fallback_name or label
        target = self._add_target(product)
        if target is None:
            self.notes.append(f"add credited no step (a repeat): {product}")
            return []
        active = self.active
        changes: list[Change] = []
        if active is not None:
            for s in self.steps[active.index:target.index]:
                if s.status in _OPEN and s.step.done_when != "add":
                    changes.append(self.complete(s, "implied by a later add"))
        before = self.cart_last
        if self.cart_start is None and not confirmed:
            self.cart_start = before  # the cart as it was before this run's first add
        changes.append(self.complete(target, f"added {product}", product=product))
        target.cart_before = None if confirmed else before
        return changes

    def _add_target(self, product: str) -> StepState | None:
        """The step an add finishes. By default the first open add step from
        the active one on, whatever the product is called (a substitute or a
        brand name still finishes it). Names only override that default when
        they point plainly at another add step: one the product names fully
        and more specifically ("Classico Pasta Sauce" while "pasta" is
        active), or the only one it shares a word with when the default
        shares none. An override onto a finished step is a repeat: None."""
        active = self.active
        tokens = words(product)
        default = None
        if active is not None:
            default = next((s for s in self.steps[active.index:]
                            if s.status in _OPEN and s.step.done_when == "add"), None)

        def share(state: StepState) -> float:
            wanted = words(state.term)
            return len(wanted & tokens) / len(wanted) if wanted else 0.0

        adds = [s for s in self.steps if s is not default and s.step.done_when == "add" and s.term]
        mine = share(default) if default is not None else 0.0
        mine_size = len(words(default.term)) if default is not None else 0
        better = [s for s in adds if share(s) == 1.0 and (mine < 1.0 or len(words(s.term)) > mine_size)]
        if not better and mine == 0.0:
            partial = [s for s in adds if share(s) > 0.0]
            better = partial if len(partial) == 1 else []
        if not better:
            return default
        ranked = sorted(better, key=lambda s: (share(s), len(words(s.term))), reverse=True)
        if len(ranked) > 1 and (share(ranked[0]), len(words(ranked[0].term))) == \
                (share(ranked[1]), len(words(ranked[1].term))):
            return default  # two steps fit equally well: keep the active one
        pick = ranked[0]
        return None if pick.status == DONE else pick

    def on_action(self) -> StepState | None:
        """One action was taken on the active step; returns that step."""
        active = self.active
        if active is not None:
            active.actions += 1
        return active

    def on_fill(self, text: str | None) -> None:
        active = self.active
        if active is not None and text:
            active.typed = True

    def on_navigate(self, url: str) -> list[Change]:
        """The URL changed after an action. A search step is done once its
        query was typed and submitted, or the URL already searches its term."""
        active = self.active
        if active is None or active.step.done_when != "search":
            return []
        if active.typed or (active.term and searched_for(url, active.term)):
            return [self.complete(active, f"searched {active.term or 'the query'}")]
        return []

    def on_cart(self, count: int | None, *, last_click: str | None = None) -> list[Change]:
        """A badge read after an observation. A rise confirms adds waiting on
        it; CONFIRM_READS flat reads undo the add (once per step). A rise after
        a plain click no label showed as an add credits the active add step.
        `last_click` is the label of the last action if it was a click that
        changed the page."""
        if count is not None and count == 0 and (self.cart_last or 0) > 0:
            count = None  # an empty badge mid-run is a render glitch: nothing was removed
        if count is None:
            return []
        previous, self.cart_last = self.cart_last, count
        changes: list[Change] = []
        rise_explained = False
        for s in self.steps:
            if s.status != DONE or s.cart_before is None:
                continue
            if count > s.cart_before:
                s.cart_before, s.flat_reads, rise_explained = None, 0, True
                continue
            s.flat_reads += 1
            if s.flat_reads >= CONFIRM_READS and s.reverted == 0:
                s.reverted += 1
                s.status, s.satisfied_by, s.product, s.cart_before = PENDING, "", "", None
                self._sync()
                changes.append(Change(s.index, s.status, "the add did not reach the cart"))
            elif s.flat_reads >= CONFIRM_READS:
                s.cart_before = None  # second unconfirmed add of this step: trust the click
        active = self.active
        # A rise of one or two after a plain click is an add the label did not
        # show ("Add", "+"). A bigger jump is the badge loading the saved cart.
        if (not rise_explained and previous is not None and 0 < count - previous <= 2 and last_click
                and active is not None and active.step.done_when == "add" and not _QUANTITY.search(last_click)
                and not is_committing(last_click)):
            if self.cart_start is None:
                self.cart_start = previous
            changes.extend(self.on_add(last_click, confirmed=True))
        return changes
