"""Run manager. Owns every in-flight run and its bus.

One entry per run id. `start` spins up an executor + supervisor task; `stop`
cancels it. WebSocket handlers hold a reference to the bus and stream events
from it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from agent.executor import Frame, PlaywrightExecutor, uses_relay
from agent.memory import InMemoryPlaybook, PlaybookStore
from agent.planner import build_plan, localise, suggest_url
from agent.providers import JevClient, adapter_for
from agent.providers.registry import get
from agent.supervisor import Supervisor
from agent.transport import BudgetEvent, Bus, ErrorEvent, FileLogger, FrameEvent, PlanEvent, StatusEvent

if TYPE_CHECKING:
    from agent.transport import Subscription

log = logging.getLogger("agent.server.manager")


@dataclass
class Run:
    run_id: UUID
    goal: str
    start_url: str
    bus: Bus = field(init=False)
    logger: FileLogger = field(init=False)
    task: asyncio.Task | None = None
    executor: PlaywrightExecutor | None = None
    supervisor: Supervisor | None = None
    state: object | None = None
    started: bool = False

    def __post_init__(self) -> None:
        self.logger = FileLogger(self.run_id)
        self.bus = Bus(sink=self.logger.write)


class RunManager:
    def __init__(self, *, playbook: PlaybookStore | None = None) -> None:
        self._runs: dict[UUID, Run] = {}
        self.playbook: PlaybookStore = playbook or InMemoryPlaybook()

    def get(self, run_id: UUID) -> Run:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    async def start(self, *, goal: str, url: str, text_model: str, vision_model: str | None = None) -> Run:
        if uses_relay():
            # Runs in the user's own Chrome share one debugging connection: the
            # new run takes priority, and any older run still going stops first.
            # (A reloaded page forgets its run, but the server keeps driving it.)
            for older in [r for r in self._runs.values() if r.task is not None and not r.task.done()]:
                await self._stop_superseded(older)
        run_id = uuid4()
        # Empty start_url signals "the planner picks it". Localisation to a
        # Canadian storefront happens inside _drive so both suggested and
        # user-supplied US retailer URLs get the same treatment.
        run = Run(run_id=run_id, goal=goal, start_url=url.strip())
        self._runs[run_id] = run
        log.debug("run %s: created (goal=%r, url=%r, text_model=%s)", run_id, goal, run.start_url, text_model)
        run.task = asyncio.create_task(self._drive(run, text_model=text_model, vision_model=vision_model))
        return run

    async def stop(self, run_id: UUID) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        if run.task is not None:
            run.task.cancel()
        if run.executor is not None:
            try:
                await run.executor.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    async def _stop_superseded(self, run: Run) -> None:
        """Stop an older run for a newer one, and say so on its stream."""
        log.info("run %s: stopped, a newer run takes the browser", run.run_id)
        try:
            run.bus.publish(StatusEvent(run_id=run.run_id, seq=await run.bus.next_seq(), status="blocked",
                                        reason="Stopped: a newer task took over the browser."))
        except Exception:  # noqa: BLE001 — the stop matters more than the notice
            log.exception("failed to publish superseded status")
        await self.stop(run.run_id)
        if run.task is not None:
            # Let its exit steps run (the relay hands the connection back) before the new run starts.
            await asyncio.wait({run.task}, timeout=10)

    def resume_captcha(self, run_id: UUID, token: str) -> bool:
        run = self._runs.get(run_id)
        if run is None or run.supervisor is None:
            return False
        return run.supervisor.resume_from_captcha(token)

    async def _emit_error(self, run: Run, layer: str, err: BaseException, *, recoverable: bool = False) -> None:
        try:
            run.bus.publish(ErrorEvent(
                run_id=run.run_id,
                seq=await run.bus.next_seq(),
                layer=layer,
                error_kind=type(err).__name__,
                message=str(err)[:400],
                recoverable=recoverable,
            ))
        except Exception:  # noqa: BLE001
            log.exception("failed to publish error event")

    async def _drive(self, run: Run, *, text_model: str, vision_model: str | None) -> None:
        del vision_model  # reserved for the vision fallback path
        try:
            # Immediate heartbeat so the UI knows the run task is alive even
            # before the planner and browser come up.
            run.bus.publish(BudgetEvent(
                run_id=run.run_id, seq=await run.bus.next_seq(),
                steps=0, model_ms=0, load_ms=0, usd=0.0,
            ))
            log.debug("run %s: heartbeat emitted", run.run_id)

            if not os.environ.get("TYPESAFE_API_KEY"):
                await self._emit_error(run, "server", RuntimeError(
                    "TYPESAFE_API_KEY not set in the environment where `uv run agent` runs."))
                log.error("run %s: TYPESAFE_API_KEY missing", run.run_id)
                return

            text_info = get(text_model)
            adapter_env = "ANTHROPIC_API_KEY" if text_info.provider == "anthropic" else "OPENAI_API_KEY"
            if not os.environ.get(adapter_env):
                await self._emit_error(run, "server", RuntimeError(
                    f"{adapter_env} not set for text_model={text_model}."))
                log.error("run %s: %s missing", run.run_id, adapter_env)
                return

            text_adapter = adapter_for(text_model, text_info.provider)

            # If the user did not supply a URL, ask the text model to pick one
            # from the goal ("buy cake ingredients from Walmart" → walmart.com).
            # Then localise US retailers to their Canadian storefront so the
            # session runs against the correct catalog and pricing.
            if not run.start_url:
                try:
                    suggested = await suggest_url(adapter=text_adapter, goal=run.goal)
                    run.start_url = localise(suggested)
                    log.debug("run %s: suggested start url %s", run.run_id, run.start_url)
                except Exception as err:  # noqa: BLE001
                    log.warning("run %s: site suggestion failed (%s); defaulting to Google", run.run_id, err)
                    run.start_url = "https://www.google.com/"
            else:
                run.start_url = localise(run.start_url)

            plan = None
            try:
                log.debug("run %s: planning...", run.run_id)
                plan = await build_plan(adapter=text_adapter, goal=run.goal, url=run.start_url)
                log.debug("run %s: planned, %d subgoals", run.run_id, len(plan.subgoals))
                run.bus.publish(PlanEvent(
                    run_id=run.run_id,
                    seq=await run.bus.next_seq(),
                    plan=[subgoal.text for subgoal in plan.subgoals],
                    active_index=0,
                    original_goal=plan.original_goal,
                    refined_goal=plan.refined_goal,
                    start_url=plan.start_url,
                ))
            except Exception as err:  # noqa: BLE001
                log.warning("run %s: planner failed (%s); continuing without a plan", run.run_id, err)

            jev = JevClient()
            # `AGENT_HEADLESS=1` runs Chromium without a window (screencast still
            # works). Default is headed so the user can watch the tab directly.
            headless = os.environ.get("AGENT_HEADLESS", "").strip().lower() in {"1", "true", "yes"}
            log.debug("run %s: launching Chromium (headless=%s)...", run.run_id, headless)
            async with PlaywrightExecutor(headless=headless) as executor:
                run.executor = executor
                log.debug("run %s: Chromium up, starting screencast", run.run_id)

                async def sink(frame: Frame) -> None:
                    run.bus.publish(FrameEvent(
                        run_id=run.run_id,
                        seq=await run.bus.next_seq(),
                        data_b64=base64.b64encode(frame.data).decode(),
                        capture_w=frame.capture_w,
                        capture_h=frame.capture_h,
                        device_ratio=frame.device_ratio,
                    ))

                await executor.start_screencast(sink)
                log.debug("run %s: screencast started, navigating and running supervisor", run.run_id)

                supervisor = Supervisor(
                    executor=executor,
                    bus=run.bus,
                    jev=jev,
                    text=text_adapter,
                    goal=plan.refined_goal if plan is not None else run.goal,
                    run_id=run.run_id,
                )
                run.supervisor = supervisor
                run.started = True
                start_url = plan.start_url if plan is not None else run.start_url
                run.state = await supervisor.run(start_url)
                # The run's totals, once, for the UI's results card: the
                # heartbeat above is the only other budget event.
                budget = run.state.budget
                run.bus.publish(BudgetEvent(
                    run_id=run.run_id, seq=await run.bus.next_seq(),
                    steps=budget.steps, model_ms=budget.model_ms, load_ms=budget.load_ms, usd=budget.usd,
                ))
                await executor.stop_screencast()
                log.debug("run %s: finished", run.run_id)
        except asyncio.CancelledError:
            log.debug("run %s: cancelled", run.run_id)
            raise
        except Exception as err:  # noqa: BLE001 — report and stop
            log.exception("run %s: fatal error", run.run_id)
            await self._emit_error(run, "server", err)
        finally:
            run.logger.close()

    async def subscribe_events(self, run_id: UUID) -> "Subscription":
        run = self.get(run_id)
        return run.bus.subscribe("events")

    async def subscribe_frames(self, run_id: UUID) -> "Subscription":
        run = self.get(run_id)
        return run.bus.subscribe("frames")

    def unsubscribe(self, run_id: UUID, subscription) -> None:  # noqa: ANN001
        run = self._runs.get(run_id)
        if run is None:
            return
        run.bus.unsubscribe(subscription)
