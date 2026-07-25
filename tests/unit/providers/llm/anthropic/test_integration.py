import json
from types import SimpleNamespace

import httpx
import pytest

from cognis.executor.backends.anthropic import AnthropicMessagesExecutorBackend
from cognis.executor.inference_types import CognisInferenceRequest, redact_inference_payload
from cognis.models.config import ModelInfo
from cognis.providers.llm.anthropic.contracts import (
    AnthropicNativeEnvelope,
    sha256_fingerprint,
)
from cognis.providers.llm.anthropic.integration import (
    NATIVE_CONTINUATION_REQUIRED_KWARG,
    NATIVE_REQUEST_CONTEXT_KWARG,
    NATIVE_TOOL_BUNDLE_KWARG,
    AnthropicContinuationRejected,
    build_native_chain,
    build_native_request,
    is_anthropic_native_provider,
)
from cognis.providers.llm.anthropic.transport import (
    AnthropicMessagesClient,
    build_anthropic_headers,
)
from cognis.providers.llm.anthropic_subscription import (
    CLAUDE_CODE_IDENTITY,
    CLAUDE_CODE_IDENTITY_BRIDGE,
    CLAUDE_CODE_USER_AGENT,
)
from cognis.providers.llm.litellm import LiteLLMProvider


def _provider(*, endpoint: str = "https://api.anthropic.com", protocol: str = "auto"):
    return SimpleNamespace(
        provider_id="anthropic",
        location="controller",
        config={
            "preset": "anthropic",
            "protocol": protocol,
            "api_base": endpoint,
            "auth_config": {"mode": "secret", "secret_name": "anthropic"},
        },
    )


@pytest.mark.asyncio
async def test_native_anthropic_client_uses_resolved_request_timeout() -> None:
    provider_impl = LiteLLMProvider.__new__(LiteLLMProvider)
    client, credential_ref = await provider_impl._native_anthropic_client(
        _provider(),
        {"api_key": "test-key", "timeout": 7.5},
    )

    assert client._timeout == 7.5
    assert credential_ref == "$credential:anthropic-api-key"


def test_official_api_key_provider_auto_uses_native_messages() -> None:
    provider = _provider()
    assert is_anthropic_native_provider(provider) is True
    context, payload, bundle = build_native_request(
        provider=provider,
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        messages=[{"role": "user", "content": "hello"}],
        request_kwargs={},
        credential_ref="$credential:test",
    )
    assert context.endpoint == "https://api.anthropic.com/v1/messages"
    assert context.credential_ref == "$credential:test"
    assert payload["messages"][0]["role"] == "user"
    assert bundle.wire_tools == ()
    headers = build_anthropic_headers(context, "secret")
    assert headers["x-api-key"] == "secret"
    assert "authorization" not in headers
    assert "anthropic-beta" not in headers


def test_native_request_preserves_adaptive_thinking_and_output_effort() -> None:
    _context, payload, _bundle = build_native_request(
        provider=_provider(),
        model="claude-opus-4-8",
        model_info=ModelInfo(model_id="claude-opus-4-8"),
        messages=[{"role": "user", "content": "hello"}],
        request_kwargs={
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "xhigh"},
        },
        credential_ref="$credential:test",
    )

    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "xhigh"}


@pytest.mark.asyncio
async def test_native_chain_uses_stable_actual_adaptive_thinking_fingerprint() -> None:
    provider = _provider()
    provider_impl = LiteLLMProvider.__new__(LiteLLMProvider)

    async def resolve_target(*_args: object, **_kwargs: object):
        return "claude-opus-4-8", provider

    provider_impl._resolve_model_target = resolve_target  # type: ignore[method-assign]
    model_info = ModelInfo(
        model_id="claude-opus-4-8",
        supports_reasoning=True,
        supports_extended_thinking=True,
    )

    first = await provider_impl.prepare_anthropic_native_chain(
        model="claude-opus-4-8",
        model_info=model_info,
        provider_id="anthropic",
        acting_user_email="owner@example.com",
        tools=[],
        alias_map={},
        stable_id_map={},
        argument_alias_map={},
        request_kwargs={},
    )
    restarted = await provider_impl.prepare_anthropic_native_chain(
        model="claude-opus-4-8",
        model_info=model_info,
        provider_id="anthropic",
        acting_user_email="owner@example.com",
        tools=[],
        alias_map={},
        stable_id_map={},
        argument_alias_map={},
        request_kwargs={"reasoning_effort": "default"},
    )

    assert first is not None
    assert restarted is not None
    first_context = first[NATIVE_REQUEST_CONTEXT_KWARG]
    restarted_context = restarted[NATIVE_REQUEST_CONTEXT_KWARG]
    assert first_context["thinking_fingerprint"] == sha256_fingerprint({"type": "adaptive"})
    assert restarted_context["thinking_fingerprint"] == first_context["thinking_fingerprint"]


def test_shared_reasoning_preparation_keeps_controller_executor_and_auth_parity() -> None:
    provider_impl = LiteLLMProvider.__new__(LiteLLMProvider)
    model_info = ModelInfo(
        model_id="claude-opus-4-8",
        supports_reasoning=True,
        supports_extended_thinking=True,
    )
    prepared_requests = []
    for location, auth_config in (
        ("controller", {"mode": "secret", "secret_name": "anthropic"}),
        ("controller", {"mode": "oauth", "provider": "anthropic_subscription"}),
        ("executor", {"mode": "secret", "secret_name": "anthropic"}),
    ):
        provider = SimpleNamespace(
            location=location,
            config={"preset": "anthropic", "auth_config": auth_config},
        )
        prepared_requests.append(
            provider_impl._prepare_generation_request_kwargs(
                {"reasoning_effort": "default", "max_tokens": 16_000},
                model_id="claude-opus-4-8",
                provider=provider,
                model_info=model_info,
            )
        )

    assert prepared_requests == [
        {"thinking": {"type": "adaptive"}, "max_tokens": 16_000},
        {"thinking": {"type": "adaptive"}, "max_tokens": 16_000},
        {"thinking": {"type": "adaptive"}, "max_tokens": 16_000},
    ]


def test_direct_native_request_compiles_typeless_custom_roots_through_the_bundle() -> None:
    _context, _payload, bundle = build_native_request(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        messages=[{"role": "user", "content": "hello"}],
        request_kwargs={
            "tools": [
                {
                    "type": "tool_search_tool_bm25_20251119",
                    "name": "tool_search_tool_bm25",
                },
                {
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "parameters": {"properties": {"city": {"type": "string"}}},
                    },
                },
            ]
        },
        credential_ref="$credential:test",
    )

    assert bundle.wire_tools[0]["input_schema"]["type"] == "object"
    assert bundle.server_tools[0]["type"] == "tool_search_tool_bm25_20251119"


def test_custom_endpoint_auto_uses_litellm_but_explicit_native_overrides() -> None:
    assert is_anthropic_native_provider(_provider(endpoint="https://gateway.example")) is False
    assert (
        is_anthropic_native_provider(
            _provider(endpoint="https://gateway.example", protocol="anthropic_messages")
        )
        is True
    )


def test_explicit_native_custom_endpoint_preserves_query_and_fragment() -> None:
    context, _payload, _bundle = build_native_request(
        provider=_provider(
            endpoint="https://gateway.example/v1/messages?version=1#messages",
            protocol="anthropic_messages",
        ),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        messages=[{"role": "user", "content": "hello"}],
        request_kwargs={},
        credential_ref="$credential:test",
    )
    assert context.endpoint == "https://gateway.example/v1/messages?version=1#messages"


def test_oauth_executor_is_rejected() -> None:
    provider = _provider()
    provider.location = "executor"
    provider.config["auth_config"] = {"mode": "oauth", "provider": "anthropic_subscription"}
    with pytest.raises(ValueError, match="controller-only"):
        is_anthropic_native_provider(provider)


def test_oauth_ignores_custom_endpoint_to_protect_bearer_token() -> None:
    provider = _provider(endpoint="https://untrusted.example/v1/messages")
    provider.config["auth_config"] = {
        "mode": "oauth",
        "provider": "anthropic_subscription",
    }
    context, _payload, _bundle = build_native_request(
        provider=provider,
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        messages=[{"role": "user", "content": "hello"}],
        request_kwargs={},
        credential_ref="$credential:test",
    )
    assert context.endpoint == "https://api.anthropic.com/v1/messages"


def test_native_request_replays_tool_calls_using_compiled_wire_name() -> None:
    _context, payload, bundle = build_native_request(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "StructuredOutput", "arguments": "{}"},
                    }
                ],
            }
        ],
        request_kwargs={
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "StructuredOutput",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        },
        credential_ref="$credential:test",
    )
    assert payload["messages"][0]["content"][0]["name"] == bundle.bindings[0].wire_name


def test_strict_tools_follow_frozen_model_capability() -> None:
    tool = {
        "type": "function",
        "function": {
            "name": "bash",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    }
    _context, _payload, disabled_bundle = build_native_request(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test", supports_strict_tools=False),
        messages=[{"role": "user", "content": "hello"}],
        request_kwargs={"tools": [tool]},
        credential_ref="$credential:test",
    )
    _context, _payload, enabled_bundle = build_native_request(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test", supports_strict_tools=True),
        messages=[{"role": "user", "content": "hello"}],
        request_kwargs={"tools": [tool]},
        credential_ref="$credential:test",
    )
    assert "strict" not in disabled_bundle.wire_tools[0]
    assert enabled_bundle.wire_tools[0]["strict"] is True


@pytest.mark.asyncio
async def test_oauth_native_request_preserves_subscription_wire_contract() -> None:
    provider = _provider()
    provider.config["auth_config"] = {
        "mode": "oauth",
        "provider": "anthropic_subscription",
    }
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    context, payload, bundle = build_native_request(
        provider=provider,
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        messages=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "stable",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    }
                ],
            },
            {"role": "user", "content": "hello"},
        ],
        request_kwargs={},
        credential_ref="$credential:test",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AnthropicMessagesClient(
            lambda _ref: _resolved_credential("oauth-token"),
            http_client=http_client,
        )
        await client.complete(
            context,
            payload,
            bundle,
            provider_fingerprint=context.chain_id,
            model_fingerprint=context.model,
        )

    assert str(seen["url"]).endswith("/v1/messages?beta=true")
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer oauth-token"
    assert "x-api-key" not in headers
    assert headers["user-agent"] == CLAUDE_CODE_USER_AGENT
    assert "oauth-2025-04-20" in headers["anthropic-beta"]
    assert "extended-cache-ttl-2025-04-11" in headers["anthropic-beta"]
    request_payload = seen["payload"]
    assert isinstance(request_payload, dict)
    assert request_payload["system"][0]["text"].startswith("x-anthropic-billing-header:")
    assert request_payload["system"][1]["text"] == CLAUDE_CODE_IDENTITY
    assert request_payload["system"][2]["text"] == CLAUDE_CODE_IDENTITY_BRIDGE


async def _resolved_credential(value: str) -> str:
    return value


def test_redaction_recursively_removes_native_api_key_headers() -> None:
    assert redact_inference_payload(
        {"headers": {"X-API-Key": "secret", "nested": [{"x-api-key": "also-secret"}]}}
    ) == {"headers": {"X-API-Key": "[redacted]", "nested": [{"x-api-key": "[redacted]"}]}}


def test_frozen_native_chain_preserves_exact_parallel_round_trip() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "mcp_exact",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "outer_safe": {
                            "type": "object",
                            "properties": {"nested_safe": {"type": "string"}},
                            "required": ["nested_safe"],
                            "additionalProperties": False,
                        }
                    },
                    "required": ["outer_safe"],
                    "additionalProperties": False,
                },
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bash",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    context, bundle = build_native_chain(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test", max_output_tokens=2048),
        exposed_tools=tools,
        alias_map={"mcp_exact": "mcp:server:exact", "bash": "bash"},
        stable_id_map={"mcp_exact": "mcp-tool-1", "bash": "builtin-bash"},
        argument_alias_map={
            "mcp:server:exact": {
                "outer_safe": {
                    "original": "outer[]",
                    "properties": {"nested_safe": "nested$value"},
                }
            }
        },
        thinking={"type": "enabled", "budget_tokens": 1024},
        credential_ref="$credential:test",
    )
    first_wire, bash_wire = (binding.wire_name for binding in bundle.bindings)
    envelope = AnthropicNativeEnvelope(
        native_blocks=(
            {"type": "thinking", "thinking": "signed", "signature": "signature"},
            {
                "type": "tool_use",
                "id": "toolu_mcp",
                "name": first_wire,
                "input": {"outer_safe": {"nested_safe": "value"}},
            },
            {
                "type": "tool_use",
                "id": "toolu_bash",
                "name": bash_wire,
                "input": {"command": "printf ok"},
            },
        ),
        stop_reason="tool_use",
        stop_details={},
        usage={},
        pending_client_message_id=None,
        pending_server_message_id=None,
        bundle_fingerprint=bundle.fingerprint,
        provider_fingerprint=context.chain_id,
        model_fingerprint=context.model,
        thinking_fingerprint=context.thinking_fingerprint,
    )
    frozen = {
        NATIVE_CONTINUATION_REQUIRED_KWARG: True,
        NATIVE_REQUEST_CONTEXT_KWARG: context.to_dict(),
        NATIVE_TOOL_BUNDLE_KWARG: bundle.to_dict(),
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "tools": tools,
    }
    restored_context, payload, restored_bundle = build_native_request(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "toolu_mcp",
                        "function": {
                            "name": "mcp:server:exact",
                            "arguments": '{"outer[]":{"nested$value":"value"}}',
                        },
                    },
                    {
                        "id": "toolu_bash",
                        "function": {"name": "bash", "arguments": '{"command":"printf ok"}'},
                    },
                ],
                "_anthropic_native_envelope": envelope.to_dict(),
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_bash",
                "content": '{"status":"error","message":"timed out"}',
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "attachment follows"}],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_mcp",
                "content": '{"status":"ok"}',
            },
        ],
        request_kwargs=frozen,
        credential_ref="$credential:test",
    )
    assert restored_context == context
    assert restored_bundle.fingerprint == bundle.fingerprint
    assert payload["messages"][0]["content"] == [dict(block) for block in envelope.native_blocks]
    result_blocks = payload["messages"][1]["content"]
    assert [block["tool_use_id"] for block in result_blocks[:2]] == [
        "toolu_mcp",
        "toolu_bash",
    ]
    assert "is_error" not in result_blocks[0]
    assert result_blocks[1]["is_error"] is True
    assert result_blocks[2] == {"type": "text", "text": "attachment follows"}
    assert payload["max_tokens"] == 2048
    assert restored_bundle.wire_tools[0]["cache_control"]["ttl"] == "1h"


def test_frozen_native_chain_rejects_provider_thinking_and_bundle_corruption() -> None:
    context, bundle = build_native_chain(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        exposed_tools=[],
        alias_map={},
        stable_id_map={},
        argument_alias_map={},
        thinking={},
        credential_ref="$credential:test",
    )
    base_kwargs = {
        NATIVE_REQUEST_CONTEXT_KWARG: context.to_dict(),
        NATIVE_TOOL_BUNDLE_KWARG: bundle.to_dict(),
    }
    with pytest.raises(AnthropicContinuationRejected, match="identity mismatch"):
        build_native_request(
            provider=_provider(),
            model="claude-test",
            model_info=ModelInfo(model_id="claude-test"),
            messages=[{"role": "user", "content": "hello"}],
            request_kwargs={**base_kwargs, "thinking": {"type": "enabled"}},
            credential_ref="$credential:test",
        )
    corrupt_bundle = bundle.to_dict()
    corrupt_bundle["fingerprint"] = sha256_fingerprint({"corrupt": True})
    with pytest.raises(AnthropicContinuationRejected, match="Corrupt frozen"):
        build_native_request(
            provider=_provider(),
            model="claude-test",
            model_info=ModelInfo(model_id="claude-test"),
            messages=[{"role": "user", "content": "hello"}],
            request_kwargs={
                NATIVE_REQUEST_CONTEXT_KWARG: context.to_dict(),
                NATIVE_TOOL_BUNDLE_KWARG: corrupt_bundle,
            },
            credential_ref="$credential:test",
        )


def test_executor_uses_exact_controller_supplied_native_chain() -> None:
    provider = _provider()
    provider.location = "executor"
    context, bundle = build_native_chain(
        provider=provider,
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test", max_output_tokens=3456),
        exposed_tools=[
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        alias_map={"bash": "bash"},
        stable_id_map={"bash": "builtin-bash"},
        argument_alias_map={},
        thinking={},
        credential_ref="$credential:anthropic-api-key",
    )
    request = CognisInferenceRequest(
        model="claude-test",
        messages=[{"role": "user", "content": "hello"}],
        request_kwargs={
            "api_key": "secret",
            "timeout": 9.5,
            NATIVE_REQUEST_CONTEXT_KWARG: context.to_dict(),
            NATIVE_TOOL_BUNDLE_KWARG: bundle.to_dict(),
        },
        provider_id="anthropic",
        backend_metadata={"anthropic_native": {"config": provider.config}},
    )
    client, restored_context, payload, restored_bundle = (
        AnthropicMessagesExecutorBackend()._prepared(request)
    )
    assert client._timeout == 9.5
    assert restored_context == context
    assert restored_bundle.fingerprint == bundle.fingerprint
    assert payload["max_tokens"] == 3456


@pytest.mark.asyncio
async def test_executor_forwards_terminal_native_event_with_envelope(monkeypatch) -> None:
    backend = AnthropicMessagesExecutorBackend()
    native_event = {"type": "message_stop"}
    envelope = {"stop_reason": "end_turn", "native_blocks": []}

    class FakeClient:
        async def stream(self, *_args, **_kwargs):
            yield {
                "anthropic_native_events": [native_event],
                "anthropic_native_envelope": envelope,
            }

    monkeypatch.setattr(
        backend,
        "_prepared",
        lambda _request: (FakeClient(), SimpleNamespace(chain_id="chain"), {}, object()),
    )
    request = CognisInferenceRequest(model="claude-test", messages=[])

    chunks = [chunk async for chunk in backend.stream_complete(request)]

    assert chunks[0] == {"anthropic_native_events": [native_event]}
    assert chunks[-1]["backend_metadata"]["anthropic_native_envelope"] == envelope


def test_completed_historical_native_envelope_keeps_its_original_wire_identity() -> None:
    old_context, old_bundle = build_native_chain(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        exposed_tools=[
            {
                "type": "function",
                "function": {
                    "name": "old_visible",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        alias_map={"old_visible": "old:canonical"},
        stable_id_map={"old_visible": "old-stable"},
        argument_alias_map={},
        thinking={},
        credential_ref="$credential:test",
    )
    old_wire_name = old_bundle.bindings[0].wire_name
    old_envelope = AnthropicNativeEnvelope(
        native_blocks=(
            {
                "type": "tool_use",
                "id": "toolu_old",
                "name": old_wire_name,
                "input": {},
            },
        ),
        stop_reason="tool_use",
        stop_details={},
        usage={},
        pending_client_message_id=None,
        pending_server_message_id=None,
        bundle_fingerprint=old_bundle.fingerprint,
        provider_fingerprint=old_context.chain_id,
        model_fingerprint=old_context.model,
        thinking_fingerprint=old_context.thinking_fingerprint,
    )
    new_context, new_bundle = build_native_chain(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        exposed_tools=[],
        alias_map={},
        stable_id_map={},
        argument_alias_map={},
        thinking={},
        credential_ref="$credential:test",
    )
    _context, payload, _bundle = build_native_request(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "toolu_old",
                        "function": {"name": "old:canonical", "arguments": "{}"},
                    }
                ],
                "_anthropic_native_envelope": old_envelope.to_dict(),
            },
            {"role": "tool", "tool_call_id": "toolu_old", "content": "done"},
            {"role": "user", "content": "new turn"},
        ],
        request_kwargs={
            NATIVE_REQUEST_CONTEXT_KWARG: new_context.to_dict(),
            NATIVE_TOOL_BUNDLE_KWARG: new_bundle.to_dict(),
        },
        credential_ref="$credential:test",
    )
    assert payload["messages"][0]["content"][0]["name"] == old_wire_name


def test_native_replay_preserves_server_tool_blocks_without_creating_server_results() -> None:
    context, bundle = build_native_chain(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        exposed_tools=[
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        alias_map={"weather": "weather"},
        stable_id_map={"weather": "weather"},
        argument_alias_map={},
        thinking={"type": "enabled", "budget_tokens": 32},
        credential_ref="$credential:test",
        server_tools=(
            {
                "type": "tool_search_tool_regex_20251119",
                "name": "tool_search_tool_regex",
            },
        ),
    )
    wire_name = bundle.bindings[0].wire_name
    blocks = [
        {"type": "thinking", "thinking": "search", "signature": "signature"},
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "tool_search_tool_regex",
            "input": {"pattern": "weather"},
        },
        {
            "type": "tool_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": {
                "type": "tool_search_tool_search_result",
                "tool_references": [{"type": "tool_reference", "tool_name": wire_name}],
            },
        },
        {"type": "tool_use", "id": "toolu_1", "name": wire_name, "input": {}},
    ]
    envelope = AnthropicNativeEnvelope(
        native_blocks=tuple(blocks),
        stop_reason="tool_use",
        stop_details={"stop_reason": "tool_use"},
        usage={},
        pending_client_message_id=None,
        pending_server_message_id="msg_1",
        bundle_fingerprint=bundle.fingerprint,
        provider_fingerprint=context.chain_id,
        model_fingerprint=context.model,
        thinking_fingerprint=context.thinking_fingerprint,
    )
    _context, payload, _bundle = build_native_request(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test"),
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "toolu_1", "function": {"name": "weather", "arguments": "{}"}}
                ],
                "_anthropic_native_envelope": envelope.to_dict(),
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "sunny"},
        ],
        request_kwargs={
            NATIVE_CONTINUATION_REQUIRED_KWARG: True,
            NATIVE_REQUEST_CONTEXT_KWARG: context.to_dict(),
            NATIVE_TOOL_BUNDLE_KWARG: bundle.to_dict(),
            "thinking": {"type": "enabled", "budget_tokens": 32},
        },
        credential_ref="$credential:test",
    )
    assert payload["messages"][0]["content"] == blocks
    assert payload["messages"][1]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "sunny"}
    ]


def test_pause_turn_replays_server_search_envelope_without_tool_results() -> None:
    context, bundle = build_native_chain(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test", supports_pause_turn=True),
        exposed_tools=[
            {
                "type": "tool_search_tool_bm25_20251119",
                "name": "tool_search_tool_bm25",
            },
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "parameters": {"type": "object", "properties": {}},
                    "defer_loading": True,
                },
            },
        ],
        alias_map={"weather": "weather"},
        stable_id_map={"weather": "weather"},
        argument_alias_map={},
        thinking={},
        credential_ref="$credential:test",
    )
    wire_name = bundle.bindings[0].wire_name
    envelope = AnthropicNativeEnvelope(
        native_blocks=(
            {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "tool_search_tool_bm25",
                "input": {"query": "weather"},
            },
            {
                "type": "tool_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": {
                    "type": "tool_search_tool_search_result",
                    "tool_references": [{"type": "tool_reference", "tool_name": wire_name}],
                },
            },
        ),
        stop_reason="pause_turn",
        stop_details={"stop_reason": "pause_turn"},
        usage={},
        pending_client_message_id=None,
        pending_server_message_id="msg_1",
        bundle_fingerprint=bundle.fingerprint,
        provider_fingerprint=context.chain_id,
        model_fingerprint=context.model,
        thinking_fingerprint=context.thinking_fingerprint,
    )
    _context, payload, _bundle = build_native_request(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test", supports_pause_turn=True),
        messages=[
            {
                "role": "assistant",
                "content": None,
                "_anthropic_native_envelope": envelope.to_dict(),
            }
        ],
        request_kwargs={
            NATIVE_CONTINUATION_REQUIRED_KWARG: True,
            NATIVE_REQUEST_CONTEXT_KWARG: context.to_dict(),
            NATIVE_TOOL_BUNDLE_KWARG: bundle.to_dict(),
        },
        credential_ref="$credential:test",
    )
    assert payload["messages"] == [
        {"role": "assistant", "content": envelope.to_dict()["native_blocks"]}
    ]


def test_end_turn_with_developer_only_follow_up_appends_user_tail() -> None:
    context, bundle = build_native_chain(
        provider=_provider(),
        model="claude-sonnet-5",
        model_info=ModelInfo(model_id="claude-sonnet-5"),
        exposed_tools=[],
        alias_map={},
        stable_id_map={},
        argument_alias_map={},
        thinking={"type": "adaptive"},
        credential_ref="$credential:test",
    )
    envelope = AnthropicNativeEnvelope(
        native_blocks=({"type": "text", "text": "Finished the requested work."},),
        stop_reason="end_turn",
        stop_details={"stop_reason": "end_turn"},
        usage={},
        pending_client_message_id=None,
        pending_server_message_id="msg_1",
        bundle_fingerprint=bundle.fingerprint,
        provider_fingerprint=context.chain_id,
        model_fingerprint=context.model,
        thinking_fingerprint=context.thinking_fingerprint,
    )

    _context, payload, _bundle = build_native_request(
        provider=_provider(),
        model="claude-sonnet-5",
        model_info=ModelInfo(model_id="claude-sonnet-5"),
        messages=[
            {"role": "user", "content": "Complete the implementation."},
            {
                "role": "assistant",
                "content": "Finished the requested work.",
                "_anthropic_native_envelope": envelope.to_dict(),
            },
            {
                "role": "developer",
                "content": "A tool reminder requires another controller cycle.",
            },
        ],
        request_kwargs={
            NATIVE_CONTINUATION_REQUIRED_KWARG: True,
            NATIVE_REQUEST_CONTEXT_KWARG: context.to_dict(),
            NATIVE_TOOL_BUNDLE_KWARG: bundle.to_dict(),
            "thinking": {"type": "adaptive"},
        },
        credential_ref="$credential:test",
    )

    assert [message["role"] for message in payload["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert payload["messages"][1]["content"] == envelope.to_dict()["native_blocks"]
    assert payload["messages"][2] == {
        "role": "user",
        "content": [{"type": "text", "text": "Continue."}],
    }
    assert payload["system"][-1]["text"] == ("A tool reminder requires another controller cycle.")


def test_native_search_reference_outside_frozen_bundle_fails_closed() -> None:
    context, bundle = build_native_chain(
        provider=_provider(),
        model="claude-test",
        model_info=ModelInfo(model_id="claude-test", supports_pause_turn=True),
        exposed_tools=[
            {
                "type": "tool_search_tool_bm25_20251119",
                "name": "tool_search_tool_bm25",
            }
        ],
        alias_map={},
        stable_id_map={},
        argument_alias_map={},
        thinking={},
        credential_ref="$credential:test",
    )
    envelope = AnthropicNativeEnvelope(
        native_blocks=(
            {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "tool_search_tool_bm25",
                "input": {"query": "weather"},
            },
            {
                "type": "tool_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": {
                    "type": "tool_search_tool_search_result",
                    "tool_references": [{"type": "tool_reference", "tool_name": "unknown"}],
                },
            },
        ),
        stop_reason="pause_turn",
        stop_details={},
        usage={},
        pending_client_message_id=None,
        pending_server_message_id="msg_1",
        bundle_fingerprint=bundle.fingerprint,
        provider_fingerprint=context.chain_id,
        model_fingerprint=context.model,
        thinking_fingerprint=context.thinking_fingerprint,
    )
    with pytest.raises(AnthropicContinuationRejected, match="Unknown Anthropic native tool search"):
        build_native_request(
            provider=_provider(),
            model="claude-test",
            model_info=ModelInfo(model_id="claude-test", supports_pause_turn=True),
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "_anthropic_native_envelope": envelope.to_dict(),
                }
            ],
            request_kwargs={
                NATIVE_CONTINUATION_REQUIRED_KWARG: True,
                NATIVE_REQUEST_CONTEXT_KWARG: context.to_dict(),
                NATIVE_TOOL_BUNDLE_KWARG: bundle.to_dict(),
            },
            credential_ref="$credential:test",
        )
