"""Routed LLM service facade."""

from __future__ import annotations

from cognis.providers.llm.litellm import LiteLLMProvider


class LLMService(LiteLLMProvider):
    """Facade for routed LLM orchestration.

    The service is the public provider-registry entry point. It currently
    preserves the LiteLLMProvider implementation while transports and OAuth
    responsibilities are carved out behind it.
    """
