"""Playwright-backed executor. One browser context, one page, one CDP session.

The surface is small on purpose: navigate, act, capture. Everything policy-
adjacent (retries, settling, cycle detection) lives in the supervisor. Every
higher-level layer receives an `Executor` (a protocol) and never a Playwright
handle, so tests supply a fake with no browser at all.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Locator,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeout,
)

from .cdp_relay import CdpTabRelay, resolve_cdp_url
from .kinds import CLOSE_LABEL, Action, NavigationInterrupted, Occluded, Outcome, StalePage
from .motion import typing_delays
from .screencast import Frame, FrameSink, decode, start_params
from .stealth import init_script

_DEFAULT_PROFILE_DIR = Path.home() / ".cache" / "agent" / "profile"

# Roles whose "fill" ends with an Enter press: a search field or a search-with-
# autocomplete always submits on Enter across the web, so letting the executor
# commit the fill removes one wasted step per search without any site-specific
# wiring. Everything else (textbox, spinbutton) requires an explicit submit.
_SUBMIT_ON_FILL_ROLES = frozenset({"searchbox", "combobox"})


def _commits_on_fill(role: str | None) -> bool:
    return role in _SUBMIT_ON_FILL_ROLES


# In-page settle: resolves once the DOM stops mutating AND no NEW network
# response has completed for `quiet_ms` since the settle started, or after
# `cap_ms`. Only new activity counts — `performance.getEntriesByType` is a
# cumulative buffer from page load, so on a long-lived tab the last
# response's timestamp is stale and the check was passing trivially even
# while an XHR was still in flight. Use PerformanceObserver instead to
# see only responses that complete DURING the settle window.
_SETTLE_JS = """(([quiet, cap]) => new Promise(resolve => {
  const started = performance.now();
  let last = performance.now();
  const observer = new MutationObserver(() => { last = performance.now(); });
  observer.observe(document.documentElement, {subtree: true, childList: true, characterData: true});
  const netObserver = new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (entry.responseEnd > last) last = entry.responseEnd;
    }
  });
  try { netObserver.observe({type: 'resource', buffered: false}); } catch (_) {}
  const check = () => {
    const now = performance.now();
    if (now - last >= quiet || now - started >= cap) {
      observer.disconnect();
      netObserver.disconnect();
      resolve(Math.round(now - started));
    } else {
      setTimeout(check, 40);
    }
  };
  setTimeout(check, 40);
}))"""


class Executor(Protocol):
    """The contract every executor implementation satisfies."""

    async def navigate(self, url: str) -> Outcome: ...
    async def act(self, action: Action) -> Outcome: ...
    async def current_url(self) -> str: ...
    async def start_screencast(self, sink: FrameSink) -> None: ...
    async def stop_screencast(self) -> None: ...


def uses_relay() -> bool:
    """Whether runs attach through the one-tab relay to the user's own Chrome.
    Such runs share one debugging connection, so only one can run at a time."""
    cdp_url = os.environ.get("AGENT_CDP_URL", "").strip()
    profile = os.environ.get("AGENT_CHROME_PROFILE", "").strip()
    return bool(cdp_url) and (cdp_url.lower() == "auto" or bool(profile))


class PlaywrightExecutor:
    """Concrete Executor over Playwright + Chromium.

    Constructed through the async context manager so the browser lifecycle is
    tied to the caller's scope. The supervisor opens one per run and closes it
    when the run ends, regardless of outcome.
    """

    def __init__(self, *, headless: bool = False, viewport: tuple[int, int] = (1280, 800),
                 rng: random.Random | None = None) -> None:
        self._headless = headless
        self._viewport = viewport
        self._rng = rng or random.Random()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._cdp: CDPSession | None = None
        self._screencast_task: asyncio.Task[None] | None = None
        # True when attached to a user-owned Chrome. Governs teardown: we
        # never close a browser we did not launch.
        self._attached = False
        self._owns_page = True
        # Set when attached through the one-tab relay (AGENT_CDP_URL=auto or
        # AGENT_CHROME_PROFILE). The relay already opened the agent's tab.
        self._relay: CdpTabRelay | None = None

    # ---- lifecycle ----------------------------------------------------------

    async def _connect_cdp_with_retry(self, cdp_url: str) -> Browser:
        """Attach to a running Chrome, retrying briefly so a just-launched
        Chrome has time to open its debugging port. Fails with a clear
        message if the port is genuinely unreachable."""
        assert self._playwright is not None
        deadline = time.monotonic() + 8.0  # tuned so `chrome ... &` in another shell has time
        attempt = 0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            attempt += 1
            try:
                return await self._playwright.chromium.connect_over_cdp(cdp_url)
            except Exception as err:  # noqa: BLE001 — retry any connection error
                last_error = err
                await asyncio.sleep(0.5)
        raise RuntimeError(
            f"Could not attach to Chrome at {cdp_url} after {attempt} tries. "
            f"Start Chrome first with `--remote-debugging-port=9222 "
            f"--user-data-dir=…` and confirm http://localhost:9222/json/version "
            f"responds. Underlying error: {last_error}"
        )

    async def __aenter__(self) -> Self:
        self._playwright = await async_playwright().start()

        # ---- Path A: attach to the user's running Chrome via CDP ----------
        # This is the fingerprint the old jevis code had: real profile, real
        # cookies, real signed-in sessions. The user launches Chrome once
        # with `--remote-debugging-port=9222 --user-data-dir=...`, and we
        # open a new tab in that browser. Sites see a returning user.
        cdp_url = os.environ.get("AGENT_CDP_URL", "").strip()
        profile = os.environ.get("AGENT_CHROME_PROFILE", "").strip()
        if cdp_url:
            # A personal Chrome (many tabs, several profiles) goes through the
            # relay: Playwright then sees only the one tab the relay opens.
            if uses_relay():
                self._relay = CdpTabRelay(resolve_cdp_url(cdp_url), profile=profile or None)
                cdp_url = await self._relay.start()
            self._browser = await self._connect_cdp_with_retry(cdp_url)
            # An attached Chrome already has contexts and tabs. Reuse the
            # first context so we inherit the user's cookies; open a new
            # tab so the user's existing tabs are untouched.
            contexts = self._browser.contexts
            self._context = contexts[0] if contexts else await self._browser.new_context()
            if self._relay is not None and self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = await self._context.new_page()
            self._attached = True
            self._owns_page = True
            # Emulate focus over CDP instead of `bring_to_front()`. Focus
            # emulation lets the tab render and dispatch focus events without
            # asking the window manager to raise the browser window. The old
            # `bring_to_front` call stole focus from whatever app the user was
            # typing in whenever the agent focused a field, which broke every
            # attempt at running the agent in the background.
            try:
                session = await self._context.new_cdp_session(self._page)
                await session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            except Exception:  # noqa: BLE001 — non-fatal; falls back to real focus
                pass
            # Do NOT add_init_script here — it would leak into every tab in
            # the user's browser and change their real browsing session.
            return self

        # ---- Path B: launch a fresh Chromium/Chrome with persistent profile
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process,Translate",
            "--disable-dev-shm-usage",
            "--no-default-browser-check",
            "--no-first-run",
        ]
        channel = os.environ.get("AGENT_CHROME_CHANNEL") or None

        profile_env = os.environ.get("AGENT_PROFILE_DIR", "").strip()
        use_profile = profile_env.lower() != "none"
        profile_path = Path(profile_env) if profile_env and use_profile else _DEFAULT_PROFILE_DIR

        context_common = dict(
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            locale="en-US",
            # The machine's own time zone unless AGENT_TIMEZONE names one (an IANA
            # name such as "America/Toronto"); never a fixed place.
            timezone_id=os.environ.get("AGENT_TIMEZONE", "").strip() or None,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Linux"',
            },
        )

        if use_profile:
            profile_path.mkdir(parents=True, exist_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(profile_path),
                headless=self._headless,
                channel=channel,
                args=args,
                ignore_default_args=["--enable-automation"],
                **context_common,
            )
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                channel=channel,
                args=args,
                ignore_default_args=["--enable-automation"],
            )
            self._context = await self._browser.new_context(**context_common)

        await self._context.add_init_script(init_script())
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        if self._screencast_task is not None:
            self._screencast_task.cancel()
        # Attached mode: never close the user's Chrome. The run's tab stays open,
        # so the user sees where the run ended (the cart, for a demo).
        # AGENT_CLOSE_TAB=1 closes the tabs this run opened (its own, and any
        # popups from it), so test suites do not pile tabs up in the browser.
        if self._attached:
            close = os.environ.get("AGENT_CLOSE_TAB", "").strip().lower() in {"1", "true", "yes"}
            if close and self._owns_page and self._context is not None:
                opened = [self._page] if self._relay is None else list(self._context.pages)
                for page in opened:  # through the relay, every page listed is one this run opened
                    try:
                        if page is not None and not page.is_closed():
                            await page.close()
                    except Exception:  # noqa: BLE001 — a tab already gone is fine
                        pass
            if self._browser is not None:
                try:
                    await self._browser.close()  # disconnects CDP; Chrome and the tab keep running
                except Exception:  # noqa: BLE001
                    pass
            if self._relay is not None:
                await self._relay.close()
        else:
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    # ---- accessors used by perception --------------------------------------

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Executor not started")
        return self._page

    async def _ensure_cdp(self) -> CDPSession:
        if self._cdp is None:
            if self._context is None or self._page is None:
                raise RuntimeError("Executor not started")
            self._cdp = await self._context.new_cdp_session(self._page)
        return self._cdp

    @property
    def cdp(self) -> CDPSession:
        if self._cdp is None:
            raise RuntimeError("CDP session not opened; call _ensure_cdp() first")
        return self._cdp

    # ---- navigation --------------------------------------------------------

    async def navigate(self, url: str) -> Outcome:
        started = time.perf_counter()
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        except PlaywrightTimeout as err:
            raise NavigationInterrupted(f"Navigation to {url!r} timed out") from err
        load_ms = int((time.perf_counter() - started) * 1000)
        return Outcome(
            page_changed=True,
            url_changed=response is not None and response.url != url,
            load_ms=load_ms,
            final_url=self.page.url,
        )

    async def current_url(self) -> str:
        return self.page.url

    # ---- action dispatch ---------------------------------------------------

    async def act(self, action: Action) -> Outcome:
        before_url = self.page.url
        started = time.perf_counter()
        try:
            if action.kind == "click":
                await self._click(action)
            elif action.kind == "fill":
                await self._fill(action)
            elif action.kind == "select":
                await self._select(action)
            elif action.kind == "scroll":
                await self._scroll(action)
            elif action.kind == "back":
                await self.page.go_back(wait_until="domcontentloaded")
            elif action.kind == "enter":
                await self.page.keyboard.press("Enter")
            elif action.kind == "wait":
                await asyncio.sleep(0.1)
        except PlaywrightTimeout as err:
            raise StalePage(f"{action.kind} target vanished before input") from err
        # Click, Enter, Back, and submit-on-fill all frequently trigger
        # navigation or a rerender. Wait for the page to settle: both DOM
        # mutations and network responses quiet for a short window. Content-
        # driven, not a fixed timeout, so a fast site returns in ~200ms while
        # a slow lazy-rendering results page (Amazon, Walmart) takes up to the
        # cap without stalling.
        submits_fill = action.kind == "fill" and _commits_on_fill(action.role)
        if action.kind in {"click", "enter", "back"} or submits_fill:
            await self._wait_for_settle()
        url_changed = self.page.url != before_url
        # A URL change means a new document. Give the SPA router time to
        # render its new tree — the first settle only saw the page unmount,
        # not the remount. Do NOT programmatically scroll here: some sites
        # (Walmart) gate their lazy-load hydration on real user scroll
        # events, and a `window.scrollTo` doesn't count. Reset-to-top would
        # then leave the page in a shell state the reader can't do anything
        # with. Old jevis has no post-nav scroll for this same reason.
        if url_changed and action.kind != "back":
            # Playwright's real network-idle signal (500ms of no in-flight
            # requests). `_wait_for_settle` uses the DOM MutationObserver +
            # `performance.getEntriesByType('resource')`, but the entries
            # buffer is cumulative from page load — on a long-lived Chrome
            # session `lastResource().responseEnd` is a stale timestamp and
            # the settle exits early while the products XHR is still in
            # flight. Wait for genuine network idle so the reader sees the
            # SPA's product-load response, not the shell.
            try:
                await self.page.wait_for_load_state("networkidle", timeout=3000)
            except PlaywrightTimeout:
                pass
            await self._prime_lazy_hydration()
            await self._wait_for_settle()
        load_ms = int((time.perf_counter() - started) * 1000)
        return Outcome(
            page_changed=True,
            url_changed=url_changed,
            load_ms=load_ms,
            final_url=self.page.url,
        )

    async def _wait_for_settle(self, quiet_ms: int = 150, cap_ms: int = 1500) -> None:
        """Return once the page has been quiet for `quiet_ms`, or after `cap_ms`.

        Quiet means: no DOM mutations AND no completed network responses in
        the trailing `quiet_ms`. Runs inside the page via a MutationObserver
        and `performance.getEntriesByType('resource')`, so the wait is exactly
        as long as the page needs and no longer. The cap protects against
        sites that keep long-poll or telemetry connections open forever.

        Ported from old jevis's SETTLE snippet — the wait was the single
        biggest lever that made the pre-rewrite behaviour reliable, and a
        hardcoded fixed timeout is a bandaid the site's own signals replace.
        """
        try:
            await self.page.evaluate(_SETTLE_JS, [quiet_ms, cap_ms])
        except Exception:  # noqa: BLE001 — non-fatal; a stale locator later will retry
            pass

    async def _locator(self, action: Action) -> Locator:
        if action.locator is None:
            raise ValueError(f"{action.kind} action requires a locator")
        primary = self.page.locator(action.locator)
        try:
            await primary.wait_for(state="visible", timeout=1500)
            return primary
        except PlaywrightTimeout:
            pass
        # Primary stamp is gone — a client-side re-render replaced the node
        # between observe and act. Fall back to an accessible-name lookup on
        # the element's role. Amazon and other SPAs do this all the time on
        # search results and product grids.
        fallback = self._name_locator(action)
        if fallback is None:
            raise StalePage(f"Locator not visible: {action.locator}")
        try:
            await fallback.wait_for(state="visible", timeout=1500)
        except PlaywrightTimeout as err:
            raise StalePage(f"Locator not visible: {action.locator} (name fallback also missed)") from err
        return fallback

    def _name_locator(self, action: Action) -> Locator | None:
        """Best-effort accessible-name lookup for a stale ref."""
        if not action.label or not action.role:
            return None
        # Playwright's role names line up with our reader's role vocabulary for
        # the common cases (button, link, textbox, checkbox, radio, ...). Names
        # from complex controls can be truncated at 200 chars upstream; match
        # loosely so a still-visible element with the same visible name wins.
        try:
            return self.page.get_by_role(action.role, name=action.label, exact=False).first  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return None

    async def _click(self, action: Action) -> None:
        locator = await self._locator(action)
        box = await locator.bounding_box()
        if box is None:
            raise Occluded(f"No box for locator {action.locator}")
        target = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        # Overlay check. Before firing mouse events at raw coordinates,
        # verify the topmost element at that pixel IS the intended target
        # (or a descendant). Walmart pops an autocomplete dropdown over the
        # results grid whenever the search box holds focus, so a click at
        # the Add-to-cart button's coordinates actually hits the dropdown
        # suggestion and navigates to a wrong search. Old jevis has this
        # check in snapshot.js and returns CoveredTarget on failure.
        # We first try to dismiss common overlays (blur the active field,
        # press Escape). If the target is still occluded, raise Occluded so
        # the supervisor bans the label and re-decides.
        if not await self._point_hits_target(locator, target):
            if CLOSE_LABEL.match(action.label or "") and await self._in_open_modal(locator):
                # The target closes its own dialog and is covered (DoorDash's
                # overlay layer sits over "Close"): Escape does what the click
                # means. Done here, with no click after it.
                await self._dismiss_overlay()
                return
            if await self._in_open_modal(locator):
                # Inside an open modal the cover is the dialog's own sticky
                # header or footer, not an overlay to dismiss: Escape would
                # close the dialog and lose every choice made in it. Bring the
                # target into view inside the dialog and test again.
                await locator.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})")
                await asyncio.sleep(0.15)
            else:
                await self._dismiss_overlay()
            if not await self._point_hits_target(locator, target):
                box = await locator.bounding_box()
                target = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2) if box else target
            if not await self._point_hits_target(locator, target):
                raise Occluded(f"Target {action.label!r} covered by an overlay")
            # Overlay dismissal may have shifted layout; refresh coordinates.
            box = await locator.bounding_box()
            if box is None:
                raise Occluded(f"Target {action.label!r} vanished after dismiss")
            target = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        # Dispatch mouse press/release via CDP directly, WITHOUT a prior
        # mousemove. `page.mouse.move` would fire a `mousemove` DOM event
        # at the target coordinate, and Walmart product cards react to
        # mouseover by popping quick-view / promo overlays that then
        # intercept the click. Old jevis (`browser.py:315`) uses
        # `Input.dispatchMouseEvent` with `mousePressed` / `mouseReleased`
        # for exactly this reason — no synthetic hover ever fires. The dev
        # UI's animated cursor stays driven by CursorMove events on the
        # transport bus, so the user still sees smooth motion.
        cdp = await self._ensure_cdp()
        for kind in ("mousePressed", "mouseReleased"):
            await cdp.send("Input.dispatchMouseEvent", {
                "type": kind,
                "x": target[0],
                "y": target[1],
                "button": "left",
                "clickCount": 1,
            })

    async def _point_hits_target(self, locator: Locator, point: tuple[float, float]) -> bool:
        """Whether elementFromPoint at `point` is the located element or a
        descendant. False on any exception — treat unknown as blocked and
        let the caller try to dismiss the overlay.
        """
        try:
            handle = await locator.element_handle(timeout=500)
            if handle is None:
                return False
            hit = await self.page.evaluate_handle(
                "({x, y}) => document.elementFromPoint(x, y)",
                {"x": point[0], "y": point[1]},
            )
            same = await self.page.evaluate(
                "([target, hit]) => target === hit || (hit && target.contains(hit))",
                [handle, hit],
            )
            return bool(same)
        except Exception:  # noqa: BLE001 — treat any failure as "not verified"
            return False

    async def _in_open_modal(self, locator: Locator) -> bool:
        """Whether the target sits inside an open modal dialog (aria-modal or a
        native modal <dialog>), as the reader defines one."""
        try:
            return bool(await locator.evaluate(
                "el => !!el.closest('[aria-modal=\"true\"], dialog:modal')"))
        except Exception:  # noqa: BLE001 — unknown: fall back to the old path
            return False

    async def _dismiss_overlay(self) -> None:
        """Best-effort overlay dismissal: blur the active element and press
        Escape. Covers the two common cases — an open searchbox autocomplete
        (blur closes it) and a modal (Escape closes it).
        """
        try:
            await self.page.evaluate(
                "() => { if (document.activeElement && document.activeElement.blur) "
                "document.activeElement.blur(); }"
            )
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.15)
        except Exception:  # noqa: BLE001 — non-fatal
            pass

    async def _fill(self, action: Action) -> None:
        if action.value is None:
            raise ValueError("fill action requires a value")
        locator = await self._locator(action)
        await locator.focus()
        await self.page.keyboard.press("ControlOrMeta+A")
        # One Playwright call with an average per-char delay. This still fires
        # keydown/keyup for every character (what bot detection watches) but
        # avoids one IPC round-trip per character.
        delays = typing_delays(action.value, self._rng)
        mean_delay = sum(delays) // len(delays) if delays else 0
        await self.page.keyboard.type(action.value, delay=mean_delay)
        # Commit on Enter for roles that mean "submit on Enter" everywhere:
        # a searchbox (any search field, URL bar, filter input) and a combobox
        # (autocomplete-backed search, address lookup). Saves one step per
        # search — the model otherwise fills, then separately clicks a Search
        # button — and keeps the loop-progress signal (url_changed) tied to
        # the fill rather than deferred to the next step. Textbox and
        # spinbutton do NOT commit here: a textbox is usually one of several
        # fields in a form (login, address) and auto-submitting on the first
        # fill would strand the rest unfilled.
        if _commits_on_fill(action.role):
            # Short wait for the autocomplete JS to mount before the Enter
            # dispatch — Walmart's combobox opens its suggestions on the last
            # keystroke, and an Enter that lands mid-mount is swallowed.
            await asyncio.sleep(0.12)
            # Dispatch via CDP, the same path the click uses. `keyboard.press`
            # routes to whatever element currently holds focus; on an SPA with
            # an autocomplete popup, that focus can be the popup, not the
            # input, and the submit handler never fires. `Input.dispatchKeyEvent`
            # sends the raw key event at the page level with the trusted bit
            # set, which Walmart's search handler accepts.
            cdp = await self._ensure_cdp()
            for kind, text in (("keyDown", "\r"), ("keyUp", "")):
                await cdp.send("Input.dispatchKeyEvent", {
                    "type": kind,
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                    **({"text": text, "unmodifiedText": text} if text else {}),
                })

    async def _select(self, action: Action) -> None:
        locator = await self._locator(action)
        await locator.select_option(action.value)

    async def _prime_lazy_hydration(self) -> None:
        """Nudge the page with real wheel events so lazy-loaded content renders.

        Walmart's search results page ships a nav-only shell (~23 elements),
        then hydrates products only after the IntersectionObserver sees a
        real user scroll. `window.scrollTo` doesn't fire the wheel/scroll
        events those observers watch, so we dispatch a small wheel down +
        wheel up via CDP — same input path a real user takes. The page ends
        back at scroll_y=0 with products rendered, so the reader captures a
        top-of-page observation with the best products in view rather than
        letting the model burn steps scrolling.
        """
        if self.page.url in {"", "about:blank"}:
            return
        cdp = await self._ensure_cdp()
        for delta_y in (300, -300):
            try:
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel",
                    "x": 400,
                    "y": 400,
                    "deltaX": 0,
                    "deltaY": delta_y,
                })
            except Exception:  # noqa: BLE001 — priming is best-effort
                return

    async def _scroll(self, action: Action) -> None:
        # Dispatch a wheel event at a fixed viewport point via CDP. Using
        # `page.mouse.wheel` first fires a `mousemove` at the current
        # cursor position, and if that position happens to be over a
        # Walmart product tile the resulting mouseover pops an overlay
        # that changes the layout before the wheel event lands. Old jevis
        # sends `Input.dispatchMouseEvent` with `mouseWheel` at a fixed
        # (550, 650) for the same reason.
        cdp = await self._ensure_cdp()
        # Fixed (400, 400) is inside every reasonable viewport. Old jevis
        # uses (550, 650). Absolute value doesn't matter for a wheel event
        # as long as it's inside a scrollable region — which an open dialog
        # is not: a wheel beside it lands on the backdrop of a locked page.
        # The reader then supplies the dialog's (or panel's) own centre.
        x, y = action.point or (400, 400)
        await cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": x,
            "y": y,
            "deltaX": 0,
            "deltaY": action.delta,
        })

    # ---- screencast --------------------------------------------------------

    async def start_screencast(self, sink: FrameSink) -> None:
        if self._screencast_task is not None:
            return
        cdp = await self._ensure_cdp()
        queue: asyncio.Queue[dict] = asyncio.Queue()

        def on_frame(event: dict) -> None:
            queue.put_nowait(event)

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send("Page.startScreencast", start_params())

        async def pump() -> None:
            try:
                while True:
                    event = await queue.get()
                    frame = decode(event)
                    await sink(frame)
                    try:
                        await cdp.send(
                            "Page.screencastFrameAck",
                            {"sessionId": frame.session_frame_id},
                        )
                    except Exception:
                        # Ack is best-effort — a lost ack pauses the stream but
                        # never fails the run.
                        pass
            except asyncio.CancelledError:
                pass

        self._screencast_task = asyncio.create_task(pump(), name="screencast-pump")

    async def stop_screencast(self) -> None:
        if self._screencast_task is None:
            return
        try:
            if self._cdp is not None:
                await self._cdp.send("Page.stopScreencast")
        finally:
            self._screencast_task.cancel()
            try:
                await self._screencast_task
            except asyncio.CancelledError:
                pass
            self._screencast_task = None


# ---- test double ----------------------------------------------------------


class FakeExecutor:
    """In-memory Executor for unit tests. Records every call, returns fixed
    outcomes, and exposes queued frames through the sink."""

    def __init__(self, *, url: str = "about:blank") -> None:
        self._url = url
        self.calls: list[tuple[str, Action | str | None]] = []
        self._sink: FrameSink | None = None

    async def navigate(self, url: str) -> Outcome:
        self.calls.append(("navigate", url))
        prev, self._url = self._url, url
        return Outcome(page_changed=True, url_changed=prev != url, load_ms=0, final_url=url)

    async def act(self, action: Action) -> Outcome:
        self.calls.append(("act", action))
        return Outcome(page_changed=True, url_changed=False, load_ms=0, final_url=self._url)

    async def current_url(self) -> str:
        return self._url

    async def start_screencast(self, sink: FrameSink) -> None:
        self._sink = sink
        self.calls.append(("start_screencast", None))

    async def stop_screencast(self) -> None:
        self._sink = None
        self.calls.append(("stop_screencast", None))

    async def emit_frame(self, frame: Frame) -> None:
        if self._sink is not None:
            await self._sink(frame)


# ---- runner --------------------------------------------------------------


async def with_executor(
    handler: Callable[[PlaywrightExecutor], Awaitable[None]],
    *,
    headless: bool = False,
) -> None:
    """Convenience runner for scripts. Not used by the supervisor, which owns
    its own executor lifecycle."""
    async with PlaywrightExecutor(headless=headless) as executor:
        await handler(executor)
