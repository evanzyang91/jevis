"""What earlier runs settled, kept for the next one: plans and products.

- A plan, by the goal's words and the start URL the person gave. Kept only
  after a run finished every step, and dropped when a run on it fails, so a
  repeated errand skips the planner (and the site suggestion) altogether.
- A product, by site and search term: the one an add step finished with.
  The next run on that site adds it straight from the results when the page
  offers its add control, with no model asked.
- A site's search results URL ("https://www.walmart.ca/en/search?q={q}"),
  read off a search the agent typed and submitted. A later step opens its
  results directly instead of typing and pressing Search.

One JSON file (`AGENT_MEMORY_DIR`, default ~/.cache/agent/memory). Moves
between pages live in the playbook store beside it (see playbook.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit, urlunsplit

log = logging.getLogger("agent.memory.recall")

DEFAULT_DIR = Path.home() / ".cache" / "agent" / "memory"


def memory_dir() -> Path | None:
    """Where memory files live, or None when `AGENT_MEMORY_DIR=off`."""
    raw = os.environ.get("AGENT_MEMORY_DIR", "").strip()
    if raw.lower() in {"off", "0", "false", "none"}:
        return None
    return Path(raw).expanduser() if raw else DEFAULT_DIR


def goal_key(goal: str, url: str = "") -> str:
    """Goals that differ only in case, spacing or end punctuation are one errand."""
    words = re.sub(r"\s+", " ", goal.strip().lower()).rstrip(".!?")
    return f"{words}|{url.strip().lower()}"


def search_template(url: str, term: str) -> str | None:
    """"https://www.amazon.ca/s?k=wireless+mouse&crid=..." with term "wireless
    mouse" -> "https://www.amazon.ca/s?k={q}": the path and the one query
    parameter that holds exactly the term. None when no parameter does."""
    parts = urlsplit(url)
    wanted = _term_key(term)
    if parts.scheme not in {"http", "https"} or not wanted:
        return None
    for name, value in parse_qsl(parts.query, keep_blank_values=False):
        if _term_key(value) == wanted:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, f"{quote_plus(name)}={{q}}", ""))
    return None


def _term_key(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip().lower())


class Recall:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._data: dict[str, Any] = {"plans": {}, "products": {}, "searches": {}}
        if path is None:
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data["plans"] = dict(loaded.get("plans") or {})
                self._data["products"] = dict(loaded.get("products") or {})
                self._data["searches"] = dict(loaded.get("searches") or {})
        except (OSError, ValueError):
            pass

    @classmethod
    def default(cls) -> Recall:
        directory = memory_dir()
        return cls(directory / "recall.json" if directory is not None else None)

    # ---- Plans ---------------------------------------------------------------

    def plan(self, goal: str, url: str = "") -> tuple[str, str] | None:
        """(planner JSON, start URL) of a plan that finished every step before."""
        entry = self._data["plans"].get(goal_key(goal, url))
        if not isinstance(entry, dict) or not entry.get("text"):
            return None
        return str(entry["text"]), str(entry.get("start_url") or "")

    def keep_plan(self, goal: str, url: str, text: str, start_url: str) -> None:
        key = goal_key(goal, url)
        old = self._data["plans"].get(key) or {}
        self._data["plans"][key] = {"text": text, "start_url": start_url,
                                    "runs": int(old.get("runs", 0)) + 1, "last": time.time()}
        self._save()

    def drop_plan(self, goal: str, url: str = "") -> None:
        if self._data["plans"].pop(goal_key(goal, url), None) is not None:
            self._save()

    # ---- Products ------------------------------------------------------------

    def product(self, host: str, term: str) -> str | None:
        """The product an add for `term` on `host` finished with, while it has
        landed more often than it failed."""
        entry = self._data["products"].get(host, {}).get(_term_key(term))
        if not isinstance(entry, dict) or int(entry.get("ok", 0)) <= int(entry.get("bad", 0)):
            return None
        return str(entry.get("product") or "") or None

    def keep_product(self, host: str, term: str, product: str, *, landed: bool = True) -> None:
        if not host or not term or not product:
            return
        site = self._data["products"].setdefault(host, {})
        key = _term_key(term)
        entry = site.get(key)
        if not landed and (not isinstance(entry, dict) or entry.get("product") != product):
            return  # a failed add of a product this term never kept
        if not isinstance(entry, dict) or (landed and entry.get("product") != product):
            entry = {"product": product, "ok": 0, "bad": 0}
        entry["ok" if landed else "bad"] = int(entry.get("ok" if landed else "bad", 0)) + 1
        entry["last"] = time.time()
        site[key] = entry
        self._save()

    # ---- Search URLs -----------------------------------------------------------

    def search_url(self, host: str, term: str) -> str | None:
        """The results URL for `term` on `host`, while its template has opened
        results more often than it failed."""
        entry = self._data["searches"].get(host)
        if not isinstance(entry, dict) or int(entry.get("ok", 0)) <= int(entry.get("bad", 0)):
            return None
        template = str(entry.get("template") or "")
        return template.replace("{q}", quote_plus(term.strip())) if "{q}" in template else None

    def keep_search(self, host: str, url: str, term: str) -> None:
        """Learn the template from a results URL whose query holds `term`."""
        template = search_template(url, term)
        if not host or template is None:
            return
        entry = self._data["searches"].get(host)
        if not isinstance(entry, dict) or entry.get("template") != template:
            entry = {"template": template, "ok": 0, "bad": 0}
        entry["ok"] = int(entry.get("ok", 0)) + 1
        entry["last"] = time.time()
        self._data["searches"][host] = entry
        self._save()

    def search_failed(self, host: str) -> None:
        entry = self._data["searches"].get(host)
        if isinstance(entry, dict):
            entry["bad"] = int(entry.get("bad", 0)) + 1
            self._save()

    # ---- File ----------------------------------------------------------------

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            spare = self._path.with_suffix(".tmp")
            spare.write_text(json.dumps(self._data), encoding="utf-8")
            os.replace(spare, self._path)
        except OSError as err:
            log.warning("memory not saved to %s (%s)", self._path, err)
