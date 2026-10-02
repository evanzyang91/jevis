"""The run loop.

One `Supervisor` owns one run. It coordinates observation, decision, action,
and outcome, and emits a typed event for each. Everything policy-adjacent
(retries, guards, budgets, verification) lives on the supervisor — the layers
below it stay stateless.

With a plan, the supervisor also keeps the plan's ledger (`planner.Progress`):
the policy works on one step at a time, each finished step is recorded once
and never redone, a BLOCKED step is rewritten once and then skipped, and the
run ends as soon as every step is done or skipped.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

from agent.executor import Action, Executor, Occluded, Outcome, StalePage
from agent.perception import Observation, detect_captcha, observe
from agent.planner import MAX_REPAIRS, Change, Plan, Progress, cart_count, is_committing, repair, verify
from agent.planner.progress import DONE, searched_for
from agent.policy import Decision, NoFieldValue, decide, field_value, should_escalate
from agent.policy.reinstruct import HINT_STEPS, Hint, evidence_present, reinstruct
from agent.providers import JevClient, TextAdapter
from agent.transport import (
    ActionEvent,
    Bus,
    CaptchaEvent,
    CursorClick,
    CursorMove,
    CursorScroll,
    DecisionEvent,
    ErrorEvent,
    FocusPulse,
    Keystroke,
    ObservationEvent,
    OutcomeEvent,
    PlanEvent,
    StatusEvent,
)
from agent.transport.events import Point, Rect

from .budget import Budget
from .guards import (
    HistoryEntry,
    StaleTracker,
    combined_ban,
    is_blocked_tail,
    reached_purchase,
    stale_over_limit,
    url_cycling,
)

RunStatus = str  # "running" | "done" | "blocked" | "error"

# A step whose DONE the verifier rejected this many times is accepted on the
# next claim: the verifier reads page text only and can be wrong, and before
# the ledger every DONE was trusted outright.
MAX_DONE_REJECTIONS = 2
# Steps skipped in a row (no step finished in between) before the run itself
# counts as blocked: the site, not the items, is the problem.
MAX_SKIPS_IN_A_ROW = 3
# Wait before re-reading the cart badge when the run's last add has not shown
# on it yet.
CART_SETTLE_S = 1.5


def plan_event(run_id: UUID, seq: int, plan: Plan, progress: Progress | None = None, note: str = "") -> PlanEvent:
    """The plan and its ledger as one event. Before the ledger exists (the
    manager's first emission) the first step is active and the rest pending."""
    if progress is not None:
        steps: list[dict[str, Any]] = progress.snapshot()
    else:
        steps = [{"text": s.text, "status": "active" if i == plan.active_index else "pending",
                  "search_term": s.search_term, "done_when": s.done_when} for i, s in enumerate(plan.subgoals)]
    return PlanEvent(
        run_id=run_id,
        seq=seq,
        plan=[s.text for s in plan.subgoals],
        active_index=plan.active_index,
        original_goal=plan.original_goal,
        refined_goal=plan.refined_goal,
        start_url=plan.start_url,
        steps=steps,  # type: ignore[arg-type] — validated into PlanStep
        constraints=list(plan.constraints),
        stop_when=plan.stop_when or "",
        note=note[:300],
    )


def _item_context(observation: Observation) -> str:
    """A name for the item an add acts on when its label names only a price
    ("Add to cart - CA$15.60" in a DoorDash item dialog): the dialog's first
    words, else the page title."""
    if observation.scroll_area == "dialog" and observation.dialog_text:
        head = observation.dialog_text.strip().split("\n", 1)[0]
        return " ".join(head.split()[:6])
    return observation.title or ""


@dataclass(slots=True)
class RunState:
    """Everything that changes during one run.

    Held on the supervisor so tests can construct one, hand it a fake
    executor and a fake policy, and drive the loop step by step.
    """

    run_id: UUID
    goal: str
    status: RunStatus = "running"
    observation: Observation | None = None
    history: list[HistoryEntry] = field(default_factory=list)
    covered: set[str] = field(default_factory=set)
    stale: StaleTracker = field(default_factory=StaleTracker)
    budget: Budget = field(default_factory=Budget)
    captcha_resume: asyncio.Event | None = None
    captcha_token: str | None = None
    # Last cursor position emitted to the UI. The overlay draws from here to
    # the next click's centre so the animation matches the real motion.
    cursor: tuple[float, float] = (640.0, 400.0)
    # Whether the previous decision's step check failed. Escalation waits for a
    # second failure in a row, so one-off doubts do not call the LLM.
    check_failed: bool = False
    # The text model's hint from the last escalation, and the step it was given at.
    hint: Hint | None = None
    hint_step: int = 0
    # The plan's ledger; None for a run without a plan (tests, planner failure).
    progress: Progress | None = None
    # The last action's history index and item context, until the next
    # observation (with its authoritative page_changed) feeds it to the ledger.
    untracked: tuple[int, str] | None = None
    # Label of the last page-changing click that was not an add, for one
    # badge read: a badge rise right after it is an add the label did not show.
    plain_click: str | None = None
    skips_in_a_row: int = 0


@dataclass(slots=True)
class Supervisor:
    """One coordinator per run.

    Not an async context manager — the executor is. The supervisor is created,
    then `run()` drives the loop to completion, then it is thrown away.
    """

    executor: Executor
    bus: Bus
    jev: JevClient
    text: TextAdapter
    goal: str
    run_id: UUID = field(default_factory=uuid4)
    state: RunState | None = None
    # Optional. With a plan the supervisor tracks it step by step (see the
    # module docstring), and the fill fast path types the active step's
    # search term. None (test harness, no planner) keeps the whole-goal loop:
    # DONE ends the run, BLOCKED ends it, every fill asks the text helper.
    plan: Plan | None = None

    async def run(self, start_url: str) -> RunState:
        state = RunState(run_id=self.run_id, goal=self.goal)
        self.state = state
        if self.plan is not None and self.plan.subgoals:
            state.progress = Progress(self.plan)
        await self._publish_status(state, "running")
        if state.progress is not None:
            await self._publish_progress(state, "tracking started")
        done_reason = ""
        try:
            await self.executor.navigate(start_url)
            while state.status == "running":
                if state.budget.exhausted():
                    await self._publish_changes(state, self._track(state, None))
                    state.status = "blocked"
                    done_reason = self._with_progress(state, "step budget exhausted")
                    break
                observation = await self._observe(state)
                state.observation = observation
                signal = detect_captcha(observation)
                if signal is not None:
                    await self._pause_for_human(state, signal.reason)
                    continue
                if state.progress is not None and state.progress.finished():
                    if state.progress.awaiting_cart():
                        # The last add is not on the cart badge yet: one more
                        # read before ending, so an add that never landed reopens
                        # its step instead of ending the run.
                        await asyncio.sleep(CART_SETTLE_S)
                        observation = await self._observe(state)
                        state.observation = observation
                    if state.progress.finished():
                        state.status, done_reason = self._finish(state)
                        break
                if is_blocked_tail(state.history):
                    state.status = "blocked"
                    done_reason = self._with_progress(state, "no action changed the page over 4 steps")
                    break
                if url_cycling(state.history):
                    state.status = "blocked"
                    done_reason = self._with_progress(
                        state, "url cycling: last dozen navigations only revisited 2-3 pages")
                    break
                decision = await self._decide(state, observation)
                if decision.operation == "DONE" and state.progress is not None:
                    # DONE means "the current step is finished". Confirm it
                    # (cheap text-model read of the page) before moving on; a
                    # rejected claim is decided again without DONE, so the
                    # policy cannot spin on it.
                    if await self._claim_done(state, observation):
                        if state.progress.finished():
                            state.status, done_reason = self._finish(state)
                            break
                        continue
                    decision = await self._decide(state, observation, ban={"DONE"})
                if decision.operation == "DONE":
                    # No plan: trust the model. The verifier layer we tried on
                    # top of this — an extra Jev call that rejected DONE and
                    # pushed the model into a re-check loop — caused more
                    # failures (cart-icon spam, invalid Jev responses) than it
                    # caught premature-DONE bugs.
                    state.status = "done"
                    done_reason = "goal reported met"
                    break
                if decision.operation == "BLOCKED":
                    if state.progress is not None:
                        # One impossible step must not end the run: rewrite it
                        # once, else skip it and go on with the rest.
                        keep_going = await self._step_blocked(state, observation)
                        if state.progress.finished():
                            state.status, done_reason = self._finish(state)
                            break
                        if keep_going:
                            continue
                    state.status = "blocked"
                    text_hint = observation.text[:160].replace("\n", " ").strip()
                    done_reason = self._with_progress(state, (
                        f"policy said no way forward on {observation.url} "
                        f"({len(observation.elements)} elements). "
                        f"Page text: {text_hint!r}"
                    ))
                    break
                await self._act(state, observation, decision)
                state.budget.stepped()
        except asyncio.CancelledError:
            # Stop button, server shutdown, or a newer run superseding this
            # one. CancelledError is a BaseException, so the `except Exception`
            # below does not catch it — without this branch the loop exits
            # without emitting a terminal status, leaving the UI stuck on
            # "running" and the stop button looking dead.
            state.status = "blocked"
            done_reason = self._with_progress(state, "stopped by user")
            await self._publish_status(state, state.status, done_reason)
            raise
        except Exception as err:  # noqa: BLE001 — report and stop, do not swallow
            state.status = "error"
            done_reason = self._with_progress(state, str(err)[:200])
            await self._emit_error("supervisor", err)
        await self._publish_status(state, state.status, done_reason)
        return state

    # ---- Plan progress -------------------------------------------------------

    def _policy_goal(self, state: RunState) -> str:
        """The goal the policy, the text helper, and the hint writer read: the
        ledger's one-step view with a plan, else the whole goal."""
        return state.progress.goal_for_policy() if state.progress is not None else state.goal

    @staticmethod
    def _with_progress(state: RunState, reason: str) -> str:
        if state.progress is None:
            return reason
        return f"{reason}. {state.progress.summary()}"

    @staticmethod
    def _finish(state: RunState) -> tuple[RunStatus, str]:
        """Every step is done or skipped. Done if any step got done; skipped
        steps are named in the reason."""
        progress = state.progress
        assert progress is not None
        if progress.done_count() == 0:
            return "blocked", f"no step could be done. {progress.summary()}"
        return "done", progress.summary()

    def _track(self, state: RunState, observation: Observation | None) -> list[Change]:
        """Feed the ledger the last action (with the page_changed the reader
        just confirmed) and the cart badge on this observation."""
        progress = state.progress
        if progress is None:
            return []
        changes: list[Change] = []
        if state.untracked is not None:
            index, context = state.untracked
            state.untracked = None
            entry = state.history[index] if index < len(state.history) else None
            if entry is not None:
                if entry.text and entry.operation == "TYPE_TEXT":
                    progress.on_fill(entry.text)
                if entry.operation == "CLICK" and entry.page_changed:
                    if is_committing(entry.action_label):
                        changes += progress.on_add(entry.action_label, fallback_name=context)
                    else:
                        state.plain_click = entry.action_label
                if entry.url_changed and entry.url:
                    changes += progress.on_navigate(entry.url)
        if observation is not None:
            count = cart_count([element.name for element in observation.elements])
            changes += progress.on_cart(count, last_click=state.plain_click)
            state.plain_click = None
        if any(change.status == DONE for change in changes):
            state.skips_in_a_row = 0
        return changes

    async def _publish_progress(self, state: RunState, note: str) -> None:
        if state.progress is None:
            return
        await self._publish(plan_event(state.run_id, await self.bus.next_seq(), state.progress.plan,
                                       state.progress, note))

    async def _publish_changes(self, state: RunState, changes: list[Change]) -> None:
        if changes:
            await self._publish_progress(state, "; ".join(
                f"step {change.index + 1} {change.status}: {change.note}" for change in changes))

    async def _claim_done(self, state: RunState, observation: Observation) -> bool:
        """The policy said DONE while steps remain: finish the active step if
        the page confirms it. True when the step was finished."""
        progress = state.progress
        assert progress is not None
        step = progress.active
        if step is None:
            return True
        if step.rejected_done >= MAX_DONE_REJECTIONS:
            await self._publish_changes(state, [progress.complete(step, "policy said done (unconfirmed)")])
            state.skips_in_a_row = 0
            return True
        page = "\n".join(part for part in (observation.dialog_text, observation.text) if part)
        try:
            result = await verify(adapter=self.text, subgoal_text=step.step.text, check=step.step.check,
                                  page_text=page, url=observation.url)
        except Exception as err:  # noqa: BLE001 — verifier down: trust the policy, as before the ledger
            await self._emit_error("planner", err)
            await self._publish_changes(state, [progress.complete(step, "policy said done (verifier failed)")])
            state.skips_in_a_row = 0
            return True
        state.budget.spent(result.model, tokens_in=result.usage["prompt_tokens"],
                           tokens_out=result.usage["completion_tokens"], latency_ms=result.latency_ms)
        if result.met:
            await self._publish_changes(state, [progress.complete(step, f"confirmed: {result.reason}")])
            state.skips_in_a_row = 0
            return True
        step.rejected_done += 1
        await self._publish_progress(state, f"step {step.index + 1} not finished yet: {result.reason}")
        return False

    async def _step_blocked(self, state: RunState, observation: Observation) -> bool:
        """The policy said BLOCKED on the active step. Rewrite the step once
        (a broader search, the closest equivalent), else skip it. True when the
        run goes on; False when steps keep blocking and the run should end."""
        progress = state.progress
        assert progress is not None
        step = progress.active
        if step is None:
            return False
        reason = f"no way forward on {observation.url}"
        if step.repairs < MAX_REPAIRS:
            fix = None
            try:
                fix = await repair(adapter=self.text, goal=progress.plan.original_goal,
                                   rules=progress.plan.constraints, step=step.step, reason=reason,
                                   url=observation.url, page_text=observation.text)
                state.budget.spent(fix.model, tokens_in=fix.usage["prompt_tokens"],
                                   tokens_out=fix.usage["completion_tokens"], latency_ms=fix.latency_ms)
            except Exception as err:  # noqa: BLE001 — no repair: skip the step below
                await self._emit_error("planner", err)
            if fix is not None and fix.step is not None:
                await self._publish_changes(state, [progress.rewrite(step, fix.step)])
                return True
            if fix is not None:
                reason = fix.reason
        await self._publish_changes(state, [progress.skip(step, reason)])
        state.skips_in_a_row += 1
        return state.skips_in_a_row < MAX_SKIPS_IN_A_ROW

    async def _publish_status(self, state: RunState, status: str, reason: str = "") -> None:
        await self._publish(StatusEvent(
            run_id=state.run_id,
            seq=await self.bus.next_seq(),
            status=status,  # type: ignore[arg-type]
            reason=reason,
        ))

    # ---- One-shot phases ---------------------------------------------------

    async def _observe(self, state: RunState) -> Observation:
        observation = await observe(self.executor.page)  # type: ignore[attr-defined]
        # Effect polling. A click/select/fill sometimes triggers an async
        # effect (cart badge, toast, in-place row swap) that lands after the
        # settle window inside the executor. If the marker did not move,
        # poll until it does or the deadline elapses. Ported from old jevis,
        # where it was the difference between a silent-fail Add-to-cart and
        # a caught one. Scrolls and waits are exempt: their effect is either
        # immediate or a scroll-reveal counted elsewhere.
        if state.history:
            last = state.history[-1]
            if (
                last.operation in {"CLICK", "SELECT", "TYPE_TEXT"}
                and observation.marker == last.marker
            ):
                deadline = time.monotonic() + (
                    3.0 if last.operation != "TYPE_TEXT" else 1.0
                )
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                    observation = await observe(self.executor.page)  # type: ignore[attr-defined]
                    if observation.marker != last.marker:
                        break
            # Retroactive correction: executor reports page_changed from a
            # coarse signal; the reader's marker is authoritative.
            actual_change = last.marker != observation.marker
            if last.page_changed and not actual_change:
                state.history[-1] = replace(last, page_changed=False)
        # The ledger reads the corrected page_changed: an add the page ignored
        # finishes nothing.
        await self._publish_changes(state, self._track(state, observation))
        state.budget.loaded(0)
        await self._publish(ObservationEvent(
            run_id=state.run_id,
            seq=await self.bus.next_seq(),
            source="dom",
            url=observation.url,
            title=observation.title,
            fingerprint=observation.fingerprint,
            element_count=len(observation.elements),
            text_preview=observation.text[:240],
            marker=observation.marker,
            scroll_y=observation.scroll_y,
            scroll_area=observation.scroll_area,
            hydration_retries=observation.hydration_retries,
            loading=observation.loading,
            viewport_w=observation.viewport[0],
            viewport_h=observation.viewport[1],
            elements=[
                [idx, element.role, (element.name or "")[:80]]
                for idx, element in enumerate(observation.elements)
            ],
        ))
        return observation

    async def _decide(self, state: RunState, observation: Observation, ban: frozenset[str] | set[str] = frozenset(),
                      ) -> Decision:
        """One policy decision on this observation. `ban` adds operations or
        labels to hide for this decision only (DONE after a rejected claim)."""
        banned = combined_ban(state.history, observation.marker, covered=state.covered) | set(ban)
        goal = self._policy_goal(state)
        history_for_policy = [
            {
                "step": entry.step,
                "operation": entry.operation,
                "target": entry.target,
                "label": entry.action_label,
                "text": entry.text,
                "page_changed": entry.page_changed,
                "url_changed": entry.url_changed,
            }
            for entry in state.history
        ]
        # A hint lasts HINT_STEPS steps, and only while its evidence is on the page.
        if state.hint is not None and not (
            len(state.history) - state.hint_step < HINT_STEPS and evidence_present(state.hint, observation)
        ):
            state.hint = None
        guidance = state.hint.guidance if state.hint is not None else None
        hint_control = state.hint.control if state.hint is not None else None
        decision = await self._ask_policy(state, observation, goal, history_for_policy, banned, guidance,
                                          hint_control)
        escalate = should_escalate(decision, state.check_failed)
        state.check_failed = decision.check == "failed"
        if escalate:
            # Stuck: a text model reads the page and writes a hint, then the
            # policy decides again with it. The policy still picks the action.
            pick = f"{decision.operation} {decision.action.label if decision.action else ''}".strip()
            hint = await reinstruct(adapter=self.text, goal=goal, observation=observation,
                                    history=history_for_policy, policy_pick=pick)
            if hint is not None:
                state.budget.spent(hint.model, tokens_in=hint.usage["prompt_tokens"],
                                   tokens_out=hint.usage["completion_tokens"], latency_ms=hint.latency_ms)
                state.hint, state.hint_step, guidance = hint, len(state.history), hint.guidance
                decision = await self._ask_policy(state, observation, goal, history_for_policy, banned,
                                                  guidance, hint.control)
                if decision.operation in {"DONE", "BLOCKED"} and decision.check == "failed":
                    # The check rejected this stop and the hint did not change it:
                    # keep working. Decide once more without that operation.
                    decision = await self._ask_policy(state, observation, goal, history_for_policy,
                                                      {*banned, decision.operation}, guidance, hint.control)
                state.check_failed = False  # the hint resets the streak
        await self._publish(DecisionEvent(
            run_id=state.run_id,
            seq=await self.bus.next_seq(),
            operation=decision.operation,
            choice=decision.target or decision.operation,
            target=decision.target,
            probabilities=dict(decision.probabilities),
            confidence=decision.confidence,
            latency_ms=decision.latency_ms,
            model=decision.model,
            banned=sorted(banned),
            offered={op: list(labels) for op, labels in decision.offered.items()},
            dialog=decision.dialog or "",
            dialog_p=decision.dialog_p,
            signal=decision.signal,
            check=decision.check or "",
            check_p=decision.check_p,
            check_switch=decision.check_switch or "",
            switched=decision.switched,
            escalate=escalate,
            guidance=guidance or "",
        ))
        return decision

    async def _ask_policy(self, state: RunState, observation: Observation, goal: str, history: list[dict],
                          banned: set[str], guidance: str | None, hint_control: str | None) -> Decision:
        """One policy decision, charged to the run's budget. Every call in a
        step, hinted or not, gets the same stale-page and retry handling."""
        ask = dict(client=self.jev, observation=observation, goal=goal, history=history,
                   banned=banned, guidance=guidance, hint_control=hint_control)
        try:
            decision = await decide(**ask)
        except StalePage as err:
            state.stale = state.stale.bump(observation.marker, len(state.covered))
            if stale_over_limit(state.stale):
                state.status = "blocked"
            raise err
        except Exception as err:  # noqa: BLE001 — retry a rejected-answer error once
            # A rejected Jev answer executes nothing, so asking again is not
            # a mutation retry. Large repetitive action spaces provoke this
            # often enough that a one-shot retry was worth several failed
            # runs in old jevis. Any other error propagates unchanged.
            if "Invalid Jev response" not in str(err):
                raise
            decision = await decide(**ask)
        state.budget.spent(
            decision.model,
            tokens_in=int(decision.usage.get("input_tokens", 0)),
            tokens_out=int(decision.usage.get("output_tokens", 0)),
            latency_ms=decision.latency_ms,
        )
        return decision

    async def _act(self, state: RunState, observation: Observation, decision: Decision) -> None:
        action: Action = decision.action
        text: str | None = None
        # Emit the intent as soon as we know the decision. The UI's last-event
        # kind now reflects the actual stage even while the text helper runs
        # or the browser waits on navigation.
        await self._publish(ActionEvent(
            run_id=state.run_id,
            seq=await self.bus.next_seq(),
            action_kind=action.kind,
            target_label=action.label,
            node_id=None,
            text=None,
        ))
        if action.kind == "fill":
            try:
                text = await self._compose_text(state, observation, action)
            except StalePage as err:
                # Record the attempted fill so the next decision's
                # already_taken streak bans this label at this marker — but
                # only at this marker. A permanent ban here (via
                # state.covered) strands multi-item shopping runs: one
                # helper decline on the Search field turns Search into a
                # forbidden action forever, and the model spirals into
                # Cart/Homepage/Grocery clicks with no way back to
                # searching. Marker-scoped ban lets the model try again on
                # the next page.
                state.history.append(HistoryEntry(
                    step=len(state.history) + 1,
                    operation=decision.operation,
                    target=decision.target,
                    action_id=action.id,
                    action_label=action.label,
                    marker=observation.marker,
                    page_changed=False,
                    url_changed=False,
                    url=observation.url,
                ))
                await self._emit_error("policy", err)
                return
            action = Action(
                id=action.id,
                kind=action.kind,
                label=action.label,
                locator=action.locator,
                value=text,
                delta=action.delta,
            )
            # Value-based no-op: field already holds the value we would type.
            # Skip the browser call but still append a history entry so
            # `repeated_label` and `inert_labels` see the attempt and can
            # ban the label if the model keeps picking it. Old jevis records
            # a page_changed=False noop for exactly this reason.
            current_value = None
            element = observation.by_ref(action.locator or "")
            if element is not None:
                current_value = element.value
            if current_value is not None and current_value == text:
                state.history.append(HistoryEntry(
                    step=len(state.history) + 1,
                    operation=decision.operation,
                    target=decision.target,
                    action_id=action.id,
                    action_label=action.label,
                    marker=observation.marker,
                    page_changed=False,
                    url_changed=False,
                    text=text,
                ))
                return
        # Emit cursor teleport BEFORE the browser action. With duration_ms=0
        # the arrow snaps to the target instantly, then the click fires, then
        # frames of the click's effect stream in. That matches the natural
        # "mouse arrives → click → effect" order a user expects.
        #
        # An earlier version emitted after `executor.act` returned, so the
        # frames of the effect (up to 3s of settle/networkidle) arrived first
        # and the cursor jumped in afterwards — the user saw the page change,
        # then the cursor caught up. Teleport-before is the correct order and
        # duration_ms=0 means no wasted animation window between arrival and
        # click.
        target_bounds = self._bounds_for(observation, action)
        if target_bounds is not None:
            await self._emit_cursor_move(state, target_bounds, action.kind)
            await self._emit_cursor_effect(state, target_bounds, action)
        try:
            outcome: Outcome = await self.executor.act(action)
        except Occluded:
            state.covered.add(action.label)
            await self._emit_error("executor", RuntimeError("Target covered"))
            return
        except StalePage:
            state.covered.add(action.label)
            await self._emit_error("executor", RuntimeError("Stale target"))
            return
        if reached_purchase(outcome.final_url, state.goal):
            # Hard stop, whatever the prompt said: leave the checkout page and
            # ban the control that led there for the rest of the run.
            state.covered.add(action.label)
            await self._emit_error("supervisor", RuntimeError(
                f"Purchase guard: {action.label!r} led to {outcome.final_url[:80]}; went back"))
            await self.executor.act(Action(id="BACK", kind="back", label="Go back"))
            return
        state.history.append(HistoryEntry(
            step=len(state.history) + 1,
            operation=decision.operation,
            target=decision.target,
            action_id=action.id,
            action_label=action.label,
            marker=observation.marker,
            page_changed=outcome.page_changed,
            url_changed=outcome.url_changed,
            text=action.value if action.kind == "fill" else None,
            url=outcome.final_url,
        ))
        # The ledger reads this action at the next observation, once the
        # reader has confirmed whether the page really changed.
        state.untracked = (len(state.history) - 1, _item_context(observation))
        state.budget.loaded(outcome.load_ms)
        await self._publish(OutcomeEvent(
            run_id=state.run_id,
            seq=await self.bus.next_seq(),
            page_changed=outcome.page_changed,
            url_changed=outcome.url_changed,
            guard_held=True,
            load_ms=outcome.load_ms,
        ))

    def _bounds_for(self, observation: Observation, action: Action) -> tuple[float, float, float, float] | None:
        """Look up the target element's bounding rect for the cursor overlay."""
        if action.kind == "scroll" and action.point is not None:
            return (action.point[0] - 1, action.point[1] - 1, 2, 2)  # the wheel point
        if not action.locator:
            return None
        element = observation.by_ref(action.locator)
        if element is None or element.bounds.w <= 0 or element.bounds.h <= 0:
            return None
        return (element.bounds.x, element.bounds.y, element.bounds.w, element.bounds.h)

    async def _emit_cursor_move(
        self, state: RunState, bounds: tuple[float, float, float, float], kind: str,
    ) -> None:
        cx = bounds[0] + bounds[2] / 2
        cy = bounds[1] + bounds[3] / 2
        intent = "click" if kind == "click" else ("scroll" if kind == "scroll" else "hover")
        # Teleport (duration_ms=0). The overlay places the arrow at the target
        # instantly and seeds a fading trail along the A→B path so the motion
        # still reads as a swipe. Any non-zero animation duration makes the
        # cursor lag the frame — a 280ms tween runs while the browser has
        # already painted the click's effect, so the arrow visibly catches up
        # afterwards. Teleport eliminates that mismatch entirely without
        # losing the motion cue.
        await self._publish(CursorMove(
            run_id=state.run_id,
            seq=await self.bus.next_seq(),
            **{"from": Point(x=state.cursor[0], y=state.cursor[1])},
            to=Point(x=cx, y=cy),
            duration_ms=0,
            intent=intent,  # type: ignore[arg-type]
        ))
        state.cursor = (cx, cy)

    async def _emit_cursor_effect(
        self, state: RunState, bounds: tuple[float, float, float, float], action: Action,
    ) -> None:
        cx = bounds[0] + bounds[2] / 2
        cy = bounds[1] + bounds[3] / 2
        if action.kind == "click":
            await self._publish(CursorClick(
                run_id=state.run_id,
                seq=await self.bus.next_seq(),
                x=cx, y=cy,
            ))
        elif action.kind == "fill":
            await self._publish(FocusPulse(
                run_id=state.run_id,
                seq=await self.bus.next_seq(),
                rect=Rect(x=bounds[0], y=bounds[1], w=bounds[2], h=bounds[3]),
            ))
            if action.value:
                await self._publish(Keystroke(
                    run_id=state.run_id,
                    seq=await self.bus.next_seq(),
                    text=action.value,
                    secret=False,
                ))
        elif action.kind == "scroll":
            await self._publish(CursorScroll(
                run_id=state.run_id,
                seq=await self.bus.next_seq(),
                x=cx, y=cy, delta_y=float(action.delta),
            ))

    async def _compose_text(self, state: RunState, observation: Observation, action: Action) -> str:
        element = observation.by_ref(action.locator or "")
        role = element.role if element else "textbox"
        current_value = element.value if element else None
        # Fast path: a search field while the active plan step has a search
        # term. The planner already knew the exact query at plan time, so
        # re-asking the text helper on every fill is pure overhead (one model
        # round-trip, 1-2s on the happy path, 10s+ on the slow tail). The term
        # is always the ACTIVE step's, so a finished item is never searched
        # again. When the field already holds that term:
        # - not searched yet (the URL does not search it): return it unchanged.
        #   The fill is a no-op the guards then ban, which pushes the policy to
        #   submit instead of retyping the same query.
        # - already searched: the policy re-fills on purpose, probably because
        #   the results did not serve; the helper writes a more specific query.
        active = state.progress.active if state.progress is not None else None
        term = active.term if active is not None else ""
        if role in {"searchbox", "combobox"} and term:
            holds_term = bool(current_value) and current_value.strip().lower() == term.lower()
            if not holds_term:
                return term
            if not searched_for(observation.url, term):
                return current_value or term
        history_for_helper: list[dict[str, Any]] = [
            {"step": entry.step, "label": entry.action_label, "page_changed": entry.page_changed}
            for entry in state.history
        ]
        try:
            value = await field_value(
                adapter=self.text,
                goal=self._policy_goal(state),
                field_name=(element.name if element else action.label) or "",
                field_role=role,
                current_value=current_value,
                page_text=observation.text,
                history=history_for_helper,
            )
        except NoFieldValue:
            raise StalePage("Text helper declined the field") from None
        state.budget.spent(
            value.model,
            tokens_in=int(value.usage.get("prompt_tokens", 0)),
            tokens_out=int(value.usage.get("completion_tokens", 0)),
            latency_ms=value.latency_ms,
        )
        return value.text

    # ---- Human hand-off ---------------------------------------------------

    async def _pause_for_human(self, state: RunState, reason: str) -> None:
        """Emit CaptchaEvent, then block until the UI POSTs the resume token."""
        state.captcha_resume = asyncio.Event()
        state.captcha_token = secrets.token_urlsafe(16)
        await self._publish(CaptchaEvent(
            run_id=state.run_id,
            seq=await self.bus.next_seq(),
            reason=reason,
            resume_token=state.captcha_token,
        ))
        await self._publish_status(state, "paused", reason)
        await state.captcha_resume.wait()
        state.captcha_resume = None
        state.captcha_token = None
        await self._publish_status(state, "running", "resumed after human check")

    def resume_from_captcha(self, token: str) -> bool:
        """Called from the server's resume-captcha endpoint. Returns whether
        the token matched a live pause on this supervisor's run."""
        state = self.state
        if state is None or state.captcha_resume is None or state.captcha_token != token:
            return False
        state.captcha_resume.set()
        return True

    # ---- Emission ---------------------------------------------------------

    async def _publish(self, event) -> None:  # noqa: ANN001
        self.bus.publish(event)

    async def _emit_error(self, layer: str, err: BaseException) -> None:
        self.bus.publish(ErrorEvent(
            run_id=self.run_id,
            seq=await self.bus.next_seq(),
            layer=layer,
            error_kind=type(err).__name__,
            message=str(err)[:400],
        ))


# ---- Test hook -----------------------------------------------------------


async def drive_once(
    supervisor: Supervisor,
    state: RunState,
    provider: Callable[[Observation], Awaitable[Decision]],
) -> None:
    """Advance the supervisor one tick using a supplied policy.

    Used only in tests where routing through a real Jev client would require a
    network stub for one call. Real runs use `Supervisor.run`.
    """
    observation = await supervisor._observe(state)  # noqa: SLF001
    state.observation = observation
    decision = await provider(observation)
    if decision.operation == "DONE":
        state.status = "done"
        return
    if decision.operation == "BLOCKED":
        state.status = "blocked"
        return
    await supervisor._act(state, observation, decision)  # noqa: SLF001
    state.budget.stepped()


# ---- Inline tests: `uv run python -m agent.supervisor.loop` --------------------------


class LoopTestFailure(AssertionError):
    """An inline supervisor test saw the wrong result."""


async def _test_hinted_decisions() -> None:
    """Unit, stand-in policy and text model. After a hint: a rejected Jev answer
    is retried, as on the first call; a stop the check never doubted (check None,
    a confident DONE) is kept, not banned; every decision is charged once."""
    import sys

    loop = sys.modules[__name__]  # "__main__" when run with -m: patch the copy that runs

    def made(operation: str, check: str | None) -> Decision:
        return Decision(operation=operation, target=None, action=None, confidence=0.9, probabilities={},
                        model="stand-in", latency_ms=1, usage={"input_tokens": 1, "output_tokens": 0},
                        check=check)

    replies: list[Any] = [made("DONE", "failed"), ValueError("Invalid Jev response (stand-in)"),
                          made("DONE", None)]
    calls: list[set[str]] = []

    async def fake_decide(**kwargs: Any) -> Decision:
        calls.append(set(kwargs["banned"]))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def fake_reinstruct(**_: Any) -> Hint:
        return Hint(guidance="The bowl is in the cart.", evidence=(), model="stand-in",
                    usage={"prompt_tokens": 1, "completion_tokens": 1}, latency_ms=1)

    saved = loop.decide, loop.reinstruct
    loop.decide, loop.reinstruct = fake_decide, fake_reinstruct
    try:
        supervisor = Supervisor(executor=None, bus=Bus(), jev=None, text=None, goal="Order a bowl.")  # type: ignore[arg-type]
        state = RunState(run_id=supervisor.run_id, goal=supervisor.goal)
        observation = Observation(url="https://example.test/", title="Cart", text="1 item", elements=(),
                                  marker="m", fingerprint="f", guards={}, can_go_back=False,
                                  can_scroll_up=False, can_scroll_down=False, viewport=(1280, 800))
        decision = await supervisor._decide(state, observation)  # noqa: SLF001
    finally:
        loop.decide, loop.reinstruct = saved
    if decision.operation != "DONE" or replies:
        raise LoopTestFailure(f"confident DONE after a hint was not kept: {decision.operation}, left={replies}")
    if any("DONE" in banned for banned in calls):
        raise LoopTestFailure("a DONE the check never doubted was banned")
    if state.budget.tokens_in != 2 + 1:  # two decisions and the hint, each charged once
        raise LoopTestFailure(f"budget charged wrong: {state.budget.tokens_in}")


async def _test_search_term_fast_path() -> None:
    """Unit, stand-in helper. The fast path types the ACTIVE step's term, so a
    finished item is never searched again whatever its product was called.
    A field that already holds the term and was not submitted gets the same
    term back (a no-op the guards ban, so the policy submits); one already
    searched goes to the helper for a more specific query."""
    import sys

    from agent.perception import Element, Rect
    from agent.planner import Plan, SubGoal
    from agent.policy.text_helper import TextValue

    loop = sys.modules[__name__]
    helper_calls: list[str] = []

    async def fake_field_value(**kwargs: Any) -> TextValue:
        helper_calls.append(kwargs["goal"])
        return TextValue(text="large eggs", model="stand-in", usage={}, latency_ms=1)

    plan = Plan(original_goal="buy cake stuff", refined_goal="search things", start_url="https://example.test/",
                subgoals=[SubGoal(text="Search 'granulated sugar' and add one bag.", check="sugar in cart",
                                  search_term="granulated sugar", done_when="add"),
                          SubGoal(text="Search 'eggs' and add one carton.", check="eggs in cart",
                                  search_term="eggs", done_when="add")])
    supervisor = Supervisor(executor=None, bus=Bus(), jev=None, text=None, goal="g", plan=plan)  # type: ignore[arg-type]
    state = RunState(run_id=supervisor.run_id, goal="g", progress=Progress(plan))

    def page(url: str, value: str | None) -> Observation:
        box = Element(ref="[data-agent-ref=e0]", role="searchbox", name="Search", bounds=Rect(0, 0, 100, 20),
                      editable=True, value=value)
        return Observation(url=url, title="t", text="", elements=(box,), marker="m", fingerprint="f", guards={},
                           can_go_back=False, can_scroll_up=False, can_scroll_down=False, viewport=(1280, 800))

    fill = Action(id="t0", kind="fill", label="Search", locator="[data-agent-ref=e0]")
    saved = loop.field_value
    loop.field_value = fake_field_value
    try:
        first = await supervisor._compose_text(state, page("https://example.test/", None), fill)  # noqa: SLF001
        # A substitute whose name lacks "granulated" still finishes the sugar step.
        state.progress.on_add("Add to cart - Great Value Organic Pure Golden Sugar, 900 g")
        second = await supervisor._compose_text(state, page("https://example.test/s?q=sugar", "granulated sugar"),
                                                fill)  # noqa: SLF001
        unsubmitted = await supervisor._compose_text(state, page("https://example.test/s?q=sugar", "eggs"),
                                                     fill)  # noqa: SLF001
        refined = await supervisor._compose_text(state, page("https://example.test/s?q=eggs", "eggs"),
                                                 fill)  # noqa: SLF001
    finally:
        loop.field_value = saved
    if (first, second, unsubmitted, refined) != ("granulated sugar", "eggs", "eggs", "large eggs"):
        raise LoopTestFailure(f"fast path wrong: {(first, second, unsubmitted, refined)}")
    if len(helper_calls) != 1 or "Current step: Search 'eggs'" not in helper_calls[0]:
        raise LoopTestFailure(f"helper not given the one-step goal: {helper_calls}")


if __name__ == "__main__":
    asyncio.run(_test_hinted_decisions())
    asyncio.run(_test_search_term_fast_path())
    print("loop.py inline tests passed")
