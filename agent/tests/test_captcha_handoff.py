"""The human-check hand-off, end to end through `Supervisor.run`.

A stand-in site plays Walmart: while it is "blocking", any page the run opens
becomes its `/blocked?url=<base64 path>` check page; a stand-in person later
does the check. No browser and no model: the reader and the policy are
replaced, everything else (detection, the pause, the cheap probe, resume,
going back to the page the check replaced) is the real code.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import pytest

import agent.supervisor.loop as loop
from agent.executor import Outcome
from agent.perception import Element, Observation, Rect
from agent.policy import Decision
from agent.supervisor import Supervisor
from agent.transport import Bus

HOME = "https://www.walmart.ca/en"


class Page:
    """The tab: what it shows, and what the agent did to it."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.title_text = ""
        self.text = ""
        self.controls = 0
        self.fronted = 0
        self.reads = 0  # full reads (the reader), not probes

    def show(self, url: str, title: str, text: str, controls: int) -> None:
        self.url, self.title_text, self.text, self.controls = url, title, text, controls

    async def title(self) -> str:
        return self.title_text

    async def content(self) -> str:
        return f"<html><title>{self.title_text}</title><body>{self.text}</body></html>"

    async def bring_to_front(self) -> None:
        self.fronted += 1


class Site:
    """Walmart, as far as these tests need it."""

    def __init__(self, *, blocking: bool) -> None:
        self.blocking = blocking
        self.page = Page()
        self.navigations: list[str] = []

    def open(self, url: str) -> None:
        path = urlparse(url).path or "/"
        if self.blocking:
            encoded = base64.b64encode(path.encode()).decode().rstrip("=")
            self.page.show(f"https://www.walmart.ca/blocked?url={encoded}&uuid=u&vid=v&g=b", "Verify Your Identity",
                           "We like real shoppers, not robots! Please press and hold the button below.", 0)
        else:
            self.page.show(url, "Walmart.ca", "Groceries Pasta Add to cart", 30)

    def person_does_the_check(self, *, site_sends_them_on: bool = True) -> None:
        """Press and hold succeeded. Walmart normally sends the visitor on to
        the page the check stood in for; sometimes the tab stays put."""
        self.blocking = False
        if site_sends_them_on:
            self.open(HOME)


class Executor:
    def __init__(self, site: Site) -> None:
        self.site = site
        self.page = site.page

    async def navigate(self, url: str) -> Outcome:
        self.site.navigations.append(url)
        self.site.open(url)
        return Outcome(page_changed=True, url_changed=True, load_ms=0, final_url=self.page.url)

    async def act(self, action: Any) -> Outcome:  # pragma: no cover — the stand-in policy never acts
        raise AssertionError("no action expected")


def _read(page: Page) -> Observation:
    page.reads += 1
    elements = tuple(Element(ref=f"e{i}", role="link", name=f"Link {i}", bounds=Rect(0, 0, 9, 9))
                     for i in range(page.controls))
    return Observation(url=page.url, title=page.title_text, text=page.text, elements=elements,
                       marker=f"{page.url}|{page.text}", fingerprint="f", guards={}, can_go_back=False,
                       can_scroll_up=False, can_scroll_down=False, viewport=(1280, 800))


@pytest.fixture
def decided(monkeypatch: pytest.MonkeyPatch) -> list[Observation]:
    """Replace the reader and the policy. The policy says DONE at once, and
    records every page it was asked about."""
    seen: list[Observation] = []

    async def fake_observe(page: Page, **_: Any) -> Observation:
        return _read(page)

    async def fake_decide(**kwargs: Any) -> Decision:
        seen.append(kwargs["observation"])
        return Decision(operation="DONE", target=None, action=None, confidence=0.9, probabilities={},
                        model="stand-in", latency_ms=1, usage={})

    monkeypatch.setattr(loop, "observe", fake_observe)
    monkeypatch.setattr(loop, "decide", fake_decide)
    return seen


def _supervisor(site: Site, events: list, *, wait_s: float = 5.0) -> Supervisor:
    return Supervisor(executor=Executor(site), bus=Bus(sink=events.append), jev=None, text=None,  # type: ignore[arg-type]
                      goal="Buy pasta.", captcha_wait_s=wait_s, captcha_poll_s=0.02)


async def _until(check: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.01)


def _statuses(events: list) -> list[tuple[str, str]]:
    return [(e.status, e.reason) for e in events if e.kind == "status"]


def _captcha(events: list) -> Any:
    return next(e for e in events if e.kind == "captcha")


async def test_resumes_by_itself_once_the_person_does_the_check(decided: list[Observation]) -> None:
    site, events = Site(blocking=True), []
    supervisor = _supervisor(site, events)
    task = asyncio.create_task(supervisor.run(HOME))
    await _until(lambda: any(e.kind == "captcha" for e in events))

    captcha = _captcha(events)
    assert captcha.url.startswith("https://www.walmart.ca/blocked?url=L2Vu")
    assert captcha.wait_s == 5
    assert "walmart.ca" in captcha.reason
    assert site.page.fronted == 1  # the check's tab was brought to the person

    await asyncio.sleep(0.1)  # several polls while the check is up: still paused
    assert task.done() is False
    site.person_does_the_check()
    state = await asyncio.wait_for(task, 3)

    assert state.status == "done"
    assert _statuses(events) == [
        ("running", ""),
        ("paused", captcha.reason),
        ("running", "check completed, continuing"),
        ("done", "goal reported met"),
    ]
    assert site.navigations == [HOME]  # Walmart sent the person on; no second trip
    assert all("blocked" not in o.url for o in decided)  # never decided on the check page


async def test_waiting_reads_nothing_while_the_check_url_is_up(decided: list[Observation]) -> None:
    site, events = Site(blocking=True), []
    task = asyncio.create_task(_supervisor(site, events).run(HOME))
    await _until(lambda: any(e.kind == "captcha" for e in events))
    reads = site.page.reads
    await asyncio.sleep(0.2)  # about ten polls
    assert site.page.reads == reads  # only the cheap probe ran; the reader never touched the check
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_continue_button_goes_back_to_the_page_the_check_replaced(decided: list[Observation]) -> None:
    site, events = Site(blocking=True), []
    supervisor = _supervisor(site, events)
    task = asyncio.create_task(supervisor.run(HOME))
    await _until(lambda: any(e.kind == "captcha" for e in events))

    site.person_does_the_check(site_sends_them_on=False)  # the tab still shows /blocked
    assert supervisor.resume_from_captcha("not the token") is False
    assert supervisor.resume_from_captcha(_captcha(events).resume_token) is True
    state = await asyncio.wait_for(task, 3)

    assert state.status == "done"
    assert site.navigations == [HOME, HOME]  # decoded from /blocked?url=L2Vu
    assert ("running", "continued by you") in _statuses(events)
    assert supervisor.resume_from_captcha(_captcha(events).resume_token) is False  # the pause is over


async def test_continue_while_still_blocked_pauses_again(decided: list[Observation]) -> None:
    site, events = Site(blocking=True), []
    supervisor = _supervisor(site, events)
    task = asyncio.create_task(supervisor.run(HOME))
    await _until(lambda: any(e.kind == "captcha" for e in events))
    supervisor.resume_from_captcha(_captcha(events).resume_token)  # pressed without doing the check
    await _until(lambda: sum(e.kind == "captcha" for e in events) == 2)
    assert task.done() is False
    assert decided == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_stops_with_a_clear_reason_when_nobody_does_the_check(decided: list[Observation]) -> None:
    site, events = Site(blocking=True), []
    state = await asyncio.wait_for(_supervisor(site, events, wait_s=1).run(HOME), 4)
    assert state.status == "blocked"
    assert _statuses(events)[-1] == ("blocked", "Nobody completed the human check on walmart.ca within 1 second.")
    assert decided == []


async def test_headless_stops_at_once_instead_of_waiting_for_nobody(decided: list[Observation]) -> None:
    site, events = Site(blocking=True), []
    state = await asyncio.wait_for(_supervisor(site, events, wait_s=0).run(HOME), 2)
    assert state.status == "blocked"
    status, reason = _statuses(events)[-1]
    assert status == "blocked" and "headless" in reason and "walmart.ca" in reason
    assert _captcha(events).wait_s == 0  # the UI is told what was met, and that nobody can do it
    assert "paused" not in [s for s, _ in _statuses(events)]
    assert site.page.fronted == 0


async def test_stop_while_waiting(decided: list[Observation]) -> None:
    site, events = Site(blocking=True), []
    supervisor = _supervisor(site, events)
    task = asyncio.create_task(supervisor.run(HOME))
    await _until(lambda: any(e.kind == "captcha" for e in events))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _statuses(events)[-1] == ("blocked", "stopped by user")
    assert supervisor.state is not None and supervisor.state.captcha_token is None


async def test_a_check_known_only_by_its_wording_is_reread_only_when_the_page_changes(
        decided: list[Observation]) -> None:
    """No URL or title gives this check away, so the cheap look cannot judge
    it alone. A full read confirms it once, and is not repeated until the
    document changes; then one more read sees it gone and the run resumes."""
    site, events = Site(blocking=False), []
    site.open = lambda url: site.page.show(url, "Shop", "Please verify you are a human. Press and hold.", 1)  # type: ignore[method-assign]
    task = asyncio.create_task(_supervisor(site, events).run("https://shop.test/"))
    await _until(lambda: any(e.kind == "captcha" for e in events))
    await asyncio.sleep(0.25)  # about a dozen polls
    assert site.page.reads == 2  # the loop's own read, then one confirming read
    assert sum(e.kind == "captcha" for e in events) == 1  # no pause/resume flapping

    site.page.show("https://shop.test/", "Shop", "Groceries Add to cart", 30)
    state = await asyncio.wait_for(task, 3)
    assert state.status == "done"
    assert [o.url for o in decided] == ["https://shop.test/"]
    assert "Groceries" in decided[0].text
