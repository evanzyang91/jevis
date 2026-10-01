"""FakeExecutor: contract parity without a real browser."""

from __future__ import annotations

import pytest

from agent.executor import Action, FakeExecutor, Frame


@pytest.mark.asyncio
async def test_navigate_updates_current_url() -> None:
    fake = FakeExecutor()
    outcome = await fake.navigate("https://example.com/")
    assert outcome.final_url == "https://example.com/"
    assert await fake.current_url() == "https://example.com/"
    assert fake.calls[0] == ("navigate", "https://example.com/")


@pytest.mark.asyncio
async def test_act_records_the_action_verbatim() -> None:
    fake = FakeExecutor()
    action = Action(id="a1", kind="click", label="Submit", locator="button")
    outcome = await fake.act(action)
    assert outcome.page_changed is True
    kind, recorded = fake.calls[0]
    assert kind == "act"
    assert recorded is action


@pytest.mark.asyncio
async def test_frames_reach_the_registered_sink() -> None:
    fake = FakeExecutor()
    received: list[Frame] = []

    async def sink(frame: Frame) -> None:
        received.append(frame)

    await fake.start_screencast(sink)
    frame = Frame(data=b"x", capture_w=10, capture_h=10, device_ratio=1.0, session_frame_id=1)
    await fake.emit_frame(frame)
    assert received == [frame]
    await fake.stop_screencast()
    await fake.emit_frame(frame)  # no sink registered → dropped silently
    assert received == [frame]
