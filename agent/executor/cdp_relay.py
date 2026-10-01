"""One-tab CDP relay between Playwright and a user's own Chrome.

`connect_over_cdp` takes over the whole browser: it attaches to every open
tab in every profile and waits for each to answer. On a personal Chrome with
many tabs, one tab that never answers (a tab put to sleep by Memory Saver)
stalls the connect. Playwright has no option to limit it to one tab.

This relay sits on 127.0.0.1 between the two. It opens ONE new tab in the
chosen profile and shows Playwright only that tab (plus popups the tab opens
and tabs Playwright itself creates). The user's other tabs are never
attached. `Browser.close` is answered locally and never reaches Chrome.

Nothing here is tied to one machine:
  AGENT_CDP_URL=auto            read the port from Chrome's DevToolsActivePort
  AGENT_CHROME_PROFILE=<name>   profile display name, as Chrome shows it
  AGENT_CHROME_USER_DATA_DIR    override the per-OS Chrome folder
  AGENT_CHROME_BINARY           override the per-OS Chrome program
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

import websockets

log = logging.getLogger("agent.executor.cdp_relay")

# Ids the relay uses for its own upstream calls. Playwright's ids start at 1,
# so the two never meet.
_RELAY_ID_BASE = 1_000_000_000
# Long enough for the user to click Chrome's "Allow remote debugging" prompt.
_UPSTREAM_OPEN_TIMEOUT_S = 120
_CALL_TIMEOUT_S = 15


class RelayError(RuntimeError):
    """The relay could not reach Chrome, or could not find the profile."""


def chrome_user_data_dir() -> Path:
    """Chrome's user data folder for this OS, unless the env overrides it."""
    override = os.environ.get("AGENT_CHROME_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Google" / "Chrome"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Google" / "Chrome" / "User Data"
    return home / ".config" / "google-chrome"


def devtools_ws_url(user_data_dir: Path) -> str:
    """The browser WebSocket Chrome writes when remote debugging is on.

    Chrome's built-in switch (chrome://inspect/#remote-debugging) serves only
    this socket, not /json/version, and picks a new port on every start."""
    path = user_data_dir / "DevToolsActivePort"
    try:
        port, ws_path = path.read_text().split()[:2]
    except (OSError, ValueError) as err:
        raise RelayError(
            f"No usable {path}. Turn on remote debugging in chrome://inspect/#remote-debugging "
            f"(or start Chrome with --remote-debugging-port), then retry."
        ) from err
    return f"ws://127.0.0.1:{port}{ws_path}"


def resolve_cdp_url(raw: str) -> str:
    """`auto` becomes the DevToolsActivePort socket; anything else is unchanged."""
    return devtools_ws_url(chrome_user_data_dir()) if raw.strip().lower() == "auto" else raw.strip()


def chrome_binary() -> str:
    """The Chrome program for this OS, unless the env overrides it."""
    override = os.environ.get("AGENT_CHROME_BINARY", "").strip()
    if override:
        return override
    if sys.platform == "darwin":
        candidates = [Path(base) / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
                      for base in ("/Applications", Path.home() / "Applications")]
    elif sys.platform.startswith("win"):
        candidates = [Path(os.environ.get(var, "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
                      for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if os.environ.get(var)]
    else:
        candidates = [Path(found) for name in ("google-chrome", "google-chrome-stable", "chromium")
                      if (found := shutil.which(name))]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise RelayError("Cannot find the Chrome program. Set AGENT_CHROME_BINARY.")


def profile_dir_for(name: str, user_data_dir: Path) -> str:
    """Folder name ("Profile 9") for a profile display name ("waterloo")."""
    try:
        cache = json.loads((user_data_dir / "Local State").read_text())["profile"]["info_cache"]
    except (OSError, KeyError, ValueError) as err:
        raise RelayError(f"Cannot read Chrome's profile list in {user_data_dir}") from err
    for folder, info in cache.items():
        if str(info.get("name", "")).strip().lower() == name.strip().lower():
            return folder
    names = sorted(str(info.get("name")) for info in cache.values())
    raise RelayError(f"No Chrome profile named {name!r}. Profiles: {names}")


def visible_targets(infos: list[dict[str, Any]], allowed: set[str]) -> list[dict[str, Any]]:
    """Target infos the client may see: the allowed tabs, and their children."""
    return [info for info in infos if info.get("targetId") in allowed or info.get("openerId") in allowed]


class _Link:
    """One debugging socket to Chrome, shared by every relay in this event loop.

    Chrome's built-in remote-debugging switch asks "Allow?" for each new
    connection. Sharing one connection means one prompt per process (the agent
    server, or a test suite) instead of one per run. Relays use it one at a
    time: the current `owner` receives every message that is not a reply to
    the link's own calls."""

    _open: dict[tuple[int, str], "_Link"] = {}

    def __init__(self, url: str, socket: Any) -> None:
        self.url, self.socket = url, socket
        self.ids = itertools.count(_RELAY_ID_BASE)
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.owner: CdpTabRelay | None = None
        self.reader = asyncio.create_task(self._read())

    @classmethod
    async def get(cls, url: str) -> "_Link":
        key = (id(asyncio.get_running_loop()), url)
        link = cls._open.get(key)
        if link is not None and not link.reader.done():
            return link
        try:
            socket = await websockets.connect(url, max_size=None, open_timeout=_UPSTREAM_OPEN_TIMEOUT_S)
        except Exception as err:  # noqa: BLE001 — one clear message for every connect failure
            raise RelayError(f"Could not open Chrome's debugging socket at {url}: {err}") from err
        link = cls(url, socket)
        cls._open[key] = link
        await link.call("Target.setDiscoverTargets", {"discover": True})
        return link

    async def call(self, method: str, params: dict[str, Any] | None = None,
                   session: str | None = None) -> dict[str, Any]:
        call_id = next(self.ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[call_id] = future
        message: dict[str, Any] = {"id": call_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        await self.socket.send(json.dumps(message))
        reply = await asyncio.wait_for(future, _CALL_TIMEOUT_S)
        if "error" in reply:
            raise RelayError(f"{method} failed: {reply['error'].get('message')}")
        return reply.get("result", {})

    async def _read(self) -> None:
        try:
            async for raw in self.socket:
                message = json.loads(raw)
                call_id = message.get("id")
                if call_id is not None and call_id >= _RELAY_ID_BASE:
                    future = self.pending.pop(call_id, None)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif self.owner is not None:
                    try:
                        await self.owner._dispatch(message)
                    except Exception as err:  # noqa: BLE001 — a client-side failure must not end the shared link
                        log.warning("cdp relay: could not pass a message to the client: %s", err)
        except websockets.ConnectionClosed:
            log.info("cdp relay: Chrome closed the debugging socket")
        finally:
            # Calls still waiting would otherwise wait out the full timeout.
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RelayError("Chrome closed the debugging socket"))
            self.pending.clear()


class CdpTabRelay:
    """Serve one tab of a running Chrome to one Playwright client."""

    def __init__(self, upstream_url: str, *, profile: str | None = None,
                 user_data_dir: Path | None = None) -> None:
        self._upstream_url = upstream_url
        self._profile = profile
        self._user_data_dir = user_data_dir or chrome_user_data_dir()
        self._link: _Link | None = None
        self._server: Any = None
        self._client: Any = None
        self._client_methods: dict[int, str] = {}
        self._allowed: set[str] = set()
        self._sessions: set[str] = set()
        self._auto_attach = False
        self._marker: str | None = None
        self._marked: asyncio.Future[str] | None = None
        self.target_id: str | None = None

    async def start(self) -> str:
        """Open the tab and the local socket. Returns the URL for Playwright."""
        self._link = await _Link.get(self._upstream_url)
        if self._link.owner is not None:
            raise RelayError("Another relay is using this Chrome connection; runs share it one at a time.")
        self._link.owner = self
        try:
            if self._profile:
                target_id = await self._open_in_profile()
            else:
                target_id = (await self._call("Target.createTarget", {"url": "about:blank"}))["targetId"]
            self.target_id = target_id
            self._allowed.add(target_id)
            self._server = await websockets.serve(self._serve_client, "127.0.0.1", 0, max_size=None)
        except BaseException:
            # The caller's exit step does not run when its enter step fails:
            # hand the link back here, or every later run in this process is refused.
            self._link.owner = None
            raise
        port = self._server.sockets[0].getsockname()[1]
        log.info("cdp relay: tab %s in profile %r on port %d", target_id[:8], self._profile, port)
        return f"ws://127.0.0.1:{port}/devtools/browser/relay"

    async def close(self) -> None:
        """Stop serving and hand the shared connection back. The tab stays
        open, as the executor wants; the client's sessions on it are detached
        so the next relay starts clean."""
        if self._server is not None:
            self._server.close()
        if self._link is not None and self._link.owner is self:
            for session in list(self._sessions):
                try:
                    await self._link.call("Target.detachFromTarget", {"sessionId": session})
                except (RelayError, asyncio.TimeoutError, websockets.ConnectionClosed):
                    pass
            self._link.owner = None

    # ---- upstream --------------------------------------------------------

    async def _call(self, method: str, params: dict[str, Any] | None = None,
                    session: str | None = None) -> dict[str, Any]:
        assert self._link is not None
        return await self._link.call(method, params, session)

    async def _open_in_profile(self) -> str:
        """Open the agent's tab in the named profile and return its target id.

        CDP cannot open a tab in another profile's context ("Failed to find
        browser context"). Chrome itself can: run with --profile-directory
        while Chrome is up, it hands the URL to the running browser, which
        opens it as a new tab in that profile's window. A unique marker in
        the URL picks that one tab out of Chrome's target events."""
        folder = profile_dir_for(self._profile or "", self._user_data_dir)
        self._marker = f"agent-relay-{uuid.uuid4().hex}"
        self._marked = asyncio.get_running_loop().create_future()
        launcher = await asyncio.create_subprocess_exec(
            chrome_binary(), f"--user-data-dir={self._user_data_dir}", f"--profile-directory={folder}",
            # Chrome drops about:blank URLs passed this way; a data: URL survives.
            f"data:text/html,<title>{self._marker}</title>",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            return await asyncio.wait_for(self._marked, _CALL_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise RelayError(f"Chrome did not open a tab in profile {self._profile!r} ({folder}).") from None
        finally:
            self._marker = None
            if launcher.returncode is None:  # it hands off and exits; never leave it behind
                try:
                    await asyncio.wait_for(launcher.wait(), 5)
                except asyncio.TimeoutError:
                    launcher.kill()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        """A message from Chrome that is not a reply to the link's own calls."""
        if message.get("id") is not None:
            await self._client_reply(message)
        else:
            await self._upstream_event(message)

    async def _client_reply(self, message: dict[str, Any]) -> None:
        method = self._client_methods.pop(message["id"], "")
        result = message.get("result")
        if method == "Target.getTargets" and result:
            result["targetInfos"] = visible_targets(result["targetInfos"], self._allowed)
        await self._to_client(message)
        if method == "Target.createTarget" and result:  # a tab Playwright opened itself
            self._allowed.add(result["targetId"])
            if self._auto_attach:
                await self._attach(result["targetId"])

    async def _upstream_event(self, message: dict[str, Any]) -> None:
        method, params, session = message.get("method", ""), message.get("params", {}), message.get("sessionId")
        if session is not None:  # an event inside a tab: forward only for tabs the client holds
            if session not in self._sessions:
                return
            if method == "Target.attachedToTarget":  # an iframe or worker of an allowed tab
                self._sessions.add(params["sessionId"])
            await self._to_client(message)
            return
        info = params.get("targetInfo", {})
        target = info.get("targetId") or params.get("targetId")
        if self._marker and self._marked is not None and not self._marked.done() \
                and self._marker in info.get("url", ""):
            self._marked.set_result(target)  # the tab _open_in_profile asked Chrome for
        if method == "Target.targetCreated" and info.get("openerId") in self._allowed:
            self._allowed.add(target)  # a popup from the agent's tab
            if self._auto_attach:
                await self._attach(target)
        if method == "Target.attachedToTarget" and target in self._allowed:
            self._sessions.add(params["sessionId"])
        elif method == "Target.detachedFromTarget":
            if params.get("sessionId") not in self._sessions:
                return
            self._sessions.discard(params.get("sessionId"))
        elif target not in self._allowed:
            return
        await self._to_client(message)

    async def _attach(self, target: str) -> None:
        """Attach the client to one allowed tab. Chrome then sends the
        attachedToTarget event the client waits for."""
        try:
            await self._call("Target.attachToTarget", {"targetId": target, "flatten": True})
        except RelayError as err:
            log.warning("cdp relay: attach to %s failed: %s", target[:8], err)

    # ---- client ----------------------------------------------------------

    async def _to_client(self, message: dict[str, Any]) -> None:
        if self._client is not None:
            await self._client.send(json.dumps(message))

    async def _serve_client(self, socket: Any) -> None:
        if self._client is not None:
            await socket.close(code=1008, reason="relay serves one client")
            return
        self._client = socket
        try:
            async for raw in socket:
                await self._from_client(json.loads(raw))
        finally:
            self._client = None

    async def _from_client(self, message: dict[str, Any]) -> None:
        method, session, call_id = message.get("method", ""), message.get("sessionId"), message.get("id")
        if session is None and method == "Target.setAutoAttach":
            # Never forward: at browser level it attaches to every tab. Attach
            # the allowed tabs instead, after the reply the client expects.
            await self._to_client({"id": call_id, "result": {}})
            self._auto_attach = bool(message.get("params", {}).get("autoAttach"))
            if self._auto_attach:
                for target in sorted(self._allowed):
                    await self._attach(target)
            return
        if session is None and method == "Browser.close":
            await self._to_client({"id": call_id, "result": {}})  # the user's Chrome stays open
            return
        if session is None and method == "Target.closeTarget" and \
                message.get("params", {}).get("targetId") not in self._allowed:
            await self._to_client({"id": call_id, "error": {"code": -32000, "message": "not a relay tab"}})
            return
        if call_id is not None:
            self._client_methods[call_id] = method
        assert self._link is not None
        await self._link.socket.send(json.dumps(message))


# ---- Inline tests: `uv run python -m agent.executor.cdp_relay` -----------------------
# The end-to-end tests launch a throwaway headless Chromium with its own temp
# profile and two extra tabs. They never touch the user's Chrome.


class RelayTestFailure(AssertionError):
    """An inline relay test saw the wrong result."""


def _test_config_resolution() -> None:
    """Unit: `auto`, profile names, and target filtering, with no Chrome running."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "DevToolsActivePort").write_text("55497\n/devtools/browser/abc\n")
        (folder / "Local State").write_text(json.dumps(
            {"profile": {"info_cache": {"Default": {"name": "Me"}, "Profile 9": {"name": "waterloo"}}}}))
        os.environ["AGENT_CHROME_USER_DATA_DIR"] = tmp
        try:
            if resolve_cdp_url("auto") != "ws://127.0.0.1:55497/devtools/browser/abc":
                raise RelayTestFailure(f"auto resolved to {resolve_cdp_url('auto')}")
        finally:
            del os.environ["AGENT_CHROME_USER_DATA_DIR"]
        if resolve_cdp_url("http://localhost:9222/") != "http://localhost:9222/":
            raise RelayTestFailure("a plain URL changed")
        if profile_dir_for("Waterloo", folder) != "Profile 9":
            raise RelayTestFailure("profile name did not map to its folder")
        try:
            profile_dir_for("nope", folder)
            raise RelayTestFailure("an unknown profile was accepted")
        except RelayError:
            pass
    infos = [{"targetId": "a"}, {"targetId": "b"}, {"targetId": "c", "openerId": "a"}]
    if [i["targetId"] for i in visible_targets(infos, {"a"})] != ["a", "c"]:
        raise RelayTestFailure("target filter showed the wrong tabs")


async def _throwaway_executable() -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        return pw.chromium.executable_path


async def _launch_throwaway_chrome(tmp: str, *, headless: bool = True) -> Any:
    """A Chromium with remote debugging and two tabs that are not the agent's.
    Windowed when a test needs Chrome's hand-off of a URL to a running browser,
    which headless mode does not do."""
    executable = await _throwaway_executable()
    mode = ["--headless=new"] if headless else ["--window-size=400,300", "--window-position=0,0"]
    process = await asyncio.create_subprocess_exec(
        executable, *mode, "--remote-debugging-port=0", f"--user-data-dir={tmp}",
        "--no-first-run", "--no-default-browser-check", "data:text/html,<title>user tab 1</title>",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    for _ in range(100):
        if (Path(tmp) / "DevToolsActivePort").exists():
            # Headless takes one start URL, so the second user tab is made here.
            async with websockets.connect(devtools_ws_url(Path(tmp)), max_size=None) as socket:
                await socket.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                              "params": {"url": "data:text/html,<title>user tab 2</title>"}}))
                while json.loads(await socket.recv()).get("id") != 1:
                    pass
            await asyncio.sleep(0.5)  # let both titles load
            return process
        await asyncio.sleep(0.1)
    process.kill()
    raise RelayTestFailure("throwaway Chromium wrote no DevToolsActivePort")


async def _all_titles(upstream: str) -> list[str]:
    async with websockets.connect(upstream, max_size=None) as socket:
        await socket.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
        while True:
            reply = json.loads(await socket.recv())
            if reply.get("id") == 1:
                return [i["title"] for i in reply["result"]["targetInfos"] if i["type"] == "page"]


async def _user_tab_titles(upstream: str) -> list[str]:
    async with websockets.connect(upstream, max_size=None) as socket:
        await socket.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
        while True:
            reply = json.loads(await socket.recv())
            if reply.get("id") == 1:
                return sorted(i["title"] for i in reply["result"]["targetInfos"]
                              if i["type"] == "page" and i["title"].startswith("user tab"))


async def _test_relay_shows_one_tab() -> None:
    """End to end: Playwright through the relay sees one tab, drives it, and
    cannot close the browser; the other tabs are untouched."""
    import tempfile

    from playwright.async_api import async_playwright

    with tempfile.TemporaryDirectory() as tmp:
        process = await _launch_throwaway_chrome(tmp)
        try:
            upstream = devtools_ws_url(Path(tmp))
            relay = CdpTabRelay(upstream, user_data_dir=Path(tmp))
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(await relay.start(), timeout=20_000)
                pages = [p for c in browser.contexts for p in c.pages]
                if len(pages) != 1:
                    raise RelayTestFailure(f"client saw {len(pages)} tabs, wanted 1: {[p.url for p in pages]}")
                await pages[0].goto("data:text/html,<title>agent tab</title>")
                if await pages[0].title() != "agent tab":
                    raise RelayTestFailure("could not drive the relay tab")
                await browser.close()
            await relay.close()
            if process.returncode is not None:
                raise RelayTestFailure("Browser.close reached Chrome and closed it")
            if await _user_tab_titles(upstream) != ["user tab 1", "user tab 2"]:
                raise RelayTestFailure("the user's other tabs changed")
        finally:
            process.kill()
            await process.wait()


async def _test_executor_through_relay() -> None:
    """End to end: PlaywrightExecutor with AGENT_CDP_URL=auto and a profile
    name finds the profile, opens its tab there, and drives it."""
    import tempfile

    from .browser import PlaywrightExecutor

    with tempfile.TemporaryDirectory() as tmp:
        # A real install already has this file; a fresh headless one writes it late.
        (Path(tmp) / "Local State").write_text(json.dumps(
            {"profile": {"info_cache": {"Default": {"name": "Relay Test"}}}}))
        process = await _launch_throwaway_chrome(tmp, headless=False)
        saved = {k: os.environ.get(k) for k in ("AGENT_CDP_URL", "AGENT_CHROME_PROFILE", "AGENT_CHROME_USER_DATA_DIR",
                                                "AGENT_CHROME_BINARY")}
        try:
            name = "relay test"  # matched case-insensitively
            os.environ.update(AGENT_CDP_URL="auto", AGENT_CHROME_PROFILE=name, AGENT_CHROME_USER_DATA_DIR=tmp,
                              AGENT_CHROME_BINARY=await _throwaway_executable())
            async with PlaywrightExecutor() as executor:
                await executor.page.goto("data:text/html,<title>executor tab</title>")
                if await executor.page.title() != "executor tab":
                    raise RelayTestFailure("executor could not drive its relay tab")
            if await _user_tab_titles(devtools_ws_url(Path(tmp))) != ["user tab 1", "user tab 2"]:
                raise RelayTestFailure("executor run changed the user's other tabs")
            if "executor tab" in await _all_titles(devtools_ws_url(Path(tmp))):
                raise RelayTestFailure("the run's tab was left open")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            process.kill()
            await process.wait()


async def _test_relays_share_one_connection() -> None:
    """End to end: two relays in a row, in one event loop, reuse one debugging
    connection (one Chrome "Allow" prompt), and each still drives only its tab."""
    import tempfile

    from playwright.async_api import async_playwright

    with tempfile.TemporaryDirectory() as tmp:
        process = await _launch_throwaway_chrome(tmp)
        try:
            upstream = devtools_ws_url(Path(tmp))
            links = set()
            # A start that fails (no such profile) hands the link back for the next run.
            try:
                await CdpTabRelay(upstream, profile="no such profile", user_data_dir=Path(tmp)).start()
                raise RelayTestFailure("a start with an unknown profile succeeded")
            except RelayError:
                pass
            async with async_playwright() as pw:
                for title in ("first run", "second run"):
                    relay = CdpTabRelay(upstream, user_data_dir=Path(tmp))
                    browser = await pw.chromium.connect_over_cdp(await relay.start(), timeout=20_000)
                    links.add(id(relay._link))
                    pages = [p for c in browser.contexts for p in c.pages]
                    if len(pages) != 1:
                        raise RelayTestFailure(f"{title}: client saw {len(pages)} tabs")
                    await pages[0].goto(f"data:text/html,<title>{title}</title>")
                    if await pages[0].title() != title:
                        raise RelayTestFailure(f"{title}: could not drive its tab")
                    await browser.close()
                    await relay.close()
            if len(links) != 1:
                raise RelayTestFailure(f"relays opened {len(links)} connections, wanted 1")
        finally:
            process.kill()
            await process.wait()


if __name__ == "__main__":
    _test_config_resolution()
    asyncio.run(_test_relays_share_one_connection())
    asyncio.run(_test_relay_shows_one_tab())
    asyncio.run(_test_executor_through_relay())
    print("cdp_relay.py inline tests passed")
