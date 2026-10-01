"""Text adapters — one per provider."""

from agent.providers.registry import get as _get

from .anthropic_ import AnthropicAdapter
from .base import AdapterError, ImageInput, TextAdapter, TextResult, TokenUsage
from .openai_ import OpenAIAdapter

__all__ = [
    "AdapterError",
    "AnthropicAdapter",
    "ImageInput",
    "OpenAIAdapter",
    "TextAdapter",
    "TextResult",
    "TokenUsage",
    "adapter_for",
]


def adapter_for(model_id: str, provider: str) -> TextAdapter:
    """Build an adapter for the registry entry `model_id`.

    Sends the provider's actual API model string, not our internal handle,
    so aliases and dated snapshots are decoupled from the UI-facing id.
    """
    try:
        api_model = _get(model_id).api_model
    except KeyError:
        api_model = model_id
    if provider == "anthropic":
        return AnthropicAdapter(model=api_model)
    if provider == "openai":
        return OpenAIAdapter(model=api_model)
    raise ValueError(f"No text adapter for provider {provider!r}")
