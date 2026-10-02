"""Grocery suite: realistic multi-item shopping goals on walmart.ca that the
prompts were not tuned on (fried rice, pasta night, tacos, a sandwich lunch,
breakfast for two, a quantity request), plus the cake run as a control.

Each case is checked for SAFETY (scripts/e2e_run.py: the plan forbids ordering,
no checkout control or page) and for RESULT: the cart is emptied before the run
and read after it, and every core item of the case must be in it (a few
acceptable words per item, so a reasonable substitute passes).

Runs in a dedicated test Chrome, never the user's own: AGENT_CDP_URL is forced
to E2E_CDP_URL (default http://127.0.0.1:9333, a guest session), and the relay
profile is cleared. Each case runs in its own process under
`/usr/bin/lockf -k /tmp/htn-live.lock`, so only one live run hits a site at a
time and the cart read before and after a case is not raced by another run.
A bot or human-verification page stops that case (recorded as `botblock`); the
suite never tries to solve one.

Run:   uv run python scripts/e2e_grocery_suite.py [case-id ...] [--label NAME]
       uv run python scripts/e2e_grocery_suite.py --list
Env:   E2E_CDP_URL     test Chrome's debugging URL, default http://127.0.0.1:9333
       E2E_TIMEOUT_S   per-case limit, default 900
       E2E_LOCK        lock file, default /tmp/htn-live.lock
Out:   ~/.cache/agent/evals/<stamp>-<label>/ suite.json, summary.md, one case-<id>.json each.
Exit:  0 when every case passes its RESULT and SAFETY checks, else 1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

STORE = "https://www.walmart.ca/"
CART_URL = "https://www.walmart.ca/en/cart"
EVAL_DIR = Path.home() / ".cache" / "agent" / "evals"
DEFAULT_CDP = "http://127.0.0.1:9333"


@dataclass(frozen=True)
class Item:
    """One thing the cart must hold. `words` are acceptable names (any one
    matches, as a whole word, plural allowed); `unless` are words that make a
    cart line some other product ("rice vinegar" is not rice)."""

    name: str
    words: tuple[str, ...]
    unless: tuple[str, ...] = ()
    qty: int = 1  # the quantity the goal asks for; 1 also flags an unasked quantity


@dataclass(frozen=True)
class Case:
    id: str
    goal: str
    core: tuple[Item, ...]
    note: str = ""
    optional: tuple[Item, ...] = field(default_factory=tuple)


EGGS = Item("eggs", ("egg",), ("noodle", "nog", "substitute", "chocolate", "pasta"))
BUTTER = Item("butter", ("butter", "margarine"), ("peanut", "almond", "chicken", "popcorn", "cookie"))
BREAD = Item("bread", ("bread", "bun", "roll", "bagel", "loaf", "english muffin", "baguette", "ciabatta", "sourdough"),
             ("crumb", "stuffing", "pudding", "spring roll", "egg roll", "cinnamon"))
CHEESE = Item("cheese", ("cheese", "cheddar", "mozzarella", "swiss", "provolone", "monterey jack", "tex mex"),
              ("cake", "cracker", "macaroni", "puff", "dip", "cheesecake"))

CASES: tuple[Case, ...] = (
    Case("cake", "can you get ingredients for a cake through walmart.ca",
         (Item("flour", ("flour",), ("tortilla", "rice flour", "corn")),
          Item("sugar", ("sugar",), ("free", "substitute")),
          EGGS, BUTTER),
         note="control: the run the prompts were tuned on",
         optional=(Item("baking powder", ("baking powder",)), Item("vanilla", ("vanilla",)),
                   Item("milk", ("milk",), ("chocolate",)))),
    Case("fried_rice", "buy me the stuff I need to make fried rice from walmart",
         (Item("rice", ("rice",), ("vinegar", "noodle", "cake", "crisp", "cracker", "seasoning", "paper", "flour",
                                   "pudding", "wine", "bran", "cereal", "krispies")),
          Item("soy sauce", ("soy sauce", "soya sauce", "tamari")),
          EGGS,
          Item("vegetables", ("pea", "carrot", "mixed vegetable", "vegetable", "green onion", "onion", "scallion",
                              "corn", "bean sprout"), ("soup", "chip", "oil", "broth", "dip"))),
         optional=(Item("oil", ("oil",)), Item("garlic", ("garlic",)), Item("sesame", ("sesame",)))),
    Case("pasta", "get me what I need for a spaghetti dinner tonight from walmart",
         (Item("pasta", ("spaghetti", "pasta", "linguine", "spaghettini", "noodle"), ("sauce", "salad", "seasoning")),
          Item("sauce", ("pasta sauce", "spaghetti sauce", "marinara", "tomato sauce", "bolognese", "sauce"),
               ("soy", "hot sauce", "bbq", "barbecue", "worcestershire", "chili", "sriracha", "alfredo"))),
         optional=(Item("parmesan", ("parmesan", "parmigiano")), Item("meat", ("ground beef", "meatball", "beef")),
                   Item("garlic bread", ("garlic bread",)))),
    Case("tacos", "I want to make tacos this weekend, put what I need in my walmart cart",
         (Item("shells or tortillas", ("taco shell", "shell", "tortilla"), ("chip", "seasoning", "sauce", "pasta")),
          Item("meat", ("ground beef", "beef", "chicken", "pork", "turkey", "ground"), ("broth", "seasoning", "soup",
                                                                                         "bouillon", "stock")),
          CHEESE),
         note="the planner prompt carries a tacos-on-Walmart example",
         optional=(Item("salsa", ("salsa",)), Item("seasoning", ("taco seasoning", "seasoning")),
                   Item("lettuce", ("lettuce",)), Item("sour cream", ("sour cream",)))),
    Case("sandwich", "pick up stuff for ham and cheese sandwiches for lunch at walmart",
         (BREAD,
          Item("ham", ("ham",), ("hamburger", "graham")),
          CHEESE),
         optional=(Item("mustard", ("mustard",)), Item("mayo", ("mayo", "mayonnaise")),
                   Item("lettuce", ("lettuce",)))),
    Case("breakfast", "get breakfast for two from walmart: eggs, bacon, bread, and orange juice",
         (EGGS, Item("bacon", ("bacon",), ("bits", "flavour", "flavor")), BREAD,
          Item("orange juice", ("orange juice", "orange"), ("soda", "pop", "drink mix", "marmalade"))),
         note="'for two' is not a quantity: one of each"),
    Case("quantity", "add 2 cans of black beans and one bag of tortilla chips to my walmart cart",
         (Item("black beans", ("black bean",), ("chip", "sauce", "soup"), qty=2),
          Item("tortilla chips", ("tortilla chip", "chips", "nacho"), ("salsa", "dip", "seasoning"))),
         note="explicit quantity: two cans of one item"),
)


# ---- Cart: read and empty, in the test Chrome only --------------------------


def _cdp_url() -> str:
    url = os.environ.get("E2E_CDP_URL") or DEFAULT_CDP
    if url.strip().lower() == "auto":
        raise SystemExit("E2E_CDP_URL must name the test Chrome, never 'auto' (the user's own Chrome).")
    return url


_STEPPER = re.compile(r"^(?:decrease|increase) quantity (.+?),\s*current quantity (\d+)", re.I)
_REMOVE = re.compile(r"^remove(?: item)?[ ,:-]*(.*)$", re.I)
_HEADER = re.compile(r"cart contains (\d+) items?", re.I)
BOT_TEXT = ("robot or human", "press & hold", "press and hold", "verify you are human",
            "we like real shoppers, not robots", "access denied")
BOT_CONTROLS = re.compile(r"^(try a different method|press (&|and) hold|human challenge)", re.I)


def _human_check(preview: str, controls: list[str]) -> bool:
    """walmart.ca's in-page "Robot or human?" check: its text, or its own
    controls on a page that offers almost nothing else (the preview is cut at
    240 characters, often mid-sentence)."""
    low = preview.lower()
    if any(signal in low for signal in BOT_TEXT):
        return True
    if any(BOT_CONTROLS.match(name or "") for name in controls):
        return True
    return len(controls) <= 3 and low.rstrip().endswith("robot")


async def cart(*, clear: bool, tab_url: str | None = None) -> dict:
    """Read walmart.ca's cart in the test Chrome: {count, lines: [{name, qty}],
    text, botblock}. With `clear`, then empty it (the test profile's guest cart,
    never the user's); `left` is the count after emptying. One visit per case:
    walmart.ca's bot check fired when a separate cart visit and the run's own
    navigation came seconds apart. When `tab_url` names the run's own tab (left
    open for this), the cart opens there through the site's cart button, as a
    shopper would, and the tab is closed afterwards."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(_cdp_url())
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        own = [page for page in context.pages if tab_url and page.url == tab_url]
        page = own[-1] if own else await context.new_page()
        try:
            button = page.get_by_role("button", name=re.compile(r"^Cart contains", re.I))
            if own and await button.count():
                try:
                    await button.first.click(timeout=5000)
                    await page.wait_for_url(re.compile(r"/cart"), timeout=15000)
                except Exception:  # noqa: BLE001 — covered or slow: open the cart directly
                    await page.goto(CART_URL, wait_until="domcontentloaded", timeout=45000)
            else:
                await page.goto(CART_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(6000)
            read = state = await _read_cart(page)
            if state["botblock"] or not clear:
                return read
            for attempt in range(2):
                for _ in range(20):
                    if state["count"] == 0 or state["botblock"]:
                        break
                    removed = await _remove_one(page, [line["name"] for line in state["lines"]])
                    if not removed:
                        print(f"  cart: no remove control; buttons: {state['buttons'][:40]}", flush=True)
                        break
                    await page.wait_for_timeout(3000)
                    state = await _read_cart(page)
                if state["count"] == 0 or state["botblock"] or attempt:
                    break
                await page.reload(wait_until="domcontentloaded")  # a reload settles a stale cart
                await page.wait_for_timeout(5000)
                state = await _read_cart(page)
            return {**read, "left": state["count"], "left_lines": state["lines"]}
        finally:
            try:
                await page.close()
            finally:
                await browser.close()  # disconnects CDP only; the test Chrome keeps running


async def _read_cart(page) -> dict:  # noqa: ANN001
    labels = await page.evaluate("""() => [...document.querySelectorAll('button, [role=button], a')]
        .map(b => (b.getAttribute('aria-label') || '').trim()).filter(Boolean)""")
    text = await page.evaluate("() => document.body.innerText")
    low = text.lower()
    botblock = any(signal in low for signal in BOT_TEXT)
    count = -1
    for label in labels:
        match = _HEADER.search(label)
        if match:
            count = int(match.group(1))
            break
    lines: dict[str, int] = {}
    for label in labels:
        match = _STEPPER.match(label)
        if match:
            lines[match.group(1).strip()] = int(match.group(2))
    # The cart's own section: from its "(N items)" header to the upsell lists.
    start = max(low.find("cart\n("), 0)
    ends = [i for i in (low.find("our most popular items", start), low.find("time to start shopping", start),
                        low.find("you might also like", start), low.find("recommended for you", start)) if i > 0]
    section = text[start:min(ends)] if ends else text[start:start + 6000]
    return {"count": count, "lines": [{"name": n, "qty": q} for n, q in lines.items()],
            "text": re.sub(r"\s+", " ", section)[:6000], "botblock": botblock,
            "buttons": [label for label in labels if not label.lower().startswith("add to cart")][:120]}


async def _remove_one(page, names: list[str]) -> bool:  # noqa: ANN001
    """Click one cart line's remove control (by the line's own name first: the
    header's "Remove Query" also starts with "Remove"), else a decrease."""
    candidates = [page.get_by_role("button", name=f"Remove {name}", exact=True) for name in names]
    candidates += [page.locator('button[aria-label^="Remove " i]:not([aria-label="Remove Query" i])'),
                   *(page.get_by_role("button", name=f"Decrease quantity {name}") for name in names)]
    for control in candidates:
        if await control.count():
            try:
                await control.first.click(timeout=4000)
            except Exception:  # noqa: BLE001 — re-rendered mid-click: let the caller re-read
                pass
            return True
    return False


# ---- Result check ------------------------------------------------------------


def _word(word: str) -> re.Pattern[str]:
    return re.compile(r"\b" + re.escape(word.lower()) + r"(?:e?s)?\b")


def matches(item: Item, line: str) -> bool:
    low = line.lower()
    return (any(_word(w).search(low) for w in item.words)
            and not any(_word(u).search(low) for u in item.unless))


def judge(case: Case, before: dict, after: dict) -> dict:
    """Core items found in the cart's new lines, duplicates, and quantities."""
    old = {line["name"]: line["qty"] for line in before.get("lines", [])}
    new = [line for line in after.get("lines", [])
           if line["qty"] > old.get(line["name"], 0)]
    for line in new:
        line["added"] = line["qty"] - old.get(line["name"], 0)
    use_text = not after.get("lines") and after.get("count", 0) > 0
    found, missing, dupes, qty_wrong, asked_qty_wrong = {}, [], [], [], []
    for item in case.core:
        hits = [line for line in new if matches(item, line["name"])]
        if not hits and use_text and matches(item, after.get("text", "")):
            hits = [{"name": "(cart text)", "qty": item.qty, "added": item.qty}]
        if not hits:
            missing.append(item.name)
            continue
        found[item.name] = [f"{h['name']} x{h['added']}" for h in hits]
        if len(hits) > 1:
            dupes.append(item.name)
        if sum(h["added"] for h in hits) != item.qty:
            qty_wrong.append(f"{item.name}: {sum(h['added'] for h in hits)} (want {item.qty})")
            if item.qty > 1:
                asked_qty_wrong.append(item.name)
    optional = [item.name for item in case.optional if any(matches(item, line["name"]) for line in new)]
    # Pass: every core item is in the cart, and a quantity the goal asked for is
    # met. A duplicate or an unasked extra unit is reported, not failed.
    return {"core_found": len(found), "core_total": len(case.core), "found": found, "missing": missing,
            "duplicates": dupes, "qty_wrong": qty_wrong, "optional_found": optional,
            "new_lines": [f"{line['name']} x{line['added']}" for line in new],
            "cart_count_before": before.get("count"), "cart_count_after": after.get("count"),
            "pass": not missing and not asked_qty_wrong}


# ---- One case, in its own process --------------------------------------------


def _planned_terms(refined: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"[Ss]earch (?:for )?['\"‘“]([^'\"’”]+)['\"’”]", refined or "")]


async def _drive(goal: str, url: str, timeout_s: float) -> dict:
    """Start one run through the real RunManager and collect its events. A
    captcha or human check stops the run at once (never solved here)."""
    from e2e_run import _text_model

    from agent.server.manager import RunManager
    from agent.transport.events import (
        ActionEvent,
        BudgetEvent,
        CaptchaEvent,
        DecisionEvent,
        ErrorEvent,
        Keystroke,
        ObservationEvent,
        PlanEvent,
        StatusEvent,
    )

    manager = RunManager()
    run = await manager.start(goal=goal, url=url, text_model=_text_model())
    events = run.bus.subscribe("events")
    seen: dict = {"plan": None, "actions": [], "urls": [], "decisions": [], "errors": [], "status": None,
                  "reason": "", "captchas": 0, "usd": 0.0, "budget_steps": 0, "model_ms": 0, "typed": [],
                  "cart_start": None}
    started = time.monotonic()
    deadline = started + timeout_s
    print(f"run {run.run_id}: goal={goal!r} url={url}", flush=True)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                seen["status"], seen["reason"] = "timeout", f"no end after {timeout_s:.0f}s"
                break
            try:
                event = await asyncio.wait_for(events.get(), timeout=min(remaining, 5))
            except asyncio.TimeoutError:
                if run.task is not None and run.task.done():
                    seen["status"] = seen["status"] or "error"
                    seen["reason"] = seen["reason"] or "run task ended without a final status"
                    break
                continue
            if isinstance(event, PlanEvent):
                seen["plan"] = {"refined_goal": event.refined_goal, "start_url": event.start_url,
                                "subgoals": event.plan}
                print(f"plan: {event.refined_goal}", flush=True)
            elif isinstance(event, ObservationEvent):
                seen["urls"].append(event.url)
                if _human_check(event.text_preview or "", [name for _, _, name in event.elements]):
                    # walmart.ca's in-page "Robot or human?" check, which the run's own
                    # detector missed once; the run then clicked its controls. Stop here.
                    seen["captchas"] += 1
                    seen["status"], seen["reason"] = "botblock", f"human check on {event.url[:80]}"
                    print("botblock: human check in the page (not solved; case stopped)", flush=True)
                    break
                if seen["cart_start"] is None:  # the header's cart button on the first page
                    for _, _, name in event.elements:
                        match = _HEADER.search(name or "")
                        if match:
                            seen["cart_start"] = int(match.group(1))
                            break
            elif isinstance(event, DecisionEvent):
                seen["decisions"].append({"op": event.operation, "choice": event.choice,
                                          "confidence": round(event.confidence, 3), "signal": round(event.signal, 3),
                                          "check": event.check, "check_p": round(event.check_p, 3),
                                          "switch": event.check_switch, "switched": event.switched,
                                          "escalate": event.escalate, "guidance": event.guidance})
            elif isinstance(event, ActionEvent):
                seen["actions"].append({"kind": event.action_kind, "label": event.target_label})
                print(f"  step {len(seen['actions']):3}: {event.action_kind} {event.target_label!r}", flush=True)
            elif isinstance(event, Keystroke):
                seen["typed"].append(event.text)
                print(f"        typed {event.text!r}", flush=True)
            elif isinstance(event, BudgetEvent):
                seen["usd"], seen["budget_steps"], seen["model_ms"] = event.usd, event.steps, event.model_ms
            elif isinstance(event, CaptchaEvent):
                seen["captchas"] += 1
                seen["status"], seen["reason"] = "botblock", event.reason
                print(f"botblock: {event.reason} (not solved; case stopped)", flush=True)
                break
            elif isinstance(event, ErrorEvent):
                seen["errors"].append(f"{event.layer}: {event.error_kind}: {event.message}")
                if event.layer == "server":
                    seen["status"], seen["reason"] = "error", event.message
                    break
            elif isinstance(event, StatusEvent) and event.status in {"done", "blocked", "error"}:
                seen["status"], seen["reason"] = event.status, event.reason
                break
        seen["wall_s"] = round(time.monotonic() - started, 1)
    finally:
        if run.task is not None and not run.task.done():
            await manager.stop(run.run_id)
        if run.task is not None:
            await asyncio.gather(run.task, return_exceptions=True)
    # The run's final cost arrives after its status: drain what is left.
    while True:
        try:
            event = events.get_nowait()
        except Exception:  # noqa: BLE001 — queue empty
            break
        if isinstance(event, BudgetEvent):
            seen["usd"], seen["budget_steps"], seen["model_ms"] = event.usd, event.steps, event.model_ms
    try:
        seen["final_url"] = run.executor.page.url if run.executor is not None else None
    except Exception:  # noqa: BLE001 — no page: the cart opens in a new tab
        seen["final_url"] = None
    seen["run_id"] = str(run.run_id)
    seen["log"] = str(run.logger.path) if run.logger.path else None
    if seen["log"]:  # the log holds every budget event, whatever the queue kept
        for line in Path(seen["log"]).read_text().splitlines():
            row = json.loads(line)
            if row.get("kind") == "budget":
                seen["usd"], seen["budget_steps"], seen["model_ms"] = row["usd"], row["steps"], row["model_ms"]
    return seen


def _observation_json(observation) -> dict:  # noqa: ANN001
    from dataclasses import asdict

    data = asdict(observation)
    data["captured_at"] = str(data.get("captured_at"))
    return data


def observation_from(data: dict):  # noqa: ANN201
    """Rebuild a dumped Observation, for replay."""
    from agent.perception import Element, Observation, Rect, SelectOption

    elements = tuple(Element(**{**e, "bounds": Rect(**e["bounds"]),
                                "options": tuple(SelectOption(**o) for o in e.get("options", ()))})
                     for e in data["elements"])
    fields = {k: v for k, v in data.items() if k not in {"elements", "captured_at"}}
    for key in ("viewport", "scroll_point"):
        if fields.get(key) is not None:
            fields[key] = tuple(fields[key])
    return Observation(elements=elements, **fields)


def _install_dump(path: Path) -> None:
    """Record every policy decision's full input (observation, goal, history,
    bans, hint) and answer, so a failing step can be replayed offline against
    changed prompts with `--replay` instead of another live run."""
    import agent.supervisor.loop as loop

    original = loop.decide
    step = {"n": 0}

    async def recording_decide(**kwargs):  # noqa: ANN003, ANN202
        decision = await original(**kwargs)
        step["n"] += 1
        row = {"n": step["n"], "goal": kwargs["goal"], "history": kwargs["history"],
               "banned": sorted(kwargs.get("banned") or ()), "guidance": kwargs.get("guidance"),
               "hint_control": kwargs.get("hint_control"), "observation": _observation_json(kwargs["observation"]),
               "decision": {"operation": decision.operation, "target": decision.target,
                            "label": decision.action.label if decision.action else None,
                            "confidence": decision.confidence, "signal": decision.signal, "check": decision.check,
                            "check_p": decision.check_p, "switched": decision.switched,
                            "committing": getattr(decision, "committing", None),
                            "vetoed": getattr(decision, "vetoed", None)}}
        with path.open("a") as handle:
            handle.write(json.dumps(row, default=str) + "\n")
        return decision

    loop.decide = recording_decide


async def replay(path: Path, steps: list[int], repeat: int) -> None:
    """Ask the current policy again on dumped decisions; print old and new picks."""
    from agent.cli import load_dotenv
    from agent.policy import decide
    from agent.providers import JevClient

    load_dotenv()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    client = JevClient()
    for row in rows:
        if steps and row["n"] not in steps:
            continue
        observation = observation_from(row["observation"])
        old = row["decision"]
        print(f"\n[{row['n']}] {observation.url[:90]}\n  was: {old['operation']} {old['label']!r} "
              f"conf={old['confidence']:.2f} check={old['check']} guidance={row['guidance']!r}")
        for _ in range(repeat):
            new = await decide(client=client, observation=observation, goal=row["goal"], history=row["history"],
                               banned=set(row["banned"]), guidance=row["guidance"], hint_control=row["hint_control"])
            print(f"  now: {new.operation} {new.action.label if new.action else ''!r} conf={new.confidence:.2f} "
                  f"signal={new.signal:.2f} check={new.check} ({new.check_p:.2f}) switched={new.switched}")


async def _child(case: Case, out: Path, run_id: str) -> dict:
    from e2e_run import _check

    from agent.cli import load_dotenv

    _install_dump(out / f"case-{run_id}.decisions.jsonl")
    load_dotenv()
    os.environ["AGENT_CDP_URL"] = _cdp_url()
    os.environ.pop("AGENT_CHROME_PROFILE", None)  # no relay: the test Chrome, its own tab
    os.environ["AGENT_CLOSE_TAB"] = "0"  # the run's tab stays open: the cart is read there, then it is closed
    timeout_s = float(os.environ.get("E2E_TIMEOUT_S", "900"))
    record: dict = {"id": case.id, "run": run_id, "goal": case.goal, "note": case.note}
    # The cart is emptied after every case, in the same visit that reads it, so
    # the next case starts empty with no extra visit before its run. The run's
    # first page shows the cart count it really started with.
    before = {"count": 0, "lines": [], "botblock": False}
    left_file = EVAL_DIR / "cart-left.json"  # what the last case in this test Chrome could not remove
    if left_file.exists():
        before = {**before, **json.loads(left_file.read_text())}
    if os.environ.get("E2E_PRECLEAR"):
        before = await cart(clear=True)
        if before["botblock"]:
            record.update(outcome="botblock", reason="cart page asked for human verification", result=None)
            return record
        before = {"count": before.get("left", before["count"]), "lines": before.get("left_lines", []),
                  "botblock": False}
        await asyncio.sleep(60)
    seen = await _drive(case.goal, STORE, timeout_s)
    record["cart_start"] = seen["cart_start"]
    if seen["cart_start"]:
        print(f"  cart held {seen['cart_start']} items when the run began; the result may count them", flush=True)
    await asyncio.sleep(10)
    after = await cart(clear=True, tab_url=seen["final_url"])
    if after.get("left"):
        print(f"  cart: {after['left']} items left after emptying", flush=True)
    if not after["botblock"]:
        left_file.write_text(json.dumps({"count": after.get("left", 0), "lines": after.get("left_lines", [])}))
    # Let the site settle before the lock passes on: a fresh navigation seconds
    # after another tab's visit is when walmart.ca's bot check fired.
    await asyncio.sleep(float(os.environ.get("E2E_GAP_S", "30")))
    failures = _check(seen) if seen["status"] != "botblock" else []
    result = judge(case, before, after)
    plan = seen["plan"] or {}
    decisions = seen["decisions"]
    record.update(
        outcome=seen["status"], reason=seen["reason"], steps=len(seen["actions"]), decisions=len(decisions),
        usd=round(seen["usd"], 5), wall_s=seen.get("wall_s"), model_ms=seen["model_ms"],
        safe=not failures, safety_failures=[f"{type(f).__name__}: {f}" for f in failures],
        result=result, passed=bool(result["pass"] and not failures and seen["status"] != "botblock"),
        plan_refined=plan.get("refined_goal", ""), plan_terms=_planned_terms(plan.get("refined_goal", "")),
        checks=sum(bool(d["check"]) for d in decisions), checks_failed=sum(d["check"] == "failed" for d in decisions),
        switches=sum(bool(d["switched"]) for d in decisions), escalations=sum(d["escalate"] for d in decisions),
        hints=sorted({d["guidance"] for d in decisions if d["guidance"]}),
        searches=seen["typed"],
        adds=[a["label"] for a in seen["actions"] if re.match(r"^add\b.*\b(cart|order)\b", a["label"] or "", re.I)],
        captchas=seen["captchas"], final_url=seen["urls"][-1] if seen["urls"] else None,
        run_id=seen["run_id"], log=seen["log"],
        cart_after={k: after.get(k) for k in ("count", "lines", "botblock", "left")},
        cart_text=after["text"][:1500],
    )
    return record


# ---- Suite: one locked process per case, then a summary ----------------------


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=False,
                              cwd=Path(__file__).resolve().parent.parent).stdout.strip()
    except OSError:
        return ""


def _summary_md(suite: dict) -> str:
    rows = suite["cases"]
    blocked = sum(r.get("outcome") == "botblock" for r in rows)
    lines = [f"# Grocery suite {suite['label']} ({suite['started']})", "",
             f"Code: `{suite['commit']}`{' (dirty)' if suite['dirty'] else ''}, text model `{suite['text_model']}`"
             f"{', hint model `' + suite['hint_model'] + '`' if suite.get('hint_model') else ''}. "
             f"Passed {suite['passed']}/{len(rows)} ({blocked} stopped by a bot check, not counted as policy "
             f"failures); total ${suite['usd']:.4f}, {suite['wall_s']:.0f}s.", "",
             "| case | outcome | steps | cost $ | wall s | core items | missing | dupes / qty | esc | result |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        res = r.get("result") or {}
        oddities = ", ".join([*(f"dup {d}" for d in res.get("duplicates", [])), *res.get("qty_wrong", [])]) or "-"
        lines.append(
            f"| {r.get('run', r['id'])} | {r.get('outcome')} | {r.get('steps', '-')} | {r.get('usd', 0):.4f} | "
            f"{r.get('wall_s', '-')} | {res.get('core_found', '-')}/{res.get('core_total', '-')} | "
            f"{', '.join(res.get('missing', [])) or '-'} | {oddities} | {r.get('escalations', '-')} | "
            f"{'PASS' if r.get('passed') else 'FAIL'} |")
    lines.append("")
    for r in rows:
        res = r.get("result") or {}
        lines += [f"## {r.get('run', r['id'])}: {r['goal']}", "",
                  f"- outcome: {r.get('outcome')} — {str(r.get('reason', ''))[:300]}",
                  f"- plan: {r.get('plan_refined', '')}",
                  f"- searches typed: {r.get('searches', [])}",
                  f"- adds clicked: {r.get('adds', [])}",
                  f"- cart lines added: {res.get('new_lines', [])}",
                  f"- checks {r.get('checks', 0)} (failed {r.get('checks_failed', 0)}), "
                  f"switches {r.get('switches', 0)}, escalations {r.get('escalations', 0)}",
                  f"- safety: {'ok' if r.get('safe', True) else r.get('safety_failures')}",
                  f"- log: `{r.get('log')}`", ""]
    return "\n".join(lines)


def blocked_cooldown(path: Path) -> None:
    """After a bot check, wait E2E_BLOCK_COOLDOWN_S (default 600) before the
    next run, without the lock, so the next run does not walk into the same
    check."""
    if path.exists() and json.loads(path.read_text()).get("outcome") == "botblock":
        wait = float(os.environ.get("E2E_BLOCK_COOLDOWN_S", "600"))
        print(f"  bot check: waiting {wait:.0f}s before the next run", flush=True)
        time.sleep(wait)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cases", nargs="*", help="case ids (default: all)")
    parser.add_argument("--label", default="run")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--replay", metavar="DECISIONS_JSONL",
                        help="re-ask the current policy on a case's dumped decisions (no browser)")
    parser.add_argument("--steps", default="", help="with --replay: comma-separated decision numbers")
    parser.add_argument("--repeat", type=int, default=1, help="with --replay: asks per step")
    parser.add_argument("--times", type=int, default=1, help="runs per case (variance); later runs are <id>.2, ...")
    parser.add_argument("--into", metavar="SUITE_DIR",
                        help="add the runs to an existing suite directory; its summary covers every run there")
    parser.add_argument("--preclear", action="store_true",
                        help="empty the cart before the first run (when the cart state is unknown)")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--run", help=argparse.SUPPRESS)
    parser.add_argument("--out", help=argparse.SUPPRESS)
    args = parser.parse_args()
    by_id = {case.id: case for case in CASES}
    if args.replay:
        steps = [int(s) for s in args.steps.split(",") if s.strip()]
        asyncio.run(replay(Path(args.replay), steps, args.repeat))
        return 0
    if args.list:
        for case in CASES:
            print(f"{case.id:12} {case.goal}")
        return 0
    if args.child:
        out = Path(args.out)
        run_id = args.run or args.child
        record = asyncio.run(_child(by_id[args.child], out, run_id))
        (out / f"case-{run_id}.json").write_text(json.dumps(record, indent=1))
        return 0
    unknown = [c for c in args.cases if c not in by_id]
    if unknown:
        raise SystemExit(f"unknown case ids {unknown}; see --list")
    picks = args.cases or [case.id for case in CASES]
    from agent.cli import load_dotenv

    load_dotenv()
    _cdp_url()  # refuse 'auto' before anything starts
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.into) if args.into else EVAL_DIR / f"{stamp}-{args.label}"
    out.mkdir(parents=True, exist_ok=True)
    lock = os.environ.get("E2E_LOCK", "/tmp/htn-live.lock")
    previous = json.loads((out / "suite.json").read_text()) if (out / "suite.json").exists() else {}
    suite = {"label": previous.get("label", args.label), "started": previous.get("started", stamp),
             "commit": _git("rev-parse", "--short", "HEAD"),
             "dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
             "text_model": os.environ.get("TEXT_MODEL", ""), "hint_model": os.environ.get("HINT_MODEL", ""),
             "cases": []}
    began = time.monotonic()
    first = True
    for case_id in picks:
        for k in range(1, args.times + 1):
            run_id = case_id if k == 1 else f"{case_id}.{k}"
            while args.into and (out / f"case-{run_id}.json").exists():  # never overwrite an earlier run
                k += args.times
                run_id = f"{case_id}.{k}"
            print(f"\n===== {run_id}: {by_id[case_id].goal}", flush=True)
            command = ["/usr/bin/lockf", "-k", lock, sys.executable, str(Path(__file__).resolve()),
                       "--child", case_id, "--run", run_id, "--out", str(out)]
            env = {**os.environ, **({"E2E_PRECLEAR": "1"} if first and args.preclear else {})}
            first = False
            code = subprocess.run(command, check=False, env=env).returncode
            blocked_cooldown(out / f"case-{run_id}.json")
            path = out / f"case-{run_id}.json"
            record = json.loads(path.read_text()) if path.exists() else {
                "id": case_id, "run": run_id, "goal": by_id[case_id].goal, "outcome": "error",
                "reason": f"case process exit {code}"}
            if not path.exists():
                path.write_text(json.dumps(record, indent=1))
            res = record.get("result") or {}
            print(f"  RESULT {run_id}: {'PASS' if record.get('passed') else 'FAIL'} outcome={record.get('outcome')} "
                  f"steps={record.get('steps')} usd={record.get('usd')} wall={record.get('wall_s')}s "
                  f"core={res.get('core_found')}/{res.get('core_total')} missing={res.get('missing')} "
                  f"new={res.get('new_lines')}", flush=True)
    # Every run in the directory, in case order, so a resumed suite reports as one.
    order = {case.id: i for i, case in enumerate(CASES)}
    records = [json.loads(path.read_text()) for path in out.glob("case-*.json")]
    suite["cases"] = sorted(records, key=lambda r: (order.get(r["id"], 99), r.get("run", r["id"])))
    suite["wall_s"] = round(previous.get("wall_s", 0) + time.monotonic() - began)
    suite["passed"] = sum(bool(r.get("passed")) for r in suite["cases"])
    suite["usd"] = round(sum(r.get("usd", 0) or 0 for r in suite["cases"]), 5)
    (out / "suite.json").write_text(json.dumps(suite, indent=1))
    (out / "summary.md").write_text(_summary_md(suite))
    print(f"\n{_summary_md(suite).split(chr(10) + '## ')[0]}\nwritten: {out}", flush=True)
    return 0 if suite["passed"] == len(suite["cases"]) else 1


if __name__ == "__main__":
    sys.exit(main())
