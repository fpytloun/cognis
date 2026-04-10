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

from cognis.logging import get_logger
from cognis.models.config import ModelInfo

RESPONSES_MODE_ENV = "COGNIS_OPENAI_RESPONSES_MODE"

logger = get_logger(__name__)


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


def responses_request_kwargs(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Translate chat-completions-style kwargs into Responses-compatible kwargs."""

    filtered = dict(request_kwargs)
    filtered.pop("cognis_llm_api", None)
    response_format = filtered.pop("response_format", None)
    if response_format is not None and "text" not in filtered and "text_format" not in filtered:
        filtered["text"] = {
            "format": response_format
            if isinstance(response_format, dict)
            else {"type": str(response_format)}
        }
    if "max_tokens" in filtered and "max_output_tokens" not in filtered:
        filtered["max_output_tokens"] = filtered.pop("max_tokens")
    tools = filtered.get("tools")
    if isinstance(tools, list):
        filtered["tools"] = [_tool_to_responses_tool(tool) for tool in tools]
    return filtered


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
    try:
        async for raw_event in stream:
            event = _to_dict(raw_event)
            event_type = _normalize_event_type(str(event.get("type", "")).strip())
            if not event_type:
                event_type = _detect_synthetic_event_type(event, raw_event)
            state.note_event(event_type)
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    state.note_text_emitted(delta)
                    yield {"choices": [{"delta": {"content": delta}}]}
                continue
            if event_type == "response.output_text.done":
                text = event.get("text")
                if isinstance(text, str) and text:
                    final_text_chunk = state.final_text_delta(text)
                    if final_text_chunk is not None:
                        yield final_text_chunk
                continue
            if event_type in {"response.content_part.added", "response.content_part.done"}:
                part = event.get("part")
                part_text = _extract_part_text(part)
                if part_text:
                    part_chunk = state.final_text_delta(part_text)
                    if part_chunk is not None:
                        yield part_chunk
                continue
            if event_type == "response.output_item.added":
                item = _get_output_item(event)
                if item is None:
                    continue
                state.register_item(item)
                message_chunk = state.message_delta(item)
                if message_chunk is not None:
                    yield message_chunk
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
                message_chunk = state.finalize_message_item(item)
                if message_chunk is not None:
                    yield message_chunk
                final_chunk = state.finalize_item(item)
                if final_chunk is not None:
                    yield final_chunk
                continue
            if event_type in {"response.completed", "response.completed.synthetic"}:
                response_payload = _to_dict(event.get("response") or event)
                fallback_text = state.final_message_fallback(response_payload)
                if fallback_text is not None:
                    yield fallback_text
                state.completed_seen = True
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
                yield {
                    "error": str(message or "Responses stream failed"),
                    "mid_stream_failure": True,
                }
    finally:
        logger.debug(
            "Responses bridge stream summary",
            extra={
                "extra_data": {
                    "event_counts": dict(sorted(state.event_counts.items())),
                    "text_emissions": state.text_emissions,
                    "tool_call_emissions": state.tool_call_emissions,
                    "completed_fallback_used": state.completed_fallback_used,
                    "completed_seen": state.completed_seen,
                }
            },
        )


class _ResponsesStreamState:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._next_tool_index = 0
        self._emitted_text = ""
        self.event_counts: dict[str, int] = {}
        self.text_emissions = 0
        self.tool_call_emissions = 0
        self.completed_fallback_used = False
        self.completed_seen = False

    def note_event(self, event_type: str) -> None:
        self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1

    def note_text_emitted(self, text: str) -> None:
        self._emitted_text += text
        self.text_emissions += 1

    def register_item(self, item: dict[str, Any]) -> None:
        item_id = str(item.get("id") or item.get("call_id") or "")
        if not item_id:
            return
        index = self._next_tool_index
        self._next_tool_index += 1
        self._items[item_id] = {
            "call_id": normalize_tool_call_id(item.get("call_id"), item.get("id"), item_id),
            "name": str(item.get("name") or "unknown_tool"),
            "arguments": str(item.get("arguments") or ""),
            "emitted": 0,
            "index": index,
        }

    def message_delta(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if str(item.get("type")) != "message":
            return None
        text = _extract_message_item_text(item)
        if not text:
            return None
        self.note_text_emitted(text)
        return {"choices": [{"delta": {"content": text}}]}

    def finalize_message_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if str(item.get("type")) != "message":
            return None
        text = _extract_message_item_text(item)
        if not text:
            return None
        if self._emitted_text.endswith(text) or text == self._emitted_text:
            return None
        if text.startswith(self._emitted_text):
            delta = text[len(self._emitted_text) :]
            if not delta:
                return None
            self.note_text_emitted(delta)
            return {"choices": [{"delta": {"content": delta}}]}
        self.note_text_emitted(text)
        return {"choices": [{"delta": {"content": text}}]}

    def final_message_fallback(self, response_payload: dict[str, Any]) -> dict[str, Any] | None:
        fallback_text, _ = _extract_response_output(response_payload)
        if not fallback_text:
            return None
        if fallback_text == self._emitted_text:
            return None
        if fallback_text.startswith(self._emitted_text):
            delta = fallback_text[len(self._emitted_text) :]
            if not delta:
                return None
            self.note_text_emitted(delta)
            self.completed_fallback_used = True
            return {"choices": [{"delta": {"content": delta}}]}
        self.note_text_emitted(fallback_text)
        self.completed_fallback_used = True
        return {"choices": [{"delta": {"content": fallback_text}}]}

    def final_text_delta(self, text: str) -> dict[str, Any] | None:
        if not text:
            return None
        if text == self._emitted_text:
            return None
        if text.startswith(self._emitted_text):
            delta = text[len(self._emitted_text) :]
            if not delta:
                return None
            self.note_text_emitted(delta)
            return {"choices": [{"delta": {"content": delta}}]}
        self.note_text_emitted(text)
        return {"choices": [{"delta": {"content": text}}]}

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
                                "index": state["index"],
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
        self.tool_call_emissions += 1
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
                                "index": state["index"],
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
                                "index": state["index"],
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
            text = _extract_message_item_text(item)
            if text:
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
    """Extract usage from a Responses API payload and normalise to chat-completions keys.

    The Responses API uses ``input_tokens`` / ``output_tokens`` while the
    chat-completions API (and all downstream Cognis consumers such as
    ``StreamAccumulator``) expect ``prompt_tokens`` / ``completion_tokens``.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {
        "prompt_tokens": usage.get("prompt_tokens") or usage.get("input_tokens", 0),
        "completion_tokens": usage.get("completion_tokens") or usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def _extract_message_item_text(item: dict[str, Any]) -> str:
    text_parts: list[str] = []
    for part in item.get("content") or []:
        part_text = _extract_part_text(part)
        if part_text:
            text_parts.append(part_text)
    return "".join(text_parts)


def _extract_part_text(part: Any) -> str:
    if not isinstance(part, dict):
        return ""
    if part.get("type") not in {"output_text", "text", "input_text"}:
        return ""
    text = part.get("text")
    return text if isinstance(text, str) else ""


def _normalize_message_content(content: Any) -> Any:
    if isinstance(content, list):
        normalized: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "")
            if part_type in {"input_text", "input_image", "input_file"}:
                normalized.append(part)
                continue
            if part_type == "text":
                normalized.append({"type": "input_text", "text": str(part.get("text") or "")})
                continue
            if part_type == "image_url":
                image_url = part.get("image_url")
                detail = part.get("detail")
                if isinstance(image_url, dict):
                    url = image_url.get("url")
                    detail = image_url.get("detail", detail)
                else:
                    url = image_url
                if isinstance(url, str) and url:
                    item: dict[str, Any] = {"type": "input_image", "image_url": url}
                    if isinstance(detail, str) and detail:
                        item["detail"] = detail
                    normalized.append(item)
                continue
            if part_type == "file":
                file_data = part.get("file")
                if isinstance(file_data, dict):
                    item = {"type": "input_file"}
                    if isinstance(file_data.get("file_id"), str):
                        item["file_id"] = file_data["file_id"]
                    elif isinstance(file_data.get("file_url"), str):
                        item["file_url"] = file_data["file_url"]
                    if isinstance(file_data.get("filename"), str):
                        item["filename"] = file_data["filename"]
                    if len(item) > 1:
                        normalized.append(item)
                continue
        return normalized
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
        import warnings

        with warnings.catch_warnings():
            # LiteLLM's model_construct() can leave the ``usage`` field as a
            # raw dict instead of a ``ResponseAPIUsage`` instance, which
            # triggers a harmless Pydantic serialisation warning on
            # ``model_dump()``.  Suppress it here — the dict is still valid.
            warnings.filterwarnings("ignore", message=".*Pydantic serializer.*")
            dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _detect_synthetic_event_type(event: dict[str, Any], raw_event: Any) -> str:
    if "response" in event and isinstance(event.get("response"), dict):
        response = event["response"]
        if isinstance(response, dict) and response.get("status") in {
            "completed",
            "failed",
            "incomplete",
        }:
            return "response.completed.synthetic"
    if event.get("status") in {"completed", "failed", "incomplete"} and (
        "output" in event or "usage" in event
    ):
        return "response.completed.synthetic"
    return f"unknown:{type(raw_event).__name__}"


def _normalize_event_type(event_type: str) -> str:
    if not event_type:
        return ""
    if event_type.startswith("ResponsesAPIStreamEvents."):
        suffix = event_type.split(".", 1)[1].lower()
        for prefix in (
            "output_text_",
            "content_part_",
            "output_item_",
            "function_call_arguments_",
            "reasoning_summary_text_",
            "reasoning_summary_part_",
            "reasoning_text_",
            "refusal_",
        ):
            if suffix.startswith(prefix):
                head = prefix.rstrip("_")
                tail = suffix[len(prefix) :]
                return f"response.{head}.{tail.replace('_', '.')}"
        if suffix.startswith("response_"):
            return f"response.{suffix[len('response_') :].replace('_', '.')}"
        return f"response.{suffix.replace('_', '.')}"
    return event_type


def _tool_to_responses_tool(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        function = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if function is not None:
            converted = {
                "type": str(tool.get("type") or "function"),
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters")
                if isinstance(function.get("parameters"), dict)
                else {},
            }
            if "defer_loading" in function:
                converted["defer_loading"] = bool(function.get("defer_loading"))
            if "strict" in function:
                converted["strict"] = bool(function.get("strict"))
            return converted
    return dict(tool) if isinstance(tool, dict) else {"type": "function", "name": str(tool)}
