"""Async DOM perception. Runs one JS reader over the current page, parses the
result into a typed Observation, and returns it.

The reader script lives in `reader.js` next to this file. Perception owns the
selector shape (`data-agent-ref="eN"`), so the executor can rely on locators it
never had to invent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
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
        covered_by=payload.get("covered_by"),
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


# How long `observe` keeps re-reading a page that has rendered nothing at all
# (no controls, no text) before handing it over anyway.
BLANK_WAIT_S = 10.0
# Re-reads allowed while a partly rendered page keeps growing.
GROWTH_READS = 5


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

    A blank read (nothing rendered: no controls, no text) is not "stopped
    growing", it has not started: keep re-reading for up to BLANK_WAIT_S
    before giving up. The amazon.ca run of 2026-10-01 read an empty page,
    accepted it after one equal re-read, and the policy ended the run.
    """
    started = time.monotonic()
    state = await _evaluate_reader(page, include_text=include_text)
    retries = 0
    growth = 0
    while True:
        blank = bool(state.get("blank"))
        if blank:
            if time.monotonic() - started >= BLANK_WAIT_S:
                break
        elif growth >= GROWTH_READS or not _looks_partial(state):
            break
        try:
            await page.evaluate(_SETTLE_JS, [200, 3000])
        except Exception:  # noqa: BLE001 — non-fatal
            pass
        if blank:
            # Nothing mutates on a page that has not started rendering, so the
            # settle returns at once; pace the re-reads instead of spinning.
            await asyncio.sleep(0.4)
        prev_count = len(state.get("elements") or [])
        prev_title = (state.get("title") or "").strip()
        state = await _evaluate_reader(page, include_text=include_text)
        retries += 1
        if blank:
            continue
        growth += 1
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
        cart_count=int(state["cart_count"]) if isinstance(state.get("cart_count"), (int, float)) else None,
        blank=bool(state.get("blank", False)),
        cover=state.get("cover") or None,
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


class LoopFixTestFailure(AssertionError):
    """An inline reachability, cart or blank-page test saw the wrong result."""


def _headless_env() -> None:
    import os

    for key in ("AGENT_CDP_URL", "AGENT_CHROME_PROFILE", "AGENT_CHROME_CHANNEL"):
        os.environ.pop(key, None)
    os.environ["AGENT_PROFILE_DIR"] = "none"  # never touch the saved profile


def _ref_of(observation: Observation, name: str) -> Element:
    for element in observation.elements:
        if element.name == name:
            return element
    raise LoopFixTestFailure(f"{name!r} not offered: {_names(observation)}")


# Every control under one invisible full-viewport layer: what the DoorDash store
# page looked like to the executor on 2026-10-01 (28 "Target covered" in a row).
_INVISIBLE_LAYER = """<!doctype html><html><body style="margin:0">
<header><a href="#home">Home</a> <button id="entree" onclick="window.clicked = 'entree'">Entree</button></header>
<main><button onclick="window.clicked = 'add'">Add to cart - Burrito</button></main>
<div id="veil" style="position:fixed;inset:0;z-index:50" onclick="window.veil = (window.veil || 0) + 1"></div>
</body></html>"""

# A dim backdrop without aria-modal over the page, a consent card on top.
_CONSENT_WALL = """<!doctype html><html><body style="margin:0">
<nav><a href="#a">Home</a> <a href="#b">Grocery</a> <a href="#c">Retail</a> <a href="#d">Deals</a></nav>
<main><button>Entree</button> <button>Most Ordered</button></main>
<div style="position:fixed;inset:0;z-index:40;background:rgba(0,0,0,0.5)"></div>
<div style="position:fixed;left:400px;top:300px;width:400px;z-index:41;background:#fff">
  <p>We use cookies</p><button>Accept all</button> <button>Essential only</button>
</div></body></html>"""


def _suggestions_page(escape_closes: bool) -> str:
    """A search box whose suggestion list covers one result's add button."""
    script = ("document.addEventListener('keydown', e => { if (e.key === 'Escape') "
              "document.querySelector('[role=listbox]').remove(); });") if escape_closes else ""
    return """<!doctype html><html><body style="margin:0">
<input role="combobox" aria-label="Search" style="position:fixed;top:0;left:0;width:400px;height:30px">
<ul role="listbox" aria-label="Search suggestions"
    style="position:fixed;top:30px;left:0;width:400px;height:120px;margin:0;background:#fff;z-index:5">
  <li role="option">eggs 12 large</li></ul>
<main style="padding-top:60px">
  <button style="margin-left:20px" onclick="window.added = 'eggs'">Add to cart - Eggs</button>
  <button style="margin-left:600px" onclick="window.added = 'milk'">Add to cart - Milk</button>
</main><script>""" + script + "</script></body></html>"


async def _test_reachability() -> None:
    """Unit: an invisible layer covers nothing; a visible blanket layer hides the
    controls under it while its own stay; a suggestion list marks only the one
    control it covers."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.set_content(_INVISIBLE_LAYER)
        veiled = await observe(page)
        await page.set_content(_CONSENT_WALL)
        wall = await observe(page)
        await page.set_content(_suggestions_page(escape_closes=False))
        listed = await observe(page)
        await browser.close()
    if veiled.cover is not None or any(e.covered_by for e in veiled.elements) or "Entree" not in _names(veiled):
        raise LoopFixTestFailure(f"invisible layer counted as a cover: {veiled.cover}")
    if _names(wall) != ["Accept all", "Essential only"] or not (wall.cover or {}).get("dropped"):
        raise LoopFixTestFailure(f"blanket layer not handled: {_names(wall)} {wall.cover}")
    eggs = _ref_of(listed, "Add to cart - Eggs")
    milk = _ref_of(listed, "Add to cart - Milk")
    if eggs.covered_by != 'listbox "Search suggestions"' or milk.covered_by is not None:
        raise LoopFixTestFailure(f"partial cover wrong: eggs={eggs.covered_by!r} milk={milk.covered_by!r}")
    if (listed.cover or {}).get("dropped"):
        raise LoopFixTestFailure("a list over one control dropped controls")


async def _e2e_click_through_and_dismiss() -> None:
    """End to end through PlaywrightExecutor: a click under an invisible layer
    lands on its target (the layer gets nothing and is restored); a click under
    a suggestion list that Escape closes lands; one under a list that stays
    raises Occluded naming the list."""
    from agent.executor import Action, Occluded, PlaywrightExecutor

    _headless_env()
    async with PlaywrightExecutor(headless=True) as executor:
        page = executor.page
        await page.set_content(_INVISIBLE_LAYER)
        entree = _ref_of(await observe(page), "Entree")
        await executor.act(Action(id="click:entree", kind="click", label="Entree", locator=entree.ref,
                                  role="button"))
        through = await page.evaluate(
            "[window.clicked, window.veil, document.getElementById('veil').style.pointerEvents]")
        await page.set_content(_suggestions_page(escape_closes=True))
        eggs = _ref_of(await observe(page), "Add to cart - Eggs")
        await executor.act(Action(id="click:eggs", kind="click", label=eggs.name, locator=eggs.ref, role="button"))
        dismissed = await page.evaluate("window.added")
        await page.set_content(_suggestions_page(escape_closes=False))
        eggs = _ref_of(await observe(page), "Add to cart - Eggs")
        refused = ""
        try:
            await executor.act(Action(id="click:eggs", kind="click", label=eggs.name, locator=eggs.ref,
                                      role="button"))
        except Occluded as err:
            refused = str(err)
        stray = await page.evaluate("window.added || null")
    if through != ["entree", None, ""]:
        raise LoopFixTestFailure(f"click through the invisible layer wrong: {through}")
    if dismissed != "eggs":
        raise LoopFixTestFailure(f"suggestion list not dismissed before the click: {dismissed!r}")
    if "Search suggestions" not in refused or stray is not None:
        raise LoopFixTestFailure(f"a click under a list that stays was not refused: {refused!r} {stray!r}")


def _search_form(label: str, swallow_enter: bool) -> str:
    """A GET search form like Walmart's header: a combobox and a Search button."""
    swallow = ("document.getElementById('q').addEventListener('keydown', "
               "e => { if (e.key === 'Enter') e.preventDefault(); });") if swallow_enter else ""
    return ("<!doctype html><html><head><title>Shop</title></head><body>"
            "<form role='search' action='/results' method='get'>"
            f"<input id='q' name='q' role='combobox' aria-label='{label}' autocomplete='off'>"
            "<button type='submit' aria-label='Search'>Go</button>"
            f"</form><script>{swallow}</script></body></html>")


async def _fill_search(label: str, swallow_enter: bool) -> tuple[str, list[str]]:
    """Fill a served search form through the executor; return the final URL and
    every results request made."""
    from agent.executor import Action, PlaywrightExecutor

    _headless_env()
    results: list[str] = []
    async with PlaywrightExecutor(headless=True) as executor:
        page = executor.page

        async def serve(route) -> None:  # noqa: ANN001 — playwright Route
            url = route.request.url
            if "/results" in url:
                results.append(url)
                body = "<!doctype html><html><head><title>Results</title></head><body>Results</body></html>"
            else:
                body = _search_form(label, swallow_enter)
            await route.fulfill(status=200, content_type="text/html", body=body)

        await page.route("https://shop.test/**", serve)
        await page.goto("https://shop.test/")
        field = _ref_of(await observe(page), label)
        outcome = await executor.act(Action(id="fill:q", kind="fill", label=field.name, locator=field.ref,
                                            role=field.role, value="eggs"))
        await page.wait_for_timeout(300)
        return (outcome.final_url if outcome.url_changed else page.url), results


async def _e2e_search_submits() -> None:
    """End to end: a search fill submits once by Enter; when the field swallows
    Enter (an autocomplete mid-mount), the field's Search button submits it;
    an address combobox is typed into and never submitted."""
    url, results = await _fill_search("Search", swallow_enter=False)
    if "results?q=eggs" not in url or len(results) != 1:
        raise LoopFixTestFailure(f"Enter did not submit exactly once: {url} {results}")
    url, results = await _fill_search("Search", swallow_enter=True)
    if "results?q=eggs" not in url or len(results) != 1:
        raise LoopFixTestFailure(f"swallowed Enter was not followed by the Search button: {url} {results}")
    url, results = await _fill_search("Enter Your Address", swallow_enter=False)
    if results:
        raise LoopFixTestFailure(f"an address combobox was submitted: {results}")


_CARTS: dict[str, int | None] = {
    '<header><button aria-label="Cart contains 13 items Total Amount $87.20">Cart</button></header>': 13,
    '<div><button aria-label="0 items, open Order Cart">0</button></div>': 0,
    '<header><a id="nav-cart" href="/gp/cart/view.html"><span>3</span> Cart</a></header>': 3,
    '<main><a href="/ip/cart-organizer">Shopping Cart Organizer, 3 Tier</a>'
    '<button>Add to cart - Flour</button></main>': None,
}


async def _test_cart_count() -> None:
    """Unit: the cart count comes from the site's cart control (Walmart,
    DoorDash, Amazon shapes); a product named "Cart" and an Add button are not
    a cart."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        for html, want in _CARTS.items():
            await page.set_content(f"<!doctype html><html><body>{html}<p>Shop</p></body></html>")
            got = (await observe(page)).cart_count
            if got != want:
                await browser.close()
                raise LoopFixTestFailure(f"cart count {got!r}, wanted {want!r} for {html}")
        await browser.close()


async def _test_blank_wait() -> None:
    """Unit: a page that renders late is waited for, not handed over empty; one
    that never renders is handed over (flagged blank) after BLANK_WAIT_S."""
    global BLANK_WAIT_S
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.set_content("<!doctype html><html><body><script>setTimeout(() => { document.body.innerHTML = "
                               "'<button>Shop</button><p>Deals</p>'; }, 1500);</script></body></html>")
        late = await observe(page)
        await page.set_content("<!doctype html><html><body></body></html>")
        saved, BLANK_WAIT_S = BLANK_WAIT_S, 1.0
        try:
            started = time.monotonic()
            empty = await observe(page)
            waited = time.monotonic() - started
        finally:
            BLANK_WAIT_S = saved
        await browser.close()
    if late.blank or "Shop" not in _names(late):
        raise LoopFixTestFailure(f"late render not waited for: blank={late.blank} {_names(late)}")
    if not empty.blank or waited < 1.0:
        raise LoopFixTestFailure(f"never-rendered page: blank={empty.blank} after {waited:.1f}s")


if __name__ == "__main__":
    asyncio.run(_test_dialog_status())
    asyncio.run(_unit_tests())
    asyncio.run(_e2e_modal_click_no_escape())
    asyncio.run(_e2e_covered_close_dismisses())
    asyncio.run(_e2e_dialog_scroll())
    asyncio.run(_e2e_page_scroll())
    asyncio.run(_test_reachability())
    asyncio.run(_e2e_click_through_and_dismiss())
    asyncio.run(_e2e_search_submits())
    asyncio.run(_test_cart_count())
    asyncio.run(_test_blank_wait())
    print("dom.py inline scroll tests passed")
