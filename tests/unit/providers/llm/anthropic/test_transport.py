from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import MappingProxyType

import httpx
import pytest

from cognis.core.tool_exposure import _normalize_anthropic_tool_schema_arguments
from cognis.models.config import ModelInfo
from cognis.providers.llm.anthropic import (
    AnthropicAuthPolicy,
    AnthropicLocation,
    AnthropicNativeEnvelope,
    AnthropicProtocol,
    AnthropicStreamDecoder,
    AnthropicToolBinding,
    AnthropicTransportError,
    CompiledAnthropicToolBundle,
    ModelInfoCapabilitySnapshot,
    ResolvedAnthropicRequestContext,
    build_anthropic_headers,
    compile_anthropic_tool_bundle,
    decode_sse,
)
from cognis.providers.llm.anthropic.transport import (
    AnthropicMessagesClient,
    _chat_response,
    _envelope_from_message,
    _payload,
    _request_endpoint,
    _request_headers,
)


def _bundle(
    *,
    server_tools: tuple[dict[str, object], ...] = (
        {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"},
        {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"},
    ),
) -> CompiledAnthropicToolBundle:
    return CompiledAnthropicToolBundle(
        wire_tools=(
            {"name": "mcp_very_exact_name", "input_schema": {"type": "object"}},
            {"name": "builtin_apply_patch", "input_schema": {"type": "object"}},
        ),
        bindings=(
            AnthropicToolBinding("mcp_very_exact_name", "mcp_very_exact_name", "mcp", {}),
            AnthropicToolBinding("builtin_apply_patch", "builtin_apply_patch", "builtin", {}),
        ),
        server_tools=server_tools,
    )


def _context(policy: AnthropicAuthPolicy) -> ResolvedAnthropicRequestContext:
    return ResolvedAnthropicRequestContext(
        provider_id="anthropic",
        model="claude-test",
        endpoint="https://api.anthropic.com/v1/messages",
        protocol=AnthropicProtocol.ANTHROPIC_MESSAGES,
        location=AnthropicLocation.CONTROLLER,
        auth_policy=policy,
        credential_ref="$credential:token",
        model_info=ModelInfoCapabilitySnapshot.from_model_info(ModelInfo(model_id="claude-test")),
        thinking_fingerprint="thinking",
        chain_id="chain",
    )


def _decoder() -> AnthropicStreamDecoder:
    return AnthropicStreamDecoder(
        bundle=_bundle(),
        provider_fingerprint="provider",
        model_fingerprint="model",
        thinking_fingerprint="thinking",
    )


def _aliased_bundle() -> CompiledAnthropicToolBundle:
    return CompiledAnthropicToolBundle(
        wire_tools=({"name": "mcp_safe_name", "input_schema": {"type": "object"}},),
        bindings=(
            AnthropicToolBinding(
                "mcp_safe_name",
                "mcp__server__execute",
                "stable-mcp-execute",
                {
                    "arg_1": "query",
                    "plain_1": "payload",
                    "nested_1": {
                        "original": "options",
                        "properties": {"field_1": "field"},
                    },
                    "rows_1": {
                        "original": "rows",
                        "properties": {"field_1": "field"},
                    },
                    "*": {
                        "original": "*",
                        "properties": {"field_1": "field"},
                    },
                },
            ),
        ),
    )


def _recursive_alias_bundle() -> CompiledAnthropicToolBundle:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "recursive",
                "description": "recursive",
                "x-stable-tool-id": "builtin:recursive",
                "parameters": {
                    "type": "object",
                    "properties": {"root": {"$ref": "#/$defs/Node"}},
                    "$defs": {
                        "Node": {
                            "type": "object",
                            "properties": {
                                "child[]": {
                                    "anyOf": [
                                        {"$ref": "#/$defs/Node"},
                                        {"type": "null"},
                                    ]
                                }
                            },
                        }
                    },
                },
            },
        }
    ]
    normalized_tools, aliases = _normalize_anthropic_tool_schema_arguments(
        tools,
        alias_map={},
    )
    return compile_anthropic_tool_bundle(
        normalized_tools,
        argument_alias_map=aliases,
        stable_id_map={"recursive": "builtin:recursive"},
    )


def test_stream_and_non_stream_transport_reverse_recursive_frozen_aliases() -> None:
    bundle = CompiledAnthropicToolBundle.from_dict(_recursive_alias_bundle().to_dict())
    wire_name = bundle.bindings[0].wire_name
    decoder = AnthropicStreamDecoder(
        bundle=bundle,
        provider_fingerprint="provider",
        model_fingerprint="model",
        thinking_fingerprint="thinking",
    )
    decoder.feed({"type": "message_start", "message": {}})
    decoder.feed(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "call", "name": wire_name},
        }
    )
    decoder.feed(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"root":{"child":{"child":null}}}',
            },
        }
    )
    chunks = decoder.feed({"type": "content_block_stop", "index": 0})
    stream_arguments = chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(stream_arguments) == {"root": {"child[]": {"child[]": None}}}

    raw = {
        "id": "msg",
        "model": "claude-opus-4-8",
        "content": [
            {
                "type": "tool_use",
                "id": "call",
                "name": wire_name,
                "input": {"root": {"child": {"child": None}}},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {},
    }
    envelope = _envelope_from_message(
        raw,
        bundle=bundle,
        provider_fingerprint="provider",
        model_fingerprint="model",
        thinking_fingerprint="thinking",
    )
    response = _chat_response(raw, "claude-opus-4-8", envelope, bundle)
    non_stream_arguments = response["choices"][0]["message"]["tool_calls"][0]["function"][
        "arguments"
    ]
    assert json.loads(non_stream_arguments) == {"root": {"child[]": {"child[]": None}}}


def test_tool_choice_translation_supports_auto_any_named_none_and_parallel_control() -> None:
    bundle = _bundle()
    context = _context(AnthropicAuthPolicy.API_KEY)
    assert _payload(context, {"tool_choice": "auto"}, bundle, stream=False)["tool_choice"] == {
        "type": "auto"
    }
    assert _payload(
        context,
        {"tool_choice": "required", "parallel_tool_calls": False},
        bundle,
        stream=False,
    )["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert _payload(
        context,
        {
            "tool_choice": {
                "type": "function",
                "function": {"name": bundle.bindings[1].canonical_name},
            },
            "disable_parallel_tool_use": False,
        },
        bundle,
        stream=False,
    )["tool_choice"] == {
        "type": "tool",
        "name": bundle.bindings[1].wire_name,
        "disable_parallel_tool_use": False,
    }
    disabled = _payload(context, {"tool_choice": "none"}, bundle, stream=False)
    assert "tools" not in disabled
    assert "tool_choice" not in disabled


@pytest.mark.parametrize("thinking_type", ["enabled", "adaptive"])
def test_extended_thinking_rejects_forced_tool_choice(thinking_type: str) -> None:
    with pytest.raises(AnthropicTransportError, match="Extended thinking"):
        _payload(
            _context(AnthropicAuthPolicy.API_KEY),
            {"thinking": {"type": thinking_type}, "tool_choice": "required"},
            _bundle(),
            stream=False,
        )


def test_disabled_adaptive_thinking_allows_forced_tool_choice() -> None:
    payload = _payload(
        _context(AnthropicAuthPolicy.API_KEY),
        {"thinking": {"type": "disabled"}, "tool_choice": "required"},
        _bundle(),
        stream=False,
    )

    assert payload["tool_choice"] == {"type": "any"}


def test_request_only_accepts_official_tool_search_server_tools() -> None:
    server_tools = (
        {
            "type": "tool_search_tool_regex_20251119",
            "name": "tool_search_tool_regex",
        },
    )
    payload = _payload(
        _context(AnthropicAuthPolicy.API_KEY),
        {
            "cognis_anthropic_server_tools": [
                {
                    "type": "tool_search_tool_regex_20251119",
                    "name": "tool_search_tool_regex",
                }
            ]
        },
        _bundle(server_tools=server_tools),
        stream=False,
    )
    assert payload["tools"][-1] == {
        "type": "tool_search_tool_regex_20251119",
        "name": "tool_search_tool_regex",
    }
    assert [tool["name"] for tool in payload["tools"]].count("tool_search_tool_regex") == 1
    with pytest.raises(AnthropicTransportError, match="Unsupported"):
        _payload(
            _context(AnthropicAuthPolicy.API_KEY),
            {"cognis_anthropic_server_tools": [{"type": "computer_20250124", "name": "computer"}]},
            _bundle(),
            stream=False,
        )


def _contains_immutable_container(value: object) -> bool:
    if isinstance(value, (MappingProxyType, tuple)):
        return True
    if isinstance(value, dict):
        return any(_contains_immutable_container(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_immutable_container(item) for item in value)
    return False


def test_payload_deeply_materializes_frozen_bundle_and_native_request_data() -> None:
    bundle = compile_anthropic_tool_bundle(
        [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "x-stable-tool-id": "builtin-bash",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "options": {
                                "type": "object",
                                "properties": {
                                    "shell": {"type": "string"},
                                    "env": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {"name": {"type": "string"}},
                                            "required": ["name"],
                                            "additionalProperties": False,
                                        },
                                    },
                                },
                                "additionalProperties": False,
                            },
                        },
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                    "input_examples": [
                        {"command": "printf ok", "options": {"env": [{"name": "TERM"}]}}
                    ],
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mcp_search",
                    "x-stable-tool-id": "mcp-search",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "filters": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"field": {"type": "string"}},
                                    "required": ["field"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
        ]
    )
    envelope = AnthropicNativeEnvelope(
        native_blocks=(
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": bundle.bindings[0].wire_name,
                "input": {"command": "printf ok", "options": {"env": [{"name": "TERM"}]}},
                "caller": {"type": "direct"},
            },
        ),
        stop_reason="tool_use",
        stop_details={},
        usage={},
        pending_client_message_id=None,
        pending_server_message_id=None,
        bundle_fingerprint=bundle.fingerprint,
        provider_fingerprint="provider",
        model_fingerprint="model",
        thinking_fingerprint="thinking",
    )
    payload = _payload(
        _context(AnthropicAuthPolicy.API_KEY),
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": envelope.native_blocks,
                },
                {
                    "role": "user",
                    "content": (
                        MappingProxyType(
                            {
                                "type": "text",
                                "text": "continue",
                                "cache_control": MappingProxyType(
                                    {"type": "ephemeral", "ttl": "5m"}
                                ),
                            }
                        ),
                    ),
                },
            ],
            "thinking": MappingProxyType({"type": "enabled", "budget_tokens": 1024}),
            "metadata": MappingProxyType({"user_id": "user-1"}),
            "cognis_internal": MappingProxyType({"must": "not leak"}),
        },
        bundle,
        stream=False,
    )

    assert bundle.wire_tools[0]["input_schema"]["properties"]["command"]["type"] == "string"
    assert isinstance(bundle.wire_tools[0], MappingProxyType)
    assert payload["messages"][0]["content"][0]["input"]["options"]["env"][0]["name"] == "TERM"
    assert payload["messages"][0]["content"][0]["caller"] == {"type": "direct"}
    assert payload["messages"][1]["content"][0]["cache_control"]["ttl"] == "5m"
    assert payload["tools"][0]["input_examples"][0]["options"]["env"][0]["name"] == "TERM"
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert "cognis_internal" not in payload
    assert not _contains_immutable_container(payload)
    json.dumps(payload)
    encoded_request = httpx.Request("POST", "https://api.anthropic.com/v1/messages", json=payload)
    assert json.loads(encoded_request.content) == payload

    payload["tools"][0]["input_schema"]["properties"]["command"]["type"] = "number"
    assert bundle.wire_tools[0]["input_schema"]["properties"]["command"]["type"] == "string"


@pytest.mark.asyncio
async def test_unsupported_payload_value_fails_before_http_request() -> None:
    sent_requests = 0
    cyclic: list[object] = []
    cyclic.append(cyclic)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal sent_requests
        sent_requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AnthropicMessagesClient(
            lambda _reference: _resolved_credential("secret"),
            http_client=http_client,
        )
        for content in (object(), cyclic):
            with pytest.raises(AnthropicTransportError, match="not JSON-compatible"):
                await client.complete(
                    _context(AnthropicAuthPolicy.API_KEY),
                    {"messages": [{"role": "user", "content": content}]},
                    _bundle(),
                    provider_fingerprint="provider",
                    model_fingerprint="model",
                )

    assert sent_requests == 0


@pytest.mark.asyncio
async def test_http_rate_limit_preserves_retry_after() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "23"},
            json={
                "error": {
                    "type": "rate_limit_error",
                    "message": "This request would exceed your account's rate limit.",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AnthropicMessagesClient(
            lambda _reference: _resolved_credential("secret"),
            http_client=http_client,
        )
        with pytest.raises(AnthropicTransportError) as exc_info:
            await client.complete(
                _context(AnthropicAuthPolicy.API_KEY),
                {"messages": [{"role": "user", "content": "hello"}]},
                _bundle(),
                provider_fingerprint="provider",
                model_fingerprint="model",
            )

    assert exc_info.value.status_code == 429
    assert exc_info.value.to_payload()["category"] == "rate_limit"
    assert exc_info.value.to_payload()["retry_after_seconds"] == 23


async def _resolved_credential(value: str) -> str:
    return value


def test_prompt_cache_beta_is_merged_without_overriding_auth_headers() -> None:
    headers = _request_headers(
        _context(AnthropicAuthPolicy.API_KEY),
        "secret",
        {
            "extra_headers": {
                "anthropic-beta": "prompt-caching-2024-07-31,extended-cache-ttl-2025-04-11",
                "authorization": "must-not-pass",
            }
        },
    )
    assert headers["x-api-key"] == "secret"
    assert "authorization" not in headers
    assert headers["anthropic-beta"] == ("prompt-caching-2024-07-31,extended-cache-ttl-2025-04-11")


def test_auth_headers_are_separated() -> None:
    api_headers = build_anthropic_headers(_context(AnthropicAuthPolicy.API_KEY), "secret")
    oauth_headers = build_anthropic_headers(_context(AnthropicAuthPolicy.OAUTH), "secret")
    assert api_headers["x-api-key"] == "secret"
    assert "authorization" not in api_headers and "anthropic-beta" not in api_headers
    assert oauth_headers["authorization"] == "Bearer secret"
    assert "x-api-key" not in oauth_headers and oauth_headers["anthropic-beta"]


def test_oauth_transport_boundary_forces_official_endpoint() -> None:
    context = _context(AnthropicAuthPolicy.OAUTH)
    object.__setattr__(context, "endpoint", "https://untrusted.example/v1/messages")
    assert _request_endpoint(context) == "https://api.anthropic.com/v1/messages?beta=true"


async def test_sse_multiline_ping_and_exact_parallel_tool_names() -> None:
    async def lines() -> AsyncIterator[str]:
        events = [
            {"type": "message_start", "message": {"id": "msg", "usage": {"input_tokens": 3}}},
            {"type": "ping"},
            {
                "type": "content_block_start",
                "index": 3,
                "content_block": {
                    "type": "tool_use",
                    "id": "a",
                    "name": "mcp_very_exact_name",
                    "caller": {"type": "direct"},
                },
            },
            {
                "type": "content_block_start",
                "index": 7,
                "content_block": {"type": "tool_use", "id": "b", "name": "builtin_apply_patch"},
            },
            {
                "type": "content_block_delta",
                "index": 7,
                "delta": {"type": "input_json_delta", "partial_json": '{"b":'},
            },
            {
                "type": "content_block_delta",
                "index": 3,
                "delta": {"type": "input_json_delta", "partial_json": '{"a":1}'},
            },
            {
                "type": "content_block_delta",
                "index": 7,
                "delta": {"type": "input_json_delta", "partial_json": "2}"},
            },
            {"type": "content_block_stop", "index": 3},
            {"type": "content_block_stop", "index": 7},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4},
            },
            {"type": "message_stop"},
        ]
        for event in events:
            encoded = json.dumps(event)
            yield "event: " + event["type"]
            yield "data: " + encoded[:1]
            yield "data: " + encoded[1:]
            yield ""

    decoder = _decoder()
    chunks = [chunk async for event in decode_sse(lines()) for chunk in decoder.feed(event)]
    tool_calls = [
        call
        for chunk in chunks
        for choice in chunk.get("choices", [])
        for call in choice.get("delta", {}).get("tool_calls", [])
    ]
    assert [(call["index"], call.get("function", {}).get("name")) for call in tool_calls[:2]] == [
        (0, "mcp_very_exact_name"),
        (1, "builtin_apply_patch"),
    ]
    envelope = decoder.envelope()
    assert envelope.usage == {"input_tokens": 3, "output_tokens": 4}
    assert [block["input"] for block in envelope.native_blocks if block["type"] == "tool_use"] == [
        {"a": 1},
        {"b": 2},
    ]
    assert envelope.native_blocks[0]["caller"] == {"type": "direct"}
    replay_payload = _payload(
        _context(AnthropicAuthPolicy.API_KEY),
        {
            "messages": (
                {"role": "assistant", "content": envelope.native_blocks},
                {"role": "user", "content": "continue"},
            )
        },
        _bundle(),
        stream=False,
    )
    assert replay_payload["messages"][0]["content"][0]["caller"] == {"type": "direct"}


@pytest.mark.parametrize("trailing", [False, True])
async def test_sse_rejects_conflicting_event_and_json_types(trailing: bool) -> None:
    async def lines() -> AsyncIterator[str]:
        yield "event: error"
        yield 'data: {"type":"ping"}'
        if not trailing:
            yield ""

    with pytest.raises(AnthropicTransportError, match="name/type mismatch"):
        async for _event in decode_sse(lines()):
            pass


def test_wire_tool_projection_uses_exact_binding_and_recursive_argument_aliases() -> None:
    decoder = AnthropicStreamDecoder(
        bundle=_aliased_bundle(),
        provider_fingerprint="provider",
        model_fingerprint="model",
        thinking_fingerprint="thinking",
    )
    decoder.feed({"type": "message_start", "message": {}})
    decoder.feed(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "call", "name": "mcp_safe_name"},
        }
    )
    liveness = decoder.feed(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "input_json_delta",
                "partial_json": (
                    '{"arg_1":"SELECT","nested_1":{"field_1":"value"},'
                    '"rows_1":[{"field_1":"row"}],"plain_1":{"field_1":"literal"},'
                    '"dynamic":{"field_1":"map"}}'
                ),
            },
        }
    )
    assert liveness == [
        {
            "anthropic_native_events": [
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": (
                            '{"arg_1":"SELECT","nested_1":{"field_1":"value"},'
                            '"rows_1":[{"field_1":"row"}],"plain_1":{"field_1":"literal"},'
                            '"dynamic":{"field_1":"map"}}'
                        ),
                    },
                }
            ]
        }
    ]
    chunks = decoder.feed({"type": "content_block_stop", "index": 0})
    call = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["function"]["name"] == "mcp__server__execute"
    assert json.loads(call["function"]["arguments"]) == {
        "query": "SELECT",
        "options": {"field": "value"},
        "rows": [{"field": "row"}],
        "payload": {"field_1": "literal"},
        "dynamic": {"field": "map"},
    }
    decoder.feed({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
    decoder.feed({"type": "message_stop"})
    assert decoder.envelope().native_blocks[0]["name"] == "mcp_safe_name"


def test_non_streaming_projection_reverses_the_same_alias_tree() -> None:
    bundle = _aliased_bundle()
    envelope = AnthropicNativeEnvelope(
        native_blocks=(
            {
                "type": "tool_use",
                "id": "call",
                "name": "mcp_safe_name",
                "input": {"arg_1": "SELECT", "plain_1": {"field_1": "literal"}},
            },
        ),
        stop_reason="tool_use",
        stop_details={},
        usage={},
        pending_client_message_id=None,
        pending_server_message_id="message",
        bundle_fingerprint=bundle.fingerprint,
        provider_fingerprint="provider",
        model_fingerprint="model",
        thinking_fingerprint="thinking",
    )
    call = _chat_response({}, "claude-test", envelope, bundle)["choices"][0]["message"][
        "tool_calls"
    ][0]
    assert call["function"]["name"] == "mcp__server__execute"
    assert json.loads(call["function"]["arguments"]) == {
        "query": "SELECT",
        "payload": {"field_1": "literal"},
    }


def test_thinking_signature_and_malformed_or_unsupported_streams_fail_safely() -> None:
    decoder = _decoder()
    decoder.feed({"type": "message_start", "message": {}})
    decoder.feed(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }
    )
    decoder.feed(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "reason"},
        }
    )
    decoder.feed(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig"},
        }
    )
    decoder.feed({"type": "content_block_stop", "index": 0})
    with pytest.raises(AnthropicTransportError, match="unopened"):
        decoder.feed({"type": "content_block_stop", "index": 0})

    unsupported = _decoder()
    unsupported.feed({"type": "message_start", "message": {}})
    with pytest.raises(AnthropicTransportError, match="unknown"):
        unsupported.feed(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "server_tool_use"},
            }
        )


def test_omitted_adaptive_thinking_stream_is_persisted_and_replayed() -> None:
    decoder = _decoder()
    decoder.feed({"type": "message_start", "message": {}})
    decoder.feed(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }
    )
    decoder.feed(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "signed-omitted"},
        }
    )
    chunks = decoder.feed({"type": "content_block_stop", "index": 0})
    assert chunks[0]["choices"][0]["delta"]["provider_thinking_blocks"] == [
        {"type": "thinking", "thinking": "", "signature": "signed-omitted"}
    ]
    decoder.feed({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
    decoder.feed({"type": "message_stop"})

    envelope = AnthropicNativeEnvelope.from_dict(decoder.envelope().to_dict())
    assert envelope.native_blocks[0]["thinking"] == ""
    payload = _payload(
        _context(AnthropicAuthPolicy.API_KEY),
        {
            "messages": (
                {"role": "assistant", "content": envelope.native_blocks},
                {"role": "user", "content": "continue"},
            )
        },
        _bundle(),
        stream=False,
    )
    assert payload["messages"][0]["content"][0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "signed-omitted",
    }


@pytest.mark.parametrize("thinking", [None, False, 0, []])
def test_omitted_thinking_requires_explicit_string(thinking: object) -> None:
    decoder = _decoder()
    decoder.feed({"type": "message_start", "message": {}})
    content_block: dict[str, object] = {"type": "thinking"}
    if thinking is not None:
        content_block["thinking"] = thinking
    with pytest.raises(AnthropicTransportError, match="requires a thinking string"):
        decoder.feed(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": content_block,
            }
        )


@pytest.mark.parametrize(
    ("events", "error"),
    [
        (
            [
                {"type": "message_start", "message": {}},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "id",
                        "name": "mcp_very_exact_name",
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": "not-json"},
                },
                {"type": "content_block_stop", "index": 0},
            ],
            "Malformed",
        ),
        (
            [
                {"type": "message_start", "message": {}},
                {"type": "message_stop"},
                {"type": "message_stop"},
            ],
            "lacks terminal",
        ),
        (
            [
                {"type": "message_start", "message": {}},
                {"type": "error", "error": {"type": "overloaded_error", "message": "retry"}},
            ],
            "retry",
        ),
    ],
)
def test_malformed_terminal_and_in_band_errors_fail_safely(
    events: list[dict[str, object]], error: str
) -> None:
    decoder = _decoder()
    with pytest.raises(AnthropicTransportError, match=error):
        for event in events:
            decoder.feed(event)
        decoder.envelope()


@pytest.mark.parametrize(
    "event",
    [
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "ping"},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
    ],
)
def test_terminal_delta_requires_exactly_one_immediate_message_stop(
    event: dict[str, object],
) -> None:
    decoder = _decoder()
    decoder.feed({"type": "message_start", "message": {}})
    decoder.feed({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
    with pytest.raises(AnthropicTransportError, match="after terminal"):
        decoder.feed(event)


@pytest.mark.parametrize(
    "terminal_delta",
    [
        {"type": "message_delta", "delta": {}},
        {"type": "message_delta", "delta": {"stop_reason": 1}},
    ],
)
def test_terminal_delta_requires_a_string_stop_reason(terminal_delta: dict[str, object]) -> None:
    decoder = _decoder()
    decoder.feed({"type": "message_start", "message": {}})
    with pytest.raises(AnthropicTransportError, match="lacks stop_reason"):
        decoder.feed(terminal_delta)


def test_finalized_content_block_index_cannot_be_reopened() -> None:
    decoder = _decoder()
    decoder.feed({"type": "message_start", "message": {}})
    decoder.feed({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
    decoder.feed({"type": "content_block_stop", "index": 0})
    with pytest.raises(AnthropicTransportError, match="Duplicated"):
        decoder.feed({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})


def test_server_tool_search_stream_preserves_native_blocks_and_excludes_server_ids() -> None:
    decoder = _decoder()
    events = [
        {"type": "message_start", "message": {"id": "msg_server", "usage": {"input_tokens": 2}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "tool_search_tool_regex",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"pattern":"weather"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": {
                    "type": "tool_search_tool_search_result",
                    "tool_references": [
                        {"type": "tool_reference", "tool_name": "mcp_very_exact_name"}
                    ],
                },
            },
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "mcp_very_exact_name",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"query":"weather"}'},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]
    chunks = [chunk for event in events for chunk in decoder.feed(event)]
    calls = [
        call
        for chunk in chunks
        for choice in chunk.get("choices", [])
        for call in choice.get("delta", {}).get("tool_calls", [])
    ]
    assert [call["id"] for call in calls] == ["toolu_1"]
    envelope = decoder.envelope()
    assert envelope.client_tool_use_ids == ("toolu_1",)
    assert envelope.server_tool_use_ids == ("srvtoolu_1",)
    assert [block["type"] for block in envelope.native_blocks] == [
        "server_tool_use",
        "tool_search_tool_result",
        "tool_use",
    ]
    assert envelope.stop_details == {"stop_reason": "tool_use", "stop_sequence": None}


def test_pause_turn_and_mixed_server_client_blocks_replay_without_server_tool_calls() -> None:
    bundle = _bundle()
    content = [
        {"type": "thinking", "thinking": "inspect", "signature": "signature"},
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "tool_search_tool_bm25",
            "input": {"query": "patch"},
        },
        {
            "type": "tool_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": {
                "type": "tool_search_tool_search_result",
                "tool_references": [{"type": "tool_reference", "tool_name": "builtin_apply_patch"}],
            },
        },
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "builtin_apply_patch",
            "input": {"patch": "*** Begin Patch"},
        },
    ]
    envelope = _envelope_from_message(
        {
            "id": "msg_1",
            "content": content,
            "stop_reason": "pause_turn",
            "stop_sequence": "native-detail",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
        bundle=bundle,
        provider_fingerprint="provider",
        model_fingerprint="model",
        thinking_fingerprint="thinking",
    )
    response = _chat_response({}, "claude-test", envelope, bundle)
    assert response["choices"][0]["finish_reason"] == "pause_turn"
    assert [call["id"] for call in response["choices"][0]["message"]["tool_calls"]] == ["toolu_1"]
    assert response["anthropic_native_envelope"]["native_blocks"] == content
    assert response["anthropic_native_envelope"]["stop_reason"] == "pause_turn"


def test_server_tool_search_pause_turn_stream_is_continuable_without_client_calls() -> None:
    decoder = _decoder()
    for event in [
        {"type": "message_start", "message": {"id": "msg_pause"}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "server_tool_use",
                "id": "srvtoolu_pause",
                "name": "tool_search_tool_regex",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"pattern":"missing"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_search_tool_result",
                "tool_use_id": "srvtoolu_pause",
                "content": {
                    "type": "tool_search_tool_search_result",
                    "tool_references": [],
                },
            },
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "pause_turn"}},
        {"type": "message_stop"},
    ]:
        decoder.feed(event)
    envelope = decoder.envelope()
    assert envelope.stop_reason == "pause_turn"
    assert envelope.client_tool_use_ids == ()
    assert envelope.server_tool_use_ids == ("srvtoolu_pause",)


@pytest.mark.parametrize(
    "blocks",
    [
        [
            {
                "type": "server_tool_use",
                "id": "dup",
                "name": "tool_search_tool_regex",
                "input": {},
            },
            {
                "type": "tool_use",
                "id": "dup",
                "name": "mcp_very_exact_name",
                "input": {},
            },
        ],
    ],
)
def test_non_streaming_server_tool_protocol_rejects_malformed_references_and_ids(
    blocks: list[dict[str, object]],
) -> None:
    with pytest.raises((AnthropicTransportError, ValueError), match="unknown|unique"):
        _envelope_from_message(
            {"content": blocks, "stop_reason": "end_turn"},
            bundle=_bundle(),
            provider_fingerprint="provider",
            model_fingerprint="model",
            thinking_fingerprint="thinking",
        )


def test_non_streaming_deferred_server_result_can_arrive_in_later_response() -> None:
    envelope = _envelope_from_message(
        {
            "content": [
                {
                    "type": "tool_search_tool_result",
                    "tool_use_id": "srvtoolu_prior_response",
                    "content": {
                        "type": "tool_search_tool_search_result",
                        "tool_references": [
                            {"type": "tool_reference", "tool_name": "mcp_very_exact_name"}
                        ],
                    },
                }
            ],
            "stop_reason": "end_turn",
        },
        bundle=_bundle(),
        provider_fingerprint="provider",
        model_fingerprint="model",
        thinking_fingerprint="thinking",
    )
    assert envelope.native_blocks[0]["tool_use_id"] == "srvtoolu_prior_response"
