"""Bus behaviour: ordered event delivery, lossy frame delivery, unsubscribe hygiene."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from agent.transport import (
    ActionEvent,
    Bus,
    FrameEvent,
    ObservationEvent,
)


async def _observation(run, seq: int) -> ObservationEvent:
    return ObservationEvent(
        run_id=run,
        seq=seq,
        source="dom",
        url="https://x/",
        fingerprint="f",
    )


@pytest.mark.asyncio
async def test_event_subscribers_see_every_event_in_order() -> None:
    bus = Bus()
    run = uuid4()
    async with bus.subscription("events") as sub:
        for i in range(3):
            bus.publish(await _observation(run, i + 1))
        seqs = [(await sub.get()).seq for _ in range(3)]
    assert seqs == [1, 2, 3]


@pytest.mark.asyncio
async def test_frame_channel_drops_oldest_under_backpressure() -> None:
    bus = Bus()
    run = uuid4()
    async with bus.subscription("frames") as sub:
        # Push more frames than the queue can hold. Oldest should drop.
        for i in range(sub.max_frames + 3):
            bus.publish(
                FrameEvent(
                    run_id=run,
                    seq=i + 1,
                    data_b64="",
                    capture_w=10,
                    capture_h=10,
                )
            )
        drained = []
        while not sub.queue.empty():
            drained.append((await sub.get()).seq)
    assert len(drained) == sub.max_frames
    # The tail always survives; the head is what gets dropped first.
    assert drained[-1] == sub.max_frames + 3


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    bus = Bus()
    run = uuid4()
    sub = bus.subscribe("events")
    bus.publish(await _observation(run, 1))
    assert (await sub.get()).seq == 1
    bus.unsubscribe(sub)
    bus.publish(await _observation(run, 2))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.get(), timeout=0.05)


@pytest.mark.asyncio
async def test_next_seq_is_monotonic_under_concurrency() -> None:
    bus = Bus()
    seqs = await asyncio.gather(*(bus.next_seq() for _ in range(50)))
    assert seqs == sorted(seqs)
    assert seqs[0] == 1
    assert seqs[-1] == 50


@pytest.mark.asyncio
async def test_late_subscriber_receives_backlog_of_events() -> None:
    """A subscriber that arrives after the run started must still see prior
    events. Otherwise a UI that connects a beat late misses the error that
    ended the run and shows a permanent 'starting' state."""
    bus = Bus()
    run = uuid4()
    for i in range(3):
        bus.publish(await _observation(run, i + 1))
    async with bus.subscription("events") as sub:
        replayed = [(await sub.get()).seq for _ in range(3)]
        bus.publish(await _observation(run, 4))
        replayed.append((await sub.get()).seq)
    assert replayed == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_events_and_frames_land_on_separate_channels() -> None:
    bus = Bus()
    run = uuid4()
    async with (
        bus.subscription("events") as events,
        bus.subscription("frames") as frames,
    ):
        bus.publish(
            ActionEvent(
                run_id=run,
                seq=1,
                action_kind="click",
                target_label="Submit",
            )
        )
        bus.publish(
            FrameEvent(
                run_id=run,
                seq=2,
                data_b64="",
                capture_w=10,
                capture_h=10,
            )
        )
        assert (await events.get()).kind == "action"
        assert (await frames.get()).kind == "frame"
        assert events.queue.empty()
        assert frames.queue.empty()
