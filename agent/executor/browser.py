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
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    ElementHandle,
    Locator,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
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

# Fields whose "fill" ends with a submit: a searchbox, or a combobox that names
# itself a search (Walmart's "Search"). A search always submits on Enter across
# the web, so committing the fill removes one wasted step per search without
# site-specific wiring. An address or location combobox does NOT: Enter there
# picks a suggestion and can save it to the account. Textbox and spinbutton
# require an explicit submit (one of several fields in a form).
_SEARCH_NAME = re.compile(r"\b(search|find)\b|looking for", re.I)
# How long a submit may take to start a navigation before the executor falls
# back to the field's own search button.
_SUBMIT_WAIT_S = 1.5


def _commits_on_fill(role: str | None, label: str | None = None) -> bool:
    if role == "searchbox":
        return True
    return role == "combobox" and bool(_SEARCH_NAME.search(label or ""))


# ---- Reachability ----------------------------------------------------------
# Mirrors the helpers in perception/reader.js: whether a click on an element
# lands on it, and what it lands on instead. An element that paints nothing
# (opacity ~0, or an empty box with no background, border or text) is an
# invisible layer the click passes through; anything else covers.
_HIT_HELPERS = """
  const INTERACTIVE_ROLES = new Set(["button", "link", "checkbox", "radio", "switch", "tab", "menuitem",
    "menuitemradio", "option", "gridcell", "combobox", "textbox", "searchbox", "spinbutton"]);
  const PAINTED_TAGS = new Set(["A", "BUTTON", "INPUT", "SELECT", "TEXTAREA", "LABEL", "IFRAME",
    "IMG", "SVG", "CANVAS", "VIDEO", "EMBED", "OBJECT", "PICTURE"]);
  const clearColor = (color) => {
    if (!color || color === "transparent") return true;
    const parts = color.match(/[\\d.]+/g);
    return !!parts && parts.length >= 4 && parseFloat(parts[3]) < 0.05;
  };
  const opacityOf = (node) => {
    let opacity = 1;
    for (let n = node; n && n.nodeType === 1; n = n.parentElement) {
      const value = parseFloat(getComputedStyle(n).opacity);
      if (!Number.isNaN(value)) opacity *= value;
    }
    return opacity;
  };
  const paintsNothing = (node) => {
    if (opacityOf(node) < 0.05) return true;
    if (node.namespaceURI === "http://www.w3.org/2000/svg") return false;  // an icon or shape paints
    if (PAINTED_TAGS.has(node.tagName.toUpperCase())) return false;
    const role = node.getAttribute("role");
    if (role && INTERACTIVE_ROLES.has(role)) return false;
    if ([...node.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) return false;
    const style = getComputedStyle(node);
    if (style.backgroundImage !== "none" || !clearColor(style.backgroundColor)) return false;
    if (style.boxShadow !== "none") return false;
    if (style.backdropFilter && style.backdropFilter !== "none") return false;
    for (const side of ["Top", "Right", "Bottom", "Left"]) {
      if (parseFloat(style[`border${side}Width`]) > 0 && !clearColor(style[`border${side}Color`])) return false;
    }
    return true;
  };
  const forwardsTo = (hit, el) => {
    const label = hit.closest && hit.closest("label");
    return !!label && (label.control === el || label.contains(el));
  };
  const POPUP = '[role="dialog"],[role="alertdialog"],[role="listbox"],[role="menu"],[role="tooltip"],'
    + 'dialog,[aria-modal="true"],header,footer,nav,aside,iframe';
  const describe = (hit, el) => {
    let layer = hit.closest(POPUP);
    if (!layer || layer.contains(el)) {
      layer = hit;
      while (layer.parentElement && !layer.parentElement.contains(el)) layer = layer.parentElement;
    }
    const role = layer.getAttribute("role") || layer.tagName.toLowerCase();
    const text = (layer.getAttribute("aria-label") || layer.getAttribute("title") || layer.innerText || "")
      .replace(/\\s+/g, " ").trim().slice(0, 60);
    return text ? `${role} "${text}"` : role;
  };
"""

# Where a click on the element would land. Tries the centre, then four inner
# points (a badge or a neighbour can cover only the middle). Returns the best
# point found: {kind: "ok"} reaches it directly; "inert" reaches it through
# invisible layers only; "ancestor" means the element paints nothing there and
# its own container takes the click; "content" names the visible layer on top;
# "offscreen" means no inner point is inside the viewport.
_REACH_JS = "(el) => {" + _HIT_HELPERS + """
  const r = el.getBoundingClientRect();
  const RANK = {ok: 4, inert: 3, ancestor: 2, content: 1};
  let best = {kind: "offscreen"};
  for (const [fx, fy] of [[0.5, 0.5], [0.25, 0.5], [0.75, 0.5], [0.5, 0.25], [0.5, 0.75]]) {
    const x = r.x + r.width * fx;
    const y = r.y + r.height * fy;
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    let found = {kind: "ancestor", x, y};
    let through = false;
    for (const hit of document.elementsFromPoint(x, y)) {
      if (hit === el || el.contains(hit) || forwardsTo(hit, el)) {
        found = {kind: through ? "inert" : "ok", x, y};
        break;
      }
      if (hit.contains(el)) break;
      if (paintsNothing(hit)) { through = true; continue; }
      found = {kind: "content", x, y, by: describe(hit, el)};
      break;
    }
    if (found.kind === "ok") return found;
    if (!(best.kind in RANK) || RANK[found.kind] > RANK[best.kind]) best = found;
  }
  return best;
}"""

# Make the invisible layers above (x, y) transparent to the pointer so a
# trusted click there reaches `el`. Each one keeps its old inline value in
# `data-agent-peel` for `_UNPEEL_JS`. Returns whether the point now reaches
# `el`; stops at the first visible layer.
_PEEL_JS = "(el, [x, y]) => {" + _HIT_HELPERS + """
  for (let i = 0; i < 12; i++) {
    const hit = document.elementFromPoint(x, y);
    if (!hit) return false;
    if (hit === el || el.contains(hit) || forwardsTo(hit, el)) return true;
    if (hit.contains(el) || !paintsNothing(hit)) return false;
    hit.setAttribute("data-agent-peel",
      hit.style.getPropertyValue("pointer-events") + "|" + hit.style.getPropertyPriority("pointer-events"));
    hit.style.setProperty("pointer-events", "none", "important");
  }
  return false;
}"""

_UNPEEL_JS = """() => {
  for (const node of document.querySelectorAll("[data-agent-peel]")) {
    const [value, priority] = node.getAttribute("data-agent-peel").split("|");
    if (value) node.style.setProperty("pointer-events", value, priority || "");
    else node.style.removeProperty("pointer-events");
    node.removeAttribute("data-agent-peel");
  }
}"""

# Whether a focused element is a search field: what ENTER may fall back to the
# field's search button for.
_IS_SEARCH_FIELD_JS = """(el) => {
  if (!el || !el.matches) return false;
  if (el.matches('input[type="search"], [role="searchbox"]')) return true;
  if (!el.matches('input, textarea, [role="combobox"], [contenteditable="true"]')) return false;
  const name = [el.getAttribute("aria-label"), el.getAttribute("placeholder"), el.getAttribute("name"),
    el.id, el.getAttribute("title")].filter(Boolean).join(" ");
  return /\\b(search|find|q|query)\\b|looking for/i.test(name)
    || !!el.closest('[role="search"], search, form[action*="search" i]');
}"""

# A search field's own submit control: a submit button of its form (named
# Search/Go/Find, or unnamed icon), else a Search-named button in its search
# landmark or near it. Stamps it `data-agent-submit` and returns whether one
# was found. Never a clear, voice, camera or close button.
_FIND_SUBMIT_JS = """(el) => {
  for (const old of document.querySelectorAll("[data-agent-submit]")) old.removeAttribute("data-agent-submit");
  const nameOf = (b) => (b.getAttribute("aria-label") || b.getAttribute("title") || b.value || b.innerText || "")
    .replace(/\\s+/g, " ").trim();
  const searchy = (n) => /\\bsearch\\b/i.test(n) || /^\\s*(go|find|submit)\\s*$/i.test(n);
  const NOT = /\\b(clear|reset|close|cancel|voice|camera|image|photo|barcode|scan|back|remove|delete)\\b/i;
  let scope = el.form || el.closest('form, [role="search"], search');
  for (let n = el.parentElement, i = 0; !scope && n && i < 4; n = n.parentElement, i++) {
    const near = [...n.querySelectorAll('button, [role="button"]')];
    if (near.some((b) => !el.contains(b) && searchy(nameOf(b)))) scope = n;
  }
  if (!scope) return false;
  const shown = (b) => !b.disabled && b.checkVisibility() && b.getBoundingClientRect().width > 0;
  const submits = (b) => !!el.form && b.form === el.form && (b.tagName === "INPUT"
    ? ["submit", "image"].includes(b.type)
    : b.tagName === "BUTTON" && (b.getAttribute("type") || "submit").toLowerCase() === "submit");
  const buttons = [...scope.querySelectorAll('button, input[type="submit"], input[type="image"], [role="button"]')]
    .filter((b) => b !== el && !el.contains(b) && shown(b) && !NOT.test(nameOf(b)));
  const pick = buttons.find((b) => submits(b) && (searchy(nameOf(b)) || !nameOf(b)))
    || buttons.find((b) => searchy(nameOf(b)));
  if (!pick) return false;
  pick.setAttribute("data-agent-submit", "1");
  return true;
}"""


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
                await self._back()
            elif action.kind == "enter":
                await self._enter()
            elif action.kind == "wait":
                await self._wait_quiet()
        except PlaywrightTimeout as err:
            raise StalePage(f"{action.kind} target vanished before input") from err
        # Click, Enter, Back, and submit-on-fill all frequently trigger
        # navigation or a rerender. Wait for the page to settle: both DOM
        # mutations and network responses quiet for a short window. Content-
        # driven, not a fixed timeout, so a fast site returns in ~200ms while
        # a slow lazy-rendering results page (Amazon, Walmart) takes up to the
        # cap without stalling.
        submits_fill = action.kind == "fill" and _commits_on_fill(action.role, action.label)
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

    async def _back(self) -> None:
        """History back. A back the page interrupts (net::ERR_ABORTED: a redirect
        or a script navigation took over) still moved the page, and ended a cake
        run as an error on its 57th step. The next read shows where it landed."""
        try:
            await self.page.go_back(wait_until="domcontentloaded")
        except PlaywrightTimeout:
            pass
        except PlaywrightError as err:
            if "ERR_ABORTED" not in str(err) and "frame was detached" not in str(err):
                raise

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
        await self._press(locator, action.label or "")

    async def _press(self, locator: Locator, label: str) -> None:
        """Click `locator` so the click lands on it.

        Overlay check first: Walmart pops an autocomplete list over the
        results grid while the search box holds focus, so a click at an Add
        button's coordinates would hit a suggestion instead. Old jevis had
        the same check in snapshot.js. When the element is not on top:

        - a covered "Close" inside an open modal presses Escape instead
          (DoorDash lays a layer over its dialog's Close);
        - otherwise the element is scrolled to the middle of its scroller
          (a sticky header, footer or banner, or a dialog's own sticky
          title), and outside a modal Escape and a blur close a popup over
          it (an autocomplete list). Escape is never pressed inside a modal:
          it would close the dialog and lose every choice made in it;
        - a layer that paints nothing (an invisible click-catcher, a dialog
          stuck at opacity 0) is clicked through: it stops taking pointer
          events for this one trusted click. A DoorDash run lost 28 steps
          to every control on the page reporting "covered" (2026-10-01);
        - an element that paints nothing at its own points (its container
          takes the click) gets a DOM click.

        A visible layer that stays on top raises Occluded naming it.
        """
        reach = await self._reach(locator)
        if reach["kind"] != "ok":
            in_modal = await self._in_open_modal(locator)
            if in_modal and CLOSE_LABEL.match(label):
                # Escape does what the click means. Done here, with no click after it.
                await self._dismiss_overlay()
                return
            if reach["kind"] != "inert":
                await self._scroll_into_view(locator)
                reach = await self._reach(locator)
                if reach["kind"] == "content" and not in_modal:
                    await self._dismiss_overlay()
                    reach = await self._reach(locator)
        kind = reach["kind"]
        if kind == "ok":
            await self._mouse_click(reach["x"], reach["y"])
        elif kind == "inert":
            await self._click_through(locator, label, reach["x"], reach["y"])
        elif kind == "ancestor":
            try:
                await locator.evaluate("el => el.click()", timeout=2000)
            except PlaywrightTimeout as err:
                raise StalePage(f"Target {label!r} vanished before input") from err
            except Exception:  # noqa: BLE001 — the click navigated mid-evaluate: it landed
                pass
        elif kind == "content":
            raise Occluded(f"Target {label!r} covered by {reach.get('by') or 'another layer'}")
        else:
            raise Occluded(f"Target {label!r} is outside the viewport")

    async def _reach(self, locator: Locator) -> dict:
        """Where a click on `locator` lands (see `_REACH_JS`). A target that
        vanished or a page that navigated away reads as a stale page."""
        try:
            return await locator.evaluate(_REACH_JS, timeout=2000)
        except PlaywrightTimeout as err:
            raise StalePage("Click target vanished before input") from err
        except Exception as err:  # noqa: BLE001 — context destroyed: the page moved on
            raise StalePage(f"Click target unreadable: {str(err)[:120]}") from err

    async def _mouse_click(self, x: float, y: float) -> None:
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
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            })

    async def _click_through(self, locator: Locator, label: str, x: float, y: float) -> None:
        """A trusted click at (x, y) with the invisible layers above it made
        transparent to the pointer for that one click, then restored."""
        try:
            reaches = await locator.evaluate(_PEEL_JS, [x, y], timeout=2000)
            if not reaches:
                raise Occluded(f"Target {label!r} covered by a layer that appeared while clicking")
            await self._mouse_click(x, y)
        finally:
            try:
                await self.page.evaluate(_UNPEEL_JS)
            except Exception:  # noqa: BLE001 — the click navigated; nothing left to restore
                pass

    async def _scroll_into_view(self, locator: Locator) -> None:
        try:
            await locator.evaluate(
                "el => el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})",
                timeout=2000,
            )
        except Exception:  # noqa: BLE001 — best effort; the reach test that follows decides
            return
        await asyncio.sleep(0.15)

    async def _in_open_modal(self, locator: Locator) -> bool:
        """Whether the target sits inside an open modal dialog (aria-modal or a
        native modal <dialog>), as the reader defines one."""
        try:
            return bool(await locator.evaluate(
                "el => !!el.closest('[aria-modal=\"true\"], dialog:modal')", timeout=1500))
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
        # Commit a search fill (see `_commits_on_fill`). Saves one step per
        # search — the model otherwise fills, then separately clicks a Search
        # button — and keeps the loop-progress signal (url_changed) tied to
        # the fill rather than deferred to the next step. Textbox and
        # spinbutton do NOT commit here: a textbox is usually one of several
        # fields in a form (login, address) and auto-submitting on the first
        # fill would strand the rest unfilled.
        if _commits_on_fill(action.role, action.label):
            # Short wait for the autocomplete JS to mount before the Enter
            # dispatch — Walmart's combobox opens its suggestions on the last
            # keystroke, and an Enter that lands mid-mount is swallowed.
            await asyncio.sleep(0.12)
            await self._submit(locator)

    async def _press_enter(self) -> None:
        """A trusted Enter at the focused element, dispatched over CDP (the
        same input path the click uses, with the `text` a real key carries)."""
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

    async def _submit(self, field: Locator | ElementHandle | None) -> None:
        """Press Enter in the focused search field; if no navigation starts
        within `_SUBMIT_WAIT_S`, press the field's own search button.

        Enter is not always enough: an autocomplete can swallow it while its
        list mounts, and the Walmart run of 2026-10-01 filled Search four
        times in a row with nothing submitted, while clicking the "Search"
        button beside the field navigated every time. The fallback presses
        that button as part of the same fill, so one TYPE_TEXT is one
        search. A field that filters in place (no navigation) and has no
        such button is left as typed.
        """
        started = asyncio.Event()    # a navigation was requested or committed
        committed = asyncio.Event()  # the main frame moved to its new URL
        main = self.page.main_frame

        def on_request(request) -> None:  # noqa: ANN001 — playwright Request
            try:
                if request.is_navigation_request() and request.frame == main:
                    started.set()
            except Exception:  # noqa: BLE001 — a request without a frame
                pass

        def on_navigated(frame) -> None:  # noqa: ANN001 — playwright Frame
            if frame == main:
                started.set()
                committed.set()

        self.page.on("request", on_request)
        self.page.on("framenavigated", on_navigated)
        stamped = False
        try:
            await self._press_enter()
            if field is None:
                return
            try:
                await asyncio.wait_for(started.wait(), timeout=_SUBMIT_WAIT_S)
            except TimeoutError:
                pass
            if started.is_set():
                # A full page load: hold until the new URL commits, so the
                # outcome reports url_changed and the read sees the results.
                try:
                    await asyncio.wait_for(committed.wait(), timeout=8.0)
                except TimeoutError:
                    pass
                return
            try:
                if isinstance(field, Locator):  # a re-rendered field must not stall on the default timeout
                    stamped = bool(await field.evaluate(_FIND_SUBMIT_JS, timeout=1500))
                else:
                    stamped = bool(await field.evaluate(_FIND_SUBMIT_JS))
                if stamped:
                    await self._press(self.page.locator("[data-agent-submit]").first, "Search")
            except Exception:  # noqa: BLE001 — best effort: the field holds the text either way
                pass
        finally:
            self.page.remove_listener("request", on_request)
            self.page.remove_listener("framenavigated", on_navigated)
            if stamped:
                try:
                    await self.page.evaluate(
                        "() => document.querySelectorAll('[data-agent-submit]')"
                        ".forEach((b) => b.removeAttribute('data-agent-submit'))")
                except Exception:  # noqa: BLE001 — navigated away; nothing to clean
                    pass

    async def _enter(self) -> None:
        """ENTER: press Enter at the focused element. In a search field, fall
        back to its search button when Enter starts no navigation."""
        field: ElementHandle | None = None
        try:
            handle = await self.page.evaluate_handle("document.activeElement")
            field = handle.as_element()
            if field is not None and not await field.evaluate(_IS_SEARCH_FIELD_JS):
                field = None
        except Exception:  # noqa: BLE001 — no readable focus: a plain Enter
            field = None
        await self._submit(field)

    async def _wait_quiet(self) -> None:
        """WAIT: give a loading page real time — network idle (bounded), then
        the DOM settle. It used to sleep 0.1s, so the policy re-picked WAIT
        while a results page was still hydrating."""
        await asyncio.sleep(0.4)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=2500)
        except PlaywrightTimeout:
            pass
        await self._wait_for_settle(quiet_ms=300, cap_ms=2000)

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
