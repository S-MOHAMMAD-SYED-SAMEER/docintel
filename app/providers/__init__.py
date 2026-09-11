"""Provider selection.

The pipeline asks for "a provider"; which one it gets is a configuration
concern, not a pipeline concern.
"""

from functools import lru_cache

from app.providers.anthropic_vision import AnthropicExtractionProvider
from app.providers.base import (
    ExtractionProvider,
    PageImage,
    ProviderError,
    ProviderRefusal,
    RawExtraction,
)


@lru_cache
def get_provider() -> ExtractionProvider:
    """The configured extraction provider, built once and reused."""
    return AnthropicExtractionProvider()


def reset_provider() -> None:
    """Drop the cached provider (used when settings change)."""
    get_provider.cache_clear()


__all__ = [
    "AnthropicExtractionProvider",
    "ExtractionProvider",
    "PageImage",
    "ProviderError",
    "ProviderRefusal",
    "RawExtraction",
    "get_provider",
    "reset_provider",
]
