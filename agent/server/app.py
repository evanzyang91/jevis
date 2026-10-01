"""FastAPI HTTP + WebSocket surface.

The Next.js app proxies to this process. Every route is thin — it delegates to
the `RunManager`. Keeping the boundary narrow means the same manager can be
driven by a script, a CLI, or a different HTTP framework later.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import AsyncIterator
from uuid import UUID

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent.providers.registry import REGISTRY, defaults

from .db import DatabaseHandle, open_database
from .manager import RunManager

_state: dict[str, object] = {}


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the DB pool once; hand its playbook store to the manager."""
    del app
    handle: DatabaseHandle = await open_database()
    _state["db"] = handle
    _state["manager"] = RunManager(playbook=handle.playbook)
    try:
        yield
    finally:
        await handle.close()


app = FastAPI(title="agent-server", version="0.1.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def manager() -> RunManager:
    if "manager" not in _state:
        raise RuntimeError("Server not started")
    return _state["manager"]  # type: ignore[return-value]


# ---- Types --------------------------------------------------------------


class StartRunRequest(BaseModel):
    goal: str
    url: str = ""
    text_model: str | None = None
    vision_model: str | None = None


class RunResponse(BaseModel):
    run_id: str


class ResumeCaptchaRequest(BaseModel):
    resume_token: str


# ---- HTTP ---------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/models")
async def models() -> dict:
    return {
        "models": [
            {
                "id": info.id,
                "provider": info.provider,
                "modalities": list(info.modalities),
                "context": info.context,
            }
            for info in REGISTRY.values()
        ],
        "defaults": defaults(),
    }


def _pick_text_model(explicit: str | None) -> str:
    """Explicit choice > `TEXT_MODEL` env > registry default."""
    if explicit:
        return explicit
    env = os.environ.get("TEXT_MODEL")
    if env and env in REGISTRY:
        return env
    fallback = defaults().get("text")
    if fallback is None:
        raise HTTPException(500, "no text model configured")
    return fallback


@app.post("/runs")
async def create_run(payload: StartRunRequest) -> RunResponse:
    if not payload.goal.strip():
        raise HTTPException(400, "goal is required")
    text_model = _pick_text_model(payload.text_model)
    if text_model not in REGISTRY:
        raise HTTPException(400, f"unknown text_model: {text_model}")
    run = await manager().start(
        goal=payload.goal.strip(),
        url=payload.url.strip(),
        text_model=text_model,
        vision_model=payload.vision_model,
    )
    return RunResponse(run_id=str(run.run_id))


@app.post("/runs/{run_id}/stop")
async def stop_run(run_id: UUID) -> dict:
    await manager().stop(run_id)
    return {"stopped": str(run_id)}


@app.post("/runs/{run_id}/resume-captcha")
async def resume_captcha(run_id: UUID, payload: ResumeCaptchaRequest) -> dict:
    ok = manager().resume_captcha(run_id, payload.resume_token)
    if not ok:
        raise HTTPException(410, "No live captcha pause for this run.")
    return {"resumed": True}


# ---- WebSocket -----------------------------------------------------------


async def _stream(socket: WebSocket, run_id_raw: str, channel: str) -> None:
    try:
        run_id = UUID(run_id_raw)
    except ValueError:
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        subscription = (
            await manager().subscribe_events(run_id)
            if channel == "events"
            else await manager().subscribe_frames(run_id)
        )
    except KeyError:
        await socket.close(code=1008)
        return
    try:
        while True:
            event = await subscription.get()
            await socket.send_json(event.to_wire())
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        manager().unsubscribe(run_id, subscription)


@app.websocket("/ws/events")
async def ws_events(socket: WebSocket) -> None:
    await _stream(socket, socket.query_params.get("run_id", ""), "events")


@app.websocket("/ws/frames")
async def ws_frames(socket: WebSocket) -> None:
    await _stream(socket, socket.query_params.get("run_id", ""), "frames")


# ---- Playbooks -----------------------------------------------------------


@app.get("/playbooks")
async def list_playbooks(host: str | None = None) -> dict:
    entries = await manager().playbook.list_by_host(host)
    return {"entries": [asdict(entry) for entry in entries]}


@app.delete("/playbooks/{entry_id}")
async def delete_playbook(entry_id: int) -> dict:
    await manager().playbook.forget(entry_id)
    return {"forgotten": entry_id}
