"""One shape for every message the agent layers speak.

Every event carries a run id and a monotonic sequence number. The dev UI replays a
run from the ordered event stream alone, so a layer that mutates state without
emitting an event makes that mutation invisible to replay. Emit first, mutate second.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _EventBase(BaseModel):
    """Shared frontmatter for every event.

    `kind` is the discriminator the TS side reads. Subclasses set it as a class
    variable so it is enforced on construction and mirrored to JSON verbatim.
    """

    # Fields declared with an alias (`from` on CursorMove) must still be
    # constructable by their Python name. Wire form always uses the alias.
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    kind: ClassVar[str] = "base"
    run_id: UUID
    seq: int = Field(ge=0)
    ts: datetime = Field(default_factory=_now)

    def to_wire(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", by_alias=True)
        payload["kind"] = self.kind
        return payload


class Rect(BaseModel):
    model_config = ConfigDict(frozen=True)
    x: float
    y: float
    w: float
    h: float


class Point(BaseModel):
    model_config = ConfigDict(frozen=True)
    x: float
    y: float


# ---- Perception -------------------------------------------------------------


class ObservationEvent(_EventBase):
    kind: ClassVar[Literal["observation"]] = "observation"
    source: Literal["dom", "vision"]
    url: str
    title: str = ""
    fingerprint: str
    element_count: int = 0
    text_preview: str = ""
    # Diagnostic — the marker is what the cheap change-detector compares. A
    # marker unchanged across a click is what triggers effect polling.
    marker: str = ""
    # Vertical scroll offset at read time. > 0 on a fresh navigation means
    # the previous page's scroll persisted and we may have missed content
    # above the fold.
    scroll_y: int = 0
    # "page", "dialog", or "panel": what a scroll on this observation moves.
    scroll_area: str = "page"
    # How many times observe() re-read waiting for hydration. > 0 means the
    # page was shell-only on the first try; the actual delivered observation
    # is the settled one.
    hydration_retries: int = 0
    # Advisory loading state (readyState != complete OR aria-busy visible).
    loading: bool = False
    viewport_w: int = 0
    viewport_h: int = 0
    # Per-element digest: `[idx, role, label]` triples for every enumerated
    # element. Post-hoc analysis of "why did the model pick X" reads this to
    # confirm whether the expected target was even offered. Kept as small
    # lists so a 50-element page is ~2KB of log — cheap.
    elements: list[list[Any]] = Field(default_factory=list)


# ---- Policy -----------------------------------------------------------------


class DecisionEvent(_EventBase):
    kind: ClassVar[Literal["decision"]] = "decision"
    operation: str
    choice: str
    target: str | None = None
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float = 0.0
    latency_ms: int = 0
    model: str = ""
    remembered: bool = False
    # What we hid from the model. Diagnostic — a re-decision that ignores a
    # ban tells us the ban was empty; a ban containing the label the model
    # then picked (impossible) would flag a wiring bug.
    banned: list[str] = Field(default_factory=list)
    # Labels that survived the ban and reached the model, grouped by operation
    # id. Answers "was the expected action even offered?" after the fact — a
    # low-confidence pick that skipped Add-to-cart is diagnosable only if the
    # log shows whether Add-to-cart was in the choice set at all.
    offered: dict[str, list[str]] = Field(default_factory=dict)
    # An open modal's diagnosis ("task" or "interruption") and its probability.
    # Empty when no modal was open or the diagnosis was not confident.
    dialog: str = ""
    dialog_p: float = 0.0
    # Confidence with one item's routes counted together ("X" + "Add to cart - X").
    signal: float = 1.0
    # The step check when the policy was unsure: "cleared", "failed", or empty
    # when it did not run. `check_switch` is a better-scoring candidate, if any.
    check: str = ""
    check_p: float = 0.0
    check_switch: str = ""
    # True where an LLM would be asked to reinstruct (two failed checks in a row,
    # or a failed DONE or BLOCKED). Log-only: the action is unchanged.
    escalate: bool = False


# ---- Execution --------------------------------------------------------------


class ActionEvent(_EventBase):
    kind: ClassVar[Literal["action"]] = "action"
    action_kind: Literal["click", "fill", "select", "scroll", "wait", "back", "enter"]
    target_label: str
    node_id: int | None = None
    text: str | None = None
    secret: bool = False


class OutcomeEvent(_EventBase):
    kind: ClassVar[Literal["outcome"]] = "outcome"
    page_changed: bool
    url_changed: bool
    guard_held: bool
    load_ms: int = 0


# ---- Cursor overlay (rendered client-side, mirrors real CDP inputs) ---------


class CursorMove(_EventBase):
    kind: ClassVar[Literal["cursor_move"]] = "cursor_move"
    from_: Point = Field(alias="from")
    to: Point
    duration_ms: int
    intent: Literal["hover", "click", "scroll"] = "hover"


class CursorClick(_EventBase):
    kind: ClassVar[Literal["cursor_click"]] = "cursor_click"
    x: float
    y: float


class CursorScroll(_EventBase):
    kind: ClassVar[Literal["cursor_scroll"]] = "cursor_scroll"
    x: float
    y: float
    delta_y: float


class Keystroke(_EventBase):
    kind: ClassVar[Literal["keystroke"]] = "keystroke"
    text: str
    secret: bool = False


class FocusPulse(_EventBase):
    kind: ClassVar[Literal["focus_pulse"]] = "focus_pulse"
    rect: Rect


# ---- Frame stream (separate socket; may drop under backpressure) ------------


class FrameEvent(_EventBase):
    kind: ClassVar[Literal["frame"]] = "frame"
    data_b64: str
    capture_w: int
    capture_h: int
    device_ratio: float = 1.0


# ---- Planning + budget ------------------------------------------------------


class PlanEvent(_EventBase):
    kind: ClassVar[Literal["plan"]] = "plan"
    plan: list[str]
    active_index: int
    original_goal: str = ""
    refined_goal: str = ""
    start_url: str = ""


class BudgetEvent(_EventBase):
    kind: ClassVar[Literal["budget"]] = "budget"
    steps: int
    model_ms: int
    load_ms: int
    usd: float


class CaptchaEvent(_EventBase):
    kind: ClassVar[Literal["captcha"]] = "captcha"
    reason: str
    resume_token: str


class ErrorEvent(_EventBase):
    kind: ClassVar[Literal["error"]] = "error"
    layer: str
    error_kind: str
    message: str
    recoverable: bool = True


class StatusEvent(_EventBase):
    """The run entered a new lifecycle state. Emitted once per transition —
    the UI's status line and phase switching both read it."""

    kind: ClassVar[Literal["status"]] = "status"
    status: Literal["running", "paused", "done", "blocked", "error"]
    reason: str = ""


# ---- Registry ---------------------------------------------------------------


Event = (
    ObservationEvent
    | DecisionEvent
    | ActionEvent
    | OutcomeEvent
    | CursorMove
    | CursorClick
    | CursorScroll
    | Keystroke
    | FocusPulse
    | FrameEvent
    | PlanEvent
    | BudgetEvent
    | CaptchaEvent
    | ErrorEvent
    | StatusEvent
)


_KIND_TO_CLASS: dict[str, type[_EventBase]] = {
    cls.kind: cls
    for cls in (
        ObservationEvent,
        DecisionEvent,
        ActionEvent,
        OutcomeEvent,
        CursorMove,
        CursorClick,
        CursorScroll,
        Keystroke,
        FocusPulse,
        FrameEvent,
        PlanEvent,
        BudgetEvent,
        StatusEvent,
        CaptchaEvent,
        ErrorEvent,
    )
}


def event_from_dict(payload: dict[str, Any]) -> Event:
    """Rebuild the typed event a wire payload names. Unknown kinds raise."""
    kind = payload.get("kind")
    cls = _KIND_TO_CLASS.get(kind or "")
    if cls is None:
        raise ValueError(f"Unknown event kind: {kind!r}")
    without_kind = {k: v for k, v in payload.items() if k != "kind"}
    return cls.model_validate(without_kind)  # type: ignore[return-value]


FRAME_KINDS: frozenset[str] = frozenset({"frame"})
EVENT_KINDS: frozenset[str] = frozenset(_KIND_TO_CLASS) - FRAME_KINDS
