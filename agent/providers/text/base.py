"""Text adapter contract.

Anthropic and OpenAI have different endpoints, different reasoning params, and
different JSON-mode names. This module names the interface every text
completion goes through, so callers write one function.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, TypeVar

import httpx

log = logging.getLogger("agent.providers.http")

T = TypeVar("T")

# httpx-family exceptions that indicate a transient network issue. TLS
# `bad record mac`, connection resets, HTTP/2 stream corruption, and read
# timeouts all show up here and clear on the next attempt.
_TRANSIENT: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
    OSError,
)


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    initial_delay: float = 0.4,
) -> T:
    """Retry a network operation on transient errors with exponential backoff.

    A single flaky socket must not end a run. This wraps one HTTP call. The
    caller keeps its own timeout via httpx; retries only cover the exception
    classes that historically clear on the next attempt (TLS MAC failures,
    connection resets, HTTP/2 stream errors).
    """
    delay = initial_delay
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except _TRANSIENT as err:
            if attempt == attempts:
                raise
            log.warning("transient network error (attempt %d/%d): %s", attempt, attempts, err)
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class TextResult:
    text: str
    model: str
    usage: TokenUsage
    latency_ms: int


@dataclass(frozen=True, slots=True)
class ImageInput:
    """One image for a multimodal request.

    `data` is raw bytes; the adapter base64-encodes for the specific provider.
    Kept in one shape so vision callers do not learn each provider's wire form.
    """

    data: bytes
    media_type: str = "image/jpeg"


class TextAdapter(Protocol):
    """One async call per completion. Callers pass a system prompt, a user
    payload (already stringified), and optional images.

    JSON-mode is signalled by `json_object=True`; the adapter picks the right
    provider knob. Never returns partial output — parsing failures raise.
    """

    async def complete(
        self,
        system: str,
        user: str,
        *,
        images: list[ImageInput] | None = None,
        json_object: bool = False,
        max_tokens: int = 1024,
    ) -> TextResult: ...


class AdapterError(RuntimeError):
    """An adapter could not produce a completion. Never partial."""


_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?```", re.DOTALL)


def strip_json_fences(text: str) -> str:
    """Return the JSON body from a possibly-fenced or annotated completion.

    Some providers ignore JSON-mode and wrap the payload in a markdown fence
    like ```json {"text": null} ```; some also add commentary after the fence.
    Preference order:
      1. The first fenced block anywhere in the reply.
      2. The substring between the first `{` and the last matching `}`.
      3. The raw text (parsing will raise later if it is still not JSON).
    """
    stripped = text.strip()
    match = _FENCE.search(stripped)
    if match:
        return match.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1].strip()
    return stripped
