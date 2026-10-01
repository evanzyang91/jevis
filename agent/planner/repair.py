"""Plan repair.

When the active sub-goal reports blocked, the supervisor asks the planner to
rewrite or drop it. Three repair attempts per run; after that the supervisor
blocks the whole run rather than looping the planner.
"""

from __future__ import annotations

import json
from dataclasses import replace

from agent.providers import TextAdapter

from .plan import Plan, PlannerError, SubGoal
from .prompts import REPAIR_PLAN

MAX_REPAIRS = 3


async def repair(*, adapter: TextAdapter, plan: Plan, reason: str) -> Plan:
    """Rewrite the active sub-goal, or drop it if the planner concedes."""
    active = plan.active
    if active is None:
        return plan
    context = {
        "goal": plan.refined_goal,
        "blocked_subgoal": {"text": active.text, "check": active.check},
        "reason": reason,
        "remaining": [sg.text for sg in plan.subgoals[plan.active_index :]],
    }
    result = await adapter.complete(
        system=REPAIR_PLAN,
        user=json.dumps(context),
        json_object=True,
    )
    try:
        parsed = json.loads(result.text)
        action = parsed["action"]
        if action == "drop":
            plan.subgoals.pop(plan.active_index)
        elif action == "rewrite":
            new = SubGoal(text=parsed["text"].strip(), check=parsed["check"].strip())
            plan.subgoals[plan.active_index] = new
        elif action == "concede":
            plan.subgoals = plan.subgoals[: plan.active_index]
        else:
            raise ValueError(f"unknown action {action!r}")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
        raise PlannerError(f"Repair response unusable: {result.text[:200]}") from err
    return replace(plan)
