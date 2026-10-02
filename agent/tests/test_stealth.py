"""The launched-browser shims touch only the headless shell.

A full Chromium (what a headed launch, new headless, or the Chrome channel
runs) must come out of the init script exactly as it went in: every value a
shim would fake there is a native, consistent one. Needs Playwright's
Chromium builds; skipped when they cannot be launched.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.async_api import async_playwright

from agent.executor.stealth import init_script

READ = """() => {
  const getter = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver').get.toString();
  const gl = document.createElement('canvas').getContext('webgl');
  const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
  return {
    webdriver: String(navigator.webdriver),
    nativeGetter: getter.includes('[native code]'),
    ownNavigatorProps: Object.getOwnPropertyNames(navigator),
    plugins: navigator.plugins.length,
    pluginArray: navigator.plugins instanceof PluginArray,
    chromeRuntime: typeof (window.chrome && window.chrome.runtime),
    renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
  };
}"""


async def _read(launch: dict[str, Any], *, shimmed: bool) -> dict[str, Any]:
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"], **launch)
        except Exception as err:  # noqa: BLE001
            pytest.skip(f"Chromium build not available: {err}")
        try:
            context = await browser.new_context()
            if shimmed:
                await context.add_init_script(init_script())
            page = await context.new_page()
            await page.goto("data:text/html,<title>x</title>")
            return await page.evaluate(READ)
        finally:
            await browser.close()


async def test_full_chromium_is_left_exactly_as_it_is() -> None:
    full = {"headless": True, "channel": "chromium"}  # new headless: the full browser, as a headed launch runs
    assert await _read(full, shimmed=True) == await _read(full, shimmed=False)


async def test_headless_shell_still_gets_its_shims() -> None:
    shell = {"headless": True}  # Playwright's default headless: the old headless shell
    native = await _read(shell, shimmed=False)
    if native["plugins"] != 0:
        pytest.skip("this Playwright's headless build is not the old shell")
    shimmed = await _read(shell, shimmed=True)
    assert shimmed["plugins"] == 3
    assert shimmed["chromeRuntime"] == "object"
    assert shimmed["renderer"] == "Intel Iris OpenGL Engine"
