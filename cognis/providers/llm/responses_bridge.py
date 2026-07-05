"""OpenAI Responses API bridge helpers.

This module keeps Cognis' internal transcript/tool model canonical while
translating OpenAI Responses payloads to and from the legacy chat-like shapes
expected by the rest of the controller.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from cognis.json_stream import merge_incremental_json_fragment
from cognis.logging import get_logger
from cognis.models.config import ModelInfo
from cognis.providers.llm.errors import classify_response_failure

RESPONSES_MODE_ENV = "COGNIS_OPENAI_RESPONSES_MODE"
RESPONSES_TOOL_CALL_TYPES = frozenset({"function_call", "apply_patch_call", "custom_tool_call"})
OUTPUT_TEXT_DELTA_DEDUPE_MIN_OVERLAP = 80
OUTPUT_TEXT_DELTA_DEDUPE_MAX_SCAN = 4096

logger = get_logger(__name__)

_NATIVE_APPLY_PATCH_OPERATION_TYPES = {"create_file", "update_file", "delete_file"}
APPLY_PATCH_FREEFORM_LARK_GRAMMAR = """start: begin_patch hunk+ end_patch
begin_patch: "*** Begin Patch" NEWLINE
end_patch: "*** End Patch" NEWLINE?
hunk: add_file | delete_file | update_file
add_file: "*** Add File: " PATH NEWLINE add_line+
delete_file: "*** Delete File: " PATH NEWLINE
update_file: "*** Update File: " PATH NEWLINE change_line*
add_line: "+" /[^\n]*/ NEWLINE
change_line: /[^\n]*/ NEWLINE
PATH: /[^\n]+/
%import common.NEWLINE
"""


@dataclass(slots=True)
class NormalizedResponseEnvelope:
    """Internal normalized representation of a Responses API payload."""

    content: str = ""
    reasoning_content: str = ""
    reasoning_summary: str = ""
    refusal: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    status: str = "completed"
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    content_source: str | None = None
    reasoning_source: str | None = None


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
    for prefix in ("litellm_proxy/", "openai/", "azure/", "openai_compatible/", "chatgpt/"):
        if lowered.startswith(prefix):
            return lowered[len(prefix) :]
    return lowered


def split_messages_for_responses(
    messages: list[dict[str, Any]],
    cache_breakpoint_index: Any,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Split canonical messages into (instructions, remaining_tail).

    For the OpenAI Responses API, the immutable prompt prefix is carried in
    the top-level ``instructions`` field rather than embedded in ``input``.
    That keeps the immutable prefix authoritative server-side (hosted
    personas like the Codex CLI system prompt cannot override it), keeps the
    ``input`` array stable to maximize auto-prefix cache hits, and shrinks
    per-turn payload size.

    Returns ``(None, messages)`` when no valid breakpoint is available so
    callers can fall back to the legacy input-only shape.
    """

    cache_breakpoint_index = _responses_instruction_breakpoint_index(cache_breakpoint_index)
    if cache_breakpoint_index is None:
        return None, messages
    if cache_breakpoint_index >= len(messages):
        return None, messages

    prefix_slice = messages[: cache_breakpoint_index + 1]
    tail_slice = messages[cache_breakpoint_index + 1 :]

    instructions_parts: list[str] = []
    for entry in prefix_slice:
        if not isinstance(entry, dict):
            continue
        if entry.get("role") != "system":
            # Non-system messages inside the prefix window cannot be projected
            # into `instructions`; fall back to legacy shape for safety.
            return None, messages
        content = entry.get("content")
        text = _extract_text_content(content)
        if text:
            instructions_parts.append(text)

    if not instructions_parts:
        return None, messages

    instructions = "\n\n".join(instructions_parts)
    return instructions, tail_slice


def _responses_instruction_breakpoint_index(value: Any) -> int | None:
    """Return the immutable-prefix breakpoint for Responses instructions."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, list):
        return None
    valid_indexes: list[int] = []
    for item in value:
        raw_index: Any
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            raw_index = item
        elif isinstance(item, dict):
            raw_index = item.get("index")
        else:
            continue
        if isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index >= 0:
            valid_indexes.append(raw_index)
    return min(valid_indexes) if valid_indexes else None


def split_system_messages_for_responses(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Move leading system messages into Responses ``instructions``.

    This is the conservative non-cache variant used for providers that require
    top-level instructions but do not need an immutable cache breakpoint.  Only
    the contiguous leading system prefix is moved; any later system messages are
    left in the input tail to preserve transcript order.
    """

    instructions_parts: list[str] = []
    tail_start = 0
    for entry in messages:
        if not isinstance(entry, dict) or entry.get("role") != "system":
            break
        content = entry.get("content")
        text = _extract_text_content(content)
        if text:
            instructions_parts.append(text)
        tail_start += 1

    if not instructions_parts:
        return None, messages

    return "\n\n".join(instructions_parts), messages[tail_start:]


def _extract_text_content(content: Any) -> str:
    """Extract plain text from a message content field (string or blocks)."""

    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        collected: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                collected.append(text.strip())
        return "\n\n".join(collected)
    return ""


def messages_to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate canonical Cognis/OpenAI-chat messages into Responses input."""

    items: list[dict[str, Any]] = []
    native_apply_patch_call_ids: set[str] = set()
    custom_tool_call_ids: set[str] = set()
    function_call_ids: set[str] = set()
    for index, message in enumerate(messages):
        role = str(message.get("role", "user"))
        content = message.get("content")
        raw_responses_output_items = message.get("_responses_output_items")
        if role == "assistant" and isinstance(raw_responses_output_items, list):
            normalized_content = _normalize_message_content(
                content,
                allow_native_attachments=False,
            )
            has_content = normalized_content not in (None, "", [])
            has_raw_message_item = any(
                isinstance(item, dict) and str(item.get("type") or "") == "message"
                for item in raw_responses_output_items
            )
            reconstructed_content_emitted = False

            for raw_item in raw_responses_output_items:
                if isinstance(raw_item, dict):
                    raw_type = str(raw_item.get("type") or "")
                    if (
                        not has_raw_message_item
                        and not reconstructed_content_emitted
                        and has_content
                        and raw_type in RESPONSES_TOOL_CALL_TYPES
                    ):
                        items.append({"role": "assistant", "content": normalized_content})
                        reconstructed_content_emitted = True
                    if raw_type == "function_call":
                        call_id = raw_item.get("call_id")
                        if isinstance(call_id, str) and call_id:
                            function_call_ids.add(call_id)
                    elif raw_type == "custom_tool_call":
                        call_id = raw_item.get("call_id")
                        if isinstance(call_id, str) and call_id:
                            custom_tool_call_ids.add(call_id)
                    elif raw_type == "apply_patch_call":
                        call_id = raw_item.get("call_id")
                        if isinstance(call_id, str) and call_id:
                            native_apply_patch_call_ids.add(call_id)
                    items.append(dict(raw_item))
            if not has_raw_message_item and has_content and not reconstructed_content_emitted:
                items.append({"role": "assistant", "content": normalized_content})
                reconstructed_content_emitted = True
            for tool_index, tool_call in enumerate(message.get("tool_calls") or []):
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") or {}
                if not isinstance(function, dict):
                    function = {}
                call_id = normalize_tool_call_id(
                    tool_call.get("id"), tool_call.get("id"), f"{index}:{tool_index}"
                )
                if (
                    call_id in function_call_ids
                    or call_id in custom_tool_call_ids
                    or call_id in native_apply_patch_call_ids
                ):
                    continue
                function_name = str(function.get("name", "unknown_tool"))
                arguments = str(function.get("arguments", "{}"))
                native_operation = _extract_native_apply_patch_operation(function_name, arguments)
                if native_operation is not None:
                    items.append(
                        {
                            "type": "apply_patch_call",
                            "call_id": call_id,
                            "status": "completed",
                            "operation": native_operation,
                        }
                    )
                    native_apply_patch_call_ids.add(call_id)
                else:
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": function_name,
                            "arguments": arguments,
                        }
                    )
                    function_call_ids.add(call_id)
            continue
        if role in {"system", "user", "assistant"} and not message.get("tool_calls"):
            items.append(
                {
                    "role": role,
                    "content": _normalize_message_content(
                        content,
                        allow_native_attachments=role == "user",
                    ),
                }
            )
            continue
        if role == "assistant":
            normalized_content = _normalize_message_content(
                content,
                allow_native_attachments=False,
            )
            if normalized_content not in (None, "", []):
                items.append({"role": "assistant", "content": normalized_content})
            for tool_index, tool_call in enumerate(message.get("tool_calls") or []):
                function = tool_call.get("function") or {}
                call_id = normalize_tool_call_id(
                    tool_call.get("id"), tool_call.get("id"), f"{index}:{tool_index}"
                )
                function_name = str(function.get("name", "unknown_tool"))
                arguments = str(function.get("arguments", "{}"))
                native_operation = _extract_native_apply_patch_operation(function_name, arguments)
                if native_operation is not None:
                    native_apply_patch_call_ids.add(call_id)
                    items.append(
                        {
                            "type": "apply_patch_call",
                            "call_id": call_id,
                            "status": "completed",
                            "operation": native_operation,
                        }
                    )
                    continue
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": function_name,
                        "arguments": arguments,
                    }
                )
                function_call_ids.add(call_id)
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                logger.warning(
                    "Dropping tool message without tool_call_id for Responses API",
                    extra={"extra_data": {"message_index": index}},
                )
                continue
            normalized_call_id = normalize_tool_call_id(call_id, call_id, str(index))
            output = content if isinstance(content, str) else json.dumps(content)
            if normalized_call_id in native_apply_patch_call_ids:
                items.append(
                    {
                        "type": "apply_patch_call_output",
                        "call_id": normalized_call_id,
                        "status": "failed" if bool(message.get("_tool_is_error")) else "completed",
                        "output": output,
                    }
                )
            elif normalized_call_id in function_call_ids:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": normalized_call_id,
                        "output": output,
                    }
                )
            elif normalized_call_id in custom_tool_call_ids:
                items.append(
                    {
                        "type": "custom_tool_call_output",
                        "call_id": normalized_call_id,
                        "output": output,
                    }
                )
            else:
                logger.warning(
                    "Dropping orphan tool output for Responses API",
                    extra={
                        "extra_data": {
                            "message_index": index,
                            "tool_call_id": normalized_call_id,
                        }
                    },
                )
            continue
        items.append(
            {
                "role": role,
                "content": _normalize_message_content(
                    content,
                    allow_native_attachments=role == "user",
                ),
            }
        )
    return items


def _extract_native_apply_patch_operation(
    function_name: str, arguments: str
) -> dict[str, Any] | None:
    if function_name != "apply_patch":
        return None
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    operation = parsed.get("operation")
    return _normalize_native_apply_patch_operation(operation)


def _normalize_native_apply_patch_operation(operation: Any) -> dict[str, Any] | None:
    """Return a Responses-safe native apply_patch operation, or None.

    The OpenAI Responses API validates prior native ``apply_patch_call`` items
    before the model runs. Replaying incomplete operations such as
    ``{"operation": {}}`` poisons the transcript with request-time 400s, so
    only well-formed operations are projected back into native history.
    """

    if not isinstance(operation, dict):
        return None
    operation_type = str(operation.get("type") or "").strip()
    path = str(operation.get("path") or "").strip()
    if operation_type not in _NATIVE_APPLY_PATCH_OPERATION_TYPES or not path:
        return None
    normalized = dict(operation)
    normalized["type"] = operation_type
    normalized["path"] = path
    return normalized


def responses_request_kwargs(
    request_kwargs: dict[str, Any],
    *,
    default_reasoning_summary: str | None = "auto",
    default_text_verbosity: str | None = None,
    include_encrypted_reasoning: bool = False,
) -> dict[str, Any]:
    """Translate chat-completions-style kwargs into Responses-compatible kwargs."""

    filtered = dict(request_kwargs)
    filtered.pop("cognis_llm_api", None)
    apply_patch_tool_type = (
        str(filtered.pop("cognis_openai_apply_patch_tool_type", "") or "").strip().lower()
    )
    reasoning_effort = filtered.pop("reasoning_effort", None)
    response_format = filtered.pop("response_format", None)
    if response_format is not None and "text" not in filtered and "text_format" not in filtered:
        normalized_format = _normalize_response_format(response_format)
        if normalized_format is not None:
            filtered["text"] = {"format": normalized_format}
    if "max_tokens" in filtered and "max_output_tokens" not in filtered:
        filtered["max_output_tokens"] = filtered.pop("max_tokens")
    if "max_completion_tokens" in filtered and "max_output_tokens" not in filtered:
        filtered["max_output_tokens"] = filtered.pop("max_completion_tokens")
    reasoning = filtered.get("reasoning")
    normalized_reasoning = dict(reasoning) if isinstance(reasoning, dict) else {}
    if isinstance(reasoning_effort, str) and reasoning_effort.strip():
        normalized_reasoning["effort"] = reasoning_effort.strip()
    if normalized_reasoning and "summary" not in normalized_reasoning:
        summary_default = (default_reasoning_summary or "auto").strip().lower()
        if summary_default and summary_default != "none":
            # Reasoning summaries are opt-in on the Responses API. Enable them
            # only when model/provider metadata says the model supports them.
            normalized_reasoning["summary"] = summary_default
    if normalized_reasoning:
        filtered["reasoning"] = normalized_reasoning
    if normalized_reasoning or include_encrypted_reasoning:
        include = filtered.get("include")
        if isinstance(include, list):
            if "reasoning.encrypted_content" not in include:
                filtered["include"] = [*include, "reasoning.encrypted_content"]
        elif include is None:
            filtered["include"] = ["reasoning.encrypted_content"]
    verbosity_default = (default_text_verbosity or "").strip().lower()
    if verbosity_default and "text" not in filtered and "text_format" not in filtered:
        filtered["text"] = {"verbosity": verbosity_default}
    elif verbosity_default and isinstance(filtered.get("text"), dict):
        text = dict(filtered["text"])
        text.setdefault("verbosity", verbosity_default)
        filtered["text"] = text
    tools = filtered.get("tools")
    if isinstance(tools, list):
        filtered["tools"] = [
            _tool_to_responses_tool(tool, apply_patch_tool_type=apply_patch_tool_type)
            for tool in tools
        ]
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


def _is_responses_tool_call_item(item: dict[str, Any]) -> bool:
    return str(item.get("type")) in {"function_call", "apply_patch_call", "custom_tool_call"}


def _tool_call_item_name(item: dict[str, Any]) -> str:
    if str(item.get("type")) == "apply_patch_call":
        return "apply_patch"
    return str(item.get("name") or "unknown_tool")


def _tool_call_item_arguments(item: dict[str, Any]) -> str:
    if str(item.get("type")) == "apply_patch_call":
        operation = _normalize_native_apply_patch_operation(item.get("operation"))
        if operation is None:
            return ""
        patch_text = _native_apply_patch_operation_to_patch_text(operation)
        if patch_text is not None:
            return json.dumps({"patchText": patch_text})
        if str(operation.get("type") or "").strip() == "update_file":
            return "[patch body omitted]"
        return ""
    if str(item.get("type")) == "custom_tool_call":
        if not item.get("input"):
            return ""
        if _tool_call_item_name(item) == "apply_patch":
            patch_text = item.get("input")
            return json.dumps({"patchText": patch_text if isinstance(patch_text, str) else ""})
        raw_input = item.get("input")
        if isinstance(raw_input, str):
            return json.dumps({"input": raw_input})
        if raw_input is not None:
            return json.dumps({"input": raw_input})
        return "{}"
    return str(item.get("arguments") or "")


def _custom_tool_input_to_arguments(tool_name: str, input_value: str) -> str:
    if tool_name == "apply_patch":
        return json.dumps({"patchText": input_value})
    return json.dumps({"input": input_value})


def _custom_tool_input_delta_to_arguments_delta(tool_name: str, input_delta: str) -> str:
    if tool_name != "apply_patch":
        return ""
    return json.dumps({"patchText": input_delta})[:-2]


def _native_apply_patch_operation_to_patch_text(operation: dict[str, Any]) -> str | None:
    operation_type = str(operation.get("type") or "").strip()
    path = str(operation.get("path") or "").strip()
    if operation_type not in _NATIVE_APPLY_PATCH_OPERATION_TYPES or not path:
        return None
    if operation_type == "delete_file":
        return f"*** Begin Patch\n*** Delete File: {path}\n*** End Patch\n"
    if operation_type == "create_file":
        content = str(operation.get("content") or "")
        lines = content.splitlines() or [""]
        added = "\n".join(f"+{line}" for line in lines)
        return f"*** Begin Patch\n*** Add File: {path}\n{added}\n*** End Patch\n"
    if operation_type == "update_file":
        # Native Responses update operations do not carry a canonical unified
        # patch body. Keep them non-dispatchable; _tool_call_item_arguments()
        # emits a conspicuous marker string instead of executable patch JSON.
        return None
    return None


def responses_to_chat_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Responses API payload into chat-completions-like shape."""

    envelope = _extract_response_envelope(payload)
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": envelope.content or None,
                    "tool_calls": envelope.tool_calls or None,
                    "reasoning_content": envelope.reasoning_content or None,
                    "reasoning": envelope.reasoning_summary or None,
                    "refusal": envelope.refusal or None,
                },
                "finish_reason": envelope.finish_reason,
            }
        ],
        "usage": envelope.usage,
        "response_status": envelope.status,
    }


async def responses_stream_to_chat_chunks(
    stream: AsyncIterator[Any],
    *,
    dedupe_output_text_delta: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Normalize Responses streaming events into chat-like delta chunks."""

    state = _ResponsesStreamState()
    last_error_event_details: dict[str, Any] | None = None
    try:
        async for raw_event in stream:
            event = _to_dict(raw_event)
            event_type = _normalize_event_type(str(event.get("type", "")).strip())
            if not event_type:
                event_type = _detect_synthetic_event_type(event, raw_event)
            state.note_event(event_type)
            provider_liveness_chunk: dict[str, Any] = {
                "provider_event": "responses",
                "provider_event_type": event_type or "unknown",
            }
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    text_chunk = state.output_text_delta(
                        event,
                        delta,
                        dedupe_cumulative=dedupe_output_text_delta,
                    )
                    if text_chunk is not None:
                        text_chunk.update(provider_liveness_chunk)
                        yield text_chunk
                        continue
                    else:
                        provider_liveness_chunk["suppressed_output_text_delta"] = True
                        provider_liveness_chunk["suppressed_output_text_delta_chars"] = len(delta)
                        provider_liveness_chunk["active_output_item_type"] = (
                            state.active_item_type()
                        )
                        yield provider_liveness_chunk
                        continue
                else:
                    yield provider_liveness_chunk
                continue
            if event_type == "response.output_text.done":
                text = event.get("text")
                if isinstance(text, str) and text:
                    final_text_chunk = state.output_text_done(
                        event,
                        text,
                    )
                    if final_text_chunk is not None:
                        final_text_chunk.update(provider_liveness_chunk)
                        yield final_text_chunk
                    else:
                        provider_liveness_chunk["suppressed_output_text_delta"] = True
                        provider_liveness_chunk["suppressed_output_text_delta_chars"] = len(text)
                        provider_liveness_chunk["active_output_item_type"] = (
                            state.active_item_type()
                        )
                        yield provider_liveness_chunk
                else:
                    yield provider_liveness_chunk
                continue
            if event_type in {"response.content_part.added", "response.content_part.done"}:
                part = event.get("part")
                part_text = _extract_content_part_text(part)
                if part_text:
                    part_chunk = state.content_part_text(
                        event,
                        part_text,
                    )
                    if part_chunk is not None:
                        part_chunk.update(provider_liveness_chunk)
                        yield part_chunk
                    else:
                        provider_liveness_chunk["suppressed_content_part_text"] = True
                        provider_liveness_chunk["suppressed_content_part_text_chars"] = len(
                            part_text
                        )
                        provider_liveness_chunk["active_output_item_type"] = (
                            state.active_item_type()
                        )
                        yield provider_liveness_chunk
                else:
                    yield provider_liveness_chunk
                continue
            if event_type in {
                "response.reasoning_text.delta",
                "response.reasoning_text.done",
            }:
                part_text = _extract_text_value(
                    event.get("delta") or event.get("text") or event.get("part")
                )
                if part_text:
                    if event_type == "response.reasoning_text.delta":
                        reasoning_chunk = state.streamed_text_delta(
                            part_text, field="reasoning_content"
                        )
                    else:
                        reasoning_chunk = state.final_text_delta(
                            part_text, field="reasoning_content"
                        )
                    if reasoning_chunk is not None:
                        reasoning_chunk.update(provider_liveness_chunk)
                        yield reasoning_chunk
                else:
                    yield provider_liveness_chunk
                if event_type == "response.reasoning_text.done":
                    state.reset_segment("reasoning_content")
                continue
            if event_type in {
                "response.reasoning_summary_text.delta",
                "response.reasoning_summary_text.done",
                "response.reasoning_summary_part.added",
                "response.reasoning_summary_part.done",
            }:
                part_text = _extract_text_value(
                    event.get("delta") or event.get("text") or event.get("part")
                )
                # Emit the part boundary marker BEFORE any text carried on the
                # same event: the consumer opens a new thinking block on the
                # boundary chunk, so text yielded first would be grouped into
                # the previous block.
                if event_type == "response.reasoning_summary_part.added":
                    part = event.get("part") or {}
                    provider_title = (
                        _extract_text_value(part.get("title")) if isinstance(part, dict) else None
                    )
                    # A new part starts a fresh dedup segment so identical
                    # repeated summary parts are not swallowed.
                    state.reset_segment("reasoning")
                    yield {
                        **provider_liveness_chunk,
                        "choices": [
                            {
                                "delta": {
                                    "reasoning_part_boundary": {
                                        "part_index": state.reasoning_part_count,
                                        "title": provider_title or None,
                                        "complete": False,
                                    }
                                }
                            }
                        ],
                    }
                    state.reasoning_part_count += 1
                if part_text:
                    if event_type == "response.reasoning_summary_text.delta":
                        reasoning_chunk = state.streamed_text_delta(part_text, field="reasoning")
                    else:
                        reasoning_chunk = state.final_text_delta(part_text, field="reasoning")
                    if reasoning_chunk is not None:
                        reasoning_chunk.update(provider_liveness_chunk)
                        yield reasoning_chunk
                if event_type == "response.reasoning_summary_part.done":
                    yield {
                        **provider_liveness_chunk,
                        "choices": [
                            {
                                "delta": {
                                    "reasoning_part_boundary": {
                                        "part_index": max(0, state.reasoning_part_count - 1),
                                        "title": None,
                                        "complete": True,
                                    }
                                }
                            }
                        ],
                    }
                    state.reset_segment("reasoning")
                if not part_text and event_type not in {
                    "response.reasoning_summary_part.added",
                    "response.reasoning_summary_part.done",
                }:
                    yield provider_liveness_chunk
                continue
            if event_type in {"response.refusal.delta", "response.refusal.done"}:
                refusal_text = _extract_text_value(
                    event.get("delta") or event.get("text") or event.get("part")
                )
                if refusal_text:
                    if event_type == "response.refusal.delta":
                        refusal_chunk = state.streamed_text_delta(refusal_text, field="refusal")
                    else:
                        refusal_chunk = state.final_text_delta(refusal_text, field="refusal")
                    if refusal_chunk is not None:
                        refusal_chunk.update(provider_liveness_chunk)
                        yield refusal_chunk
                else:
                    yield provider_liveness_chunk
                continue
            if event_type == "response.output_item.added":
                item = _get_output_item(event)
                if item is None:
                    continue
                _, is_new = state.activate_item(item)
                message_chunk = state.message_delta(item, emit_initial=is_new)
                if message_chunk is not None:
                    message_chunk.update(provider_liveness_chunk)
                    yield message_chunk
                initial_chunk = state.initial_tool_delta(item, emit_name=True)
                if initial_chunk is not None:
                    initial_chunk.update(provider_liveness_chunk)
                    yield initial_chunk
                continue
            if event_type == "response.function_call_arguments.delta":
                chunk = state.arguments_delta(event)
                if chunk is not None:
                    chunk.update(provider_liveness_chunk)
                    yield chunk
                else:
                    yield provider_liveness_chunk
                continue
            if event_type == "response.function_call_arguments.done":
                item = _get_output_item(event)
                if item is None:
                    # The real Responses API sends this event with item_id +
                    # arguments and NO nested item. Synthesize one so argument
                    # recovery works when deltas were dropped before item
                    # registration (otherwise recovery depends solely on
                    # output_item.done arriving intact).
                    item_id = str(event.get("item_id") or "").strip()
                    done_arguments = event.get("arguments")
                    if item_id and isinstance(done_arguments, str) and done_arguments:
                        item = {
                            "type": "function_call",
                            "id": item_id,
                            "arguments": done_arguments,
                        }
                if item is not None:
                    state.register_item(item)
                    initial_chunk = state.initial_tool_delta(item, emit_name=True)
                    if initial_chunk is not None:
                        initial_chunk.update(provider_liveness_chunk)
                        yield initial_chunk
                    final_chunk = state.finalize_item(item)
                    if final_chunk is not None:
                        final_chunk.update(provider_liveness_chunk)
                        yield final_chunk
                else:
                    yield provider_liveness_chunk
                continue
            if event_type == "response.custom_tool_call_input.delta":
                chunk = state.custom_tool_input_delta(event)
                progress_chunk = state.custom_tool_progress_delta(event)
                if chunk is not None:
                    chunk.update(provider_liveness_chunk)
                    yield chunk
                if progress_chunk is not None:
                    progress_chunk.update(provider_liveness_chunk)
                    yield progress_chunk
                if chunk is None and progress_chunk is None:
                    yield provider_liveness_chunk
                continue
            if event_type == "response.custom_tool_call_input.done":
                chunk = state.custom_tool_input_done(event)
                progress_chunk = state.custom_tool_progress_delta(event, complete=True)
                if progress_chunk is not None:
                    progress_chunk.update(provider_liveness_chunk)
                    yield progress_chunk
                if chunk is not None:
                    chunk.update(provider_liveness_chunk)
                    yield chunk
                if chunk is None and progress_chunk is None:
                    yield provider_liveness_chunk
                continue
            if event_type == "response.output_item.done":
                item = _get_output_item(event)
                if item is None:
                    continue
                state.complete_item(item)
                state.note_raw_output_item_emitted(item)
                yield {
                    **provider_liveness_chunk,
                    "responses_output_item": item,
                }
                message_chunk = state.finalize_message_item(item)
                if message_chunk is not None:
                    message_chunk.update(provider_liveness_chunk)
                    yield message_chunk
                initial_chunk = state.initial_tool_delta(item, emit_name=True)
                if initial_chunk is not None:
                    initial_chunk.update(provider_liveness_chunk)
                    yield initial_chunk
                final_chunk = state.finalize_item(item)
                if final_chunk is not None:
                    final_chunk.update(provider_liveness_chunk)
                    yield final_chunk
                continue
            if event_type in {"response.completed", "response.completed.synthetic"}:
                response_payload = _to_dict(event.get("response") or event)
                for fallback_chunk in state.final_message_fallback(response_payload):
                    fallback_chunk.update(provider_liveness_chunk)
                    yield fallback_chunk
                for fallback_chunk in state.final_tool_fallback(response_payload):
                    fallback_chunk.update(provider_liveness_chunk)
                    yield fallback_chunk
                state.completed_seen = True
                yield {
                    **provider_liveness_chunk,
                    "choices": [
                        {"delta": {}, "finish_reason": _extract_finish_reason(response_payload)}
                    ],
                    "usage": _extract_usage(response_payload),
                    "response_status": str(response_payload.get("status") or "completed"),
                    "response_instructions": response_payload.get("instructions"),
                }
                continue
            if event_type == "error":
                last_error_event_details = _response_error_event_details(event)
                yield provider_liveness_chunk
                continue
            if event_type == "response.failed":
                failure_details = _response_failure_details(event)
                if last_error_event_details:
                    failure_details = _merge_prior_error_event_details(
                        failure_details,
                        last_error_event_details,
                    )
                    last_error_event_details = None
                failure_payload = classify_response_failure(failure_details)
                message = failure_details.get("message")
                logger.warning(
                    "Responses stream failed event",
                    extra={"extra_data": failure_details},
                )
                yield {
                    **provider_liveness_chunk,
                    "error": str(message or "Responses stream failed"),
                    "response_error": failure_payload,
                    "mid_stream_failure": True,
                }
                continue
            yield provider_liveness_chunk
    finally:
        logger.debug(
            "Responses bridge stream summary",
            extra={"extra_data": state.diagnostics()},
        )


def _truncate_response_failure_value(value: Any, *, max_chars: int = 500) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    text = str(value)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...<truncated {len(text) - max_chars} chars>"


def _response_failure_details(event: dict[str, Any]) -> dict[str, Any]:
    error = event.get("error") or {}
    response = event.get("response") or {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    if not isinstance(response, dict):
        response = {}
    details: dict[str, Any] = {
        "event_type": event.get("type") or "response.failed",
        "response_id": response.get("id") or event.get("response_id") or event.get("id"),
        "response_status": response.get("status") or event.get("status"),
    }
    for key in ("type", "code", "message", "param"):
        if key in error:
            details[key] = _truncate_response_failure_value(error.get(key))
    if "details" in error:
        details["details"] = _truncate_response_failure_value(error.get("details"))
    return {key: value for key, value in details.items() if value not in (None, "")}


def _response_error_event_details(event: dict[str, Any]) -> dict[str, Any]:
    """Return safe details from a raw Responses ``error`` stream event."""

    details = _response_failure_details(event)
    for key in ("code", "message", "param"):
        if key in event and key not in details:
            details[key] = _truncate_response_failure_value(event.get(key))
    error_type = event.get("error_type")
    if error_type is not None and "type" not in details:
        details["type"] = _truncate_response_failure_value(error_type)
    event_details = event.get("details")
    if event_details is not None and "details" not in details:
        details["details"] = _truncate_response_failure_value(event_details)
    return {key: value for key, value in details.items() if value not in (None, "")}


def _merge_prior_error_event_details(
    failure_details: dict[str, Any],
    prior_error_details: dict[str, Any],
) -> dict[str, Any]:
    """Attach the preceding raw ``error`` event to a later ``response.failed`` event."""

    merged = dict(failure_details)
    for key in ("type", "code", "message", "param", "details", "response_id", "response_status"):
        if key not in merged and key in prior_error_details:
            merged[key] = prior_error_details[key]
    merged["previous_error_event"] = dict(prior_error_details)
    return merged


class _ResponsesStreamState:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._item_aliases: dict[str, str] = {}
        self._active_item_key: str | None = None
        self._next_tool_index = 0
        self._emitted_content = ""
        self._emitted_reasoning = ""
        self._emitted_reasoning_summary = ""
        self._emitted_refusal = ""
        # Segment-scoped emitted text: reset at part/item boundaries so
        # dedup never misfires across distinct reasoning parts or message
        # items that legitimately repeat earlier text.
        self._emitted_segments: dict[str, str] = {
            "content": "",
            "reasoning_content": "",
            "reasoning": "",
            "refusal": "",
        }
        self.event_counts: dict[str, int] = {}
        self.recent_event_types: deque[str] = deque(maxlen=20)
        self.text_emissions = 0
        self.tool_call_emissions = 0
        self.completed_fallback_used = False
        self.completed_seen = False
        # Counter for reasoning summary parts (used for boundary markers)
        self.reasoning_part_count = 0
        self._emitted_raw_output_item_keys: set[str] = set()

    def note_event(self, event_type: str) -> None:
        normalized = event_type or "unknown"
        self.event_counts[normalized] = self.event_counts.get(normalized, 0) + 1
        self.recent_event_types.append(normalized)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "event_counts": dict(sorted(self.event_counts.items())),
            "recent_event_types": list(self.recent_event_types),
            "text_emissions": self.text_emissions,
            "tool_call_emissions": self.tool_call_emissions,
            "completed_fallback_used": self.completed_fallback_used,
            "completed_seen": self.completed_seen,
        }

    def note_text_emitted(self, field: str, text: str) -> None:
        if field == "reasoning_content":
            self._emitted_reasoning += text
        elif field == "reasoning":
            self._emitted_reasoning_summary += text
        elif field == "refusal":
            self._emitted_refusal += text
        else:
            self._emitted_content += text
        segment_key = field if field in self._emitted_segments else "content"
        self._emitted_segments[segment_key] += text
        self.text_emissions += 1

    def _emitted_value(self, field: str) -> str:
        if field == "reasoning_content":
            return self._emitted_reasoning
        if field == "reasoning":
            return self._emitted_reasoning_summary
        if field == "refusal":
            return self._emitted_refusal
        return self._emitted_content

    def _emitted_for_scope(self, field: str, scope: str) -> str:
        if scope == "segment":
            segment_key = field if field in self._emitted_segments else "content"
            return self._emitted_segments[segment_key]
        return self._emitted_value(field)

    def reset_segment(self, field: str) -> None:
        """Reset segment-scoped dedup state at a part/item boundary."""
        segment_key = field if field in self._emitted_segments else "content"
        self._emitted_segments[segment_key] = ""

    def register_item(self, item: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        item_key, aliases = self._item_identity(item)
        if not item_key:
            return None, False
        existing = self._resolve_item_state(item_key, *aliases)
        if existing is not None:
            item_name = _tool_call_item_name(item)
            if item_name != "unknown_tool":
                existing["name"] = item_name
            initial_arguments = _tool_call_item_arguments(item)
            if initial_arguments:
                merge_result = merge_incremental_json_fragment(
                    str(existing.get("arguments") or ""),
                    initial_arguments,
                )
                existing["arguments"] = merge_result.merged
                if merge_result.replaced:
                    existing["emitted"] = 0
            self._bind_item_aliases(existing["state_key"], aliases)
            return existing, False
        index = self._next_tool_index
        self._next_tool_index += 1
        state = {
            "state_key": item_key,
            "call_id": normalize_tool_call_id(item.get("call_id"), item.get("id"), item_key),
            "item_type": str(item.get("type") or ""),
            "item_id": str(item.get("id") or "").strip() or None,
            "phase": str(item.get("phase") or "").strip() or None,
            "name": _tool_call_item_name(item),
            "name_emitted": False,
            "arguments": _tool_call_item_arguments(item),
            "emitted": 0,
            "index": index,
        }
        self._items[item_key] = state
        self._bind_item_aliases(item_key, aliases)
        if state["item_type"] == "message":
            # A new message item starts a fresh dedup segment so text that
            # legitimately repeats a previous item is not swallowed.
            self.reset_segment("content")
        return state, True

    @staticmethod
    def _message_text_metadata(
        state: dict[str, Any],
        *,
        content_source: str,
    ) -> dict[str, Any]:
        return {
            "response_item_id": state.get("item_id") or state.get("state_key"),
            "response_item_type": "message",
            "response_message_phase": state.get("phase"),
            "content_source": content_source,
        }

    def activate_item(self, item: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        state, is_new = self.register_item(item)
        if state is not None:
            self._active_item_key = str(state.get("state_key") or "") or None
        return state, is_new

    def complete_item(self, item: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        state, is_new = self.register_item(item)
        if state is not None and self._active_item_key == state.get("state_key"):
            self._active_item_key = None
        return state, is_new

    def active_item_type(self) -> str | None:
        state = self._resolve_item_state(self._active_item_key or "")
        if state is None:
            return None
        return str(state.get("item_type") or "") or None

    def _item_identity(self, item: dict[str, Any]) -> tuple[str, list[str]]:
        raw_item_id = str(item.get("id") or "").strip()
        raw_call_id = str(item.get("call_id") or "").strip()
        fallback_seed = raw_call_id or raw_item_id or self._next_tool_index
        normalized_call_id = normalize_tool_call_id(
            raw_call_id or None,
            raw_item_id or None,
            fallback_seed,
        )
        aliases = [alias for alias in {raw_item_id, raw_call_id, normalized_call_id} if alias]
        return normalized_call_id, aliases

    def _bind_item_aliases(self, state_key: str, aliases: list[str]) -> None:
        for alias in aliases:
            self._item_aliases[alias] = state_key

    def _raw_output_item_key(self, item: dict[str, Any]) -> str:
        keys = self._raw_output_item_keys(item)
        if keys:
            return sorted(keys)[0]
        return json.dumps(item, sort_keys=True, default=str)

    def _raw_output_item_keys(self, item: dict[str, Any]) -> set[str]:
        raw_item_id = str(item.get("id") or "").strip()
        raw_call_id = str(item.get("call_id") or "").strip()
        normalized_call_id = normalize_tool_call_id(
            raw_call_id or None,
            raw_item_id or None,
            raw_call_id or raw_item_id or "",
        )
        return {
            key
            for key in (raw_item_id, raw_call_id, normalized_call_id)
            if isinstance(key, str) and key
        }

    def note_raw_output_item_emitted(self, item: dict[str, Any]) -> None:
        keys = self._raw_output_item_keys(item)
        if keys:
            self._emitted_raw_output_item_keys.update(keys)
        else:
            self._emitted_raw_output_item_keys.add(self._raw_output_item_key(item))

    def raw_output_item_emitted(self, item: dict[str, Any]) -> bool:
        keys = self._raw_output_item_keys(item)
        if keys:
            return any(key in self._emitted_raw_output_item_keys for key in keys)
        return self._raw_output_item_key(item) in self._emitted_raw_output_item_keys

    def _resolve_item_state(self, *aliases: str) -> dict[str, Any] | None:
        for alias in aliases:
            if not alias:
                continue
            state_key = self._item_aliases.get(alias, alias)
            state = self._items.get(state_key)
            if state is not None:
                return state
        return None

    def output_text_delta(
        self,
        event: dict[str, Any],
        delta: str,
        *,
        dedupe_cumulative: bool = False,
    ) -> dict[str, Any] | None:
        state = self._resolve_item_state(str(event.get("item_id") or ""))
        if state is None:
            state = self._resolve_item_state(self._active_item_key or "")
        if state is not None and str(state.get("item_type") or "") != "message":
            return None
        if state is None:
            return None
        normalized_delta = (
            self.incremental_text_delta(delta, field="content") if dedupe_cumulative else delta
        )
        if normalized_delta is None:
            return None
        self.note_text_emitted("content", normalized_delta)
        return {
            **self._message_text_metadata(
                state,
                content_source="response.output_text.delta",
            ),
            "choices": [{"delta": {"content": normalized_delta}}],
        }

    def output_text_done(
        self,
        event: dict[str, Any],
        text: str,
    ) -> dict[str, Any] | None:
        state = self._resolve_item_state(str(event.get("item_id") or ""))
        if state is None:
            state = self._resolve_item_state(self._active_item_key or "")
        if state is not None and str(state.get("item_type") or "") != "message":
            return None
        if state is None:
            return None
        chunk = self.final_text_delta(text, field="content")
        if chunk is None:
            return None
        chunk.update(
            self._message_text_metadata(
                state,
                content_source="response.output_text.done",
            )
        )
        return chunk

    def content_part_text(
        self,
        event: dict[str, Any],
        text: str,
    ) -> dict[str, Any] | None:
        state = self._resolve_item_state(str(event.get("item_id") or ""))
        part = event.get("part")
        if state is None and isinstance(part, dict):
            state = self._resolve_item_state(str(part.get("item_id") or ""))
        if state is None:
            state = self._resolve_item_state(self._active_item_key or "")
        if state is not None and str(state.get("item_type") or "") != "message":
            return None
        if state is None:
            return None
        chunk = self.final_text_delta(text, field="content")
        if chunk is None:
            return None
        chunk.update(
            self._message_text_metadata(
                state,
                content_source="response.content_part",
            )
        )
        return chunk

    def message_delta(self, item: dict[str, Any], *, emit_initial: bool) -> dict[str, Any] | None:
        if str(item.get("type")) != "message":
            return None
        if not emit_initial:
            return None
        text = _extract_message_item_text(item)
        if not text:
            return None
        # Register first so a brand-new message item resets the content
        # segment before this item's text is recorded against it.
        state, _ = self.register_item(item)
        self.note_text_emitted("content", text)
        if state is None:
            return {"choices": [{"delta": {"content": text}}]}
        return {
            **self._message_text_metadata(
                state,
                content_source="response.output_item.added",
            ),
            "choices": [{"delta": {"content": text}}],
        }

    def finalize_message_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if str(item.get("type")) != "message":
            return None
        text = _extract_message_item_text(item)
        if not text:
            return None
        state, _ = self.register_item(item)
        if state is None:
            return self.final_text_delta(text, field="content")
        chunk = self.final_text_delta(text, field="content")
        if chunk is None:
            return None
        chunk.update(
            self._message_text_metadata(
                state,
                content_source="response.output_item.done",
            )
        )
        return chunk

    def final_message_fallback(self, response_payload: dict[str, Any]) -> list[dict[str, Any]]:
        # The completed response object is a lossy aggregate of the typed
        # Responses stream. Codex treats response.completed as a terminal
        # metadata event and does not synthesize visible assistant text from
        # top-level output_text. Follow that model here: use completed fallback
        # only to recover structured non-visible fields and explicit refusal
        # text. Message content must come from typed output_text/content_part or
        # message item events while streaming, otherwise a provider-side
        # scratchpad leak in output_text can become a persisted assistant_message.
        envelope = _extract_response_envelope(response_payload)
        chunks: list[dict[str, Any]] = []
        raw_reasoning_items: list[dict[str, Any]] = []
        for raw_item in response_payload.get("output") or []:
            if not isinstance(raw_item, dict):
                continue
            if str(raw_item.get("type") or "") != "reasoning":
                continue
            if self.raw_output_item_emitted(raw_item):
                continue
            self.note_raw_output_item_emitted(raw_item)
            raw_reasoning_items.append(dict(raw_item))
        for response_field, fallback_text in (
            ("reasoning_content", envelope.reasoning_content),
            ("reasoning", envelope.reasoning_summary),
            ("refusal", envelope.refusal),
        ):
            if not fallback_text:
                continue
            # Fallback payloads aggregate text across all parts, so dedup must
            # compare against everything emitted for the field, not the
            # current segment.
            chunk = self.final_text_delta(fallback_text, field=response_field, scope="total")
            if chunk is not None:
                self.completed_fallback_used = True
                chunks.append(chunk)
        if raw_reasoning_items:
            self.completed_fallback_used = True
            chunks.extend(
                {"responses_output_item": dict(raw_item)} for raw_item in raw_reasoning_items
            )
        return chunks

    def streamed_text_delta(self, text: str, *, field: str) -> dict[str, Any] | None:
        """Handle a true incremental delta event (``*.delta``).

        Unlike :meth:`final_text_delta` this never drops short deltas that
        happen to match the emitted suffix — a legitimately repeated token
        (e.g. "very very") must stream through. Only large overlaps are
        treated as provider replays via :meth:`incremental_text_delta`.
        """
        if not text:
            return None
        normalized_delta = self.incremental_text_delta(text, field=field)
        if not normalized_delta:
            return None
        self.note_text_emitted(field, normalized_delta)
        return {"choices": [{"delta": {field: normalized_delta}}]}

    def final_text_delta(
        self,
        text: str,
        *,
        field: str = "content",
        scope: str = "segment",
    ) -> dict[str, Any] | None:
        """Handle terminal/cumulative text (``*.done``, part text, fallbacks).

        Dedup compares against segment-scoped emitted text by default so a
        later part/item that legitimately repeats an earlier one is not
        swallowed. Completed-response fallbacks pass ``scope="total"`` because
        they aggregate text across all parts.
        """
        if not text:
            return None
        emitted = self._emitted_for_scope(field, scope)
        if text == emitted:
            return None
        if emitted and emitted.endswith(text):
            return None
        normalized_delta: str | None
        if emitted and text.startswith(emitted):
            normalized_delta = text[len(emitted) :]
        else:
            normalized_delta = self.incremental_text_delta(text, field=field, scope=scope)
        if not normalized_delta:
            return None
        self.note_text_emitted(field, normalized_delta)
        return {"choices": [{"delta": {field: normalized_delta}}]}

    def incremental_text_delta(
        self,
        delta: str,
        *,
        field: str = "content",
        scope: str = "segment",
    ) -> str | None:
        """Normalize Responses text deltas that replay already emitted text.

        Responses streams usually send true deltas, but Codex/direct transports
        can occasionally replay a large previous suffix or send a cumulative
        snapshot through response.output_text.delta. Guard only on large overlaps
        so intentionally repeated short text still streams unchanged.
        """

        if not delta:
            return None
        emitted = self._emitted_for_scope(field, scope)
        if not emitted:
            return delta

        min_overlap = OUTPUT_TEXT_DELTA_DEDUPE_MIN_OVERLAP
        if len(emitted) >= min_overlap and delta.startswith(emitted):
            suffix = delta[len(emitted) :]
            return suffix or None

        if len(delta) >= min_overlap and emitted.endswith(delta):
            return None

        overlap_limit = min(
            len(emitted),
            len(delta) - 1,
            OUTPUT_TEXT_DELTA_DEDUPE_MAX_SCAN,
        )
        for overlap in range(overlap_limit, min_overlap - 1, -1):
            if emitted.endswith(delta[:overlap]):
                suffix = delta[overlap:]
                return suffix or None

        return delta

    def initial_tool_delta(self, item: dict[str, Any], *, emit_name: bool) -> dict[str, Any] | None:
        if not _is_responses_tool_call_item(item):
            return None
        item_key, aliases = self._item_identity(item)
        state = self._resolve_item_state(item_key, *aliases)
        if state is None:
            return None
        chunk: dict[str, Any] = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": state["index"],
                                "id": state["call_id"],
                                "function": {},
                            }
                        ]
                    }
                }
            ]
        }
        function = chunk["choices"][0]["delta"]["tool_calls"][0]["function"]
        name_pending = not bool(state.get("name_emitted"))
        # Re-emit the name when a placeholder was emitted first: an item
        # registered from an arguments.done event (no nested item on the real
        # API) has no name yet; the real name arrives with output_item.done.
        name_upgraded = (
            bool(state.get("name_emitted_placeholder")) and state["name"] != "unknown_tool"
        )
        if emit_name and (name_pending or name_upgraded):
            function["name"] = state["name"]
            state["name_emitted"] = True
            state["name_emitted_placeholder"] = state["name"] == "unknown_tool"
        unseen_arguments = str(state["arguments"])[int(state["emitted"]) :]
        if unseen_arguments:
            function["arguments"] = unseen_arguments
            state["emitted"] += len(unseen_arguments)
        if not function:
            return None
        self.tool_call_emissions += 1
        return chunk

    def arguments_delta(self, event: dict[str, Any]) -> dict[str, Any] | None:
        item_id = str(event.get("item_id") or "")
        state = self._resolve_item_state(item_id)
        delta = event.get("delta")
        if state is None or not isinstance(delta, str) or not delta:
            return None
        existing_arguments = str(state.get("arguments") or "")
        merge_result = merge_incremental_json_fragment(existing_arguments, delta)
        state["arguments"] = merge_result.merged
        if not merge_result.emitted:
            return None
        if merge_result.replaced:
            state["emitted"] = len(state["arguments"])
        else:
            state["emitted"] += len(merge_result.emitted)
        return {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": state["index"],
                                "id": state["call_id"],
                                "function": {"arguments": merge_result.emitted},
                            }
                        ]
                    }
                }
            ]
        }

    def custom_tool_input_delta(self, event: dict[str, Any]) -> dict[str, Any] | None:
        item_id = str(event.get("item_id") or "")
        state = self._resolve_item_state(item_id)
        delta = event.get("delta")
        if state is None or not isinstance(delta, str) or not delta:
            return None
        if str(state.get("item_type") or "") != "custom_tool_call":
            return None
        self._note_custom_tool_input_progress(state, delta)
        return self._append_custom_tool_input(state, delta)

    def custom_tool_input_done(self, event: dict[str, Any]) -> dict[str, Any] | None:
        item_id = str(event.get("item_id") or "")
        state = self._resolve_item_state(item_id)
        input_value = event.get("input")
        if state is None or not isinstance(input_value, str):
            return None
        if str(state.get("item_type") or "") != "custom_tool_call":
            return None
        self._note_custom_tool_input_progress(state, input_value, replace=True, complete=True)
        existing_arguments = str(state.get("arguments") or "")
        target_arguments = _custom_tool_input_to_arguments(
            str(state.get("name") or "unknown_tool"),
            input_value,
        )
        if existing_arguments == target_arguments:
            return None
        if existing_arguments and target_arguments.startswith(existing_arguments):
            return self._append_custom_tool_input(
                state,
                target_arguments[len(existing_arguments) :],
            )
        state["arguments"] = target_arguments
        state["emitted"] = len(target_arguments)
        self.tool_call_emissions += 1
        return {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": state["index"],
                                "id": state["call_id"],
                                "function": {"arguments": target_arguments},
                            }
                        ]
                    }
                }
            ]
        }

    def _append_custom_tool_input(
        self,
        state: dict[str, Any],
        input_delta: str,
    ) -> dict[str, Any] | None:
        tool_name = str(state.get("name") or "unknown_tool")
        argument_delta = _custom_tool_input_delta_to_arguments_delta(tool_name, str(input_delta))
        if not argument_delta:
            return None
        state["arguments"] = str(state.get("arguments") or "") + argument_delta
        state["emitted"] = len(str(state.get("arguments") or ""))
        self.tool_call_emissions += 1
        return {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": state["index"],
                                "id": state["call_id"],
                                "function": {"arguments": argument_delta},
                            }
                        ]
                    }
                }
            ]
        }

    def _note_custom_tool_input_progress(
        self,
        state: dict[str, Any],
        input_value: str,
        *,
        replace: bool = False,
        complete: bool = False,
    ) -> None:
        existing_input = str(state.get("custom_input") or "")
        custom_input = input_value if replace else existing_input + input_value
        state["custom_input"] = custom_input
        state["custom_input_chars"] = len(custom_input)
        state["custom_input_lines"] = custom_input.count("\n") + (1 if custom_input else 0)
        state["custom_input_complete"] = complete

    def custom_tool_progress_delta(
        self,
        event: dict[str, Any],
        *,
        complete: bool = False,
    ) -> dict[str, Any] | None:
        item_id = str(event.get("item_id") or "")
        state = self._resolve_item_state(item_id)
        if state is None or str(state.get("item_type") or "") != "custom_tool_call":
            return None
        name = str(state.get("name") or "unknown_tool")
        input_chars = int(state.get("custom_input_chars") or 0)
        if input_chars <= 0:
            return None
        input_lines = int(state.get("custom_input_lines") or 0)
        return {
            "choices": [
                {
                    "delta": {
                        "tool_progress": {
                            "index": state["index"],
                            "id": state["call_id"],
                            "name": name,
                            "phase": "preparing_input",
                            "input_chars": input_chars,
                            "input_lines": input_lines,
                            "complete": complete or bool(state.get("custom_input_complete")),
                        }
                    }
                }
            ]
        }

    def finalize_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if not _is_responses_tool_call_item(item):
            return None
        item_key, aliases = self._item_identity(item)
        state = self._resolve_item_state(item_key, *aliases)
        if state is None:
            return None
        final_arguments = _tool_call_item_arguments(item)
        merge_result = merge_incremental_json_fragment(
            str(state.get("arguments") or ""),
            final_arguments,
        )
        merged_arguments = merge_result.merged
        emitted = int(state.get("emitted", 0))
        if merge_result.replaced:
            delta = merge_result.emitted
            state["arguments"] = merged_arguments
            state["emitted"] = len(merged_arguments)
        else:
            if len(merged_arguments) <= emitted:
                return None
            delta = merged_arguments[emitted:]
            state["arguments"] = merged_arguments
            state["emitted"] = len(merged_arguments)
        if not delta:
            return None
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

    def final_tool_fallback(self, response_payload: dict[str, Any]) -> list[dict[str, Any]]:
        envelope = _extract_response_envelope(response_payload)
        chunks: list[dict[str, Any]] = []
        for index, tool_call in enumerate(envelope.tool_calls):
            function = tool_call.get("function") or {}
            fallback_id = str(tool_call.get("id") or f"fallback_call_{index}")
            item = {
                "type": "function_call",
                "id": fallback_id,
                "call_id": fallback_id,
                "name": str(function.get("name") or "unknown_tool"),
                "arguments": str(function.get("arguments") or "{}"),
            }
            if not self.raw_output_item_emitted(item):
                self.note_raw_output_item_emitted(item)
                self.completed_fallback_used = True
                chunks.append({"responses_output_item": dict(item)})
            self.register_item(item)
            initial_chunk = self.initial_tool_delta(item, emit_name=True)
            if initial_chunk is not None:
                self.completed_fallback_used = True
                chunks.append(initial_chunk)
            final_chunk = self.finalize_item(item)
            if final_chunk is not None:
                self.completed_fallback_used = True
                chunks.append(final_chunk)
        return chunks


def _extract_response_envelope(payload: dict[str, Any]) -> NormalizedResponseEnvelope:
    envelope = NormalizedResponseEnvelope(
        status=str(payload.get("status") or "completed"),
        finish_reason=_extract_finish_reason(payload),
        usage=_extract_usage(payload),
    )

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    summary_parts: list[str] = []
    refusal_parts: list[str] = []

    for index, item in enumerate(payload.get("output") or []):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", ""))
        if item_type == "message":
            text = _extract_message_item_text(item)
            if text:
                content_parts.append(text)
                envelope.content_source = envelope.content_source or "message"
            continue
        if item_type == "reasoning":
            reasoning_text, summary_text = _extract_reasoning_item(item)
            if reasoning_text:
                reasoning_parts.append(reasoning_text)
                envelope.reasoning_source = envelope.reasoning_source or "reasoning"
            if summary_text:
                summary_parts.append(summary_text)
            continue
        if item_type == "refusal":
            refusal_text = _extract_text_value(item.get("content") or item.get("refusal") or item)
            if refusal_text:
                refusal_parts.append(refusal_text)
            continue
        if item_type in {"function_call", "apply_patch_call", "custom_tool_call"}:
            envelope.tool_calls.append(
                {
                    "id": normalize_tool_call_id(item.get("call_id"), item.get("id"), index),
                    "type": "function",
                    "function": {
                        "name": _tool_call_item_name(item),
                        "arguments": _tool_call_item_arguments(item) or "{}",
                    },
                }
            )

    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            continue
        for index, tool_call in enumerate(message.get("tool_calls") or []):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") or {}
            if not isinstance(function, dict):
                function = {}
            fallback_id = normalize_tool_call_id(tool_call.get("id"), None, index)
            envelope.tool_calls.append(
                {
                    "id": fallback_id,
                    "type": "function",
                    "function": {
                        "name": str(function.get("name") or "unknown_tool"),
                        "arguments": str(function.get("arguments") or "{}"),
                    },
                }
            )

    envelope.content = "".join(content_parts)
    envelope.reasoning_content = "".join(reasoning_parts)
    envelope.reasoning_summary = "".join(summary_parts)
    envelope.refusal = "".join(refusal_parts)
    return envelope


def _extract_finish_reason(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "completed")
    if status == "completed":
        if _payload_has_tool_calls(payload):
            return "tool_calls"
        return "stop"
    if status == "incomplete":
        return "length"
    if status == "failed":
        return "error"
    return "stop"


def _payload_has_tool_calls(payload: dict[str, Any]) -> bool:
    for item in payload.get("output") or []:
        if isinstance(item, dict) and str(item.get("type") or "") in RESPONSES_TOOL_CALL_TYPES:
            return True
    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if isinstance(message, dict) and message.get("tool_calls"):
            return True
    return False


def _extract_usage(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract usage from a Responses API payload and normalise to chat-completions keys.

    The Responses API uses ``input_tokens`` / ``output_tokens`` while the
    chat-completions API (and all downstream Cognis consumers such as
    ``StreamAccumulator``) expect ``prompt_tokens`` / ``completion_tokens``.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    input_token_count = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    output_token_count = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    normalized = dict(usage)
    normalized.update(
        {
            "prompt_tokens": input_token_count,
            "completion_tokens": output_token_count,
            "total_tokens": usage.get(
                "total_tokens",
                input_token_count + output_token_count,
            ),
        }
    )
    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, dict) and isinstance(
        input_details.get("cached_tokens"), int | float
    ):
        normalized["cached_tokens"] = int(input_details["cached_tokens"])
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, dict) and isinstance(
        output_details.get("reasoning_tokens"), int | float
    ):
        normalized["reasoning_tokens"] = int(output_details["reasoning_tokens"])
    return normalized


def _normalize_response_format(response_format: Any) -> dict[str, Any] | None:
    if isinstance(response_format, dict):
        return response_format
    if isinstance(response_format, str):
        normalized = response_format.strip().lower()
        if normalized in {"json", "json_object"}:
            return {"type": "json_object"}
        if normalized == "text":
            return {"type": "text"}
        logger.warning(
            "Ignoring unsupported Responses API response_format",
            extra={"extra_data": {"response_format": normalized}},
        )
    return None


def _extract_message_item_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    text_parts: list[str] = []
    for part in content:
        part_text = _extract_content_part_text(part)
        if part_text:
            text_parts.append(part_text)
    return "".join(text_parts)


def _extract_reasoning_item(item: dict[str, Any]) -> tuple[str, str]:
    content_text = _extract_text_value(item.get("content"))
    summary_text = _extract_text_value(item.get("summary"))
    if not summary_text:
        summary_text = _extract_text_value(item.get("summary_text"))
    return content_text, summary_text


def _extract_content_part_text(part: Any) -> str:
    if not isinstance(part, dict):
        return ""
    if part.get("type") not in {"output_text", "text", "input_text"}:
        return ""
    text = part.get("text")
    return text if isinstance(text, str) else ""


def _extract_part_text(part: Any) -> str:
    if not isinstance(part, dict):
        return ""
    if part.get("type") not in {
        "output_text",
        "text",
        "input_text",
        "summary",
        "reasoning_text",
        "reasoning_summary_text",
        "refusal",
        "refusal_text",
    }:
        return ""
    text = part.get("text")
    return text if isinstance(text, str) else ""


def _extract_text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text_parts: list[str] = []
        for item in value:
            part_text = _extract_text_value(item)
            if part_text:
                text_parts.append(part_text)
        return "".join(text_parts)
    if isinstance(value, dict):
        direct = _extract_part_text(value)
        if direct:
            return direct
        for key in ("content", "summary", "text", "refusal", "reasoning"):
            part_text = _extract_text_value(value.get(key))
            if part_text:
                return part_text
        return ""
    return ""


def _non_user_attachment_content_text(part: dict[str, Any]) -> str:
    part_type = str(part.get("type") or "")
    if part_type in {"input_image", "image_url"}:
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            image_url = image_url.get("url")
        if isinstance(image_url, str) and image_url:
            return f"[Image attachment omitted from non-user message: {image_url}]"
        return "[Image attachment omitted from non-user message.]"
    if part_type in {"input_file", "file"}:
        file_url = part.get("file_url")
        file_id = part.get("file_id")
        filename = part.get("filename")
        file_data = part.get("file")
        if isinstance(file_data, dict):
            file_url = file_data.get("file_url", file_url)
            file_id = file_data.get("file_id", file_id)
            filename = file_data.get("filename", filename)
        refs = [
            str(value)
            for value in (filename, file_id, file_url)
            if isinstance(value, str) and value
        ]
        if refs:
            return "[File attachment omitted from non-user message: " + ", ".join(refs) + "]"
        return "[File attachment omitted from non-user message.]"
    return "[Attachment omitted from non-user message.]"


def _normalize_message_content(content: Any, *, allow_native_attachments: bool = True) -> Any:
    if isinstance(content, list):
        normalized: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "")
            if part_type == "input_text":
                normalized.append(part)
                continue
            if part_type == "input_file":
                if not allow_native_attachments:
                    normalized.append(
                        {"type": "input_text", "text": _non_user_attachment_content_text(part)}
                    )
                    continue
                input_file_item = {"type": "input_file"}
                if isinstance(part.get("file_id"), str) and part["file_id"]:
                    input_file_item["file_id"] = part["file_id"]
                elif isinstance(part.get("file_url"), str) and part["file_url"]:
                    input_file_item["file_url"] = part["file_url"]
                if len(input_file_item) > 1:
                    normalized.append(input_file_item)
                continue
            if part_type == "input_image":
                if allow_native_attachments:
                    normalized.append(part)
                else:
                    normalized.append(
                        {"type": "input_text", "text": _non_user_attachment_content_text(part)}
                    )
                continue
            if part_type == "text":
                normalized.append({"type": "input_text", "text": str(part.get("text") or "")})
                continue
            if part_type == "image_url":
                if not allow_native_attachments:
                    normalized.append(
                        {"type": "input_text", "text": _non_user_attachment_content_text(part)}
                    )
                    continue
                image_url = part.get("image_url")
                detail = part.get("detail")
                if isinstance(image_url, dict):
                    url = image_url.get("url")
                    detail = image_url.get("detail", detail)
                else:
                    url = image_url
                if isinstance(url, str) and url:
                    input_image_item: dict[str, Any] = {
                        "type": "input_image",
                        "image_url": url,
                    }
                    if isinstance(detail, str) and detail:
                        input_image_item["detail"] = detail
                    normalized.append(input_image_item)
                continue
            if part_type == "file":
                if not allow_native_attachments:
                    normalized.append(
                        {"type": "input_text", "text": _non_user_attachment_content_text(part)}
                    )
                    continue
                file_data = part.get("file")
                if isinstance(file_data, dict):
                    item = {"type": "input_file"}
                    if isinstance(file_data.get("file_id"), str) and file_data["file_id"]:
                        item["file_id"] = file_data["file_id"]
                    elif isinstance(file_data.get("file_url"), str) and file_data["file_url"]:
                        item["file_url"] = file_data["file_url"]
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


def response_model_dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        with warnings.catch_warnings():
            # LiteLLM's model_construct() can leave the ``usage`` field as a
            # raw dict instead of a ``ResponseAPIUsage`` instance, which
            # triggers a harmless Pydantic serialisation warning on
            # ``model_dump()``.  Suppress it here — the dict is still valid.
            warnings.filterwarnings("ignore", message=".*Pydantic serializer.*")
            warnings.filterwarnings("ignore", message=".*PydanticSerializationUnexpectedValue.*")
            try:
                dumped = value.model_dump(warnings=False)
            except TypeError:
                dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _to_dict(value: Any) -> dict[str, Any]:
    return response_model_dump(value)


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


def _tool_to_responses_tool(tool: Any, *, apply_patch_tool_type: str = "") -> dict[str, Any]:
    if isinstance(tool, dict):
        if tool.get("type") == "apply_patch":
            if apply_patch_tool_type in {"", "freeform"}:
                return _freeform_apply_patch_tool()
            return {"type": "apply_patch"}
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


def _freeform_apply_patch_tool() -> dict[str, Any]:
    return {
        "type": "custom",
        "name": "apply_patch",
        "description": (
            "Apply patch to files. Use the apply_patch envelope exactly. "
            "This is a FREEFORM tool, so do not wrap the patch in JSON."
        ),
        "format": {
            "type": "grammar",
            "syntax": "lark",
            "definition": APPLY_PATCH_FREEFORM_LARK_GRAMMAR,
        },
    }
