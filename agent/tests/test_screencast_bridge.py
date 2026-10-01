"""End-to-end screencast wiring: PlaywrightExecutor → Bus → subscriber.

Skips when Chromium is not installed. Confirms that the executor's screencast
callback produces FrameEvent objects on the bus's frame channel.

Chromium's screencast fires on paint. A `data:` page paints once and goes idle
— so this test drives paints from the outside until a frame lands or the
timeout trips. In a real run the pointer moves and pages navigate, so this
priming is not needed.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from uuid import uuid4

import pytest

from agent.executor import Frame, PlaywrightExecutor
from agent.transport import Bus, FrameEvent


def _chromium_available() -> bool:
    root = Path.home() / ".cache" / "ms-playwright"
    if not root.exists():
        return False
    return any(child.name.startswith("chromium") for child in root.iterdir())


pytestmark = pytest.mark.skipif(
    not _chromium_available(),
    reason="Chromium not installed. Run `uv run playwright install chromium`.",
)


@pytest.mark.asyncio
async def test_screencast_frames_land_on_the_bus() -> None:
    bus = Bus()
    run_id = uuid4()
    async with bus.subscription("frames") as frames:
        async with PlaywrightExecutor(headless=True) as executor:
            await executor.navigate("data:text/html,<body><h1>ok</h1></body>")

            async def sink(frame: Frame) -> None:
                bus.publish(FrameEvent(
                    run_id=run_id,
                    seq=await bus.next_seq(),
                    data_b64=base64.b64encode(frame.data).decode(),
                    capture_w=frame.capture_w,
                    capture_h=frame.capture_h,
                    device_ratio=frame.device_ratio,
                ))

            await executor.start_screencast(sink)

            deadline = 5.0
            step = 0.1
            elapsed = 0.0
            first: FrameEvent | None = None
            counter = 0
            while elapsed < deadline:
                try:
                    await executor.page.evaluate(
                        f"document.body.style.marginTop = '{counter}px'"
                    )
                except Exception:  # noqa: BLE001 — page can go away mid-poll
                    break
                counter += 1
                await asyncio.sleep(step)
                elapsed += step
                if not frames.queue.empty():
                    first = await frames.get()
                    break

            await executor.stop_screencast()

    assert first is not None, "screencast produced no frames within the deadline"
    assert first.data_b64
    assert first.capture_w > 0
    assert first.capture_h > 0
