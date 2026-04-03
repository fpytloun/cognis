"""Inference router — decouples LLM provider from executor provider.

When an LLM provider is configured with ``location="executor"``, the
inference router finds a matching executor with inference capability
and routes the ``llm.complete`` call over the WebSocket connection.

This avoids a circular dependency between ``LiteLLMProvider`` and
``CompositeExecutorProvider``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from cognis.core.executor_resolution import labels_match
from cognis.logging import get_logger
from cognis.providers.executor.websocket import (
    ExecutorDisconnectedError,
    WebSocketExecutorProvider,
)

_logger = get_logger(__name__)


class InferenceRouter:
    """Routes LLM inference requests to executor-side endpoints.

    Injected into ``LiteLLMProvider`` at construction time.  The LLM
    provider calls ``route_stream()`` when it detects a provider with
    ``location="executor"``.
    """

    def __init__(self, ws_provider: WebSocketExecutorProvider) -> None:
        self._ws_provider = ws_provider

    async def route_stream(
        self,
        messages: list[dict[str, Any]],
        model: str,
        executor_labels: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Route a streaming LLM request to a matching executor.

        Finds an executor with ``inference=True`` capability and matching
        labels, then streams ``llm.chunk`` / ``llm.done`` back.

        Yields dicts in the same format as ``LiteLLMProvider.stream_generate``:
        - ``{"choices": [{"delta": {"content": "..."}}]}`` for content chunks
        - ``{"error": "...", "mid_stream_failure": True}`` on failure
        """
        conn = await self._find_inference_executor(executor_labels)
        if conn is None:
            yield {
                "error": "No executor with inference capability found",
                "mid_stream_failure": True,
            }
            return

        request_id = uuid.uuid4().hex
        try:
            async for chunk in conn.llm_complete_stream(
                request_id=request_id,
                messages=messages,
                model=model,
                **kwargs,
            ):
                if chunk.get("error"):
                    yield {"error": chunk["error"], "mid_stream_failure": True}
                    return
                if chunk.get("done"):
                    # Final chunk with usage — yield as a completion marker
                    usage = chunk.get("usage", {})
                    yield {
                        "choices": [
                            {
                                "delta": {},
                                "finish_reason": chunk.get("finish_reason", "stop"),
                            }
                        ],
                        "usage": usage,
                    }
                    return
                # Regular content chunk — wrap in LiteLLM-compatible format
                yield {
                    "choices": [
                        {
                            "delta": {
                                "content": chunk.get("content"),
                                "tool_calls": chunk.get("tool_calls"),
                            },
                        }
                    ],
                }
        except ExecutorDisconnectedError:
            yield {
                "error": "Executor disconnected during inference",
                "mid_stream_failure": True,
            }

    async def route_generate(
        self,
        messages: list[dict[str, Any]],
        model: str,
        executor_labels: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Route a non-streaming LLM request to a matching executor.

        Collects all streaming chunks and returns a single response dict.
        """
        content_parts: list[str] = []
        tool_calls: list[Any] = []
        usage: dict[str, Any] = {}
        finish_reason = "stop"

        async for chunk in self.route_stream(
            messages=messages, model=model, executor_labels=executor_labels, **kwargs
        ):
            if chunk.get("mid_stream_failure"):
                raise RuntimeError(chunk.get("error", "Inference failed"))
            choices = chunk.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                if delta.get("content"):
                    content_parts.append(delta["content"])
                if delta.get("tool_calls"):
                    tool_calls.extend(delta["tool_calls"])
                fr = choices[0].get("finish_reason")
                if fr:
                    finish_reason = fr
            if chunk.get("usage"):
                usage = chunk["usage"]

        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "".join(content_parts) if content_parts else None,
                        "tool_calls": tool_calls or None,
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }

    async def _find_inference_executor(self, executor_labels: dict[str, str] | None) -> Any | None:
        """Find a connected executor with inference capability."""
        active = await self._ws_provider.list_active()
        for handle in active:
            if not handle.capabilities.inference:
                continue
            # Check label match if specified
            if executor_labels:
                # Handle metadata may contain labels
                handle_labels = handle.metadata.get("labels", {})
                if not labels_match(handle_labels, executor_labels):
                    continue
            # Found a matching executor
            try:
                return await self._ws_provider.get_executor(handle)
            except Exception:
                continue
        return None
