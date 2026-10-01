"""Captcha detection is a small set of URL and text signals."""

from __future__ import annotations

from datetime import datetime, timezone

from agent.perception import Observation, detect_captcha


def _obs(*, url: str = "https://example.com/", title: str = "", text: str = "") -> Observation:
    return Observation(
        url=url,
        title=title,
        text=text,
        elements=(),
        marker="m",
        fingerprint="fp",
        guards={},
        can_go_back=False,
        can_scroll_up=False,
        can_scroll_down=False,
        viewport=(1280, 800),
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_google_sorry_url_is_detected() -> None:
    signal = detect_captcha(_obs(url="https://www.google.com/sorry/index"))
    assert signal is not None
    assert "/sorry/" == signal.match


def test_unusual_traffic_wording_is_detected() -> None:
    signal = detect_captcha(_obs(
        url="https://www.google.com/somewhere",
        text="Our systems have detected unusual traffic from your computer network.",
    ))
    assert signal is not None


def test_normal_page_returns_none() -> None:
    assert detect_captcha(_obs(url="https://en.wikipedia.org/wiki/Toronto", text="Toronto is a city")) is None


def test_recaptcha_text_signal() -> None:
    signal = detect_captcha(_obs(text="Please solve this reCAPTCHA to continue"))
    assert signal is not None


def test_cloudflare_challenge_text() -> None:
    signal = detect_captcha(_obs(text="Checking your browser before accessing"))
    assert signal is not None
