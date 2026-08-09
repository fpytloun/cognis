"""Native Anthropic Messages transport with strict SSE lifecycle validation.

This module intentionally has no provider configuration or agent-loop dependency.
Callers pass a non-secret request context and resolve its credential only when a
request is about to cross the HTTP boundary.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from cognis.core.tool_exposure import reverse_tool_argument_aliases
from cognis.providers.llm.anthropic.contracts import (
    AnthropicAuthPolicy,
    AnthropicContinuationStatus,
    AnthropicNativeEnvelope,
    CompiledAnthropicToolBundle,
    ResolvedAnthropicRequestContext,
    materialize_json,
)
from cognis.providers.llm.anthropic_subscription import (
    ANTHROPIC_EXTENDED_CACHE_TTL_BETA,
    ANTHROPIC_REQUIRED_BETAS,
    CLAUDE_CODE_USER_AGENT,
)
from cognis.providers.llm.errors import (
    LLMStreamProviderError,
    MidStreamErrorCategory,
    MidStreamErrorPayload,
    classify_llm_exception,
    retry_after_seconds_from_headers,
)

CredentialResolver = Callable[[str], Awaitable[str]]
_OFFICIAL_MESSAGES_ENDPOINT = "https://api.anthropic.com/v1/messages"
_REQUIRED_BLOCK_TYPES = frozenset(
    {
        "text",
        "thinking",
        "redacted_thinking",
        "tool_use",
        "server_tool_use",
        "tool_search_tool_result",
    }
)
_TOOL_SEARCH_SERVER_TOOL_TYPES = {
    "tool_search_tool_regex_20251119": "tool_search_tool_regex",
    "tool_search_tool_bm25_20251119": "tool_search_tool_bm25",
}
_SUPPORTED_STOP_REASONS = frozenset(
    {
        "end_turn",
        "max_tokens",
        "model_context_window_exceeded",
        "stop_sequence",
        "tool_use",
        "pause_turn",
        "refusal",
    }
)


class AnthropicTransportError(LLMStreamProviderError):
    """A safe, normalized native Messages protocol or HTTP error."""

    def __init__(
        self,
        message: str,
        *,
        payload: MidStreamErrorPayload | None = None,
        status_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(message, payload=payload)


def build_anthropic_headers(
    context: ResolvedAnthropicRequestContext, credential: str
) -> dict[str, str]:
    """Build auth-specific headers without retaining the credential anywhere else."""

    if not credential:
        raise ValueError("Anthropic credential resolved to an empty value")
    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "application/json",
    }
    if context.auth_policy is AnthropicAuthPolicy.API_KEY:
        headers["x-api-key"] = credential
    else:
        # Claude Code identity and beta selection belong only to OAuth requests.
        headers["authorization"] = f"Bearer {credential}"
        headers["anthropic-beta"] = ",".join(ANTHROPIC_REQUIRED_BETAS)
        headers["user-agent"] = CLAUDE_CODE_USER_AGENT
    return headers


def _request_headers(
    context: ResolvedAnthropicRequestContext,
    credential: str,
    request: Mapping[str, Any],
) -> dict[str, str]:
    headers = build_anthropic_headers(context, credential)
    extra_headers = request.get("extra_headers")
    if isinstance(extra_headers, Mapping):
        protected = {
            "authorization",
            "content-type",
            "x-api-key",
            "user-agent",
            "anthropic-version",
        }
        for key, value in extra_headers.items():
            normalized_key = str(key).strip()
            lowered = normalized_key.lower()
            if not normalized_key or lowered in protected or not isinstance(value, str):
                continue
            if lowered == "anthropic-beta":
                existing = headers.get("anthropic-beta")
                values = dict.fromkeys(
                    item.strip()
                    for item in ",".join(filter(None, (existing, value))).split(",")
                    if item.strip()
                )
                headers["anthropic-beta"] = ",".join(values)
            else:
                headers[normalized_key] = value
    if _payload_uses_extended_cache_ttl(request):
        existing = headers.get("anthropic-beta")
        values = dict.fromkeys(
            item.strip()
            for item in ",".join(filter(None, (existing, ANTHROPIC_EXTENDED_CACHE_TTL_BETA))).split(
                ","
            )
            if item.strip()
        )
        headers["anthropic-beta"] = ",".join(values)
    return headers


def _payload_uses_extended_cache_ttl(value: Any) -> bool:
    if isinstance(value, Mapping):
        cache_control = value.get("cache_control")
        if (
            isinstance(cache_control, Mapping)
            and str(cache_control.get("ttl") or "").strip().lower() == "1h"
        ):
            return True
        return any(_payload_uses_extended_cache_ttl(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_payload_uses_extended_cache_ttl(item) for item in value)
    return False


def _request_endpoint(context: ResolvedAnthropicRequestContext) -> str:
    if context.auth_policy is AnthropicAuthPolicy.OAUTH:
        endpoint = _OFFICIAL_MESSAGES_ENDPOINT
    else:
        return context.endpoint
    parsed = urlsplit(endpoint)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("beta", "true")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _payload(
    context: ResolvedAnthropicRequestContext,
    request: Mapping[str, Any],
    bundle: CompiledAnthropicToolBundle,
    *,
    stream: bool,
) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in request.items()
        if not str(key).startswith("cognis_")
        and key
        not in {
            "parallel_tool_calls",
            "disable_parallel_tool_use",
            "tool_choice",
            "extra_headers",
        }
    }
    payload["model"] = context.model
    payload["stream"] = stream
    requested_server_tools = _server_tool_definitions(request.get("cognis_anthropic_server_tools"))
    server_tools = [dict(tool) for tool in bundle.server_tools]
    if requested_server_tools and requested_server_tools != server_tools:
        raise AnthropicTransportError("Anthropic server tool definitions differ from frozen bundle")
    payload["tools"] = [dict(tool) for tool in bundle.wire_tools] + server_tools
    tool_choice = _anthropic_tool_choice(
        request.get("tool_choice"),
        bundle,
        disable_parallel_tool_use=(
            bool(request["disable_parallel_tool_use"])
            if request.get("disable_parallel_tool_use") is not None
            else (
                not bool(request["parallel_tool_calls"])
                if request.get("parallel_tool_calls") is not None
                else None
            )
        ),
    )
    if tool_choice is None:
        payload.pop("tools", None)
    elif payload["tools"]:
        thinking = request.get("thinking")
        thinking_enabled = isinstance(thinking, Mapping) and thinking.get("type") != "disabled"
        if thinking_enabled and tool_choice["type"] in {"any", "tool"}:
            raise AnthropicTransportError(
                "Extended thinking only supports automatic or disabled tool choice"
            )
        payload["tool_choice"] = tool_choice
    try:
        materialized = materialize_json(payload)
    except TypeError as exc:
        raise AnthropicTransportError(
            f"Anthropic request payload is not JSON-compatible: {exc}"
        ) from exc
    if not isinstance(materialized, dict):
        raise AnthropicTransportError("Anthropic request payload must be a JSON object")
    return materialized


def _server_tool_definitions(value: Any) -> list[dict[str, Any]]:
    """Allow only explicit official tool-search server tools for this request."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise AnthropicTransportError("Anthropic server tools must be a list")
    tools: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise AnthropicTransportError("Anthropic server tool must be an object")
        tool_type = raw.get("type")
        expected_name = (
            _TOOL_SEARCH_SERVER_TOOL_TYPES.get(tool_type) if isinstance(tool_type, str) else None
        )
        if (
            expected_name is None
            or raw.get("name") != expected_name
            or set(raw) != {"type", "name"}
        ):
            raise AnthropicTransportError("Unsupported Anthropic server tool definition")
        tools.append({"type": str(tool_type), "name": expected_name})
    if len({tool["name"] for tool in tools}) != len(tools):
        raise AnthropicTransportError("Duplicate Anthropic server tool definition")
    return tools


def _anthropic_tool_choice(
    value: Any,
    bundle: CompiledAnthropicToolBundle,
    *,
    disable_parallel_tool_use: bool | None,
) -> dict[str, Any] | None:
    if value is None or value == "auto":
        choice: dict[str, Any] = {"type": "auto"}
    elif isinstance(value, str) and value in {"required", "any"}:
        choice = {"type": "any"}
    elif value == "none":
        return None
    elif isinstance(value, Mapping):
        choice_type = value.get("type")
        if choice_type in {"auto", "any"}:
            choice = {"type": str(choice_type)}
        else:
            function = value.get("function")
            requested_name = (
                function.get("name") if isinstance(function, Mapping) else value.get("name")
            )
            binding = next(
                (
                    item
                    for item in bundle.bindings
                    if requested_name in {item.canonical_name, item.wire_name}
                ),
                None,
            )
            if binding is None:
                raise AnthropicTransportError("Named Anthropic tool choice is not in frozen bundle")
            choice = {"type": "tool", "name": binding.wire_name}
    else:
        raise AnthropicTransportError("Unsupported Anthropic tool choice")
    if disable_parallel_tool_use is not None:
        choice["disable_parallel_tool_use"] = disable_parallel_tool_use
    return choice


def _provider_error(
    details: Mapping[str, Any],
    *,
    event_type: str,
    status_code: int | None = None,
    retry_after_seconds: float | None = None,
) -> AnthropicTransportError:
    error = details.get("error")
    body = error if isinstance(error, Mapping) else details
    message = str(body.get("message") or "Anthropic Messages request failed")
    code = body.get("type") or body.get("code")
    lowered = f"{message} {code or ''}".lower()
    category = MidStreamErrorCategory.OTHER.value
    if "rate" in lowered and "limit" in lowered:
        category = MidStreamErrorCategory.RATE_LIMIT.value
    elif "overloaded" in lowered or "server" in lowered:
        category = MidStreamErrorCategory.PROVIDER_5XX.value
    elif "context" in lowered and "window" in lowered:
        category = MidStreamErrorCategory.CONTEXT_OVERFLOW.value
    payload: MidStreamErrorPayload = {
        "category": category,
        "code": str(code) if code else None,
        "message": message[:500],
        "provider_event": event_type,
        "details": dict(body),
    }
    if retry_after_seconds is not None:
        payload["retry_after_seconds"] = retry_after_seconds
    return AnthropicTransportError(message, payload=payload, status_code=status_code)


async def decode_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Decode SSE records, including multiline data fields, without guessing events."""

    event_name: str | None = None
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                raw = "\n".join(data)
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise AnthropicTransportError(
                        "Malformed Anthropic SSE JSON",
                        payload={
                            "category": MidStreamErrorCategory.OTHER.value,
                            "message": str(exc),
                        },
                    ) from exc
                if not isinstance(event, dict):
                    raise AnthropicTransportError(
                        "Anthropic SSE event must be an object",
                        payload={
                            "category": MidStreamErrorCategory.OTHER.value,
                            "message": "invalid event",
                        },
                    )
                if event_name and "type" not in event:
                    event["type"] = event_name
                elif event_name and event.get("type") != event_name:
                    raise AnthropicTransportError("Anthropic SSE event name/type mismatch")
                yield event
            event_name, data = None, []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data.append(value)
    if data:
        # SSE streams may legally omit a final blank line.
        raw = "\n".join(data)
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AnthropicTransportError("Malformed trailing Anthropic SSE JSON") from exc
        if not isinstance(event, dict):
            raise AnthropicTransportError("Anthropic SSE event must be an object")
        if event_name and "type" not in event:
            event["type"] = event_name
        elif event_name and event.get("type") != event_name:
            raise AnthropicTransportError("Anthropic SSE event name/type mismatch")
        yield event


@dataclass
class _Block:
    kind: str
    value: dict[str, Any]
    json_parts: list[str] = field(default_factory=list)


class AnthropicStreamDecoder:
    """Stateful Messages event decoder producing compatibility chunks and envelope."""

    def __init__(
        self,
        *,
        bundle: CompiledAnthropicToolBundle,
        provider_fingerprint: str,
        model_fingerprint: str,
        thinking_fingerprint: str,
    ) -> None:
        self.bundle = bundle
        self.provider_fingerprint = provider_fingerprint
        self.model_fingerprint = model_fingerprint
        self.thinking_fingerprint = thinking_fingerprint
        self.blocks: dict[int, _Block] = {}
        self.tool_indices: dict[int, int] = {}
        self.usage: dict[str, Any] = {}
        self.stop_reason: str | None = None
        self.stop_details: dict[str, Any] = {}
        self.message_id: str | None = None
        self.started = False
        self.stopped = False
        self.terminal_delta_seen = False
        self.failed = False
        self._final_blocks: dict[int, dict[str, Any]] = {}
        self._tool_use_ids: set[str] = set()
        self._server_result_ids: set[str] = set()

    def feed(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise AnthropicTransportError("Anthropic event is missing type")
        if event_type == "error":
            self.failed = True
            raise _provider_error(event, event_type=event_type)
        if self.stopped:
            raise AnthropicTransportError("Anthropic stream has events after message_stop")
        if self.terminal_delta_seen and event_type != "message_stop":
            raise AnthropicTransportError(
                "Anthropic stream has events after terminal message_delta"
            )
        if event_type == "ping":
            return []
        if event_type == "message_start":
            if self.started:
                raise AnthropicTransportError("Duplicated Anthropic message_start")
            self.started = True
            message = event.get("message")
            if not isinstance(message, Mapping):
                raise AnthropicTransportError("Anthropic message_start lacks message")
            self.message_id = message.get("id") if isinstance(message.get("id"), str) else None
            self._merge_usage(message.get("usage"))
            return []
        if not self.started:
            raise AnthropicTransportError("Anthropic event arrived before message_start")
        if event_type == "content_block_start":
            return self._start_block(event)
        if event_type == "content_block_delta":
            return self._delta(event)
        if event_type == "content_block_stop":
            return self._stop_block(event)
        if event_type == "message_delta":
            if self.terminal_delta_seen:
                raise AnthropicTransportError("Duplicated Anthropic message_delta")
            if self.blocks:
                raise AnthropicTransportError(
                    "Anthropic message_delta preceded content block stops"
                )
            delta = event.get("delta")
            if not isinstance(delta, Mapping):
                raise AnthropicTransportError("Anthropic message_delta lacks delta")
            stop_reason = delta.get("stop_reason")
            if not isinstance(stop_reason, str) or not stop_reason:
                raise AnthropicTransportError("Anthropic terminal message_delta lacks stop_reason")
            if stop_reason not in _SUPPORTED_STOP_REASONS:
                raise AnthropicTransportError(
                    f"Unsupported Anthropic continuation stop reason: {stop_reason}"
                )
            self.stop_reason = stop_reason
            self.stop_details = dict(delta)
            self.terminal_delta_seen = True
            self._merge_usage(event.get("usage"))
            return [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": _map_stop_reason(self.stop_reason),
                        }
                    ]
                }
            ]
        if event_type == "message_stop":
            if not self.terminal_delta_seen:
                raise AnthropicTransportError("Anthropic message_stop lacks terminal message_delta")
            if self.blocks:
                raise AnthropicTransportError(
                    "Anthropic message stopped with incomplete content blocks"
                )
            self.stopped = True
            return [{"usage": _compat_usage(self.usage), "anthropic_native_events": [dict(event)]}]
        # Anthropic explicitly permits additions; only known required semantics are strict.
        return [{"anthropic_native_events": [dict(event)]}]

    def _start_block(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        index = _index(event)
        if index in self.blocks or index in self._final_blocks:
            raise AnthropicTransportError("Duplicated Anthropic content block index")
        block = event.get("content_block")
        if not isinstance(block, Mapping) or not isinstance(block.get("type"), str):
            raise AnthropicTransportError("Anthropic content_block_start lacks a block")
        kind = str(block["type"])
        if kind not in _REQUIRED_BLOCK_TYPES:
            raise AnthropicTransportError(f"Unsupported Anthropic required block type: {kind}")
        value = dict(block)
        if kind in {"tool_use", "server_tool_use"}:
            name = value.get("name")
            if not isinstance(name, str) or (
                kind == "server_tool_use" and name not in _TOOL_SEARCH_SERVER_TOOL_TYPES.values()
            ):
                raise AnthropicTransportError(f"Anthropic {kind} references an unknown tool name")
            if kind == "tool_use":
                _binding_for_wire_name(self.bundle, name)
            elif not any(tool.get("name") == name for tool in self.bundle.server_tools):
                raise AnthropicTransportError(
                    "Anthropic server_tool_use references an unfrozen server tool"
                )
            if not isinstance(value.get("id"), str) or not value["id"]:
                raise AnthropicTransportError(f"Anthropic {kind} lacks id")
            if value["id"] in self._tool_use_ids:
                raise AnthropicTransportError("Duplicate Anthropic tool-use ID")
            self._tool_use_ids.add(value["id"])
            if kind == "tool_use":
                self.tool_indices[index] = len(self.tool_indices)
            value["input"] = {}
        elif kind == "tool_search_tool_result":
            _validate_stream_tool_search_result(value, self.bundle)
            tool_use_id = str(value["tool_use_id"])
            if tool_use_id in self._server_result_ids:
                raise AnthropicTransportError("Duplicate Anthropic tool search result")
            self._server_result_ids.add(tool_use_id)
        elif kind == "text":
            value["text"] = str(value.get("text") or "")
        elif kind == "thinking":
            thinking = value.get("thinking")
            if not isinstance(thinking, str):
                raise AnthropicTransportError("Anthropic thinking block requires a thinking string")
            value["thinking"] = thinking
            value.pop("signature", None)
        elif kind == "redacted_thinking":
            value["data"] = str(value.get("data") or "")
        self.blocks[index] = _Block(kind, value)
        return []

    def _delta(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        index = _index(event)
        block = self.blocks.get(index)
        if block is None:
            raise AnthropicTransportError("Anthropic delta references unopened content block")
        delta = event.get("delta")
        if not isinstance(delta, Mapping) or not isinstance(delta.get("type"), str):
            raise AnthropicTransportError("Anthropic content delta is invalid")
        kind = str(delta["type"])
        if kind == "text_delta" and block.kind == "text" and isinstance(delta.get("text"), str):
            block.value["text"] += delta["text"]
            return [{"choices": [{"index": 0, "delta": {"content": delta["text"]}}]}]
        if (
            kind == "thinking_delta"
            and block.kind == "thinking"
            and isinstance(delta.get("thinking"), str)
        ):
            block.value["thinking"] += delta["thinking"]
            return [{"choices": [{"index": 0, "delta": {"reasoning_content": delta["thinking"]}}]}]
        if (
            kind == "signature_delta"
            and block.kind == "thinking"
            and isinstance(delta.get("signature"), str)
        ):
            block.value["signature"] = str(block.value.get("signature") or "") + delta["signature"]
            return []
        if (
            kind == "redacted_thinking_delta"
            and block.kind == "redacted_thinking"
            and isinstance(delta.get("data"), str)
        ):
            block.value["data"] += delta["data"]
            return []
        if (
            kind == "input_json_delta"
            and block.kind in {"tool_use", "server_tool_use"}
            and isinstance(delta.get("partial_json"), str)
        ):
            block.json_parts.append(delta["partial_json"])
            # Keep the raw stream observable without exposing incomplete,
            # provider-facing arguments through the compatibility projection.
            return [{"anthropic_native_events": [dict(event)]}]
        raise AnthropicTransportError("Out-of-order or unsupported Anthropic content delta")

    def _stop_block(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        index = _index(event)
        block = self.blocks.pop(index, None)
        if block is None:
            raise AnthropicTransportError("Anthropic content stop references unopened block")
        if block.kind in {"tool_use", "server_tool_use"}:
            raw = "".join(block.json_parts)
            try:
                parsed = json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                raise AnthropicTransportError("Malformed Anthropic tool input JSON") from exc
            if not isinstance(parsed, dict):
                raise AnthropicTransportError("Anthropic tool input must decode to an object")
            block.value["input"] = parsed
        self._final_blocks[index] = block.value
        if block.kind == "thinking":
            if not isinstance(block.value.get("thinking"), str) or not block.value.get("signature"):
                raise AnthropicTransportError("Incomplete Anthropic thinking block")
            return [
                {"choices": [{"index": 0, "delta": {"provider_thinking_blocks": [block.value]}}]}
            ]
        if block.kind == "tool_use":
            binding = _binding_for_wire_name(self.bundle, str(block.value["name"]))
            canonical_input = _decode_argument_aliases(
                block.value["input"], binding.reverse_argument_aliases
            )
            return [
                _tool_chunk(
                    self.tool_indices[index],
                    block.value,
                    json.dumps(canonical_input, ensure_ascii=False, separators=(",", ":")),
                    canonical_name=binding.canonical_name,
                )
            ]
        return []

    def _merge_usage(self, usage: Any) -> None:
        if isinstance(usage, Mapping):
            self.usage.update(
                {str(key): value for key, value in usage.items() if value is not None}
            )

    def envelope(self) -> AnthropicNativeEnvelope:
        if not self.stopped:
            raise AnthropicTransportError("Anthropic stream ended before message_stop")
        if self.failed:
            raise AnthropicTransportError("Anthropic stream cannot succeed after an in-band error")
        blocks = tuple(self._ordered_blocks())
        return AnthropicNativeEnvelope(
            native_blocks=blocks,
            stop_reason=self.stop_reason,
            stop_details=self.stop_details,
            usage=self.usage,
            pending_client_message_id=None,
            pending_server_message_id=self.message_id,
            bundle_fingerprint=self.bundle.fingerprint,
            provider_fingerprint=self.provider_fingerprint,
            model_fingerprint=self.model_fingerprint,
            thinking_fingerprint=self.thinking_fingerprint,
            continuation_status=AnthropicContinuationStatus.CONTINUABLE,
        )

    def _ordered_blocks(self) -> list[dict[str, Any]]:
        return [self._final_blocks[index] for index in sorted(self._final_blocks)]


def _index(event: Mapping[str, Any]) -> int:
    value = event.get("index")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnthropicTransportError("Anthropic content event has an invalid index")
    return value


def _map_stop_reason(reason: str | None) -> str:
    if reason == "tool_use":
        return "tool_calls"
    if reason in {"max_tokens", "model_context_window_exceeded"}:
        return "length"
    if reason == "pause_turn":
        # WS5c owns bounded provider replay; do not erase its continuation signal.
        return "pause_turn"
    return "stop"


def _validate_stream_tool_search_result(
    block: Mapping[str, Any],
    bundle: CompiledAnthropicToolBundle,
) -> None:
    tool_use_id = block.get("tool_use_id")
    content = block.get("content")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise AnthropicTransportError("Anthropic tool search result lacks server tool ID")
    if not isinstance(content, Mapping) or set(content) != {"type", "tool_references"}:
        raise AnthropicTransportError("Malformed Anthropic tool search result")
    if content.get("type") != "tool_search_tool_search_result":
        raise AnthropicTransportError("Unsupported Anthropic tool search result content")
    references = content.get("tool_references")
    if not isinstance(references, (list, tuple)):
        raise AnthropicTransportError("Malformed Anthropic tool search references")
    wire_names = {binding.wire_name for binding in bundle.bindings}
    for reference in references:
        if (
            not isinstance(reference, Mapping)
            or set(reference) != {"type", "tool_name"}
            or reference.get("type") != "tool_reference"
            or not isinstance(reference.get("tool_name"), str)
            or reference["tool_name"] not in wire_names
        ):
            raise AnthropicTransportError("Malformed or unknown Anthropic tool search reference")


def _compat_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(usage)
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if isinstance(input_tokens, int):
        result["prompt_tokens"] = input_tokens
    if isinstance(output_tokens, int):
        result["completion_tokens"] = output_tokens
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        result["total_tokens"] = input_tokens + output_tokens
    return result


def _binding_for_wire_name(bundle: CompiledAnthropicToolBundle, wire_name: str) -> Any:
    matches = [binding for binding in bundle.bindings if binding.wire_name == wire_name]
    if len(matches) != 1:
        raise AnthropicTransportError(
            "Anthropic tool_use references an unknown or ambiguous wire tool name"
        )
    binding = matches[0]
    if not all((binding.wire_name, binding.canonical_name, binding.stable_id)):
        raise AnthropicTransportError("Anthropic tool binding is incomplete")
    return binding


def _decode_argument_aliases(value: Any, aliases: Mapping[str, Any]) -> Any:
    try:
        return reverse_tool_argument_aliases(value, aliases)
    except (TypeError, ValueError) as exc:
        raise AnthropicTransportError(
            "Anthropic argument aliases are invalid or ambiguous"
        ) from exc


def _tool_chunk(
    index: int, block: Mapping[str, Any], arguments: str, *, canonical_name: str
) -> dict[str, Any]:
    function: dict[str, Any] = {"arguments": arguments, "name": canonical_name}
    tool: dict[str, Any] = {
        "index": index,
        "function": function,
        "id": block["id"],
        "type": "function",
    }
    return {"choices": [{"index": 0, "delta": {"tool_calls": [tool]}}]}


class AnthropicMessagesClient:
    """Auth-agnostic native Messages client.

    The resolver receives only a credential reference. Its returned secret is
    used to construct the immediate HTTP request and is never retained by the
    client, request context, decoder, or result metadata.
    """

    def __init__(
        self,
        credential_resolver: CredentialResolver,
        *,
        timeout: float = 120.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credential_resolver = credential_resolver
        self._timeout = timeout
        self._http_client = http_client

    async def complete(
        self,
        context: ResolvedAnthropicRequestContext,
        request: Mapping[str, Any],
        bundle: CompiledAnthropicToolBundle,
        *,
        provider_fingerprint: str,
        model_fingerprint: str,
    ) -> dict[str, Any]:
        """Execute a non-streaming request and produce native-compatible chat data."""

        response, owned_client = await self._request(
            context,
            _payload(context, request, bundle, stream=False),
            request=request,
        )
        try:
            await response.aread()
            data = response.json()
        finally:
            await response.aclose()
            if owned_client is not None:
                await owned_client.aclose()
        if not isinstance(data, dict):
            raise AnthropicTransportError("Anthropic Messages response must be an object")
        if data.get("type") == "error":
            raise _provider_error(data, event_type="response")
        envelope = _envelope_from_message(
            data,
            bundle=bundle,
            provider_fingerprint=provider_fingerprint,
            model_fingerprint=model_fingerprint,
            thinking_fingerprint=context.thinking_fingerprint,
        )
        return _chat_response(data, context.model, envelope, bundle)

    async def stream(
        self,
        context: ResolvedAnthropicRequestContext,
        request: Mapping[str, Any],
        bundle: CompiledAnthropicToolBundle,
        *,
        provider_fingerprint: str,
        model_fingerprint: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute a streaming request, closing response/client on cancellation."""

        response, owned_client = await self._request(
            context,
            _payload(context, request, bundle, stream=True),
            request=request,
        )
        decoder = AnthropicStreamDecoder(
            bundle=bundle,
            provider_fingerprint=provider_fingerprint,
            model_fingerprint=model_fingerprint,
            thinking_fingerprint=context.thinking_fingerprint,
        )
        try:
            async for event in decode_sse(response.aiter_lines()):
                for chunk in decoder.feed(event):
                    yield chunk
            envelope = decoder.envelope()
            yield {
                "anthropic_native_envelope": envelope.to_dict(),
                "anthropic_native_events": [{"type": "message_stop"}],
            }
        finally:
            await response.aclose()
            if owned_client is not None:
                await owned_client.aclose()

    async def _request(
        self,
        context: ResolvedAnthropicRequestContext,
        payload: Mapping[str, Any],
        *,
        request: Mapping[str, Any],
    ) -> tuple[httpx.Response, httpx.AsyncClient | None]:
        if context.credential_ref is None:
            raise ValueError("Anthropic Messages transport requires credential_ref")
        credential = await self._credential_resolver(context.credential_ref)
        headers = _request_headers(context, credential, request)
        owned_client = (
            None if self._http_client is not None else httpx.AsyncClient(timeout=self._timeout)
        )
        client = self._http_client or owned_client
        assert client is not None
        try:
            http_request = client.build_request(
                "POST", _request_endpoint(context), headers=headers, json=payload
            )
            response = await client.send(http_request, stream=True)
            if response.status_code >= 400:
                body: dict[str, Any] = {}
                try:
                    await response.aread()
                    parsed = response.json()
                    if isinstance(parsed, dict):
                        body = parsed
                finally:
                    await response.aclose()
                    if owned_client is not None:
                        await owned_client.aclose()
                raise _provider_error(
                    body,
                    event_type="http",
                    status_code=response.status_code,
                    retry_after_seconds=retry_after_seconds_from_headers(response.headers),
                )
            return response, owned_client
        except httpx.HTTPError as exc:
            if owned_client is not None:
                await owned_client.aclose()
            raise AnthropicTransportError(
                "Anthropic Messages transport request failed",
                payload=classify_llm_exception(exc),
            ) from exc


def _envelope_from_message(
    message: Mapping[str, Any],
    *,
    bundle: CompiledAnthropicToolBundle,
    provider_fingerprint: str,
    model_fingerprint: str,
    thinking_fingerprint: str,
) -> AnthropicNativeEnvelope:
    content = message.get("content")
    if not isinstance(content, list) or not all(isinstance(block, Mapping) for block in content):
        raise AnthropicTransportError("Anthropic response has invalid content blocks")
    for block in content:
        if block.get("type") not in _REQUIRED_BLOCK_TYPES:
            raise AnthropicTransportError(
                f"Unsupported Anthropic required block type: {block.get('type')}"
            )
        if block.get("type") == "tool_use":
            name = block.get("name")
            input_value = block.get("input")
            if not isinstance(name, str) or not isinstance(input_value, Mapping):
                raise AnthropicTransportError("Anthropic tool_use has incomplete binding or input")
            _binding_for_wire_name(bundle, name)
        elif block.get("type") == "server_tool_use":
            name = block.get("name")
            if not isinstance(name, str) or not any(
                tool.get("name") == name for tool in bundle.server_tools
            ):
                raise AnthropicTransportError(
                    "Anthropic server_tool_use references an unfrozen server tool"
                )
    stop_reason = message.get("stop_reason")
    if stop_reason is not None and (
        not isinstance(stop_reason, str) or stop_reason not in _SUPPORTED_STOP_REASONS
    ):
        raise AnthropicTransportError(
            f"Unsupported Anthropic continuation stop reason: {stop_reason}"
        )
    envelope = AnthropicNativeEnvelope(
        native_blocks=tuple(dict(block) for block in content),
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        stop_details={"stop_sequence": message.get("stop_sequence")}
        if message.get("stop_sequence") is not None
        else {},
        usage=dict(message["usage"]) if isinstance(message.get("usage"), Mapping) else {},
        pending_client_message_id=None,
        pending_server_message_id=message.get("id") if isinstance(message.get("id"), str) else None,
        bundle_fingerprint=bundle.fingerprint,
        provider_fingerprint=provider_fingerprint,
        model_fingerprint=model_fingerprint,
        thinking_fingerprint=thinking_fingerprint,
    )
    seen_server_result_ids: set[str] = set()
    for block in envelope.native_blocks:
        if block.get("type") == "tool_search_tool_result":
            _validate_stream_tool_search_result(block, bundle)
            tool_use_id = str(block["tool_use_id"])
            if tool_use_id in seen_server_result_ids:
                raise AnthropicTransportError("Duplicate Anthropic tool search result")
            seen_server_result_ids.add(tool_use_id)
    return envelope


def _chat_response(
    raw: Mapping[str, Any],
    model: str,
    envelope: AnthropicNativeEnvelope,
    bundle: CompiledAnthropicToolBundle,
) -> dict[str, Any]:
    text = "".join(
        str(block["text"]) for block in envelope.native_blocks if block.get("type") == "text"
    )
    tool_calls = []
    for block in envelope.native_blocks:
        if block.get("type") != "tool_use":
            continue
        binding = _binding_for_wire_name(bundle, str(block["name"]))
        input_value = block.get("input")
        if not isinstance(input_value, Mapping):
            raise AnthropicTransportError("Anthropic tool_use input must be an object")
        tool_calls.append(
            {
                "id": block["id"],
                "type": "function",
                "function": {
                    "name": binding.canonical_name,
                    "arguments": json.dumps(
                        _decode_argument_aliases(input_value, binding.reverse_argument_aliases),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
        )
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    thinking = [
        dict(block)
        for block in envelope.native_blocks
        if block.get("type") in {"thinking", "redacted_thinking"}
    ]
    if thinking:
        message["thinking_blocks"] = thinking
    return {
        "id": raw.get("id"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": raw.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _map_stop_reason(envelope.stop_reason),
            }
        ],
        "usage": _compat_usage(envelope.usage),
        "anthropic_native_envelope": envelope.to_dict(),
    }
