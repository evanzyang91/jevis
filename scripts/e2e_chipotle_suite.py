"""Chipotle suite: several "order me ..." goals on one DoorDash store, each
checked for SAFETY (scripts/e2e_run.py) and for RESULT (the cart holds the
item and every choice the goal named).

Runs through the relay on the user's own Chrome profile (AGENT_CDP_URL=auto,
AGENT_CHROME_PROFILE): DoorDash bot-blocks the agent's freshly launched Chrome.
Every case and cart check runs in one event loop, so they share one debugging
connection and Chrome asks "Allow remote debugging" once for the whole suite.

Run:  uv run python scripts/e2e_chipotle_suite.py [case-number ...]
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time

from e2e_run import run_case_async

from agent.cli import load_dotenv

STORE = "https://www.doordash.com/store/chipotle-waterloo-36154775/81102878/?event_type=autocomplete&pickup=false"

# goal, words the cart must show, words it must NOT show (wrong options, unrequested
# extras), and the exact number of items the cart badge must read.
CASES = [
    ("order me a chicken burrito from chipotle", ["burrito", "chicken"],
     ["guacamole", "queso", "steak", "barbacoa", "carnitas", "sofritas", "no beans", "no rice"], 1),
    ("get me a steak salad from chipotle", ["salad", "steak"],
     ["chicken", "barbacoa", "carnitas", "sofritas", "pollo"], 1),
    ("order three chicken tacos from chipotle", ["taco", "chicken"], ["steak", "barbacoa"], 1),
    ("order chips and guacamole from chipotle", ["chips", "guacamole"], ["queso"], 1),
    ("order me a sofritas bowl with brown rice and pinto beans from chipotle",
     ["bowl", "sofritas", "brown rice", "pinto"], ["white rice", "black beans", "chicken"], 1),
]


def _item_text(cart: str) -> str:
    """The cart's own lines, without DoorDash's "Complement your cart" upsell list."""
    return cart.split("Complement your cart")[0]


def _relay_settings() -> None:
    """Load .env and default to the relay on the user's Chrome."""
    load_dotenv()
    os.environ["AGENT_CDP_URL"] = os.environ.get("E2E_CDP_URL", "auto")
    os.environ.setdefault("AGENT_CLOSE_TAB", "1")  # the cart checks open tabs too
    if not os.environ.get("AGENT_CHROME_PROFILE"):
        raise SystemExit("Set AGENT_CHROME_PROFILE to the Chrome profile to run in (its display name).")


async def _cart(clear: bool) -> tuple[int, str]:
    """Open the store, read the cart drawer, and empty it when asked."""
    from agent.executor import PlaywrightExecutor

    async with PlaywrightExecutor() as executor:
        page = executor.page
        await page.goto(STORE, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        for name in ("Essential only", "Close"):  # best effort: banners and leftover dialogs
            button = page.get_by_role("button", name=name, exact=True)
            try:
                if await button.count():
                    await button.first.click(timeout=3000)
                    await page.wait_for_timeout(600)
            except Exception:  # noqa: BLE001 — covered or gone: try Escape instead
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(400)
        icon = page.locator('[data-testid="OrderCartIconButton"]').first

        async def count() -> int:
            label = await icon.get_attribute("aria-label") or ""
            match = re.match(r"(\d+)", label)
            return int(match.group(1)) if match else -1

        n = await count()
        text = ""
        if n > 0:
            for attempt in range(3):  # a leftover DoorDash dialog can cover the cart button
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
                try:
                    await icon.click(timeout=5000)
                    break
                except Exception:  # noqa: BLE001 — covered: dismiss and retry
                    if attempt == 2:
                        raise
            await page.wait_for_timeout(2500)
            text = await page.evaluate("() => document.body.innerText.replace(/\\s+/g, ' ')")
            start = text.find("Your cart from")
            text = text[start:start + 600] if start >= 0 else text[:600]
            if clear:
                for _ in range(12):  # each pass removes one unit, if the drawer offers a control
                    remove = page.locator(
                        'button[aria-label*="remove" i], button[aria-label*="delete" i], '
                        'button[aria-label*="decrease" i], [data-testid*="Remove" i], [data-testid*="Decrement" i]')
                    if not await remove.count():
                        labels = await page.evaluate("""() => [...document.querySelectorAll('button')]
                            .filter(b => b.offsetParent).map(b => b.getAttribute('aria-label') || b.innerText)
                            .filter(Boolean).slice(-30)""")
                        print(f"  cart: no remove control found; visible buttons: {labels}", flush=True)
                        break
                    try:  # the stepper re-renders as it changes: a short, tolerant click
                        await remove.first.click(timeout=3000)
                    except Exception:  # noqa: BLE001 — detached mid-click; re-read and retry
                        pass
                    await page.wait_for_timeout(1500)
                    if await count() == 0:
                        break
                n = await count()
        return n, text


async def _suite(picks: list[int]) -> list[tuple]:
    rows = []
    for i in picks:
        goal, words, forbidden, count = CASES[i]
        print(f"\n===== case {i + 1}: {goal}", flush=True)
        before, _ = await _cart(clear=True)
        if before != 0:
            print(f"  cart not empty before the run ({before}); result check may be wrong", flush=True)
        started = time.monotonic()
        safe = await run_case_async(goal, STORE) == 0
        after, text = await _cart(clear=False)
        items = _item_text(text).lower()
        missing = [w for w in words if w.lower() not in items]
        extra = [w for w in forbidden if w.lower() in items]
        ok = safe and after == count and not missing and not extra
        rows.append((i + 1, goal, ok, safe, after, count, missing, extra, round(time.monotonic() - started)))
        print(f"  RESULT: {'PASS' if ok else 'FAIL'} safe={safe} items={after}/{count} missing={missing} "
              f"unwanted={extra}", flush=True)
        print(f"  cart: {text[:200]}", flush=True)
    return rows


def main() -> int:
    _relay_settings()
    picks = [int(a) - 1 for a in sys.argv[1:]] or list(range(len(CASES)))
    rows = asyncio.run(_suite(picks))
    print("\n===== suite summary")
    for n, goal, ok, safe, after, count, missing, extra, secs in rows:
        print(f"  {n}. {'PASS' if ok else 'FAIL'}  {goal[:60]:60}  safe={safe} items={after}/{count} "
              f"missing={missing} unwanted={extra} {secs}s")
    print(f"  passed {sum(r[2] for r in rows)}/{len(rows)}")
    return 0 if all(r[2] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
