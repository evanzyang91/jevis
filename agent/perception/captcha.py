"""Detect human-verification and captcha pages.

Purely observational: given a settled `Observation`, decide whether the page
in front of the agent is asking a human to prove they are not a bot. The
supervisor uses this to pause the run and hand control back to the user.

Kept as a small set of URL and text signals rather than model-driven, because
this decision must run every step and must never be its own failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .observation import Observation

_URL_PATH_SIGNALS = (
    "/sorry/",           # Google's bot check
    "/errors/validatecaptcha",  # Amazon
    "/challenge",         # Cloudflare, others
    "/captcha",
    "/hcaptcha",
    "/px-captcha",        # PerimeterX
    "/distil_r_captcha",  # Distil
    "/blocked",           # Walmart's PerimeterX-backed challenge page
)

_TEXT_SIGNALS = (
    # Google
    "unusual traffic from your computer network",
    # Amazon
    "enter the characters you see below",
    "type the characters you see in this image",
    "sorry, we just need to make sure you're not a robot",
    # Generic
    "please verify you are a human",
    "verify you are human",
    "checking your browser",
    "one more step",
    "confirm you're not a robot",
    "confirm you are not a robot",
    "solve this captcha",
    "recaptcha",
    "hcaptcha",
    "cloudflare",
    "access denied",
    "access to this page has been denied",
    # Walmart / PerimeterX press-and-hold
    "we like real shoppers, not robots",
    "press & hold",
    "press and hold",
    "verify your identity",
)


@dataclass(frozen=True, slots=True)
class CaptchaSignal:
    """Non-None when the page looks like a human check. `reason` is a short
    phrase for the UI banner."""

    reason: str
    match: str


def detect(observation: Observation) -> CaptchaSignal | None:
    """Return a signal when the page looks like a bot-check, or None."""
    url_path = (urlparse(observation.url).path or "").lower()
    for signal in _URL_PATH_SIGNALS:
        if signal in url_path:
            return CaptchaSignal(reason="Human verification page detected.", match=signal)

    haystack = f"{observation.title}\n{observation.text}".lower()
    for signal in _TEXT_SIGNALS:
        if signal in haystack:
            return CaptchaSignal(reason="The page asks you to prove you are human.", match=signal)

    return None
