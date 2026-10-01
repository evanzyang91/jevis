"""Round-trip and validation checks for the typed event set."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent.transport import (
    ActionEvent,
    CursorMove,
    DecisionEvent,
    ErrorEvent,
    FrameEvent,
    ObservationEvent,
    event_from_dict,
)


def test_observation_event_round_trips_through_wire() -> None:
    run = uuid4()
    original = ObservationEvent(
        run_id=run,
        seq=1,
        source="dom",
        url="https://example.com/",
        title="Example",
        fingerprint="abc",
        element_count=3,
        text_preview="hello",
    )
    wire = original.to_wire()
    assert wire["kind"] == "observation"
    rebuilt = event_from_dict(wire)
    assert isinstance(rebuilt, ObservationEvent)
    assert rebuilt == original


def test_decision_event_defaults() -> None:
    event = DecisionEvent(
        run_id=uuid4(),
        seq=2,
        operation="CLICK",
        choice="1",
    )
    assert event.target is None
    assert event.probabilities == {}
    assert event.remembered is False


def test_cursor_move_uses_from_alias() -> None:
    event = CursorMove.model_validate(
        {
            "run_id": str(uuid4()),
            "seq": 3,
            "from": {"x": 0, "y": 0},
            "to": {"x": 100, "y": 100},
            "duration_ms": 200,
        }
    )
    wire = event.to_wire()
    # `from` is a Python keyword; the wire form must still use the plain name.
    assert wire["from"] == {"x": 0.0, "y": 0.0}


def test_error_event_marks_recoverable() -> None:
    event = ErrorEvent(
        run_id=uuid4(),
        seq=4,
        layer="executor",
        error_kind="stale_page",
        message="Page changed",
    )
    assert event.recoverable is True


def test_frame_event_is_a_frame() -> None:
    event = FrameEvent(
        run_id=uuid4(),
        seq=5,
        data_b64="",
        capture_w=1280,
        capture_h=720,
    )
    assert event.kind == "frame"


def test_action_event_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        ActionEvent.model_validate(
            {
                "run_id": str(uuid4()),
                "seq": 6,
                "action_kind": "teleport",
                "target_label": "nowhere",
            }
        )


def test_event_from_dict_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        event_from_dict({"kind": "nope", "run_id": str(uuid4()), "seq": 0})
