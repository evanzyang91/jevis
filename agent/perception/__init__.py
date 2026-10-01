"""Perception: read the page, return a typed Observation."""

from .captcha import CaptchaSignal
from .captcha import detect as detect_captcha
from .dom import observe
from .observation import Element, Observation, Rect, SelectOption
from .vision import VisionCall, VisionError, observe_visually

__all__ = [
    "CaptchaSignal",
    "Element",
    "Observation",
    "Rect",
    "SelectOption",
    "VisionCall",
    "VisionError",
    "detect_captcha",
    "observe",
    "observe_visually",
]
