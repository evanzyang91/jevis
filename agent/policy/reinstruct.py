"""Reinstruct: a text model writes a hint when the policy is stuck.

Called only on an escalated step (two failed step checks in a row, or a
failed DONE or BLOCKED). The hint goes into the policy's state as `guidance`
and the policy decides again: the text model never picks the action, so every
action is still a typed choice from the offered set. The hint names 1-3
evidence phrases from the page; the supervisor keeps it only while they are
still on the page, and for at most HINT_STEPS steps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from agent.perception import Observation
from agent.providers import TextAdapter, adapter_for, get

from .jev_policy import commits_product
from .prompts import REINSTRUCT

log = logging.getLogger("agent.policy.reinstruct")

REINSTRUCT_TIMEOUT_S = 15.0
HINT_STEPS = 3
_HINT_ADAPTERS: dict[str, TextAdapter] = {}


def hint_adapter(default: TextAdapter) -> TextAdapter:
    """The text model that writes hints: HINT_MODEL (a registry id) when it is
    set and its provider's key is present, else the run's own text model.
    Hints are rare (an escalated step) and decide whether a stuck run
    recovers, so a stronger model than the per-field helper can pay off."""
    name = os.environ.get("HINT_MODEL", "").strip()
    if not name:
        return default
    if name not in _HINT_ADAPTERS:
        try:
            info = get(name)
        except KeyError:
            log.warning("HINT_MODEL %r is not a registry id; hints use the run's text model", name)
            return default
        key = "ANTHROPIC_API_KEY" if info.provider == "anthropic" else "OPENAI_API_KEY"
        if not os.environ.get(key):
            log.warning("HINT_MODEL %r needs %s; hints use the run's text model", name, key)
            return default
        _HINT_ADAPTERS[name] = adapter_for(info.id, info.provider)
    return _HINT_ADAPTERS[name]


def progress(history: list[dict[str, Any]]) -> dict[str, list[str]]:
    """What the whole run has done, not only its recent tail: every product it
    added and every query it typed. With only the last 20 actions, hints told
    a cake run to search flour again 30 steps after flour was added."""
    added = [str(entry.get("label") or "") for entry in history
             if entry.get("operation") == "CLICK" and entry.get("page_changed")
             and commits_product(str(entry.get("label") or ""))]
    searched = [str(entry["text"]) for entry in history
                if entry.get("operation") == "TYPE_TEXT" and entry.get("text")]
    return {"added": added, "searched": searched}


@dataclass(frozen=True, slots=True)
class Hint:
    guidance: str
    evidence: tuple[str, ...]
    model: str
    usage: dict[str, int]
    latency_ms: int
    # The exact control the hint names for the next step, if any.
    control: str | None = None


def _seen(observation: Observation) -> str:
    """The page's text, dialog text, and control names, lower-cased."""
    return " ".join([observation.text, observation.dialog_text or "",
                     *(element.name for element in observation.elements)]).lower()


def evidence_present(hint: Hint, observation: Observation) -> bool:
    """True while every evidence phrase is still on the page or its controls."""
    seen = _seen(observation)
    return all(phrase.lower() in seen for phrase in hint.evidence)


async def reinstruct(
    *, adapter: TextAdapter, goal: str, observation: Observation, history: list[dict[str, Any]],
    policy_pick: str, banned: Iterable[str] = (),
) -> Hint | None:
    """One hint, or None on timeout or an unusable reply. Never raises: a broken
    hint must not end a run that the policy can still continue. `banned` holds
    the labels the policy cannot choose now (covered, inert, looping): a hint
    that names one cannot be followed."""
    names = {element.name for element in observation.elements}
    # Operations the guards withhold, as the hint names them. A pasta run's
    # hints said "scroll down" twenty times while scrolling was banned as inert.
    blocked_ops = {"SCROLL_DOWN": "scroll down", "SCROLL_UP": "scroll up", "BACK": "back", "ENTER": "enter",
                   "WAIT": "wait"}
    banned = set(banned)
    context = {
        "goal": goal,
        **progress(history),
        "page": {"url": observation.url, "title": observation.title, "text": observation.text[:4000]},
        "dialog": observation.dialog_text or None,
        "controls": [f"{element.role}: {element.name}" for element in observation.elements][:80],
        "unavailable": [*(word for op, word in blocked_ops.items() if op in banned),
                        *sorted(label for label in banned if label in names),
                        # An operation-scoped ban ("CLICK:Search") withholds one use of a
                        # control that shares its name with another (the Search box).
                        *sorted(f"{label.split(':', 1)[1]} (as {label.split(':', 1)[0].lower()})"
                                for label in banned if ":" in label and label.split(":", 1)[1] in names)][:40],
        "recent_actions": history[-20:],
        "policy_pick": policy_pick,
    }
    adapter = hint_adapter(adapter)
    try:
        # A reasoning model spends part of the limit before it writes the reply.
        result = await asyncio.wait_for(
            adapter.complete(system=REINSTRUCT, user=json.dumps(context), json_object=True, max_tokens=1200),
            timeout=REINSTRUCT_TIMEOUT_S,
        )
        parsed = json.loads(result.text)
        guidance = str(parsed["guidance"]).strip()
        evidence = tuple(str(p).strip() for p in parsed.get("evidence", []) if str(p).strip())[:3]
        control = str(parsed.get("control") or "").strip() or None
    except (asyncio.TimeoutError, KeyError, TypeError, ValueError) as err:
        log.warning("reinstruct failed: %s", err)
        return None
    except Exception as err:  # noqa: BLE001 — provider errors: skip the hint, keep the run
        log.warning("reinstruct provider error: %s", err)
        return None
    if not guidance:
        return None
    # Evidence the page does not hold would expire the hint at once; drop it instead.
    seen = _seen(observation)
    return Hint(guidance=guidance[:400], evidence=tuple(p for p in evidence if p.lower() in seen),
                model=result.model,
                usage={"prompt_tokens": result.usage.prompt_tokens,
                       "completion_tokens": result.usage.completion_tokens},
                latency_ms=result.latency_ms, control=control)


# ---- Inline tests: `uv run python -m agent.policy.reinstruct` ------------------------


class ReinstructTestFailure(AssertionError):
    """An inline reinstruct test saw the wrong result."""


async def _test_evidence_kept_only_if_on_page() -> None:
    """Unit, stand-in text model: evidence the page holds is kept, evidence it
    does not hold is dropped, and the hint then lives on that page."""
    from types import SimpleNamespace

    from agent.perception import Element, Rect

    class _Adapter:
        async def complete(self, **_: Any) -> Any:
            reply = {"guidance": "Choose Sofritas.", "evidence": ["Choose Protein", "Not on page"],
                     "control": "Sofritas"}
            return SimpleNamespace(text=json.dumps(reply), model="stand-in", latency_ms=5,
                                   usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))

    observation = Observation(
        url="https://example.test/", title="Bowl", text="Choose Protein Required", marker="m", fingerprint="f",
        elements=(Element(ref="[data-agent-ref=e0]", role="radio", name="Sofritas", bounds=Rect(0, 0, 10, 10)),),
        guards={}, can_go_back=False, can_scroll_up=False, can_scroll_down=False, viewport=(1280, 800))
    hint = await reinstruct(adapter=_Adapter(), goal="Order a sofritas bowl.",  # type: ignore[arg-type]
                            observation=observation, history=[], policy_pick="CLICK Close")
    if hint is None or hint.evidence != ("Choose Protein",) or hint.control != "Sofritas":
        raise ReinstructTestFailure(f"hint wrong: {hint}")
    if not evidence_present(hint, observation):
        raise ReinstructTestFailure("kept evidence not found on its own page")


if __name__ == "__main__":
    asyncio.run(_test_evidence_kept_only_if_on_page())
    print("reinstruct.py inline tests passed")
