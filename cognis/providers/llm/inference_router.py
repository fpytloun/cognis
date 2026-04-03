"""Route LLM inference through matching remote executors."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from cognis.core.executor_resolution import labels_match
from cognis.providers.executor.websocket import ExecutorDisconnectedError, WebSocketExecutorProvider


class InferenceRouter:
    """Proxy provider calls through a selected executor."""

    def __init__(self, ws_provider: WebSocketExecutorProvider) -> None:
        self._ws_provider = ws_provider

    async def route_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        executor_labels: dict[str, str] | None = None,
        request_kwargs: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        conn = await self._find_executor(executor_labels)
        if conn is None:
            yield {"error": "No executor matches the provider selector", "mid_stream_failure": True}
            return

        try:
            async for chunk in conn.llm_complete_stream(
                request_id=uuid.uuid4().hex,
                messages=messages,
                model=model,
                request_kwargs=request_kwargs or {},
            ):
                if chunk.get("error"):
                    yield {"error": chunk["error"], "mid_stream_failure": True}
                    return
                if chunk.get("done"):
                    yield {
                        "choices": [
                            {"delta": {}, "finish_reason": chunk.get("finish_reason", "stop")}
                        ],
                        "usage": chunk.get("usage", {}),
                    }
                    return
                yield {
                    "choices": [
                        {
                            "delta": {
                                "content": chunk.get("content"),
                                "tool_calls": chunk.get("tool_calls"),
                                "reasoning_content": chunk.get("reasoning_content"),
                            }
                        }
                    ]
                }
        except ExecutorDisconnectedError:
            yield {"error": "Executor disconnected during inference", "mid_stream_failure": True}

    async def route_generate(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        executor_labels: dict[str, str] | None = None,
        request_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content_parts: list[str] = []
        tool_calls: list[Any] = []
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        async for chunk in self.route_stream(
            messages=messages,
            model=model,
            executor_labels=executor_labels,
            request_kwargs=request_kwargs,
        ):
            if chunk.get("mid_stream_failure"):
                raise RuntimeError(chunk.get("error", "Inference failed"))
            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content"):
                    content_parts.append(delta["content"])
                if delta.get("tool_calls"):
                    tool_calls.extend(delta["tool_calls"])
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
            if chunk.get("usage"):
                usage = chunk["usage"]
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "".join(content_parts) or None,
                        "tool_calls": tool_calls or None,
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }

    async def _find_executor(self, executor_labels: dict[str, str] | None) -> Any | None:
        active = await self._ws_provider.list_active()
        for handle in active:
            labels = handle.metadata.get("labels", {}) if handle.metadata else {}
            if executor_labels and not labels_match(labels, executor_labels):
                continue
            try:
                return await self._ws_provider.get_executor(handle)
            except Exception:
                continue
        return None
