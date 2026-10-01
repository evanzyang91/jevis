"""Registry rules and adapter error paths (no network calls)."""

from __future__ import annotations

import os

import pytest

from agent.providers import (
    REGISTRY,
    AdapterError,
    AnthropicAdapter,
    OpenAIAdapter,
    adapter_for,
    defaults,
    get,
    with_modality,
)


def test_registry_contains_the_two_named_text_models_and_jev() -> None:
    assert set(REGISTRY) >= {"haiku-4-5", "gpt-4o-mini", "jev-latest"}


def test_defaults_pick_a_text_and_a_vision_model() -> None:
    picks = defaults()
    assert "text" in picks
    assert "vision" in picks


def test_with_modality_returns_only_capable_models() -> None:
    for info in with_modality("vision"):
        assert "vision" in info.modalities


def test_get_raises_on_unknown_model() -> None:
    with pytest.raises(KeyError):
        get("does-not-exist")


def test_adapter_for_returns_matching_adapter_type() -> None:
    assert isinstance(adapter_for("haiku-4-5", "anthropic"), AnthropicAdapter)
    assert isinstance(adapter_for("gpt-4o-mini", "openai"), OpenAIAdapter)


def test_adapter_for_raises_on_unknown_provider() -> None:
    with pytest.raises(ValueError):
        adapter_for("x", "invented")


@pytest.mark.asyncio
async def test_anthropic_adapter_raises_when_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    adapter = AnthropicAdapter(model="haiku-4-5")
    with pytest.raises(AdapterError):
        await adapter.complete("system", "user")


@pytest.mark.asyncio
async def test_openai_adapter_raises_when_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    adapter = OpenAIAdapter(model="gpt-4o-mini")
    with pytest.raises(AdapterError):
        await adapter.complete("system", "user")


def test_environment_snapshot_stays_clean() -> None:
    """Sanity: nothing above accidentally sets an API key on the process env."""
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TYPESAFE_API_KEY"):
        assert not os.environ.get(name, "").startswith("test-")
