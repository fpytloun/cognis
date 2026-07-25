from __future__ import annotations

import json
from types import MappingProxyType

import pytest

from cognis.models.config import ModelInfo
from cognis.providers.llm.anthropic import (
    AnthropicAuthPolicy,
    AnthropicContinuationStatus,
    AnthropicLocation,
    AnthropicNativeEnvelope,
    AnthropicProtocol,
    AnthropicToolBinding,
    CompiledAnthropicToolBundle,
    ModelInfoCapabilitySnapshot,
    ResolvedAnthropicRequestContext,
    resolve_anthropic_protocol,
)
from cognis.providers.llm.anthropic.contracts import (
    MAX_ENVELOPE_BLOCKS,
    materialize_json,
    sha256_fingerprint,
)


def _bundle(
    *, reversed_tools: bool = False, diagnostics: tuple[str, ...] = ()
) -> CompiledAnthropicToolBundle:
    pairs = [
        ({"name": "mcp_Bash", "input_schema": {"type": "object"}}, "bash", "tool_bash"),
        ({"name": "mcp_Grep", "input_schema": {"type": "object"}}, "grep", "tool_grep"),
    ]
    if reversed_tools:
        pairs.reverse()
    return CompiledAnthropicToolBundle(
        wire_tools=tuple(pair[0] for pair in pairs),
        bindings=tuple(
            AnthropicToolBinding(
                wire_name=tool["name"],
                canonical_name=canonical_name,
                stable_id=stable_id,
                reverse_argument_aliases={"wire_arg": "canonical_arg"},
            )
            for tool, canonical_name, stable_id in pairs
        ),
        strict_diagnostics=diagnostics,
    )


def _envelope(**overrides: object) -> AnthropicNativeEnvelope:
    payload: dict[str, object] = {
        "native_blocks": ({"type": "text", "text": "done"},),
        "stop_reason": "end_turn",
        "stop_details": {},
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "pending_client_message_id": "client_1",
        "pending_server_message_id": "msg_1",
        "bundle_fingerprint": "bundle",
        "provider_fingerprint": "provider",
        "model_fingerprint": "model",
        "thinking_fingerprint": "thinking",
    }
    payload.update(overrides)
    return AnthropicNativeEnvelope(**payload)  # type: ignore[arg-type]


def test_materialize_json_deeply_thaws_and_validates_json_values() -> None:
    frozen = MappingProxyType({"nested": (MappingProxyType({"value": "ok"}),)})

    assert materialize_json(frozen) == {"nested": [{"value": "ok"}]}
    with pytest.raises(TypeError, match="key at \\$ must be a string"):
        materialize_json({1: "invalid"})
    with pytest.raises(TypeError, match="number at \\$ must be finite"):
        materialize_json(float("nan"))
    with pytest.raises(TypeError, match="string at \\$ is not valid UTF-8"):
        materialize_json("\ud800")
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(TypeError, match="Cyclic JSON container at \\$\\[0\\]"):
        materialize_json(cyclic)


def test_tool_bundle_fingerprint_is_deterministic_and_ignores_diagnostics() -> None:
    assert _bundle().fingerprint == _bundle(diagnostics=("informational",)).fingerprint


def test_tool_bundle_fingerprint_is_order_sensitive() -> None:
    assert _bundle().fingerprint != _bundle(reversed_tools=True).fingerprint


def test_tool_bundle_rejects_binding_collisions_and_order_mismatch() -> None:
    with pytest.raises(ValueError, match="bijective"):
        AnthropicToolBinding("wire", "canonical", "stable", {"a": "same", "b": "same"})
    with pytest.raises(ValueError, match="exactly match"):
        CompiledAnthropicToolBundle(
            wire_tools=({"name": "wire"},),
            bindings=(AnthropicToolBinding("other", "canonical", "stable", {}),),
        )


def test_tool_binding_validates_recursive_argument_alias_metadata() -> None:
    binding = AnthropicToolBinding(
        "wire",
        "canonical",
        "stable",
        {
            "root": {
                "original": "root",
                "properties": {"child": {"original": "child[]", "properties": {}}},
            },
            "$cognis_refs": {
                "#/$defs/Node": {
                    "child": {
                        "original": "child[]",
                        "properties": {"$cognis_ref": "#/$defs/Node"},
                    }
                }
            },
        },
    )

    assert (
        binding.reverse_argument_aliases["$cognis_refs"]["#/$defs/Node"]["child"]["properties"][
            "$cognis_ref"
        ]
        == "#/$defs/Node"
    )
    with pytest.raises(TypeError, match="references must be strings"):
        AnthropicToolBinding("wire", "canonical", "stable", {"$cognis_ref": 1})
    with pytest.raises(TypeError, match="definitions must be a mapping"):
        AnthropicToolBinding("wire", "canonical", "stable", {"$cognis_refs": []})
    with pytest.raises(TypeError, match="string keys and alias trees"):
        AnthropicToolBinding(
            "wire",
            "canonical",
            "stable",
            {"$cognis_refs": {"#/$defs/Node": "invalid"}},
        )
    with pytest.raises(ValueError, match="is not defined"):
        AnthropicToolBinding(
            "wire",
            "canonical",
            "stable",
            {
                "root": {
                    "original": "root",
                    "properties": {"$cognis_ref": "#/$defs/Missing"},
                }
            },
        )
    with pytest.raises(TypeError, match="only allowed at the root"):
        AnthropicToolBinding(
            "wire",
            "canonical",
            "stable",
            {
                "root": {
                    "original": "root",
                    "properties": {"$cognis_refs": {}},
                }
            },
        )


def test_request_context_contains_a_capability_snapshot_but_no_credential_value() -> None:
    context = ResolvedAnthropicRequestContext(
        provider_id="anthropic",
        model="claude-sonnet",
        endpoint="https://api.anthropic.com",
        protocol=AnthropicProtocol.ANTHROPIC_MESSAGES,
        location=AnthropicLocation.CONTROLLER,
        auth_policy=AnthropicAuthPolicy.API_KEY,
        credential_ref="$credential:anthropic.api_key",
        model_info=ModelInfoCapabilitySnapshot.from_model_info(ModelInfo(model_id="claude-sonnet")),
        thinking_fingerprint="thinking",
        chain_id="chain",
    )
    serialized = json.dumps(context.to_dict())
    assert "api_key" in serialized
    assert "super-secret-value" not in serialized
    assert "credential_value" not in serialized
    with pytest.raises(TypeError, match="credential_value"):
        ResolvedAnthropicRequestContext(
            provider_id="anthropic",
            model="claude-sonnet",
            endpoint="https://api.anthropic.com",
            protocol=AnthropicProtocol.ANTHROPIC_MESSAGES,
            location=AnthropicLocation.CONTROLLER,
            auth_policy=AnthropicAuthPolicy.API_KEY,
            credential_ref="$credential:anthropic.api_key",
            credential_value="super-secret-value",  # type: ignore[call-arg]
            model_info=context.model_info,
            thinking_fingerprint="thinking",
            chain_id="chain",
        )
    with pytest.raises(ValueError, match=r"\$credential:"):
        ResolvedAnthropicRequestContext(
            provider_id="anthropic",
            model="claude-sonnet",
            endpoint="https://api.anthropic.com",
            protocol=AnthropicProtocol.ANTHROPIC_MESSAGES,
            location=AnthropicLocation.CONTROLLER,
            auth_policy=AnthropicAuthPolicy.API_KEY,
            credential_ref="super-secret-value",
            model_info=context.model_info,
            thinking_fingerprint="thinking",
            chain_id="chain",
        )


def test_envelope_round_trip_and_mismatch_detection() -> None:
    envelope = _envelope()
    restored = AnthropicNativeEnvelope.from_dict(json.loads(envelope.to_json()))
    assert restored == envelope
    restored.assert_matches(
        bundle_fingerprint="bundle",
        provider_fingerprint="provider",
        model_fingerprint="model",
        thinking_fingerprint="thinking",
    )
    with pytest.raises(ValueError, match="mismatch"):
        restored.assert_matches(
            bundle_fingerprint="changed",
            provider_fingerprint="provider",
            model_fingerprint="model",
            thinking_fingerprint="thinking",
        )
    with pytest.raises(ValueError, match="contract version"):
        AnthropicNativeEnvelope.from_dict({**envelope.to_dict(), "contract_version": 2})
    with pytest.raises(ValueError, match="not continuable"):
        _envelope(continuation_status=AnthropicContinuationStatus.NON_CONTINUABLE).assert_matches(
            bundle_fingerprint="bundle",
            provider_fingerprint="provider",
            model_fingerprint="model",
            thinking_fingerprint="thinking",
        )


def test_envelope_rejects_oversize_and_unsupported_blocks_without_truncating() -> None:
    with pytest.raises(ValueError, match="Unsupported Anthropic assistant block"):
        _envelope(native_blocks=({"type": "image", "source": {}},))
    with pytest.raises(TypeError, match="object input"):
        _envelope(
            native_blocks=({"type": "tool_use", "id": "toolu_1", "name": "bash", "input": "bad"},)
        )
    with pytest.raises(ValueError, match="exceeds"):
        _envelope(
            native_blocks=tuple(
                {"type": "text", "text": "x"} for _ in range(MAX_ENVELOPE_BLOCKS + 1)
            )
        )
    with pytest.raises(ValueError, match="exceeds"):
        _envelope(native_blocks=({"type": "text", "text": "x" * (256 * 1024)},))


def test_envelope_preserves_documented_tool_use_callers() -> None:
    envelope = _envelope(
        native_blocks=(
            {
                "type": "tool_use",
                "id": "toolu_direct",
                "name": "bash",
                "input": {"command": "true"},
                "caller": {"type": "direct"},
            },
        )
    )

    restored = AnthropicNativeEnvelope.from_dict(envelope.to_dict())
    assert restored.native_blocks[0]["caller"] == {"type": "direct"}


@pytest.mark.parametrize(
    ("caller", "match"),
    [
        ("direct", "must be an object"),
        ({"type": "direct", "tool_id": "unexpected"}, "unsupported fields"),
        (
            {"type": "code_execution_20260120", "tool_id": "srvtoolu_code"},
            "Unsupported",
        ),
        ({"type": "unknown"}, "Unsupported"),
    ],
)
def test_envelope_rejects_malformed_tool_use_callers(
    caller: object,
    match: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        _envelope(
            native_blocks=(
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "bash",
                    "input": {},
                    "caller": caller,
                },
            )
        )


def test_envelope_preserves_omitted_adaptive_thinking() -> None:
    envelope = _envelope(
        native_blocks=({"type": "thinking", "thinking": "", "signature": "signed-omitted"},)
    )

    restored = AnthropicNativeEnvelope.from_dict(envelope.to_dict())
    assert restored.native_blocks[0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "signed-omitted",
    }


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://api.anthropic.com/v1/messages", AnthropicProtocol.ANTHROPIC_MESSAGES),
        ("https://proxy.example.test/v1", AnthropicProtocol.LITELLM),
    ],
)
def test_auto_protocol_resolution(endpoint: str, expected: AnthropicProtocol) -> None:
    resolution = resolve_anthropic_protocol(
        AnthropicProtocol.AUTO,
        endpoint=endpoint,
        auth_policy=AnthropicAuthPolicy.API_KEY,
        location=AnthropicLocation.EXECUTOR,
    )
    assert resolution.protocol is expected


def test_oauth_is_native_and_controller_only() -> None:
    resolution = resolve_anthropic_protocol(
        AnthropicProtocol.AUTO,
        endpoint="https://proxy.example.test/v1",
        auth_policy=AnthropicAuthPolicy.OAUTH,
        location=AnthropicLocation.CONTROLLER,
    )
    assert resolution.protocol is AnthropicProtocol.ANTHROPIC_MESSAGES
    with pytest.raises(ValueError, match="controller-only"):
        resolve_anthropic_protocol(
            AnthropicProtocol.AUTO,
            endpoint="https://api.anthropic.com",
            auth_policy=AnthropicAuthPolicy.OAUTH,
            location=AnthropicLocation.EXECUTOR,
        )


def test_contract_collections_are_immutable_and_reject_non_string_json_keys() -> None:
    bindings = [
        AnthropicToolBinding("mcp_Bash", "bash", "tool_bash", {}),
    ]
    diagnostics = ["strict"]
    bundle = CompiledAnthropicToolBundle(
        wire_tools=({"name": "mcp_Bash", "input_schema": {"type": "object"}},),
        bindings=bindings,
        strict_diagnostics=diagnostics,
    )
    bindings.clear()
    diagnostics.clear()
    assert len(bundle.bindings) == 1
    assert bundle.strict_diagnostics == ("strict",)
    with pytest.raises(TypeError, match="keys must be strings"):
        CompiledAnthropicToolBundle(
            wire_tools=({1: "not permitted"},),
            bindings=(),
        )
    with pytest.raises(TypeError, match="keys must be strings"):
        sha256_fingerprint({1: "not permitted"})
