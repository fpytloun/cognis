"""Internal LLM transport interfaces and LiteLLM implementation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

import litellm

from cognis.providers.llm.usage_binding import UsageBinding, take_binding
from cognis.providers.llm.usage_binding import observed_sdk as observed_sdk
from cognis.providers.llm.usage_capture import UsageRequest


def strip_controller_transport_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Remove Cognis-only routing metadata at the provider wire boundary."""

    # Reserved namespaces belong to controller tracing/routing, not upstream SDKs.
    return {
        key: value for key, value in kwargs.items() if not key.startswith(("cognis_", "_cognis_"))
    }


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

    @staticmethod
    def _request_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        from cognis.providers.llm.recovery import active_attempt_budget

        result = strip_controller_transport_kwargs(kwargs)
        budget = active_attempt_budget.get()
        if budget is not None:
            result.update(num_retries=0, max_retries=0)
            result["timeout"] = min(
                float(result.get("timeout") or budget.remaining), budget.remaining
            )
        return result

    async def completion(self, **kwargs: Any) -> Any:
        binding = take_binding(kwargs)
        return await observed_sdk(litellm.acompletion, binding, operation="chat")(
            **self._request_kwargs(kwargs),
        )

    async def responses(self, **kwargs: Any) -> Any:
        binding = take_binding(kwargs)
        return await observed_sdk(litellm.aresponses, binding, operation="responses")(
            **self._request_kwargs(kwargs),
        )


class ObservedLLMTransport:
    """Request-owned adapter around one physical Chat/Responses dispatch.

    Recovery remains outside this adapter. Every resubmission through this
    object gets a new physical attempt with the same logical request identity.
    Construction requires an observer; there is no production noop fallback.
    """

    name = "observed"

    def __init__(
        self,
        transport: LLMTransport,
        *,
        usage_request: UsageRequest,
        provider_id: str,
        effective_model: str,
    ) -> None:
        self.transport = transport
        self.usage_request = usage_request
        self.provider_id = provider_id
        self.effective_model = effective_model

    async def completion(self, **kwargs: Any) -> Any:
        """Observe Chat without forwarding accounting objects to the SDK."""
        return await self._call("chat", self.transport.completion, kwargs)

    async def responses(self, **kwargs: Any) -> Any:
        """Observe Responses before conversion into Chat chunks."""
        return await self._call("responses", self.transport.responses, kwargs)

    async def _call(
        self,
        operation: Literal["chat", "responses"],
        dispatch: Callable[..., Awaitable[Any]],
        kwargs: dict[str, Any],
    ) -> Any:
        binding = take_binding(kwargs) or UsageBinding(
            self.usage_request,
            self.provider_id,
            self.effective_model,
        )
        return await observed_sdk(dispatch, binding, operation=operation)(**kwargs)
