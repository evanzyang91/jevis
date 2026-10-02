"""Replay a run's event log through the plan-progress ledger, offline.

Shows which logged events would finish, skip, or revert which plan step, and
what the policy's per-step goal would have read at the end. No browser, no
model calls: it feeds the ledger the same signals the supervisor does (adds,
fills, URL changes, cart-badge reads).

Run:  uv run python scripts/replay_progress.py ~/.cache/agent/logs/<run>.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.planner import Progress, cart_count, is_committing, parse_plan  # noqa: E402


def replay(path: Path) -> Progress | None:
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    plan_event = next((e for e in events if e["kind"] == "plan"), None)
    if plan_event is None:
        print("no plan event in this log")
        return None
    # Logs from before the ledger carry step texts only: let the parser split
    # rules from steps and read each step's term from its quoted phrase.
    steps = plan_event.get("steps") or [{"text": t, "check": t} for t in plan_event["plan"]]
    raw = json.dumps({"goal": plan_event.get("refined_goal") or " ".join(plan_event["plan"]),
                      "subgoals": [{"text": s["text"], "check": s.get("check") or s["text"],
                                    "search_term": s.get("search_term")} for s in steps]})
    plan = parse_plan(raw, goal=plan_event.get("original_goal") or "", url=plan_event.get("start_url"))
    progress = Progress(plan)
    print(f"steps: {[s.name for s in progress.steps]}\nrules: {plan.constraints}\nstop: {plan.stop_when}\n")
    step = 0
    action = None
    last_click: str | None = None
    for event in events:
        kind = event["kind"]
        if kind == "observation":
            step += 1
            count = cart_count([el[2] for el in event.get("elements", [])])
            for change in progress.on_cart(count, last_click=last_click):
                print(f"  obs {step:2}: cart={count} -> step {change.index + 1} {change.status}: {change.note}")
            last_click = None
        elif kind == "action":
            action = event
        elif kind == "keystroke" and action is not None and action["action_kind"] == "fill":
            progress.on_fill(event["text"])
        elif kind == "outcome" and action is not None:
            label = action["target_label"]
            changes = []
            if action["action_kind"] == "click" and event["page_changed"]:
                last_click = label
                if is_committing(label):
                    changes += progress.on_add(label)
                    if not changes:
                        print(f"  act {step:2}: {label!r} -> {progress.notes[-1]}")
            if event["url_changed"]:
                changes += progress.on_navigate(progress.plan.start_url)
            for change in changes:
                print(f"  act {step:2}: {label[:60]!r} -> step {change.index + 1} {change.status}: {change.note}")
            action = None
        elif kind == "decision" and event["operation"] in {"DONE", "BLOCKED"}:
            print(f"  dec {step:2}: policy said {event['operation']} with active step "
                  f"{progress.active.name if progress.active else None!r}")
    print(f"\nfinished: {progress.finished()}  active: {progress.active.name if progress.active else None}")
    print(f"summary: {progress.summary()}\n")
    print("policy goal at the end:\n" + progress.goal_for_policy())
    return progress


if __name__ == "__main__":
    replay(Path(sys.argv[1]).expanduser())
