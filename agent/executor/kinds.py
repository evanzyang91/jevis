"""Value types for one execution step.

The executor stays swappable — real Chromium now, a mock in tests, a remote
worker later — so its inputs and outputs live in one small module that any
implementation can depend on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ActionKind = Literal["click", "fill", "select", "scroll", "wait", "back", "enter"]

# Labels of controls whose only job is to close a dialog. The executor and the
# policy share this rule. A bare "x" or "×" closes; "X-Large" is an option.
CLOSE_LABEL = re.compile(r"^\s*(?:(?:close|dismiss|cancel|no,? thanks|not now)\b|×|x\s*$)", re.I)


@dataclass(frozen=True, slots=True)
class Action:
    """One thing to do next. Element-scoped kinds carry a locator; page-level
    kinds (scroll, wait, back, enter) carry only their own parameters.

    The locator is a serialised Playwright selector — never a raw CSS or XPath
    string synthesised by a model. Perception owns selector generation. Role is
    kept alongside so the executor can fall back to an accessible-name lookup
    when the primary stamp gets replaced by a client-side re-render.
    """

    id: str
    kind: ActionKind
    label: str
    locator: str | None = None
    role: str | None = None
    value: str | None = None
    delta: int = 0
    # Scroll only: the viewport point the wheel lands on, so it moves an open
    # dialog's or panel's own scroller. None keeps the page-level default.
    point: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class Outcome:
    """What happened. The supervisor decides what to do about it; the
    executor only reports."""

    page_changed: bool
    url_changed: bool
    load_ms: int
    final_url: str


class StalePage(RuntimeError):
    """The observation the caller acted on is no longer current."""


class Occluded(StalePage):
    """The target is present but covered by another element."""


class NavigationInterrupted(RuntimeError):
    """A CDP call raced a document navigation and could not complete."""
