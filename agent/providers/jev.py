"""TypeSafe Jev — constrained choice.

One HTTP call per decision. Jev decodes only from the allowed set, so a
malformed response cannot cause an action. We validate the shape defensively
anyway: a wrong probability distribution is not an action either.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .text.base import with_retries

ENDPOINT = "https://api.typesafe.ai/v1/systemone"


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    """One answer for one question. `probabilities` covers exactly the offered
    ids and sums to 1 within rounding tolerance."""

    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True, slots=True)
class NoulAnswer:
    """One answer for one yes/no question. `noul` is the probability of yes,
    in [0, 1]. A Noul carries no confidence."""

    noul: float


@dataclass(frozen=True, slots=True)
class JevResult:
    """A dict of `question_id -> ChoiceAnswer`, plus wire-level telemetry.

    The caller decides which questions to consume; head-selected answers can
    be ignored without cost. Noul questions (`"type": "noul"`) answer in
    `nouls`, not `answers`, so choice consumers never meet a probability of yes.
    """

    answers: dict[str, ChoiceAnswer]
    model: str
    usage: dict[str, int]
    latency_ms: int
    nouls: dict[str, NoulAnswer] = field(default_factory=dict)


class JevError(RuntimeError):
    """Server rejected the question, or the response failed validation."""


class JevOversized(JevError):
    """Question exceeded server-side token or choice caps. Retry with a
    smaller action space."""


def _validate(answer_raw: dict[str, Any], allowed: set[str]) -> ChoiceAnswer:
    reason: str | None = None
    try:
        choice = answer_raw["choice"]
        probs_raw = answer_raw["probabilities"]
        confidence = answer_raw["confidence"]
    except (KeyError, TypeError):
        raise JevError(f"Jev response missing keys; got {answer_raw!r}") from None
    if choice not in allowed:
        reason = f"choice {choice!r} not in offered set"
    elif set(probs_raw) != allowed:
        missing = allowed - set(probs_raw)
        extra = set(probs_raw) - allowed
        reason = f"probability keys do not match offered set (missing={sorted(missing)[:3]} extra={sorted(extra)[:3]})"
    else:
        numbers = [*probs_raw.values(), confidence]
        if not all(isinstance(n, (int, float)) and math.isfinite(n) and 0 <= n <= 1 for n in numbers):
            reason = "one or more probability values out of [0,1] range or non-numeric"
        elif abs(sum(probs_raw.values()) - 1) >= 0.02:
            reason = f"probabilities sum to {sum(probs_raw.values()):.3f}, not 1"
        elif probs_raw[choice] < max(probs_raw.values()) - 1e-6:
            reason = "chosen id is not the top-probability id"
    if reason is not None:
        raise JevError(f"Invalid Jev response ({reason}); allowed={sorted(allowed)[:6]}...")
    return ChoiceAnswer(
        choice=choice,
        probabilities={k: float(v) for k, v in probs_raw.items()},
        confidence=float(confidence),
    )


def _validate_noul(answer_raw: dict[str, Any]) -> NoulAnswer:
    try:
        value = answer_raw["noul"]
    except (KeyError, TypeError):
        raise JevError(f"Jev noul response missing keys; got {answer_raw!r}") from None
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not is_number or not math.isfinite(value) or not 0 <= value <= 1:
        raise JevError(f"Invalid Jev response (noul {value!r} not a number in [0,1])")
    return NoulAnswer(noul=float(value))


class JevClient:
    """One HTTP client per process. Safe to share across coroutines."""

    def __init__(self, *, model: str = "jev-latest", client: httpx.AsyncClient | None = None) -> None:
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=25, http2=True)

    async def ask(
        self,
        *,
        state: dict[str, Any],
        questions: dict[str, dict[str, Any]],
    ) -> JevResult:
        key = os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise JevError("TYPESAFE_API_KEY not set")
        body = {"model": self._model, "state": state, "questions": questions}
        started = time.perf_counter()
        response = await with_retries(lambda: self._client.post(
            ENDPOINT,
            headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
            json=body,
        ))
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.is_error:
            detail = response.text[:300]
            if "max_tokens_exceeded" in detail or "Too many choices" in detail:
                raise JevOversized(detail)
            raise JevError(f"Jev HTTP {response.status_code}: {detail}")
        result = response.json()
        answers: dict[str, ChoiceAnswer] = {}
        nouls: dict[str, NoulAnswer] = {}
        for question_id, definition in questions.items():
            raw = result.get("answers", {}).get(question_id)
            if raw is None:
                continue
            if definition.get("type") == "noul":
                nouls[question_id] = _validate_noul(raw)
                continue
            allowed = set(definition["criteria"].keys())
            answers[question_id] = _validate(raw, allowed)
        return JevResult(
            answers=answers,
            model=result.get("model") or self._model,
            usage=result.get("usage") or {},
            latency_ms=latency_ms,
            nouls=nouls,
        )


# ---- Inline tests: `uv run python -m agent.providers.jev` -----------------------------


class NoulTestFailure(AssertionError):
    """An inline Noul test saw the wrong result."""


def _test_validate_noul() -> None:
    """Unit: a Noul answer parses, and an out-of-range or missing value is rejected."""
    if _validate_noul({"type": "noul", "noul": 0.95}).noul != 0.95:
        raise NoulTestFailure("a valid noul did not parse to 0.95")
    for bad in ({"noul": 1.5}, {"noul": "yes"}, {"noul": True}, {"noul": float("nan")}, {}):
        try:
            _validate_noul(bad)
        except JevError:
            continue
        raise NoulTestFailure(f"invalid noul accepted: {bad!r}")


async def _test_ask_mixed_mocked() -> None:
    """End to end, mocked server: one request with a Choice and a Noul fills both maps."""
    os.environ.setdefault("TYPESAFE_API_KEY", "test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "jev-1.13.0",
            "answers": {
                "operation": {"type": "choice", "choice": "DONE",
                              "probabilities": {"DONE": 0.9, "WAIT": 0.1}, "confidence": 0.8},
                "goal_met": {"type": "noul", "noul": 0.97},
            },
            "usage": {"input_tokens": 10, "output_tokens": 0},
        })

    client = JevClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await client.ask(state={}, questions={
        "operation": {"type": "choice", "criteria": {"DONE": "", "WAIT": ""}},
        "goal_met": {"type": "noul", "instructions": "Is the goal met?"},
    })
    if result.answers["operation"].choice != "DONE" or "goal_met" in result.answers:
        raise NoulTestFailure(f"choice map wrong: {result.answers!r}")
    if result.nouls["goal_met"].noul != 0.97:
        raise NoulTestFailure(f"noul map wrong: {result.nouls!r}")


async def _test_ask_mixed_live() -> None:
    """End to end, live API: a Choice and a Noul in one request both come back valid.
    Skipped when TYPESAFE_API_KEY is not set. Costs a fraction of a cent."""
    if os.environ.get("TYPESAFE_API_KEY", "test") == "test":
        print("skip: live test needs TYPESAFE_API_KEY")
        return
    result = await JevClient().ask(
        state={"page": {"title": "Your cart", "text": "Cart: 1 item. Banana, $0.30. Subtotal $0.30."}},
        questions={
            "operation": {"type": "choice", "instructions": "Goal: add a banana to the cart. What next?",
                          "criteria": {"DONE": "The goal is visibly met.", "WAIT": "Wait for the page."}},
            "goal_met": {"type": "noul",
                         "instructions": "Does `page` show that a banana is in the cart?"},
        },
    )
    if "operation" not in result.answers or not 0 <= result.nouls["goal_met"].noul <= 1:
        raise NoulTestFailure(f"live mixed request wrong: {result!r}")
    print(f"live: operation={result.answers['operation'].choice} goal_met={result.nouls['goal_met'].noul:.2f}")


if __name__ == "__main__":
    import asyncio

    _test_validate_noul()
    asyncio.run(_test_ask_mixed_live())  # before the mocked test, which may set a dummy key
    asyncio.run(_test_ask_mixed_mocked())
    print("jev.py inline tests passed")
