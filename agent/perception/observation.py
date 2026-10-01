"""Typed shape of one page observation.

Perception's job is to hand every other layer the same picture: what elements
are actionable, where they are, and what changed since last time. The policy
never sees a raw DOM node; the executor never sees a raw selector string it
generated. Everything travels as these dataclasses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

Role = Literal[
    "button",
    "link",
    "checkbox",
    "radio",
    "switch",
    "tab",
    "menuitem",
    "menuitemradio",
    "option",
    "gridcell",
    "combobox",
    "textbox",
    "searchbox",
    "spinbutton",
    "select",
]

Section = Literal["header", "nav", "main", "aside", "footer", "dialog", "form", "search"]


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True, slots=True)
class SelectOption:
    label: str
    value: str
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class Element:
    """One interactive control on the page.

    `ref` is a Playwright-consumable selector. Perception owns its shape so no
    other layer synthesises selectors from labels or roles.
    """

    ref: str
    role: str
    name: str
    bounds: Rect
    editable: bool = False
    value: str | None = None
    checked: bool | None = None
    selected: bool | None = None
    expanded: bool | None = None
    section: str | None = None
    context: str | None = None
    # aria-haspopup value ("menu", "listbox", "dialog"). Non-None means
    # clicking this element opens a submenu rather than navigating.
    opens: str | None = None
    # Rendered on-screen text when it differs from the accessible name. Sites
    # use `aria-hidden="true"` as a visual-styling toggle (highlighted-
    # completion spans, decorative icons), so the ARIA-correct accessible
    # name drops text a sighted user does see. When a mismatch exists it is
    # usually itself the signal — an autocomplete row named "mouse" whose
    # visible text is "mouse pad" is one to flag.
    visible: str | None = None
    options: tuple[SelectOption, ...] = ()


@dataclass(frozen=True, slots=True)
class Observation:
    """A settled snapshot of the page.

    `marker` is the cheap change detector (fast comparison); `fingerprint` is
    the full-content hash for cross-run comparisons and playbook keys. The
    supervisor uses `marker` in the hot loop and `fingerprint` for storage.
    """

    url: str
    title: str
    text: str
    elements: tuple[Element, ...]
    marker: str
    fingerprint: str
    guards: Mapping[str, str]
    can_go_back: bool
    can_scroll_up: bool
    can_scroll_down: bool
    viewport: tuple[int, int]
    # True when the page reports readyState != "complete" or shows an aria-busy
    # progress indicator. The policy sees this and can choose Wait deliberately.
    loading: bool = False
    # Vertical scroll position at read time. Diagnostic — a value > 0 on a page
    # that just navigated is the signal that the previous page's scroll
    # persisted (Walmart SPA behaviour) and we may have missed elements above.
    scroll_y: int = 0
    # What a scroll moves: "page", an open modal "dialog", or an app "panel"
    # when the page itself cannot scroll. `can_scroll_*` describe this area.
    scroll_area: str = "page"
    # Viewport point at the centre of a dialog or panel scroller, where a
    # wheel must land to move it. None for the page.
    scroll_point: tuple[int, int] | None = None
    # Pixels per scroll for that dialog or panel: most of its height, so no
    # row is skipped. None keeps the page step.
    scroll_step: int | None = None
    # The open modal dialog's own text. None when no modal is open.
    dialog_text: str | None = None
    # How many times `observe` re-read the page waiting for hydration.
    # Diagnostic only; 0 for a page that read cleanly on the first try.
    hydration_retries: int = 0
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def by_ref(self, ref: str) -> Element | None:
        for element in self.elements:
            if element.ref == ref:
                return element
        return None
