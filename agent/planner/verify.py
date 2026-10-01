"""Sub-goal verification.

The policy may claim DONE against an optimistic snapshot. Before the run
finishes, verification re-reads the page and confirms every sub-goal's check
phrase is met. One sub-goal, one call — Jev-shaped so the wire cost stays
small.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from agent.providers import TextAdapter

from .prompts import VERIFY_SUBGOAL


@dataclass(frozen=True, slots=True)
class VerifyResult:
    met: bool
    reason: str
    latency_ms: int
    model: str
    usage: dict[str, int]


class VerifyError(RuntimeError):
    """Verification could not produce a decision."""


async def verify(
    *,
    adapter: TextAdapter,
    subgoal_text: str,
    check: str,
    page_text: str,
    url: str,
) -> VerifyResult:
    """Return one boolean judgement plus a short reason for the trace."""
    context = {"subgoal": subgoal_text, "check": check, "url": url, "page": page_text[:3000]}
    result = await adapter.complete(
        system=VERIFY_SUBGOAL,
        user=json.dumps(context),
        json_object=True,
    )
    try:
        parsed = json.loads(result.text)
        met = parsed["met"]
        reason = parsed.get("reason", "")
        if not isinstance(met, bool):
            raise ValueError("met must be boolean")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
        raise VerifyError(f"Verifier response unusable: {result.text[:200]}") from err
    return VerifyResult(
        met=met,
        reason=str(reason)[:200],
        latency_ms=result.latency_ms,
        model=result.model,
        usage={"prompt_tokens": result.usage.prompt_tokens, "completion_tokens": result.usage.completion_tokens},
    )
