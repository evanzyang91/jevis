"""Text helper: turn the current field into a typed value.

Runs on any text adapter. The value is JSON-parsed and validated before it
reaches the executor; a partial or non-string reply raises `NoFieldValue` so
the supervisor drops that target and re-decides. A hard timeout wraps the
adapter call so a hung provider never freezes the run — a timeout is treated
as a decline, exactly like `{"text": null}`, so the supervisor moves on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from agent.providers import TextAdapter

from .prompts import TEXT_VALUE

log = logging.getLogger("agent.policy.text_helper")

HELPER_TIMEOUT_S = 20.0


class NoFieldValue(RuntimeError):
    """The helper declined to supply a value for this field."""


@dataclass(frozen=True, slots=True)
class TextValue:
    text: str
    model: str
    usage: dict[str, int]
    latency_ms: int


async def field_value(
    *,
    adapter: TextAdapter,
    goal: str,
    field_name: str,
    field_role: str,
    current_value: str | None,
    page_text: str,
    history: list[dict[str, Any]],
) -> TextValue:
    """Return one value for one field.

    Timeouts convert to `NoFieldValue`. That lets the supervisor drop the
    target and let the policy re-decide (typically toward Enter or a Search
    button) instead of stalling on a slow provider.
    """
    context = {
        "goal": goal,
        "field": {"label": field_name, "role": field_role, "current": current_value},
        "page": page_text[:2000],
        "recent_actions": history[-24:],
    }
    try:
        result = await asyncio.wait_for(
            adapter.complete(system=TEXT_VALUE, user=json.dumps(context), json_object=True),
            timeout=HELPER_TIMEOUT_S,
        )
    except asyncio.TimeoutError as err:
        log.warning("text helper timed out after %ss for field %r", HELPER_TIMEOUT_S, field_name)
        raise NoFieldValue(f"Text helper timed out after {HELPER_TIMEOUT_S}s") from err
    try:
        parsed = json.loads(result.text)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Text helper returned invalid JSON: {result.text[:200]}") from err
    if set(parsed) != {"text"}:
        raise RuntimeError(f"Text helper response has wrong shape: {parsed}")
    value = parsed["text"]
    if value is None:
        raise NoFieldValue("Helper declined to supply a value")
    # Treat empty / non-string / oversize as a decline rather than a hard
    # failure. A bad value from the helper should drop the target and let
    # the policy re-decide, not crash the whole run — same shape as an
    # explicit `{"text": null}`.
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise NoFieldValue(f"Helper returned an invalid value: {value!r}")
    return TextValue(
        text=value,
        model=result.model,
        usage={"prompt_tokens": result.usage.prompt_tokens, "completion_tokens": result.usage.completion_tokens},
        latency_ms=result.latency_ms,
    )
