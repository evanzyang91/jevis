"""OpenAI chat completions adapter.

Works with any OpenAI-compatible endpoint (OpenAI itself, OpenRouter, Groq,
DeepSeek). The base URL and key come from the environment so multiple adapters
can coexist without new plumbing.
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

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIAdapter(TextAdapter):
    """Uses env vars `OPENAI_API_KEY` and `OPENAI_BASE_URL`. A caller that
    needs a distinct base URL per model can pass one at construction time."""

    def __init__(
        self,
        *,
        model: str,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
    ) -> None:
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=30, http2=True)
        self._base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._api_key = os.environ.get(api_key_env)

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
            raise AdapterError("OpenAI API key not set")
        user_content: list[dict[str, Any]] | str
        if images:
            user_content = [{"type": "text", "text": user}]
            for image in images:
                encoded = base64.b64encode(image.data).decode()
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{image.media_type};base64,{encoded}"},
                })
        else:
            user_content = user
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        response = await with_retries(lambda: self._client.post(
            f"{self._base_url}/chat/completions",
            headers={
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
            },
            json=payload,
        ))
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.is_error:
            raise AdapterError(f"OpenAI HTTP {response.status_code}: {response.text[:400]}")
        body = response.json()
        try:
            text = body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as err:
            raise AdapterError(f"OpenAI response missing content: {body}") from err
        if json_object:
            text = strip_json_fences(text)
        usage_raw = body.get("usage") or {}
        return TextResult(
            text=text,
            model=body.get("model") or self._model,
            usage=TokenUsage(
                prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
                completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            ),
            latency_ms=latency_ms,
        )
