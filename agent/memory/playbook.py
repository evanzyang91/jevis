"""Cross-run playbook backed by Postgres.

Every step that changed the page records a `(host, path, previous_shape,
next_shape)` row. On future runs at the same situation, the playbook offers
recalled next-shapes only when they have been seen twice and their success
rate is at least 50%.

The store is an interface — callers hand in an `asyncpg.Pool`, or a fake pool
in tests. This keeps `SessionMemory` and `Playbook` symmetric and lets the
supervisor swap either without new plumbing.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from agent.executor import Action
from agent.perception import Observation

from .shape import action_shape, situation

log = logging.getLogger("agent.memory.playbook")


@dataclass(frozen=True, slots=True)
class PlaybookEntry:
    id: int
    host: str
    path: str
    previous: str
    next: str
    seen: int
    succeeded: int
    failed: int

    @property
    def success_rate(self) -> float:
        total = self.succeeded + self.failed
        return 1.0 if total == 0 else self.succeeded / total


class PlaybookStore(Protocol):
    """The operations every playbook implementation supports."""

    async def record(self, host: str, path: str, previous: str, next_shape: str) -> None: ...
    async def recall(self, host: str, path: str, previous: str) -> list[PlaybookEntry]: ...
    async def confirm(self, host: str, path: str, previous: str, next_shape: str, success: bool) -> None: ...
    async def list_by_host(self, host: str | None) -> list[PlaybookEntry]: ...
    async def forget(self, entry_id: int) -> None: ...


class PostgresPlaybook:
    """Real Postgres store. Uses the sqlc-shaped queries in db/queries/playbooks.sql."""

    def __init__(self, pool) -> None:  # noqa: ANN001 — asyncpg.Pool typed loosely to keep import light
        self._pool = pool

    async def record(self, host: str, path: str, previous: str, next_shape: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                insert into playbooks (host, path, previous, next, seen, last_used)
                values ($1, $2, $3, $4, 1, now())
                on conflict (host, path, previous, next)
                do update set seen = playbooks.seen + 1, last_used = now()
                """,
                host, path, previous, next_shape,
            )

    async def recall(self, host: str, path: str, previous: str) -> list[PlaybookEntry]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                select id, host, path, previous, next, seen, succeeded, failed
                from playbooks
                where host = $1 and path = $2 and previous = $3
                  and seen >= 2
                  and (succeeded + failed = 0
                       or succeeded::float / (succeeded + failed) >= 0.5)
                order by last_used desc, succeeded desc
                limit 8
                """,
                host, path, previous,
            )
        return [PlaybookEntry(**dict(row)) for row in rows]

    async def confirm(self, host: str, path: str, previous: str, next_shape: str, success: bool) -> None:
        column = "succeeded" if success else "failed"
        async with self._pool.acquire() as connection:
            await connection.execute(
                f"""
                update playbooks
                set {column} = {column} + 1, last_used = now()
                where host = $1 and path = $2 and previous = $3 and next = $4
                """,
                host, path, previous, next_shape,
            )

    async def list_by_host(self, host: str | None) -> list[PlaybookEntry]:
        async with self._pool.acquire() as connection:
            if host:
                rows = await connection.fetch(
                    """select id, host, path, previous, next, seen, succeeded, failed
                       from playbooks where host = $1 order by last_used desc limit 200""",
                    host,
                )
            else:
                rows = await connection.fetch(
                    """select id, host, path, previous, next, seen, succeeded, failed
                       from playbooks order by last_used desc limit 200"""
                )
        return [PlaybookEntry(**dict(row)) for row in rows]

    async def forget(self, entry_id: int) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute("delete from playbooks where id = $1", entry_id)


class InMemoryPlaybook:
    """Test double. Same interface, no database."""

    def __init__(self) -> None:
        self._rows: list[dict] = []

    async def record(self, host: str, path: str, previous: str, next_shape: str) -> None:
        for row in self._rows:
            same = row["host"] == host and row["path"] == path
            if same and row["previous"] == previous and row["next"] == next_shape:
                row["seen"] += 1
                return
        self._rows.append({
            "id": len(self._rows) + 1,
            "host": host, "path": path, "previous": previous, "next": next_shape,
            "seen": 1, "succeeded": 0, "failed": 0,
        })

    async def recall(self, host: str, path: str, previous: str) -> list[PlaybookEntry]:
        results: list[PlaybookEntry] = []
        for row in self._rows:
            if row["host"] != host or row["path"] != path or row["previous"] != previous:
                continue
            if row["seen"] < 2:
                continue
            total = row["succeeded"] + row["failed"]
            if total > 0 and row["succeeded"] / total < 0.5:
                continue
            results.append(PlaybookEntry(**row))
        results.sort(key=lambda entry: (entry.succeeded, entry.seen), reverse=True)
        return results

    async def confirm(self, host: str, path: str, previous: str, next_shape: str, success: bool) -> None:
        for row in self._rows:
            same = row["host"] == host and row["path"] == path
            if same and row["previous"] == previous and row["next"] == next_shape:
                key = "succeeded" if success else "failed"
                row[key] += 1
                return

    async def list_by_host(self, host: str | None) -> list[PlaybookEntry]:
        rows = [row for row in self._rows if not host or row["host"] == host]
        rows.sort(key=lambda row: row["id"], reverse=True)
        return [PlaybookEntry(**row) for row in rows]

    async def forget(self, entry_id: int) -> None:
        self._rows = [row for row in self._rows if row["id"] != entry_id]


class FilePlaybook(InMemoryPlaybook):
    """The in-memory store, kept in a JSON file so what one run learned is
    there for the next after a server restart. The default without Postgres."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            self._rows = [row for row in rows if isinstance(row, dict) and "next" in row]
        except (OSError, ValueError):
            self._rows = []

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            spare = self._path.with_suffix(".tmp")
            spare.write_text(json.dumps(self._rows), encoding="utf-8")
            os.replace(spare, self._path)
        except OSError as err:
            log.warning("playbook not saved to %s (%s)", self._path, err)

    async def record(self, host: str, path: str, previous: str, next_shape: str) -> None:
        await super().record(host, path, previous, next_shape)
        self._renumber()
        self._save()

    async def confirm(self, host: str, path: str, previous: str, next_shape: str, success: bool) -> None:
        await super().confirm(host, path, previous, next_shape, success)
        self._save()

    async def forget(self, entry_id: int) -> None:
        await super().forget(entry_id)
        self._save()

    def _renumber(self) -> None:
        """Ids stay unique after a forget (the base class numbers by count)."""
        seen: set[int] = set()
        top = max((row["id"] for row in self._rows), default=0)
        for row in self._rows:
            if row["id"] in seen:
                top += 1
                row["id"] = top
            seen.add(row["id"])


def _host_from(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host[:200].lower()


async def record_outcome(
    store: PlaybookStore,
    *,
    url: str,
    previous: Action | None,
    action: Action,
) -> None:
    """Convenience: record one executed move as playbook input."""
    host = _host_from(url)
    path, previous_shape = situation(url, previous)
    await store.record(host, path, previous_shape, action_shape(action))


async def suggested_action(
    store: PlaybookStore,
    *,
    observation: Observation,
    previous: Action | None,
) -> Action | None:
    """Return one recalled action for the current page, if the playbook has
    exactly one plausible match on the observed elements."""
    host = _host_from(observation.url)
    path, previous_shape = situation(observation.url, previous)
    candidates = await store.recall(host, path, previous_shape)
    if not candidates:
        return None
    from agent.policy.action_space import build

    space = build(observation)
    for candidate in candidates:
        matches: list[Action] = []
        for operation in space.operations:
            for target in operation.targets:
                if action_shape(target.action) == candidate.next:
                    matches.append(target.action)
        if len(matches) == 1:
            return matches[0]
    return None
