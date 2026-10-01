"""Async Postgres pool for the server.

One pool per process, created on startup, closed on shutdown. Falls back to
`None` when `DATABASE_URL` is not set — the server still boots and uses the
in-memory playbook, so a developer without Docker can still exercise the UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import asyncpg

from agent.memory import InMemoryPlaybook, PlaybookStore, PostgresPlaybook


@dataclass
class DatabaseHandle:
    """Owns the pool and the playbook store built from it."""

    pool: asyncpg.Pool | None
    playbook: PlaybookStore

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()


async def open_database() -> DatabaseHandle:
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not url:
        return DatabaseHandle(pool=None, playbook=InMemoryPlaybook())
    pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=8)
    return DatabaseHandle(pool=pool, playbook=PostgresPlaybook(pool))
