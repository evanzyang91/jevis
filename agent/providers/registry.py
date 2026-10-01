"""One entry per model the agent can call.

Adding a new model is one dict entry. Every consumer (pricing, UI dropdown,
adapter selection) reads from this table so a new model does not require code
changes across the tree.

`id` is a short handle used everywhere internally (env vars, UI dropdowns,
budget rollups). `api_model` is what the provider actually expects on the
wire. They differ because provider IDs get versioned
(`claude-haiku-4-5-20251001`) and the internal handle should not need to
change every release.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Provider = Literal["anthropic", "openai", "typesafe"]
Modality = Literal["text", "vision"]


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Pricing is per million tokens. Zero for an unpriced or free model."""

    id: str
    provider: Provider
    api_model: str
    modalities: tuple[Modality, ...]
    price_in_per_mtok: float
    price_out_per_mtok: float
    context: int
    default: bool = False


REGISTRY: dict[str, ModelInfo] = {
    # Anthropic
    "haiku-4-5": ModelInfo(
        id="haiku-4-5",
        provider="anthropic",
        api_model="claude-haiku-4-5",
        modalities=("text", "vision"),
        price_in_per_mtok=1.00,
        price_out_per_mtok=5.00,
        context=200_000,
        default=True,
    ),
    # OpenAI — models available directly on api.openai.com
    "gpt-4o": ModelInfo(
        id="gpt-4o",
        provider="openai",
        api_model="gpt-4o",
        modalities=("text", "vision"),
        price_in_per_mtok=2.50,
        price_out_per_mtok=10.00,
        context=128_000,
    ),
    "gpt-4o-mini": ModelInfo(
        id="gpt-4o-mini",
        provider="openai",
        api_model="gpt-4o-mini",
        modalities=("text", "vision"),
        price_in_per_mtok=0.15,
        price_out_per_mtok=0.60,
        context=128_000,
    ),
    "gpt-4.1": ModelInfo(
        id="gpt-4.1",
        provider="openai",
        api_model="gpt-4.1",
        modalities=("text", "vision"),
        price_in_per_mtok=2.00,
        price_out_per_mtok=8.00,
        context=1_000_000,
    ),
    "gpt-4.1-mini": ModelInfo(
        id="gpt-4.1-mini",
        provider="openai",
        api_model="gpt-4.1-mini",
        modalities=("text", "vision"),
        price_in_per_mtok=0.40,
        price_out_per_mtok=1.60,
        context=1_000_000,
    ),
    "gpt-4.1-nano": ModelInfo(
        id="gpt-4.1-nano",
        provider="openai",
        api_model="gpt-4.1-nano",
        modalities=("text", "vision"),
        price_in_per_mtok=0.10,
        price_out_per_mtok=0.40,
        context=1_000_000,
    ),
    "gpt-5.6-luna": ModelInfo(
        id="gpt-5.6-luna",
        provider="openai",
        api_model="gpt-5.6-luna",
        modalities=("text",),
        price_in_per_mtok=0.20,
        price_out_per_mtok=1.20,
        context=128_000,
    ),
    "gpt-5.6-sol": ModelInfo(
        id="gpt-5.6-sol",
        provider="openai",
        api_model="gpt-5.6-sol",
        modalities=("text",),
        price_in_per_mtok=4.00,
        price_out_per_mtok=20.00,
        context=200_000,
    ),
    "gpt-5.6-terra": ModelInfo(
        id="gpt-5.6-terra",
        provider="openai",
        api_model="gpt-5.6-terra",
        modalities=("text",),
        price_in_per_mtok=4.00,
        price_out_per_mtok=20.00,
        context=200_000,
    ),
    "gpt-6-astra": ModelInfo(
        id="gpt-6-astra",
        provider="openai",
        api_model="gpt-6-astra",
        modalities=("text", "vision"),
        price_in_per_mtok=10.00,
        price_out_per_mtok=50.00,
        context=200_000,
    ),
    # TypeSafe Jev — the constrained-choice policy
    "jev-latest": ModelInfo(
        id="jev-latest",
        provider="typesafe",
        api_model="jev-latest",
        modalities=("text",),
        price_in_per_mtok=0.042,
        price_out_per_mtok=0.0,
        context=32_000,
    ),
}


def get(model_id: str) -> ModelInfo:
    if model_id not in REGISTRY:
        raise KeyError(f"Unknown model id: {model_id!r}")
    return REGISTRY[model_id]


def defaults() -> dict[Modality, str]:
    picks: dict[Modality, str] = {}
    for info in REGISTRY.values():
        if not info.default:
            continue
        for modality in info.modalities:
            picks.setdefault(modality, info.id)
    return picks


def with_modality(modality: Modality) -> list[ModelInfo]:
    return [info for info in REGISTRY.values() if modality in info.modalities]
