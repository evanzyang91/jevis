"""The human-check hand-off in a real Chromium, against a local stand-in site.

The stand-in check page behaves like Walmart's: `/blocked?url=L2Vu` stands in
for `/en`, and once "the person" is done (here: a timer in the page) it sends
the visitor on. The page also watches for the agent: it records any marker
event Playwright dispatches, any attribute the reader stamps, and any script
the agent runs in its world. While the run waits, it must see none of that.

Needs a Playwright Chromium; skipped when one cannot be launched.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from playwright.async_api import async_playwright

import agent.supervisor.loop as loop
from agent.executor import Outcome
from agent.policy import Decision
from agent.supervisor import Supervisor
from agent.transport import Bus

CHECK_PAGE = """<!doctype html>
<html><head><title>Verify Your Identity</title></head>
<body>
<p>We like real shoppers, not robots! Please press and hold the button below to verify yourself.</p>
<div id="px-captcha"></div>
<script>
  // What a bot-check script could see of the agent while the person works.
  // Each entry: [wall-clock ms, what].
  const seen = [];
  const note = (what) => seen.push([performance.timeOrigin + performance.now(), what]);
  for (const name of ["__playwright_mark_target__", "__playwright_reset_targets__"]) {
    document.addEventListener(name, () => note(name), true);
  }
  new MutationObserver((records) => {
    for (const r of records) {
      const added = [...r.addedNodes].map((n) => n.nodeName).join(",");
      note(`mutation:${r.attributeName || r.type} on ${r.target.nodeName} ${added}`);
    }
  }).observe(document.documentElement, {subtree: true, attributes: true, childList: true});
  const title = Object.getOwnPropertyDescriptor(Document.prototype, "title");
  Object.defineProperty(document, "title", {
    get() { note("main-world title read"); return title.get.call(this); },
    configurable: true,
  });
  addEventListener("load", () => note("load"));
  setTimeout(async () => {
    await fetch("/report", {method: "POST", body: JSON.stringify(seen)});
    location.href = "/en";  // the person is through: the site sends them on
  }, Number(new URLSearchParams(location.search).get("hold") || "1500"));
</script>
</body></html>"""

HOME_PAGE = """<!doctype html>
<html><head><title>Walmart.ca stand-in</title></head>
<body><header><a href="/en">Home</a><input type="search" aria-label="Search"><button>Search</button></header>
<main>""" + "".join(f'<article><h2>Pasta {i}</h2><button>Add to cart - Pasta {i}</button></article>'
                     for i in range(8)) + "</main></body></html>"


class _Site(BaseHTTPRequestHandler):
    reports: list[list[str]] = []

    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        body = CHECK_PAGE if self.path.startswith("/blocked") else HOME_PAGE
        self._send(body.encode())

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        _Site.reports.append(json.loads(self.rfile.read(length) or b"[]"))
        self._send(b"{}")

    def _send(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture
def site() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    _Site.reports = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class _Executor:
    def __init__(self, page: Any) -> None:
        self.page = page
        self.navigations: list[str] = []

    async def navigate(self, url: str) -> Outcome:
        self.navigations.append(url)
        await self.page.goto(url, wait_until="domcontentloaded")
        return Outcome(page_changed=True, url_changed=True, load_ms=0, final_url=self.page.url)

    async def act(self, action: Any) -> Outcome:  # pragma: no cover
        raise AssertionError("no action expected")


async def test_real_browser_waits_unseen_then_resumes_on_the_target_page(
        site: str, monkeypatch: pytest.MonkeyPatch) -> None:
    decided: list[str] = []

    async def fake_decide(**kwargs: Any) -> Decision:
        decided.append(kwargs["observation"].url)
        return Decision(operation="DONE", target=None, action=None, confidence=0.9, probabilities={},
                        model="stand-in", latency_ms=1, usage={})

    monkeypatch.setattr(loop, "decide", fake_decide)
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as err:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {err}")
        try:
            page = await (await browser.new_context()).new_page()
            events: list = []
            supervisor = Supervisor(executor=_Executor(page), bus=Bus(sink=events.append), jev=None,  # type: ignore[arg-type]
                                    text=None, goal="Buy pasta.", captcha_wait_s=20, captcha_poll_s=0.25)  # type: ignore[arg-type]
            # The run asks for the check page directly, as Walmart's redirect would land it there.
            state = await asyncio.wait_for(supervisor.run(f"{site}/blocked?url=L2Vu&hold=2000"), 20)
        finally:
            await browser.close()

    statuses = [(e.status, e.reason) for e in events if e.kind == "status"]
    assert ("paused", "127.0.0.1 wants a person to confirm they are human.") in statuses
    assert ("running", "check completed, continuing") in statuses
    assert state.status == "done"
    assert decided == [f"{site}/en"]  # decided once, on the page the check stood in for

    # What the check page saw of the agent. Before the pause, the run's one
    # read of the page (the reader, in the page's world) reads the title;
    # that is how the check was found. From the pause on, while the person
    # works and the run looks every 0.25 s, the page sees nothing at all: no
    # marker events, no stamped attributes, no script in its world.
    [report] = _Site.reports
    paused_at = next(e for e in events if e.kind == "captcha").ts.timestamp() * 1000
    during_wait = [what for at, what in report if at > paused_at]
    assert during_wait == []
    assert not [what for _, what in report if what.startswith(("__playwright", "mutation:data-agent-ref"))]
