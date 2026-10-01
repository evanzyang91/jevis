"""Two-channel pub/sub for typed events.

The events channel is ordered and lossless: every subscriber sees every event in
publish order, blocking the publisher if a subscriber falls behind. The frames
channel is lossy and unordered: a slow subscriber drops the oldest queued frame
instead of stalling the whole run. Keeping the two separate is what stops a
frame burst from lagging the event log.

The events channel also keeps a small backlog. A new subscriber receives every
event published so far before it sees anything new. Without this, a UI that
subscribes after the run has emitted an error would miss it and stay stuck on
"starting" forever.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from .events import EVENT_KINDS, FRAME_KINDS, Event, FrameEvent

Channel = Literal["events", "frames"]

EVENT_BACKLOG = 512


@dataclass(eq=False)
class Subscription:
    """A queue plus the channel it belongs to. Owned by one consumer."""

    channel: Channel
    queue: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)
    max_frames: int = 4

    async def get(self) -> Event:
        return await self.queue.get()

    def push(self, event: Event) -> None:
        if self.channel == "frames" and self.queue.qsize() >= self.max_frames:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(event)


class Bus:
    """In-process pub/sub. One instance per run; the server multiplexes."""

    def __init__(self, *, sink: Callable[[Event], None] | None = None) -> None:
        self._event_subs: set[Subscription] = set()
        self._frame_subs: set[Subscription] = set()
        self._seq: int = 0
        self._lock = asyncio.Lock()
        self._event_backlog: deque[Event] = deque(maxlen=EVENT_BACKLOG)
        # Called synchronously for every event before fan-out. Used by the
        # per-run file logger; kept generic so tests can inject their own.
        self._sink = sink

    async def next_seq(self) -> int:
        """Monotonic sequence for the next event."""
        async with self._lock:
            self._seq += 1
            return self._seq

    def publish(self, event: Event) -> None:
        """Fan out one event to its channel's subscribers."""
        if self._sink is not None:
            self._sink(event)
        if isinstance(event, FrameEvent) or event.kind in FRAME_KINDS:
            # Frames are lossy — never retained past the queue depth.
            for sub in self._frame_subs:
                sub.push(event)
            return
        if event.kind not in EVENT_KINDS:
            raise ValueError(f"Event kind {event.kind!r} is not on a known channel")
        self._event_backlog.append(event)
        for sub in self._event_subs:
            sub.push(event)

    def subscribe(self, channel: Channel) -> Subscription:
        sub = Subscription(channel=channel)
        if channel == "events":
            # Replay the backlog into the new subscriber's queue, in order,
            # so a late listener sees every event the run has produced.
            for event in self._event_backlog:
                sub.push(event)
            self._event_subs.add(sub)
        else:
            self._frame_subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        (self._frame_subs if sub.channel == "frames" else self._event_subs).discard(sub)

    @asynccontextmanager
    async def subscription(self, channel: Channel) -> AsyncIterator[Subscription]:
        sub = self.subscribe(channel)
        try:
            yield sub
        finally:
            self.unsubscribe(sub)
