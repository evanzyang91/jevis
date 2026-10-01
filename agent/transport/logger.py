"""Per-run JSONL logger.

Every non-frame event the bus fans out is also appended to a file so a finished
run can be inspected after the UI has disconnected. Frames are excluded because
their base64 payload would balloon the file — the event stream alone is enough
to replay a run's decisions, actions, and outcomes.

The file is opened lazily on the first event and flushed after each write so a
crashed process still leaves a usable trace on disk.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import IO
from uuid import UUID

from .events import Event, FrameEvent

log = logging.getLogger("agent.transport.logger")

DEFAULT_LOG_DIR = Path.home() / ".cache" / "agent" / "logs"


class FileLogger:
    """One JSONL file per run. Safe to construct even if the directory is
    unwritable; failures are logged once and further writes become no-ops."""

    def __init__(self, run_id: UUID, *, directory: Path | None = None) -> None:
        self._run_id = run_id
        env_dir = os.environ.get("AGENT_LOG_DIR")
        self._dir = directory or (Path(env_dir) if env_dir else DEFAULT_LOG_DIR)
        self._path: Path | None = None
        self._handle: IO[str] | None = None
        self._broken = False

    @property
    def path(self) -> Path | None:
        return self._path

    def write(self, event: Event) -> None:
        if self._broken or isinstance(event, FrameEvent) or event.kind == "frame":
            return
        handle = self._open()
        if handle is None:
            return
        try:
            handle.write(json.dumps(event.to_wire(), separators=(",", ":")) + "\n")
            handle.flush()
        except OSError as err:
            log.warning("run %s: log write failed (%s); disabling file logger", self._run_id, err)
            self._broken = True
            self.close()

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    def _open(self) -> IO[str] | None:
        if self._handle is not None:
            return self._handle
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path = self._dir / f"{self._run_id}.jsonl"
            self._handle = self._path.open("a", encoding="utf-8")
            log.info("run %s: logging events to %s", self._run_id, self._path)
        except OSError as err:
            log.warning("run %s: could not open log file (%s); disabling", self._run_id, err)
            self._broken = True
            return None
        return self._handle
