"""Done verification.

The policy answers "is the goal met?" in a constrained-choice head that only
sees the current page. It can fire DONE while the URL still holds a search
query or a filter, and the loop cannot catch that on its own. This verifier is
a second LLM call — separate model context, evidence-driven — that reads the
goal, the URL, the page text, and the action history, then returns a met/gap
judgement. The supervisor calls it whenever the policy chooses DONE, and only
accepts DONE when the verifier agrees.

The call is small and hard-timed so a slow provider never freezes the loop; a
timeout counts as "not met" with a reason the trace can surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from agent.providers import TextAdapter

from .prompts import VERIFY_DONE

log = logging.getLogger("agent.policy.verifier")

VERIFY_TIMEOUT_S = 15.0


@dataclass(frozen=True, slots=True)
class DoneVerdict:
    met: bool
    reason: str
    model: str
    usage: dict[str, int]
    latency_ms: int


async def verify_done(
    *,
    adapter: TextAdapter,
    goal: str,
    url: str,
    page_text: str,
    history: list[dict[str, Any]],
) -> DoneVerdict:
    """Return the verifier's judgement on whether the goal is met.

    Failures degrade to `met=False` with a reason, never raise — a broken
    verifier must not strand a run that the policy already believes is done."""
    context = {
        "goal": goal,
        "url": url,
        "page": page_text[:3000],
        "recent_actions": history[-24:],
    }
    try:
        result = await asyncio.wait_for(
            adapter.complete(system=VERIFY_DONE, user=json.dumps(context), json_object=True),
            timeout=VERIFY_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.warning("done verifier timed out after %ss", VERIFY_TIMEOUT_S)
        return DoneVerdict(met=False, reason="verifier timed out", model="", usage={}, latency_ms=0)
    try:
        parsed = json.loads(result.text)
        met = bool(parsed["met"])
        reason = str(parsed.get("reason", ""))[:200]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
        log.warning("done verifier reply unusable: %s", err)
        return DoneVerdict(met=False, reason=f"verifier reply unusable: {err}", model="", usage={}, latency_ms=0)
    return DoneVerdict(
        met=met,
        reason=reason,
        model=result.model,
        usage={
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
        },
        latency_ms=result.latency_ms,
    )
