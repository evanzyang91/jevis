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
        group=payload.get("group"),
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
        scroll_area=str(state.get("scroll_area") or "page"),
        scroll_point=_point(state.get("scroll_point")),
        scroll_step=int(state["scroll_step"]) if state.get("scroll_step") else None,
        dialog_text=state.get("dialog_text") or None,
        dialog_status=state.get("dialog_status") or None,
        hydration_retries=int(state.get("_retries", 0)),
    )


def _point(raw: object) -> tuple[int, int] | None:
    """`[x, y]` from the reader, or None."""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return (int(raw[0]), int(raw[1]))
    return None


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


# ---- Inline tests: `uv run python -m agent.perception.dom` ---------------------------
# Headless Chromium on fixed HTML. One unit test per scroll gap, then two end to
# end through PlaywrightExecutor and `decide()` with a stand-in Jev answer.


class ScrollTestFailure(AssertionError):
    """An inline scroll test saw the wrong result."""


# A locked page behind a modal whose 300px scroller holds the options, as on
# DoorDash: Chicken and Steak in view, Barbacoa clipped below the box but still
# inside the viewport, Carnitas below the viewport.
_MODAL_PAGE = """<!doctype html><html><body style="margin:0;overflow:hidden">
<button style="position:fixed;left:10px;top:10px">Background add</button>
<div role="dialog" aria-modal="true" style="position:fixed;left:455px;top:40px;width:560px;background:#fff">
  <button style="height:20px">Close</button>
  <div id="sc" style="height:300px;overflow:auto">
    <label style="display:block;height:60px"><input type="radio" name="p"> Chicken</label>
    <label style="display:block;height:60px"><input type="radio" name="p"> Steak</label>
    <div style="height:280px"></div>
    <label style="display:block;height:60px"><input type="radio" name="p"> Beef Barbacoa</label>
    <div style="height:900px"></div>
    <label style="display:block;height:60px"><input type="radio" name="p"> Carnitas</label>
  </div>
</div></body></html>"""

_LONG_PAGE = """<!doctype html><html><body style="margin:0">
<button>Top</button><div style="height:3000px"></div><button>Bottom</button></body></html>"""


def _names(observation: Observation) -> list[str]:
    return [element.name for element in observation.elements]


async def _unit_tests() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.set_content(_MODAL_PAGE)
        before = await observe(page)
        # Gap 1: scroll state comes from the dialog's scroller, not the locked body.
        if before.scroll_area != "dialog" or not before.can_scroll_down or before.can_scroll_up:
            raise ScrollTestFailure(f"dialog scroll state wrong: {before.scroll_area} "
                                    f"down={before.can_scroll_down} up={before.can_scroll_up}")
        # Gap 2: the wheel point is inside the dialog's scroller.
        point = before.scroll_point
        if point is None or not (455 <= point[0] <= 1015 and 60 <= point[1] <= 360):
            raise ScrollTestFailure(f"scroll point outside the dialog scroller: {point}")
        # Gap 3: a control clipped by the scroller is not offered, though inside the viewport.
        if any("Barbacoa" in name for name in _names(before)):
            raise ScrollTestFailure("clipped option was offered")
        # Gap 4: controls behind an open modal are not offered.
        if "Background add" in _names(before) or "Close" not in _names(before):
            raise ScrollTestFailure(f"modal filter wrong: {_names(before)}")
        # Gap 5: a scroll inside the dialog changes the marker.
        await page.evaluate("document.getElementById('sc').scrollTop = 10")
        if (await observe(page)).marker == before.marker:
            raise ScrollTestFailure("marker ignored a scroll inside the dialog")
        # Gap 6: the step fits the box (80% of 300px), so no row is skipped.
        if before.scroll_step != 240:
            raise ScrollTestFailure(f"scroll step {before.scroll_step}, wanted 240")
        # Regression: an ordinary long page keeps the page path unchanged.
        await page.set_content(_LONG_PAGE)
        plain = await observe(page)
        if (plain.scroll_area, plain.scroll_point, plain.scroll_step) != ("page", None, None) \
                or not plain.can_scroll_down:
            raise ScrollTestFailure(f"page path changed: {plain.scroll_area} {plain.scroll_point}")
        await browser.close()


class _ScrollJev:
    """Stands in for JevClient: always answers SCROLL_DOWN, records the state sent."""

    def __init__(self) -> None:
        self.state: dict | None = None

    async def ask(self, *, state: dict, questions: dict):  # noqa: ANN201
        from agent.providers import ChoiceAnswer, JevResult

        if "operation" not in questions:  # the dialog diagnosis: give none
            return JevResult(answers={}, model="stand-in", usage={}, latency_ms=0)
        self.state = state
        options = questions["operation"]["criteria"]
        answer = ChoiceAnswer(choice="SCROLL_DOWN",
                              probabilities={key: float(key == "SCROLL_DOWN") for key in options},
                              confidence=1.0)
        return JevResult(answers={"operation": answer}, model="stand-in", usage={}, latency_ms=0)


async def _executor_scroll(html: str) -> tuple[Observation, Observation, _ScrollJev]:
    """Decide SCROLL_DOWN on `html` through the real policy code, act, re-observe."""
    import os

    from agent.executor import PlaywrightExecutor
    from agent.policy.jev_policy import decide

    for key in ("AGENT_CDP_URL", "AGENT_CHROME_PROFILE", "AGENT_CHROME_CHANNEL"):
        os.environ.pop(key, None)
    os.environ["AGENT_PROFILE_DIR"] = "none"  # never touch the saved profile
    jev = _ScrollJev()
    async with PlaywrightExecutor(headless=True) as executor:
        await executor.page.set_content(html)
        before = await observe(executor.page)
        decision = await decide(client=jev, observation=before, goal="Choose Beef Barbacoa.", history=[])  # type: ignore[arg-type]
        await executor.act(decision.action)
        await executor.page.wait_for_timeout(300)
        after = await observe(executor.page)
    return before, after, jev


async def _e2e_dialog_scroll() -> None:
    """End to end: SCROLL_DOWN on a dialog moves the dialog and reveals Barbacoa."""
    before, after, jev = await _executor_scroll(_MODAL_PAGE)
    if (jev.state or {}).get("page", {}).get("scroll_area") != "dialog":
        raise ScrollTestFailure("policy state did not say the dialog scrolls")
    if not any("Barbacoa" in name for name in _names(after)):
        raise ScrollTestFailure(f"Barbacoa not revealed after scroll: {_names(after)}")
    if after.marker == before.marker:
        raise ScrollTestFailure("scroll did not register as a page change")


async def _e2e_page_scroll() -> None:
    """End to end: on an ordinary page, SCROLL_DOWN still scrolls the page."""
    before, after, jev = await _executor_scroll(_LONG_PAGE)
    if "scroll_area" in (jev.state or {}).get("page", {}):
        raise ScrollTestFailure("page request gained a scroll_area field")
    if after.scroll_y <= before.scroll_y:
        raise ScrollTestFailure(f"page did not scroll: {before.scroll_y} -> {after.scroll_y}")


# A dialog whose sticky title covers the top of its scroller, with Escape wired
# to close it (as DoorDash does). The radio starts half under the title.
_STICKY_DIALOG = """<!doctype html><html><body style="margin:0;overflow:hidden">
<div id="dlg" role="dialog" aria-modal="true" style="position:fixed;left:300px;top:40px;width:500px;background:#fff">
  <div id="sc" style="height:300px;overflow:auto;position:relative">
    <div style="position:sticky;top:0;height:50px;background:#eee;z-index:2">Burrito Bowl</div>
    <div style="height:200px"></div>
    <label style="display:block;height:40px"><input type="radio" name="p" id="r"> Beef Barbacoa</label>
    <div style="height:600px"></div>
  </div>
</div>
<script>
  document.addEventListener('keydown', e => { if (e.key === 'Escape') document.getElementById('dlg').remove(); });
  const sc = document.getElementById('sc'); sc.scrollTop = 215;  // radio now under the sticky title
</script></body></html>"""


async def _e2e_modal_click_no_escape() -> None:
    """End to end: a click on a dialog option hidden under the dialog's sticky
    title scrolls it into view instead of pressing Escape, so the dialog stays
    open and the option is chosen."""
    import os

    from agent.executor import Action, PlaywrightExecutor

    for key in ("AGENT_CDP_URL", "AGENT_CHROME_PROFILE", "AGENT_CHROME_CHANNEL"):
        os.environ.pop(key, None)
    os.environ["AGENT_PROFILE_DIR"] = "none"
    async with PlaywrightExecutor(headless=True) as executor:
        page = executor.page
        await page.set_content(_STICKY_DIALOG)
        await page.evaluate("document.getElementById('r').setAttribute('data-agent-ref', 'e0')")
        await executor.act(Action(id="click:e0", kind="click", label="Beef Barbacoa",
                                  locator="[data-agent-ref=e0]", role="radio"))
        still_open = await page.evaluate("!!document.getElementById('dlg')")
        chosen = await page.evaluate("!!(document.getElementById('r') || {}).checked")
    if not still_open:
        raise ScrollTestFailure("clicking a covered dialog option closed the dialog")
    if not chosen:
        raise ScrollTestFailure("covered dialog option was not chosen")


# A dialog whose Close button sits under a transparent overlay layer, as on
# DoorDash's "Hungry now? View similar stores" dialog. Escape closes it.
_COVERED_CLOSE = """<!doctype html><html><body style="margin:0">
<div id="dlg" role="dialog" aria-modal="true"
     style="position:fixed;left:300px;top:40px;width:500px;height:300px;background:#fff">
  <button id="x" aria-label="Close" style="margin:10px">x</button><p>Hungry now? View similar stores</p>
</div>
<div style="position:fixed;inset:0;z-index:5"></div>
<script>
  document.addEventListener('keydown', e => { if (e.key === 'Escape') document.getElementById('dlg').remove(); });
</script></body></html>"""


async def _e2e_covered_close_dismisses() -> None:
    """End to end: a covered "Close" inside a modal closes the dialog (Escape),
    instead of failing as covered and leaving the run to wander."""
    import os

    from agent.executor import Action, PlaywrightExecutor

    for key in ("AGENT_CDP_URL", "AGENT_CHROME_PROFILE", "AGENT_CHROME_CHANNEL"):
        os.environ.pop(key, None)
    os.environ["AGENT_PROFILE_DIR"] = "none"
    async with PlaywrightExecutor(headless=True) as executor:
        page = executor.page
        await page.set_content(_COVERED_CLOSE)
        await page.evaluate("document.getElementById('x').setAttribute('data-agent-ref', 'e0')")
        await executor.act(Action(id="click:e0", kind="click", label="Close", locator="[data-agent-ref=e0]",
                                  role="button"))
        closed = await page.evaluate("!document.getElementById('dlg')")
    if not closed:
        raise ScrollTestFailure("covered Close did not close its dialog")


_OPTION_DIALOG = """<!doctype html><html><body>
<div role="dialog" aria-modal="true" style="position:fixed;left:200px;top:20px;width:500px;height:700px;overflow:auto">
  <div><button>Rice Required • Select 1</button>
    <div><label><input type="radio" id="w" checked> White Rice</label>
         <label><input type="radio"> Brown Rice</label></div>
  </div>
  <div><button>Beans Required • Select 1</button>
    <div><label><input type="radio" id="b"> Black Beans</label><label><input type="radio"> Pinto Beans</label></div>
  </div>
  <div><button>Toppings (Optional) • Select up to 3</button>
    <div><label><input type="checkbox"> Queso +CA$2.15</label></div>
  </div>
  <button id="add">Make 1 required selection - CA$15.60</button>
</div>
<script>document.getElementById('b').addEventListener('change',
  () => { document.getElementById('add').innerText = 'Add to cart - CA$15.60'; });</script>
</body></html>"""


async def _test_dialog_status() -> None:
    """Unit: the reader reports which required groups lack a choice, and when the
    add control is ready, across the whole dialog."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.set_content(_OPTION_DIALOG)
        before = (await observe(page)).dialog_status or {}
        await page.click("#b")
        after = (await observe(page)).dialog_status or {}
        await browser.close()
    if before.get("required_open") != ["Beans"] or before.get("required_done") != ["Rice"] or before.get("add_ready"):
        raise ScrollTestFailure(f"dialog status before beans wrong: {before}")
    ready = after.get("add_ready") and "Add to cart" in (after.get("add_control") or "")
    if after.get("required_open") != [] or not ready:
        raise ScrollTestFailure(f"dialog status after beans wrong: {after}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_test_dialog_status())
    asyncio.run(_unit_tests())
    asyncio.run(_e2e_modal_click_no_escape())
    asyncio.run(_e2e_covered_close_dismisses())
    asyncio.run(_e2e_dialog_scroll())
    asyncio.run(_e2e_page_scroll())
    print("dom.py inline scroll tests passed")
