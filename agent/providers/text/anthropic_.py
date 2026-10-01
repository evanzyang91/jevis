"""Anthropic messages API adapter.

Wraps the `/v1/messages` endpoint over httpx. Reasoning defaults off; enable it
per-call by supplying `reasoning=...`. Vision uses the standard image content
blocks with base64 payloads.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any

import httpx

from .base import (
    AdapterError,
    ImageInput,
    TextAdapter,
    TextResult,
    TokenUsage,
    strip_json_fences,
    with_retries,
)

ANTHROPIC_VERSION = "2023-06-01"
ENDPOINT = "https://api.anthropic.com/v1/messages"


class AnthropicAdapter(TextAdapter):
    """One HTTP client per adapter instance. Callers should create one adapter
    per process; adapters are safe to share across coroutines."""

    def __init__(self, *, model: str, client: httpx.AsyncClient | None = None) -> None:
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=30, http2=True)
        self._api_key = os.environ.get("ANTHROPIC_API_KEY")

    async def complete(
        self,
        system: str,
        user: str,
        *,
        images: list[ImageInput] | None = None,
        json_object: bool = False,
        max_tokens: int = 1024,
    ) -> TextResult:
        if not self._api_key:
            raise AdapterError("ANTHROPIC_API_KEY not set")
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for image in images or []:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.media_type,
                    "data": base64.b64encode(image.data).decode(),
                },
            })
        # JSON-mode is signalled by a system suffix; the messages API does not
        # take a `response_format` parameter but honours the instruction reliably.
        system_text = system
        if json_object:
            system_text = f"{system}\nReply with valid JSON only."
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system_text,
            "messages": [{"role": "user", "content": content}],
        }
        started = time.perf_counter()
        response = await with_retries(lambda: self._client.post(
            ENDPOINT,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=payload,
        ))
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.is_error:
            raise AdapterError(f"Anthropic HTTP {response.status_code}: {response.text[:400]}")
        body = response.json()
        blocks = body.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text").strip()
        if json_object:
            text = strip_json_fences(text)
        usage_raw = body.get("usage") or {}
        return TextResult(
            text=text,
            model=body.get("model") or self._model,
            usage=TokenUsage(
                prompt_tokens=int(usage_raw.get("input_tokens", 0)),
                completion_tokens=int(usage_raw.get("output_tokens", 0)),
            ),
            latency_ms=latency_ms,
        )
