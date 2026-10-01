"""Vision fallback perception.

Used when DOM perception yields nothing actionable, when a canvas covers most
of the viewport, or after three stales on one page marker. The chosen vision
model returns element boxes; the fallback packages those into the same
`Observation` shape the DOM path produces, so the policy sees one interface.

Coordinates the model returns are in the captured JPEG's pixel space. The
executor consumes them by clicking through CDP at raw x/y — no locator needed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page

from agent.providers import ImageInput, TextAdapter

from .observation import Element, Observation, Rect

VISION_PROMPT = """You look at one screenshot and list every interactive control.
Return a JSON object with one key, elements: an array of objects with:
  - role: one of button, link, textbox, checkbox, radio, tab, select
  - name: the visible label; short, no punctuation past the label itself
  - x, y, w, h: the pixel-space bounding box in the image
  - editable: true only for text inputs the agent should type into
Skip anything not clickable or typable. Return valid JSON only."""


class VisionError(RuntimeError):
    """Vision perception could not produce a usable observation."""


@dataclass(frozen=True, slots=True)
class VisionCall:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


async def observe_visually(*, page: Page, adapter: TextAdapter) -> tuple[Observation, VisionCall]:
    """Capture one JPEG, call the vision model, package the reply as an
    Observation. Retries once if the reply is not valid JSON."""
    screenshot = await page.screenshot(type="jpeg", quality=80, full_page=False)
    try:
        result = await adapter.complete(
            system=VISION_PROMPT,
            user="Describe every interactive control in this screenshot.",
            images=[ImageInput(data=screenshot, media_type="image/jpeg")],
            json_object=True,
        )
        parsed = _parse(result.text)
    except (VisionError, json.JSONDecodeError):
        # One retry. Vision models sometimes emit an apology before the JSON;
        # a fresh call almost always produces clean output.
        result = await adapter.complete(
            system=VISION_PROMPT + "\nReturn only JSON. No prose.",
            user="Describe every interactive control in this screenshot.",
            images=[ImageInput(data=screenshot, media_type="image/jpeg")],
            json_object=True,
        )
        parsed = _parse(result.text)

    viewport = page.viewport_size or {"width": 1280, "height": 800}
    elements: list[Element] = []
    guards: dict[str, str] = {}
    for index, item in enumerate(parsed):
        try:
            rect = Rect(
                x=float(item["x"]),
                y=float(item["y"]),
                w=float(item["w"]),
                h=float(item["h"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        role = str(item.get("role") or "button")
        name = str(item.get("name") or "").strip()
        ref = f"vision:{index}:{int(rect.x)},{int(rect.y)}"
        elements.append(Element(
            ref=ref,
            role=role,
            name=name,
            bounds=rect,
            editable=bool(item.get("editable", False)),
        ))
        guards[ref] = hashlib.sha1(
            f"{role}|{name}|{int(rect.x)}|{int(rect.y)}|{int(rect.w)}|{int(rect.h)}".encode()
        ).hexdigest()

    fingerprint = hashlib.sha256(json.dumps(
        [{"role": e.role, "name": e.name} for e in elements],
        sort_keys=True,
    ).encode()).hexdigest()

    call = VisionCall(
        text=result.text,
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        latency_ms=result.latency_ms,
    )
    observation = Observation(
        url=page.url,
        title=await page.title(),
        text="",
        elements=tuple(elements),
        marker=fingerprint[:16],
        fingerprint=fingerprint,
        guards=guards,
        can_go_back=False,
        can_scroll_up=False,
        can_scroll_down=False,
        viewport=(int(viewport["width"]), int(viewport["height"])),
    )
    return observation, call


def _parse(text: str) -> list[dict[str, Any]]:
    payload = json.loads(text)
    if isinstance(payload, dict) and "elements" in payload:
        elements = payload["elements"]
    elif isinstance(payload, list):
        elements = payload
    else:
        raise VisionError("Vision response missing 'elements' array")
    if not isinstance(elements, list):
        raise VisionError("Vision response 'elements' is not a list")
    return elements
