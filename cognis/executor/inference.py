"""Executor-side LiteLLM proxy for remote inference routing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import litellm


class InferenceHandler:
    """Proxy LLM requests from the controller through LiteLLM.

    The controller resolves provider configuration, credentials, and model
    routing. The executor simply runs the same LiteLLM call remotely so the
    network origin is the executor host instead of the controller.
    """

    async def close(self) -> None:
        """Close any background resources.

        The current implementation uses ``litellm`` directly and has nothing
        persistent to close, but the method keeps the shutdown path uniform.
        """

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        request_kwargs: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a completion through LiteLLM using controller-provided args."""
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        index = 0

        try:
            stream = await litellm.acompletion(
                model=model,
                messages=messages,
                stream=True,
                **request_kwargs,
            )
            async for chunk in stream:
                payload = dict(chunk)
                if payload.get("usage"):
                    usage = payload["usage"]
                choices = payload.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                content = delta.get("content")
                tool_calls = delta.get("tool_calls")
                reasoning_content = delta.get("reasoning_content")
                if content is None and tool_calls is None and reasoning_content is None:
                    continue
                yield {
                    "content": content,
                    "tool_calls": tool_calls,
                    "reasoning_content": reasoning_content,
                    "index": index,
                }
                index += 1
        except Exception as exc:
            yield {
                "done": True,
                "error": f"Inference error: {str(exc)[:500]}",
                "finish_reason": "error",
            }
            return

        yield {"done": True, "usage": usage, "finish_reason": finish_reason}

    async def generate(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        request_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a non-streaming completion through LiteLLM."""
        response = await litellm.acompletion(
            model=model,
            messages=messages,
            stream=False,
            **request_kwargs,
        )
        return response.model_dump()
