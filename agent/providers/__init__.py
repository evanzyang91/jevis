"""Model providers: registry, text adapters, and the Jev constrained-choice client."""

from .jev import ChoiceAnswer, JevClient, JevError, JevOversized, JevResult
from .registry import REGISTRY, Modality, ModelInfo, Provider, defaults, get, with_modality
from .text import (
    AdapterError,
    AnthropicAdapter,
    ImageInput,
    OpenAIAdapter,
    TextAdapter,
    TextResult,
    TokenUsage,
    adapter_for,
)

__all__ = [
    "REGISTRY",
    "AdapterError",
    "AnthropicAdapter",
    "ChoiceAnswer",
    "ImageInput",
    "JevClient",
    "JevError",
    "JevOversized",
    "JevResult",
    "Modality",
    "ModelInfo",
    "OpenAIAdapter",
    "Provider",
    "TextAdapter",
    "TextResult",
    "TokenUsage",
    "adapter_for",
    "defaults",
    "get",
    "with_modality",
]
