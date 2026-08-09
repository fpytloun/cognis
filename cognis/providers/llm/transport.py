"""Internal LLM transport interfaces and LiteLLM implementation."""

from __future__ import annotations

from typing import Any, Protocol

import litellm


def strip_controller_transport_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Remove Cognis-only routing metadata at the provider wire boundary."""

    return {key: value for key, value in kwargs.items() if key != "_cognis_executor_affinity_id"}


class ChatCompletionsTransport(Protocol):
    """Transport boundary for Chat Completions-compatible calls."""

    name: str

    async def completion(self, **kwargs: Any) -> Any:
        """Call a Chat Completions-compatible transport."""


class ResponsesTransport(Protocol):
    """Transport boundary for Responses-compatible calls."""

    name: str

    async def responses(self, **kwargs: Any) -> Any:
        """Call a Responses-compatible transport."""


class LLMTransport(ChatCompletionsTransport, ResponsesTransport, Protocol):
    """Transport that supports both major text-generation APIs."""


class LiteLLMTransport:
    """Thin adapter around LiteLLM network calls."""

    name = "litellm"

    async def completion(self, **kwargs: Any) -> Any:
        return await litellm.acompletion(**strip_controller_transport_kwargs(kwargs))

    async def responses(self, **kwargs: Any) -> Any:
        return await litellm.aresponses(**strip_controller_transport_kwargs(kwargs))
