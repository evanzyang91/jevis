"""TypeSafe Jev — constrained choice.

One HTTP call per decision. Jev decodes only from the allowed set, so a
malformed response cannot cause an action. We validate the shape defensively
anyway: a wrong probability distribution is not an action either.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
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
class JevResult:
    """A dict of `question_id -> ChoiceAnswer`, plus wire-level telemetry.

    The caller decides which questions to consume; head-selected answers can
    be ignored without cost.
    """

    answers: dict[str, ChoiceAnswer]
    model: str
    usage: dict[str, int]
    latency_ms: int


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
        for question_id, definition in questions.items():
            raw = result.get("answers", {}).get(question_id)
            if raw is None:
                continue
            allowed = set(definition["criteria"].keys())
            answers[question_id] = _validate(raw, allowed)
        return JevResult(
            answers=answers,
            model=result.get("model") or self._model,
            usage=result.get("usage") or {},
            latency_ms=latency_ms,
        )
