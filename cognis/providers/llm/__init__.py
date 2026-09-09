"""LLM provider (LiteLLM)."""

from __future__ import annotations

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

from cognis.providers.llm.errors import (
    LLMStreamFailure,
    LLMStreamIdleTimeout,
    LLMStreamProviderError,
    MidStreamErrorCategory,
    OpenAIToolSearchFallbackRequired,
)

__all__ = [
    "LLMStreamFailure",
    "LLMStreamIdleTimeout",
    "LLMStreamProviderError",
    "MidStreamErrorCategory",
    "OpenAIToolSearchFallbackRequired",
]
