"""The run loop.

One `Supervisor` owns one run. It coordinates observation, decision, action,
and outcome, and emits a typed event for each. Everything policy-adjacent
(retries, guards, budgets, verification) lives on the supervisor — the layers
below it stay stateless.
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
from agent.policy import Decision, NoFieldValue, decide, field_value
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
    StatusEvent,
)
from agent.transport.events import Point, Rect

from .budget import Budget
from .guards import (
    HistoryEntry,
    StaleTracker,
    combined_ban,
    is_blocked_tail,
    stale_over_limit,
    url_cycling,
)

RunStatus = str  # "running" | "done" | "blocked" | "error"


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

    async def run(self, start_url: str) -> RunState:
        state = RunState(run_id=self.run_id, goal=self.goal)
        self.state = state
        await self._publish_status(state, "running")
        done_reason = ""
        try:
            await self.executor.navigate(start_url)
            while state.status == "running":
                if state.budget.exhausted():
                    state.status = "blocked"
                    done_reason = "step budget exhausted"
                    break
                observation = await self._observe(state)
                state.observation = observation
                signal = detect_captcha(observation)
                if signal is not None:
                    await self._pause_for_human(state, signal.reason)
                    continue
                if is_blocked_tail(state.history):
                    state.status = "blocked"
                    done_reason = "no action changed the page over 4 steps"
                    break
                if url_cycling(state.history):
                    state.status = "blocked"
                    done_reason = "url cycling: last dozen navigations only revisited 2-3 pages"
                    break
                decision = await self._decide(state, observation)
                if decision.operation == "DONE":
                    # Trust the model. The verifier layer we tried on top of
                    # this — an extra Jev call that rejected DONE and pushed
                    # the model into a re-check loop — caused more failures
                    # (cart-icon spam, invalid Jev responses) than it caught
                    # premature-DONE bugs. Old jevis had no verifier and
                    # worked, and the model's own prompt already carries the
                    # rules for when DONE is legitimate.
                    state.status = "done"
                    done_reason = "goal reported met"
                    break
                if decision.operation == "BLOCKED":
                    state.status = "blocked"
                    text_hint = observation.text[:160].replace("\n", " ").strip()
                    done_reason = (
                        f"policy said no way forward on {observation.url} "
                        f"({len(observation.elements)} elements). "
                        f"Page text: {text_hint!r}"
                    )
                    break
                await self._act(state, observation, decision)
                state.budget.stepped()
        except Exception as err:  # noqa: BLE001 — report and stop, do not swallow
            state.status = "error"
            done_reason = str(err)[:200]
            await self._emit_error("supervisor", err)
        await self._publish_status(state, state.status, done_reason)
        return state

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

    async def _decide(self, state: RunState, observation: Observation) -> Decision:
        banned = combined_ban(state.history, observation.marker, covered=state.covered)
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
        try:
            decision = await decide(
                client=self.jev,
                observation=observation,
                goal=state.goal,
                history=history_for_policy,
                banned=banned,
            )
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
            decision = await decide(
                client=self.jev,
                observation=observation,
                goal=state.goal,
                history=history_for_policy,
                banned=banned,
            )
        state.budget.spent(
            decision.model,
            tokens_in=int(decision.usage.get("input_tokens", 0)),
            tokens_out=int(decision.usage.get("output_tokens", 0)),
            latency_ms=decision.latency_ms,
        )
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
        ))
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
        history_for_helper: list[dict[str, Any]] = [
            {"step": entry.step, "label": entry.action_label, "page_changed": entry.page_changed}
            for entry in state.history
        ]
        try:
            value = await field_value(
                adapter=self.text,
                goal=state.goal,
                field_name=(element.name if element else action.label) or "",
                field_role=element.role if element else "textbox",
                current_value=element.value if element else None,
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
