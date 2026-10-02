"""Step repair.

When the policy reports BLOCKED on the active step, the supervisor asks the
text model once for another way to meet that step (a broader search, the
closest equivalent item). If there is none, or the rewritten step blocks
too, the supervisor skips the step and goes on with the rest: one item the
store does not sell must not end the whole run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from agent.providers import TextAdapter

from .plan import PlannerError, SubGoal, done_when_for
from .prompts import REPAIR_PLAN

# Rewrites per step. A rewritten step that blocks again is skipped.
MAX_REPAIRS = 1


@dataclass(frozen=True, slots=True)
class Repair:
    """`step` is the rewritten step, or None when the step must be skipped."""

    step: SubGoal | None
    reason: str
    model: str
    usage: dict[str, int]
    latency_ms: int


async def repair(
    *,
    adapter: TextAdapter,
    goal: str,
    rules: list[str],
    step: SubGoal,
    reason: str,
    url: str = "",
    page_text: str = "",
) -> Repair:
    """Rewrite one blocked step, or say to skip it. Raises PlannerError on an
    unusable reply; the caller then skips the step."""
    context = {
        "goal": goal,
        "rules": rules,
        "step": {"text": step.text, "check": step.check, "search_term": step.search_term},
        "reason": reason,
        "url": url,
        "page": page_text[:2000],
    }
    result = await adapter.complete(system=REPAIR_PLAN, user=json.dumps(context), json_object=True)
    usage = {"prompt_tokens": result.usage.prompt_tokens, "completion_tokens": result.usage.completion_tokens}
    try:
        parsed = json.loads(result.text)
        action = parsed["action"]
        if action == "skip":
            return Repair(step=None, reason=str(parsed.get("reason") or reason)[:200], model=result.model,
                          usage=usage, latency_ms=result.latency_ms)
        if action != "rewrite":
            raise ValueError(f"unknown action {action!r}")
        text = str(parsed["text"]).strip()
        if not text:
            raise ValueError("empty rewritten step")
        raw_term = parsed.get("search_term")
        term = raw_term.strip() if isinstance(raw_term, str) and raw_term.strip() else None
        new = SubGoal(text=text, check=str(parsed.get("check") or "").strip() or text, search_term=term,
                      # Keep the step's kind unless its new wording plainly adds an item.
                      done_when=done_when_for(text, term, step.done_when))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
        raise PlannerError(f"Repair response unusable: {result.text[:200]}") from err
    return Repair(step=new, reason="rewritten", model=result.model, usage=usage, latency_ms=result.latency_ms)
