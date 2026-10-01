"""Entry point. `uv run agent` starts the FastAPI server on port 8787."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


def _find_env() -> Path | None:
    """Walk up from the current directory and this module's location looking
    for a `.env` file. Repos are launched from many places (repo root, an
    editor working dir, a subshell), so probing both roots covers the common
    cases without asking the user to remember."""
    seen: set[Path] = set()
    starts = [Path.cwd(), Path(__file__).resolve().parent]
    for start in starts:
        for candidate in [start, *start.parents]:
            if candidate in seen:
                break
            seen.add(candidate)
            path = candidate / ".env"
            if path.is_file():
                return path
    return None


def load_dotenv() -> None:
    """Read `.env` into `os.environ`, overwriting empty values.

    Only values that are missing OR empty get replaced, so a real
    shell-exported override always wins over `.env`.
    """
    path = _find_env()
    if path is None:
        return
    loaded: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        current = os.environ.get(key)
        if not current:  # unset or empty
            os.environ[key] = value
            loaded.append(key)
    if loaded:
        print(f"agent: loaded {len(loaded)} vars from {path}", file=sys.stderr)


def main() -> None:
    load_dotenv()
    port = int(os.environ.get("AGENT_PORT", "8787"))
    host = os.environ.get("AGENT_HOST", "127.0.0.1")
    reload = os.environ.get("AGENT_RELOAD", "").strip().lower() in {"1", "true", "yes"}
    reload_kwargs: dict = {}
    if reload:
        reload_kwargs = {"reload": True, "reload_dirs": ["agent"]}
    uvicorn.run("agent.server:app", host=host, port=port, log_level="warning", **reload_kwargs)


if __name__ == "__main__":
    main()
