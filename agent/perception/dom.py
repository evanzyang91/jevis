"""Async DOM perception. Runs one JS reader over the current page, parses the
result into a typed Observation, and returns it.

The reader script lives in `reader.js` next to this file. Perception owns the
selector shape (`data-agent-ref="eN"`), so the executor can rely on locators it
never had to invent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .observation import Element, Observation, Rect, SelectOption

_READER = Path(__file__).with_name("reader.js").read_text()

# In-page wait: resolves once DOM mutations and completed resource loads have
# been quiet for `quiet_ms`, or after `cap_ms`. Used when a first read finds
# the page still loading — we do not want the policy deciding on a page
# whose <title> is empty and whose interactive count is 20 because the
# results have not yet hydrated. Content-driven, so it costs exactly as long
# as the site needs, no more.
_SETTLE_JS = """(([quiet, cap]) => new Promise(resolve => {
  const started = performance.now();
  let last = performance.now();
  const observer = new MutationObserver(() => { last = performance.now(); });
  observer.observe(document.documentElement, {subtree: true, childList: true, characterData: true});
  const lastResource = () => {
    const entries = performance.getEntriesByType('resource');
    return entries.length ? entries[entries.length - 1].responseEnd : 0;
  };
  const check = () => {
    const now = performance.now();
    if (now - Math.max(last, lastResource()) >= quiet || now - started >= cap) {
      observer.disconnect();
      resolve(Math.round(now - started));
    } else {
      setTimeout(check, 40);
    }
  };
  setTimeout(check, 40);
}))"""


def _rect_from(payload: dict) -> Rect:
    return Rect(
        x=float(payload.get("x", 0)),
        y=float(payload.get("y", 0)),
        w=float(payload.get("w", 0)),
        h=float(payload.get("h", 0)),
    )


def _element_from(payload: dict) -> Element:
    return Element(
        ref=payload["ref"],
        role=payload["role"],
        name=payload.get("name", ""),
        bounds=_rect_from(payload.get("bounds") or {}),
        editable=bool(payload.get("editable", False)),
        value=payload.get("value"),
        checked=payload.get("checked"),
        selected=payload.get("selected"),
        expanded=payload.get("expanded"),
        section=payload.get("section"),
        context=payload.get("context"),
        opens=payload.get("opens"),
        visible=payload.get("visible"),
        options=tuple(
            SelectOption(
                label=option.get("label", ""),
                value=option.get("value", ""),
                disabled=bool(option.get("disabled", False)),
            )
            for option in (payload.get("options") or [])
        ),
    )


def _looks_partial(state: dict) -> bool:
    """Whether the observation looks like a page still hydrating.

    `readyState` and `aria-busy` catch the coarse case, but SPAs commonly
    report themselves loaded before their main content renders (Walmart's
    search page ships the header, sets readyState=complete, then fills
    products async). A missing title is a strong signal — every real page
    sets one by the time the user is expected to interact.

    Deliberately advisory: `observe` uses this to decide whether to sample
    again, and element-count + title stability is the ultimate accept
    criterion, so a page with a genuinely-empty title returns after one
    extra settle.
    """
    if state.get("loading"):
        return True
    if len(state.get("elements") or []) < 8:
        return True
    return not (state.get("title") or "").strip()


def _fingerprint(state: dict) -> str:
    """Full-content hash of the observation. Used for playbook keys and dev-UI
    comparisons; the cheap `marker` handles the hot path."""
    payload = {
        "url": state["url"],
        "elements": [
            {"role": e["role"], "name": e["name"], "value": e.get("value")}
            for e in state["elements"]
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def observe(page: Page, *, include_text: bool = True) -> Observation:
    """Read the page once and return a typed Observation.

    Retries the read on transient navigation. Playwright raises
    "Execution context was destroyed" when the document navigates mid-eval;
    that is normal on a click that leaves the page, so we wait for the new
    document and read again.

    Then applies a content-stability loop: some sites (Walmart, Amazon)
    report `readyState=complete` before their product cards hydrate, so a
    first read sees a shell of ~20 header controls with an empty <title>.
    Sample until the element count stops growing and the title stabilises —
    marker equality is not a valid accept criterion here because the marker
    hashes the top-32 role|name pairs, which for Walmart is the stable
    header shell while products stream in below.
    """
    state = await _evaluate_reader(page, include_text=include_text)
    retries = 0
    for _ in range(5):
        if not _looks_partial(state):
            break
        try:
            await page.evaluate(_SETTLE_JS, [200, 3000])
        except Exception:  # noqa: BLE001 — non-fatal
            pass
        prev_count = len(state.get("elements") or [])
        prev_title = (state.get("title") or "").strip()
        state = await _evaluate_reader(page, include_text=include_text)
        retries += 1
        next_count = len(state.get("elements") or [])
        next_title = (state.get("title") or "").strip()
        # If neither element count nor title moved, the page has stopped
        # growing. Accept even if it is genuinely sparse — further waits
        # would be dead time.
        if next_count <= prev_count and next_title == prev_title:
            break
    state["_retries"] = retries

    elements = tuple(_element_from(item) for item in state.get("elements", []))
    viewport = tuple(state.get("viewport", (0, 0)))
    return Observation(
        url=state["url"],
        title=state.get("title", ""),
        text=state.get("text", ""),
        elements=elements,
        marker=state["marker"],
        fingerprint=_fingerprint(state),
        guards=dict(state.get("guards") or {}),
        can_go_back=bool(state.get("can_go_back", False)),
        can_scroll_up=bool(state.get("can_scroll_up", False)),
        can_scroll_down=bool(state.get("can_scroll_down", False)),
        viewport=(int(viewport[0]), int(viewport[1])),
        loading=bool(state.get("loading", False)),
        scroll_y=int(state.get("scroll_y", 0)),
        hydration_retries=int(state.get("_retries", 0)),
    )


async def _evaluate_reader(page: Page, *, include_text: bool) -> dict:
    """Run reader.js with retries for transient navigations."""
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            return await page.evaluate(_READER, {"include_text": include_text})
        except PlaywrightTimeout as err:
            last_error = err
            await page.wait_for_timeout(30)
            continue
        except Exception as err:  # noqa: BLE001 — inspect message to decide retry
            message = str(err)
            transient = (
                "Execution context was destroyed" in message
                or "context is destroyed" in message.lower()
                or "Target closed" in message
                or "frame was detached" in message
            )
            if not transient:
                raise
            last_error = err
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except PlaywrightTimeout:
                await page.wait_for_timeout(50 * (attempt + 1))
    raise RuntimeError(f"DOM reader failed after retries: {last_error}")
