"""Live frame stream from a Chromium page.

Playwright exposes CDP sessions per page, and CDP's `Page.startScreencast` is
what feeds a low-latency JPEG stream. This module wraps that plumbing behind a
small callable so the executor stays thin and tests can drive frames without a
real browser.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Frame:
    data: bytes
    capture_w: int
    capture_h: int
    device_ratio: float
    session_frame_id: int


FrameSink = Callable[[Frame], Awaitable[None]]


def decode(event: dict) -> Frame:
    """Turn a raw `Page.screencastFrame` payload into a typed Frame.

    Kept as a pure function so tests can call it against synthetic events, and
    so failures in decoding never touch the CDP session.
    """
    metadata = event.get("metadata") or {}
    return Frame(
        data=base64.b64decode(event["data"]),
        capture_w=int(metadata.get("deviceWidth") or 0),
        capture_h=int(metadata.get("deviceHeight") or 0),
        device_ratio=float(metadata.get("pageScaleFactor") or 1.0),
        session_frame_id=int(event.get("sessionId") or 0),
    )


DEFAULT_FPS = 30
DEFAULT_QUALITY = 80
DEFAULT_MAX_W = 1280


def start_params(*, fps: int = DEFAULT_FPS, quality: int = DEFAULT_QUALITY, max_w: int = DEFAULT_MAX_W) -> dict:
    """The `Page.startScreencast` params. Emitted as one dict so a test can
    assert on the exact wire shape without importing CDP anywhere."""
    return {
        "format": "jpeg",
        "quality": quality,
        "maxWidth": max_w,
        "everyNthFrame": max(1, round(60 / fps)),
    }
