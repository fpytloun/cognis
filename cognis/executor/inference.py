"""Executor-side LLM inference handler.

Forwards ``llm.complete`` requests to a local OpenAI-compatible endpoint
(ollama, vllm, llama.cpp, LiteLLM proxy, etc.) and streams results back
as ``llm.chunk`` / ``llm.done`` notifications.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from cognis.models.tool import InferenceConfig

logger = logging.getLogger("cognis.executor.inference")


class InferenceHandler:
    """Handles LLM completion requests using a local endpoint."""

    def __init__(self, config: InferenceConfig) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.endpoint or "",
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def stream_complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a chat completion from the local endpoint.

        Yields dicts with ``content``, ``tool_calls``, ``index`` keys for
        each chunk, and a final dict with ``done=True``, ``usage``, and
        ``finish_reason``.
        """
        resolved_model = model or self.config.default_model
        if not resolved_model:
            yield {
                "done": True,
                "error": "No model specified and no default model configured",
                "finish_reason": "error",
            }
            return

        body: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        index = 0
        total_usage: dict[str, int] = {}
        finish_reason = "stop"

        try:
            async with self._client.stream("POST", "/v1/chat/completions", json=body) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    yield {
                        "done": True,
                        "error": f"LLM endpoint returned {response.status_code}: "
                        f"{error_body.decode()[:500]}",
                        "finish_reason": "error",
                    }
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk_data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # Extract usage if present (some providers send it in chunks)
                    if "usage" in chunk_data and chunk_data["usage"]:
                        total_usage = chunk_data["usage"]

                    choices = chunk_data.get("choices", [])
                    if not choices:
                        continue

                    choice = choices[0]
                    delta = choice.get("delta", {})
                    choice_finish = choice.get("finish_reason")
                    if choice_finish:
                        finish_reason = choice_finish

                    content = delta.get("content")
                    tool_calls = delta.get("tool_calls")

                    if content or tool_calls:
                        yield {
                            "content": content,
                            "tool_calls": tool_calls,
                            "index": index,
                        }
                        index += 1

        except httpx.ConnectError:
            yield {
                "done": True,
                "error": f"Cannot connect to inference endpoint: {self.config.endpoint}",
                "finish_reason": "error",
            }
            return
        except httpx.TimeoutException:
            yield {
                "done": True,
                "error": "Inference request timed out",
                "finish_reason": "error",
            }
            return
        except Exception as exc:
            yield {
                "done": True,
                "error": f"Inference error: {str(exc)[:500]}",
                "finish_reason": "error",
            }
            return

        # Final done message
        yield {
            "done": True,
            "usage": total_usage,
            "finish_reason": finish_reason,
        }
