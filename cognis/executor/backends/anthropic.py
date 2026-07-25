"""Executor-native Anthropic Messages backend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

from cognis.executor.inference_types import CognisInferenceRequest
from cognis.models.config import ModelInfo
from cognis.providers.llm.anthropic.integration import build_native_request
from cognis.providers.llm.anthropic.transport import AnthropicMessagesClient
from cognis.providers.llm.retry import with_llm_retry


class AnthropicMessagesExecutorBackend:
    """Run API-key Anthropic Messages requests from the selected executor."""

    name = "anthropic_messages"

    async def close(self) -> None:
        return None

    def _prepared(self, request: CognisInferenceRequest) -> tuple[Any, Any, Any, Any]:
        metadata = request.backend_metadata.get("anthropic_native")
        if not isinstance(metadata, dict):
            raise ValueError("Native Anthropic executor request lacks provider metadata")
        config = dict(metadata.get("config") or {})
        config["protocol"] = "anthropic_messages"
        config.setdefault("preset", "anthropic")
        provider = SimpleNamespace(
            provider_id=request.provider_id or "anthropic",
            location="executor",
            config=config,
        )
        model_info = ModelInfo.model_validate(
            metadata.get("model_info") or {"model_id": request.model}
        )
        api_key = request.request_kwargs.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("Native Anthropic executor request lacks API key")

        async def resolve_credential(_ref: str) -> str:
            return api_key

        context, payload, bundle = build_native_request(
            provider=provider,
            model=request.model,
            model_info=model_info,
            messages=request.messages,
            request_kwargs=request.request_kwargs,
            credential_ref="$credential:executor-anthropic-api-key",
        )
        configured_timeout = request.request_kwargs.get("timeout")
        timeout = (
            float(configured_timeout)
            if isinstance(configured_timeout, int | float)
            and not isinstance(configured_timeout, bool)
            and configured_timeout > 0
            else 120.0
        )
        return (
            AnthropicMessagesClient(resolve_credential, timeout=timeout),
            context,
            payload,
            bundle,
        )

    async def stream_complete(
        self, request: CognisInferenceRequest
    ) -> AsyncIterator[dict[str, Any]]:
        client, context, payload, bundle = self._prepared(request)
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        terminal_metadata: dict[str, Any] = {}
        async for chunk in client.stream(
            context,
            payload,
            bundle,
            provider_fingerprint=context.chain_id,
            model_fingerprint=request.model,
        ):
            envelope = chunk.get("anthropic_native_envelope")
            if isinstance(envelope, dict):
                terminal_metadata["anthropic_native_envelope"] = envelope
            native_events = chunk.get("anthropic_native_events")
            if isinstance(native_events, list):
                # Preserve native stream metadata across the executor RPC
                # boundary.  The controller owns interpretation and durable
                # replay; this backend must not filter provider events.
                yield {"anthropic_native_events": native_events}
            if isinstance(envelope, dict):
                continue
            raw_usage = chunk.get("usage")
            if isinstance(raw_usage, dict):
                usage = raw_usage
            raw_choices = chunk.get("choices")
            if not isinstance(raw_choices, list) or not raw_choices:
                continue
            first_choice = raw_choices[0]
            choice: dict[str, Any] = first_choice if isinstance(first_choice, dict) else {}
            raw_delta = choice.get("delta")
            delta: dict[str, Any] = raw_delta if isinstance(raw_delta, dict) else {}
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
            yield {
                "content": delta.get("content"),
                "tool_calls": delta.get("tool_calls"),
                "reasoning_content": delta.get("reasoning_content"),
                "reasoning": delta.get("reasoning"),
                "provider_thinking_blocks": delta.get("provider_thinking_blocks"),
                "provider_event_type": "anthropic_messages",
            }
        yield {
            "done": True,
            "usage": usage,
            "finish_reason": finish_reason,
            "backend_metadata": terminal_metadata,
        }

    async def generate(self, request: CognisInferenceRequest) -> dict[str, Any]:
        client, context, payload, bundle = self._prepared(request)
        configured_retries = request.request_kwargs.get("max_retries")
        max_retries = (
            configured_retries
            if isinstance(configured_retries, int) and not isinstance(configured_retries, bool)
            else 3
        )
        response = await with_llm_retry(
            client.complete,
            context,
            payload,
            bundle,
            provider_fingerprint=context.chain_id,
            model_fingerprint=request.model,
            max_retries=max_retries,
            operation=f"executor.anthropic_messages({request.model})",
        )
        return cast(dict[str, Any], response)
