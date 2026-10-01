"""Integration test for DOM perception.

Launches a real Chromium via Playwright against a data: URL so the test needs
no network. Skipped when the Chromium binary is not installed; developers can
run `uv run playwright install chromium` to enable it locally.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from agent.perception import observe

FIXTURE = """
<!doctype html>
<html>
  <head><title>Fixture</title></head>
  <body>
    <header><h1>Store</h1></header>
    <main>
      <form>
        <label>Query <input id="q" name="q" placeholder="Search" /></label>
        <button type="submit">Search</button>
      </form>
      <section aria-label="results">
        <article>
          <h2>Flour</h2>
          <button data-name="flour">Add to cart</button>
        </article>
        <article>
          <h2>Sugar</h2>
          <button data-name="sugar">Add to cart</button>
        </article>
      </section>
      <select id="qty">
        <option value="1">1</option>
        <option value="2">2</option>
      </select>
    </main>
    <footer><a href="/help">Help</a></footer>
  </body>
</html>
"""


def _fixture_url() -> str:
    encoded = base64.b64encode(FIXTURE.encode()).decode()
    return f"data:text/html;base64,{encoded}"


def _chromium_available() -> bool:
    """Playwright downloads Chromium into a per-version cache; if the folder
    is missing the launch will fail. Skip in that case."""
    root = Path.home() / ".cache" / "ms-playwright"
    if not root.exists():
        return False
    return any(child.name.startswith("chromium") for child in root.iterdir())


pytestmark = pytest.mark.skipif(
    not _chromium_available(),
    reason="Chromium not installed. Run `uv run playwright install chromium`.",
)


@pytest.mark.asyncio
async def test_observe_reports_interactive_elements() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(_fixture_url(), wait_until="domcontentloaded")
            observation = await observe(page)
            roles = [e.role for e in observation.elements]
            names = [e.name for e in observation.elements]
            assert "textbox" in roles or "searchbox" in roles
            assert any(name.startswith("Search") for name in names)
            add_buttons = [e for e in observation.elements if e.name == "Add to cart"]
            assert len(add_buttons) == 2
            # Each element has a stable, distinct ref that Playwright can locate.
            refs = [e.ref for e in observation.elements]
            assert len(refs) == len(set(refs))
            for element in observation.elements[:2]:
                located = page.locator(element.ref)
                assert await located.count() == 1
            # Guards cover every reported element.
            assert set(observation.guards) == set(refs)
            # Marker is stable across identical observations.
            again = await observe(page)
            assert observation.marker == again.marker
        finally:
            await context.close()
            await browser.close()
