"""Jev validation and oversized handling. No real network."""

from __future__ import annotations

import httpx
import pytest

from agent.providers import JevClient, JevError, JevOversized


def _client(handler: httpx.MockTransport) -> JevClient:
    return JevClient(client=httpx.AsyncClient(transport=handler))


@pytest.mark.asyncio
async def test_ask_validates_a_well_formed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "jev-latest",
                "answers": {
                    "operation": {
                        "choice": "CLICK",
                        "probabilities": {"CLICK": 0.9, "WAIT": 0.1},
                        "confidence": 0.85,
                    },
                },
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        )

    client = _client(httpx.MockTransport(handler))
    result = await client.ask(
        state={},
        questions={"operation": {"criteria": {"CLICK": "", "WAIT": ""}}},
    )
    assert result.answers["operation"].choice == "CLICK"


@pytest.mark.asyncio
async def test_ask_rejects_a_probability_distribution_that_does_not_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answers": {
                    "operation": {
                        "choice": "CLICK",
                        "probabilities": {"CLICK": 0.5, "WAIT": 0.4},
                        "confidence": 0.8,
                    },
                },
            },
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(JevError):
        await client.ask(
            state={},
            questions={"operation": {"criteria": {"CLICK": "", "WAIT": ""}}},
        )


@pytest.mark.asyncio
async def test_ask_reports_oversized_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="max_tokens_exceeded")

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(JevOversized):
        await client.ask(state={}, questions={"operation": {"criteria": {"A": ""}}})


@pytest.mark.asyncio
async def test_ask_requires_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    client = JevClient()
    with pytest.raises(JevError):
        await client.ask(state={}, questions={"operation": {"criteria": {"A": ""}}})
