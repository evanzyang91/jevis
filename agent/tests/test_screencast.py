"""Screencast frame decoding and start params."""

from __future__ import annotations

import base64

from agent.executor.screencast import DEFAULT_FPS, decode, start_params


def test_decode_reads_base64_and_metadata() -> None:
    frame = decode(
        {
            "data": base64.b64encode(b"pixels").decode(),
            "metadata": {
                "deviceWidth": 1280,
                "deviceHeight": 720,
                "pageScaleFactor": 2.0,
            },
            "sessionId": 42,
        }
    )
    assert frame.data == b"pixels"
    assert frame.capture_w == 1280
    assert frame.capture_h == 720
    assert frame.device_ratio == 2.0
    assert frame.session_frame_id == 42


def test_decode_tolerates_missing_metadata() -> None:
    frame = decode({"data": base64.b64encode(b"").decode(), "sessionId": 0})
    assert frame.capture_w == 0
    assert frame.capture_h == 0
    assert frame.device_ratio == 1.0


def test_start_params_are_shaped_for_page_start_screencast() -> None:
    params = start_params()
    assert params["format"] == "jpeg"
    assert params["quality"] > 0
    assert params["maxWidth"] > 0
    # everyNthFrame is derived from FPS; at 30 FPS on a 60Hz compositor we
    # expect every second frame.
    assert params["everyNthFrame"] == max(1, round(60 / DEFAULT_FPS))
