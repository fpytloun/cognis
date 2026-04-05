"""OpenAI Responses API bridge helpers.

This module keeps Cognis' internal transcript/tool model canonical while
translating OpenAI Responses payloads to and from the legacy chat-like shapes
expected by the rest of the controller.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

from cognis.models.config import ModelInfo

RESPONSES_MODE_ENV = "COGNIS_OPENAI_RESPONSES_MODE"


def should_use_openai_responses(
    *,
    model: str,
    model_info: ModelInfo,
    rollout_mode: str,
) -> bool:
    normalized_name = normalize_openai_model_name(model)
    is_openai_family = normalized_name.startswith(("gpt-", "o1", "o3", "o4"))
    if rollout_mode == "off":
        return False
    if rollout_mode == "on":
        return is_openai_family
    return is_openai_family and bool(model_info.supports_responses_api)


def normalize_openai_model_name(model_name: str) -> str:
    lowered = model_name.lower()
    for prefix in ("litellm_proxy/", "openai/", "azure/", "openai_compatible/"):
        if lowered.startswith(prefix):
            return lowered[len(prefix) :]
    return lowered


def messages_to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate canonical Cognis/OpenAI-chat messages into Responses input."""

    items: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = str(message.get("role", "user"))
        content = message.get("content")
        if role in {"system", "user", "assistant"} and not message.get("tool_calls"):
            items.append(
                {
                    "role": "developer" if role == "system" else role,
                    "content": _normalize_message_content(content),
                }
            )
            continue
        if role == "assistant":
            normalized_content = _normalize_message_content(content)
            if normalized_content not in (None, "", []):
                items.append({"role": "assistant", "content": normalized_content})
            for tool_index, tool_call in enumerate(message.get("tool_calls") or []):
                function = tool_call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": normalize_tool_call_id(
                            tool_call.get("id"), tool_call.get("id"), f"{index}:{tool_index}"
                        ),
                        "name": str(function.get("name", "unknown_tool")),
                        "arguments": str(function.get("arguments", "{}")),
                    }
                )
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": normalize_tool_call_id(
                        message.get("tool_call_id"), message.get("tool_call_id"), str(index)
                    ),
                    "output": content if isinstance(content, str) else json.dumps(content),
                }
            )
            continue
        items.append({"role": role, "content": _normalize_message_content(content)})
    return items


def normalize_tool_call_id(
    call_id: str | None, item_id: str | None, fallback_seed: str | int
) -> str:
    """Return a stable tool call id across controller/executor normalization."""

    if isinstance(call_id, str) and call_id.strip():
        return call_id
    if isinstance(item_id, str) and item_id.strip():
        return item_id
    suffix = hashlib.sha1(str(fallback_seed).encode()).hexdigest()[:10]
    return f"resp_call_{suffix}"


def responses_to_chat_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Responses API payload into chat-completions-like shape."""

    message_content, tool_calls = _extract_response_output(payload)
    normalized: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": message_content or None,
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": _extract_finish_reason(payload),
            }
        ],
        "usage": _extract_usage(payload),
    }
    return normalized


async def responses_stream_to_chat_chunks(
    stream: AsyncIterator[Any],
) -> AsyncIterator[dict[str, Any]]:
    """Normalize Responses streaming events into chat-like delta chunks."""

    state = _ResponsesStreamState()
    async for raw_event in stream:
        event = _to_dict(raw_event)
        event_type = str(event.get("type", ""))
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                yield {"choices": [{"delta": {"content": delta}}]}
            continue
        if event_type == "response.output_item.added":
            item = _get_output_item(event)
            if item is None:
                continue
            state.register_item(item)
            initial_chunk = state.initial_tool_delta(item)
            if initial_chunk is not None:
                yield initial_chunk
            continue
        if event_type == "response.function_call_arguments.delta":
            chunk = state.arguments_delta(event)
            if chunk is not None:
                yield chunk
            continue
        if event_type == "response.output_item.done":
            item = _get_output_item(event)
            if item is None:
                continue
            final_chunk = state.finalize_item(item)
            if final_chunk is not None:
                yield final_chunk
            continue
        if event_type == "response.completed":
            response_payload = _to_dict(event.get("response") or {})
            yield {
                "choices": [
                    {"delta": {}, "finish_reason": _extract_finish_reason(response_payload)}
                ],
                "usage": _extract_usage(response_payload),
            }
            continue
        if event_type == "response.failed":
            error = event.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            yield {"error": str(message or "Responses stream failed"), "mid_stream_failure": True}


class _ResponsesStreamState:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    def register_item(self, item: dict[str, Any]) -> None:
        item_id = str(item.get("id") or item.get("call_id") or "")
        if not item_id:
            return
        self._items[item_id] = {
            "call_id": normalize_tool_call_id(item.get("call_id"), item.get("id"), item_id),
            "name": str(item.get("name") or "unknown_tool"),
            "arguments": str(item.get("arguments") or ""),
            "emitted": 0,
        }

    def initial_tool_delta(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if str(item.get("type")) != "function_call":
            return None
        item_id = str(item.get("id") or item.get("call_id") or "")
        state = self._items.get(item_id)
        if state is None:
            return None
        chunk = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": state["call_id"],
                                "function": {"name": state["name"]},
                            }
                        ]
                    }
                }
            ]
        }
        if state["arguments"]:
            chunk["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] = state[
                "arguments"
            ]
            state["emitted"] = len(state["arguments"])
        return chunk

    def arguments_delta(self, event: dict[str, Any]) -> dict[str, Any] | None:
        item_id = str(event.get("item_id") or "")
        state = self._items.get(item_id)
        delta = event.get("delta")
        if state is None or not isinstance(delta, str) or not delta:
            return None
        state["arguments"] += delta
        state["emitted"] += len(delta)
        return {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": state["call_id"],
                                "function": {"arguments": delta},
                            }
                        ]
                    }
                }
            ]
        }

    def finalize_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if str(item.get("type")) != "function_call":
            return None
        item_id = str(item.get("id") or item.get("call_id") or "")
        state = self._items.get(item_id)
        if state is None:
            return None
        final_arguments = str(item.get("arguments") or "")
        if len(final_arguments) <= int(state.get("emitted", 0)):
            return None
        delta = final_arguments[int(state["emitted"]) :]
        state["arguments"] = final_arguments
        state["emitted"] = len(final_arguments)
        return {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": state["call_id"],
                                "function": {"arguments": delta},
                            }
                        ]
                    }
                }
            ]
        }


def _extract_response_output(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for index, item in enumerate(payload.get("output") or []):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", ""))
        if item_type == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        content_parts.append(text)
        elif item_type == "function_call":
            tool_calls.append(
                {
                    "id": normalize_tool_call_id(item.get("call_id"), item.get("id"), index),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or "unknown_tool"),
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }
            )
    if not content_parts and isinstance(payload.get("output_text"), str):
        content_parts.append(str(payload["output_text"]))
    return "".join(content_parts), tool_calls


def _extract_finish_reason(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "completed")
    if status in {"completed", "incomplete"}:
        return "stop"
    if status == "failed":
        return "error"
    return "stop"


def _extract_usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage")
    return usage if isinstance(usage, dict) else {}


def _normalize_message_content(content: Any) -> Any:
    if isinstance(content, list):
        return content
    if content is None:
        return ""
    return content


def _get_output_item(event: dict[str, Any]) -> dict[str, Any] | None:
    item = event.get("item") or event.get("output_item")
    return item if isinstance(item, dict) else None


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}
