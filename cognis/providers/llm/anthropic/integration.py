"""Provider-facing helpers for the native Anthropic Messages transport.

This module deliberately keeps provider configuration and secret resolution out
of ``transport.py``.  Its output is a non-secret request context plus a
transport payload and the exact compiled tool bundle used for the call.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cognis.models.config import ModelInfo
from cognis.providers.llm.anthropic.contracts import (
    AnthropicAuthPolicy,
    AnthropicLocation,
    AnthropicNativeEnvelope,
    AnthropicProtocol,
    CompiledAnthropicToolBundle,
    ModelInfoCapabilitySnapshot,
    ResolvedAnthropicRequestContext,
    resolve_anthropic_protocol,
    sha256_fingerprint,
)
from cognis.providers.llm.anthropic.tool_bundle import compile_anthropic_tool_bundle
from cognis.providers.llm.anthropic_subscription import (
    CLAUDE_CODE_IDENTITY,
    CLAUDE_CODE_IDENTITY_BRIDGE,
    _billing_header,
    _convert_messages,
    _first_user_text,
)

OFFICIAL_MESSAGES_ENDPOINT = "https://api.anthropic.com/v1/messages"
NATIVE_REQUEST_CONTEXT_KWARG = "cognis_anthropic_request_context"
NATIVE_TOOL_BUNDLE_KWARG = "cognis_anthropic_tool_bundle"
NATIVE_CONTINUATION_REQUIRED_KWARG = "cognis_anthropic_continuation_required"


class AnthropicContinuationRejected(RuntimeError):
    """A native continuation failed closed before another provider request."""


def _messages_endpoint(provider: Any) -> str:
    config = dict(getattr(provider, "config", {}) or {})
    endpoint = str(config.get("api_base") or config.get("base_url") or OFFICIAL_MESSAGES_ENDPOINT)
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1/messages"):
        normalized_path = path
    elif path.endswith("/v1"):
        normalized_path = f"{path}/messages"
    else:
        normalized_path = f"{path}/v1/messages"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, normalized_path, parsed.query, parsed.fragment)
    )


def build_native_chain(
    *,
    provider: Any,
    model: str,
    model_info: ModelInfo,
    exposed_tools: Sequence[Mapping[str, Any]],
    alias_map: Mapping[str, str],
    stable_id_map: Mapping[str, str],
    argument_alias_map: Mapping[str, Mapping[str, Any]],
    thinking: Any,
    credential_ref: str | None,
    server_tools: Sequence[Mapping[str, Any]] = (),
) -> tuple[ResolvedAnthropicRequestContext, CompiledAnthropicToolBundle]:
    """Freeze the continuation-critical request identity and tool bundle once."""

    config = dict(getattr(provider, "config", {}) or {})
    auth = config.get("auth_config")
    is_oauth = isinstance(auth, dict) and str(auth.get("mode") or "").lower() == "oauth"
    auth_policy = AnthropicAuthPolicy.OAUTH if is_oauth else AnthropicAuthPolicy.API_KEY
    endpoint = OFFICIAL_MESSAGES_ENDPOINT if is_oauth else _messages_endpoint(provider)
    resolution = resolve_anthropic_protocol(
        AnthropicProtocol(str(config.get("protocol") or "auto").strip().lower()),
        endpoint=endpoint,
        auth_policy=auth_policy,
        location=AnthropicLocation(str(getattr(provider, "location", "controller"))),
    )
    bundle = compile_anthropic_tool_bundle(
        exposed_tools,
        alias_map=alias_map,
        stable_id_map=stable_id_map,
        argument_alias_map=argument_alias_map,
        server_tools=server_tools,
        strict_policy="preferred" if model_info.supports_strict_tools else "disabled",
    )
    context = ResolvedAnthropicRequestContext(
        provider_id=str(provider.provider_id),
        model=model,
        endpoint=endpoint,
        protocol=resolution.protocol,
        location=resolution.location,
        auth_policy=auth_policy,
        credential_ref=credential_ref,
        model_info=ModelInfoCapabilitySnapshot.from_model_info(model_info),
        thinking_fingerprint=sha256_fingerprint(thinking or {}),
        chain_id=sha256_fingerprint(
            {
                "provider_id": provider.provider_id,
                "model": model,
                "endpoint": endpoint,
                "protocol": resolution.protocol.value,
            }
        ),
    )
    return context, bundle


def is_anthropic_native_provider(provider: Any) -> bool:
    config = dict(getattr(provider, "config", {}) or {})
    if str(config.get("preset") or "").strip().lower() != "anthropic":
        return False
    auth = config.get("auth_config")
    if not isinstance(auth, dict) or not str(auth.get("mode") or "").strip():
        # Legacy Anthropic presets without Cognis-managed credentials retain
        # LiteLLM's environment-variable compatibility behavior.
        return False
    is_oauth = isinstance(auth, dict) and str(auth.get("mode") or "").lower() == "oauth"
    protocol = AnthropicProtocol(str(config.get("protocol") or "auto").strip().lower())
    endpoint = str(config.get("api_base") or config.get("base_url") or OFFICIAL_MESSAGES_ENDPOINT)
    resolution = resolve_anthropic_protocol(
        protocol,
        endpoint=endpoint,
        auth_policy=AnthropicAuthPolicy.OAUTH if is_oauth else AnthropicAuthPolicy.API_KEY,
        location=AnthropicLocation(str(getattr(provider, "location", "controller"))),
    )
    return resolution.protocol is AnthropicProtocol.ANTHROPIC_MESSAGES


def build_native_request(
    *,
    provider: Any,
    model: str,
    model_info: ModelInfo,
    messages: list[dict[str, Any]],
    request_kwargs: Mapping[str, Any],
    credential_ref: str | None,
) -> tuple[ResolvedAnthropicRequestContext, dict[str, Any], Any]:
    """Build one native request without including a credential in metadata."""

    config = dict(getattr(provider, "config", {}) or {})
    auth = config.get("auth_config")
    is_oauth = isinstance(auth, dict) and str(auth.get("mode") or "").lower() == "oauth"
    auth_policy = AnthropicAuthPolicy.OAUTH if is_oauth else AnthropicAuthPolicy.API_KEY
    endpoint = OFFICIAL_MESSAGES_ENDPOINT if is_oauth else _messages_endpoint(provider)
    resolution = resolve_anthropic_protocol(
        AnthropicProtocol(str(config.get("protocol") or "auto").strip().lower()),
        endpoint=endpoint,
        auth_policy=auth_policy,
        location=AnthropicLocation(str(getattr(provider, "location", "controller"))),
    )
    frozen_context = request_kwargs.get(NATIVE_REQUEST_CONTEXT_KWARG)
    frozen_bundle = request_kwargs.get(NATIVE_TOOL_BUNDLE_KWARG)
    if (frozen_context is None) != (frozen_bundle is None):
        raise AnthropicContinuationRejected("Incomplete frozen Anthropic native chain")
    if frozen_context is not None:
        if not isinstance(frozen_context, Mapping) or not isinstance(frozen_bundle, Mapping):
            raise AnthropicContinuationRejected("Corrupt frozen Anthropic native chain")
        try:
            context = ResolvedAnthropicRequestContext.from_dict(frozen_context)
            bundle = CompiledAnthropicToolBundle.from_dict(frozen_bundle)
        except (KeyError, TypeError, ValueError) as exc:
            raise AnthropicContinuationRejected("Corrupt frozen Anthropic native chain") from exc
        actual_identity = (
            str(provider.provider_id),
            model,
            endpoint,
            resolution.protocol,
            resolution.location,
            auth_policy,
            sha256_fingerprint(request_kwargs.get("thinking") or {}),
        )
        expected_identity = (
            context.provider_id,
            context.model,
            context.endpoint,
            context.protocol,
            context.location,
            context.auth_policy,
            context.thinking_fingerprint,
        )
        if actual_identity != expected_identity:
            raise AnthropicContinuationRejected("Anthropic native continuation identity mismatch")
    else:
        tools = request_kwargs.get("tools")
        exposed_tools: Sequence[Mapping[str, Any]] = (
            [tool for tool in tools if isinstance(tool, Mapping)] if isinstance(tools, list) else []
        )
        stable_ids = {
            str(tool["function"]["name"]): str(tool["function"]["name"])
            for tool in exposed_tools
            if isinstance(tool.get("function"), Mapping)
            and isinstance(tool["function"].get("name"), str)
        }
        raw_server_tools = request_kwargs.get("cognis_anthropic_server_tools")
        if raw_server_tools is not None and (
            not isinstance(raw_server_tools, list)
            or not all(isinstance(tool, Mapping) for tool in raw_server_tools)
        ):
            raise AnthropicContinuationRejected("Invalid Anthropic server tool definitions")
        context, bundle = build_native_chain(
            provider=provider,
            model=model,
            model_info=model_info,
            exposed_tools=exposed_tools,
            alias_map={},
            stable_id_map=stable_ids,
            argument_alias_map={},
            thinking=request_kwargs.get("thinking"),
            credential_ref=credential_ref,
            server_tools=tuple(raw_server_tools or ()),
        )
    tool_call_names: dict[str, str] = {}
    for message in messages:
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for call in raw_calls:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            call_id = call.get("id")
            if (
                isinstance(function, Mapping)
                and isinstance(function.get("name"), str)
                and isinstance(call_id, str)
                and call_id
            ):
                tool_call_names[call_id] = function["name"]
    require_active_continuation = bool(request_kwargs.get(NATIVE_CONTINUATION_REQUIRED_KWARG))
    replay_messages, native_tool_use_ids = _prepare_native_replay_messages(
        messages,
        context,
        bundle,
        require_active_continuation=require_active_continuation,
    )
    system, anthropic_messages = _convert_messages(replay_messages)
    _append_developer_follow_up_user_tail(
        replay_messages,
        anthropic_messages,
        require_active_continuation=require_active_continuation,
    )
    if context.auth_policy is AnthropicAuthPolicy.OAUTH:
        first_user_text = _first_user_text(anthropic_messages)
        system = [
            {"type": "text", "text": _billing_header(first_user_text)},
            {"type": "text", "text": CLAUDE_CODE_IDENTITY},
            {"type": "text", "text": CLAUDE_CODE_IDENTITY_BRIDGE},
            *system,
        ]
    wire_names = {binding.canonical_name: binding.wire_name for binding in bundle.bindings}
    for message in anthropic_messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_use_id = block.get("id")
            if isinstance(tool_use_id, str) and tool_use_id in native_tool_use_ids:
                continue
            canonical = tool_call_names.get(tool_use_id) if isinstance(tool_use_id, str) else None
            if canonical in wire_names:
                block["name"] = wire_names[canonical]
    payload: dict[str, Any] = {
        "messages": anthropic_messages,
        "system": system,
        "max_tokens": int(
            request_kwargs.get("max_tokens")
            or request_kwargs.get("max_completion_tokens")
            or context.model_info.max_output_tokens
        ),
    }
    for key in (
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
        "metadata",
        "thinking",
        "output_config",
        "tool_choice",
        "parallel_tool_calls",
        "disable_parallel_tool_use",
        "extra_headers",
        "cognis_anthropic_server_tools",
    ):
        if request_kwargs.get(key) is not None:
            payload[key] = request_kwargs[key]
    return context, payload, bundle


def _append_developer_follow_up_user_tail(
    source_messages: Sequence[Mapping[str, Any]],
    anthropic_messages: list[dict[str, Any]],
    *,
    require_active_continuation: bool,
) -> None:
    """End developer-only follow-up cycles with a real Anthropic user message."""

    if (
        not require_active_continuation
        or not anthropic_messages
        or anthropic_messages[-1].get("role") != "assistant"
    ):
        return
    native_index: int | None = None
    envelope: AnthropicNativeEnvelope | None = None
    for index in range(len(source_messages) - 1, -1, -1):
        source = source_messages[index]
        raw_envelope = source.get("_anthropic_native_envelope")
        if source.get("role") != "assistant" or not isinstance(raw_envelope, Mapping):
            continue
        try:
            envelope = AnthropicNativeEnvelope.from_dict(raw_envelope)
        except (KeyError, TypeError, ValueError) as exc:
            raise AnthropicContinuationRejected("Corrupt active Anthropic native envelope") from exc
        native_index = index
        break
    if envelope is None or native_index is None or envelope.stop_reason != "end_turn":
        return
    trailing_roles = [
        str(message.get("role") or "") for message in source_messages[native_index + 1 :]
    ]
    if not trailing_roles or any(role not in {"developer", "system"} for role in trailing_roles):
        return
    anthropic_messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": "Continue."}],
        }
    )


def _prepare_native_replay_messages(
    messages: list[dict[str, Any]],
    context: ResolvedAnthropicRequestContext,
    bundle: CompiledAnthropicToolBundle,
    *,
    require_active_continuation: bool,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Validate and normalize exact native assistant/result groups for conversion."""

    prepared: list[dict[str, Any]] = []
    native_tool_use_ids: set[str] = set()
    native_assistant_indexes = [
        position
        for position, message in enumerate(messages)
        if message.get("role") == "assistant"
        and isinstance(message.get("_anthropic_native_envelope"), Mapping)
    ]
    active_continuation_index = (
        native_assistant_indexes[-1]
        if require_active_continuation and native_assistant_indexes
        else None
    )
    index = 0
    while index < len(messages):
        source = messages[index]
        if source.get("role") != "assistant":
            prepared.append(deepcopy(source))
            index += 1
            continue
        raw_envelope = source.get("_anthropic_native_envelope")
        if raw_envelope is None:
            # Old non-native history remains compatible. Once a chain is frozen,
            # however, provider-native tool calls must never be reconstructed.
            prepared.append(deepcopy(source))
            index += 1
            continue
        if not isinstance(raw_envelope, Mapping):
            raise AnthropicContinuationRejected("Corrupt Anthropic native envelope")
        try:
            envelope = AnthropicNativeEnvelope.from_dict(raw_envelope)
            if (
                any(
                    block.get("type") in {"server_tool_use", "tool_search_tool_result"}
                    for block in envelope.native_blocks
                )
                and envelope.bundle_fingerprint != bundle.fingerprint
            ):
                raise AnthropicContinuationRejected("Anthropic tool search bundle mismatch")
            if index == active_continuation_index:
                envelope.assert_matches(
                    bundle_fingerprint=bundle.fingerprint,
                    provider_fingerprint=context.chain_id,
                    model_fingerprint=context.model,
                    thinking_fingerprint=context.thinking_fingerprint,
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise AnthropicContinuationRejected(
                "Anthropic native envelope is not continuable"
            ) from exc
        _validate_frozen_tool_search_references(envelope, bundle)
        tool_ids = list(envelope.client_tool_use_ids)
        if len(tool_ids) != len(set(tool_ids)):
            raise AnthropicContinuationRejected("Invalid Anthropic native client tool-use group")
        if native_tool_use_ids.intersection(tool_ids):
            raise AnthropicContinuationRejected("Duplicate Anthropic native tool-use identity")
        native_tool_use_ids.update(tool_ids)
        native_assistant = deepcopy(source)
        native_assistant["_anthropic_native_blocks"] = envelope.to_dict()["native_blocks"]
        prepared.append(native_assistant)

        if not tool_ids:
            index += 1
            continue
        result_messages: dict[str, dict[str, Any]] = {}
        trailing_messages: list[dict[str, Any]] = []
        index += 1
        while index < len(messages) and messages[index].get("role") != "assistant":
            candidate = deepcopy(messages[index])
            if candidate.get("role") == "tool":
                call_id = candidate.get("tool_call_id")
                if call_id in tool_ids:
                    if call_id in result_messages:
                        raise AnthropicContinuationRejected(
                            "Duplicate Anthropic native tool result"
                        )
                    candidate["_tool_is_error"] = _native_result_is_error(candidate)
                    result_messages[str(call_id)] = candidate
                else:
                    trailing_messages.append(candidate)
            else:
                trailing_messages.append(candidate)
            index += 1
        if set(result_messages) != set(tool_ids):
            raise AnthropicContinuationRejected("Incomplete Anthropic native tool-result group")
        prepared.extend(result_messages[call_id] for call_id in tool_ids)
        prepared.extend(trailing_messages)
    return prepared, native_tool_use_ids


def _validate_frozen_tool_search_references(
    envelope: AnthropicNativeEnvelope,
    bundle: CompiledAnthropicToolBundle,
) -> None:
    """Reject server search promotions that are absent from the frozen bundle."""

    if not any(
        block.get("type") in {"server_tool_use", "tool_search_tool_result"}
        for block in envelope.native_blocks
    ):
        # Historical client-only native envelopes keep their original bundle
        # identity and remain replayable after a later turn changes tools.
        return
    wire_names = {binding.wire_name for binding in bundle.bindings}
    server_names = {str(tool["name"]) for tool in bundle.server_tools}
    for block in envelope.native_blocks:
        block_type = block.get("type")
        if block_type == "tool_use" and block.get("name") not in wire_names:
            raise AnthropicContinuationRejected("Unknown Anthropic native client tool reference")
        if block_type == "server_tool_use" and block.get("name") not in server_names:
            raise AnthropicContinuationRejected("Unknown Anthropic native server tool reference")
        if block_type != "tool_search_tool_result":
            continue
        content = block.get("content")
        references = content.get("tool_references") if isinstance(content, Mapping) else None
        if not isinstance(references, (list, tuple)):
            raise AnthropicContinuationRejected("Malformed Anthropic native tool search result")
        if any(
            not isinstance(reference, Mapping) or reference.get("tool_name") not in wire_names
            for reference in references
        ):
            raise AnthropicContinuationRejected("Unknown Anthropic native tool search reference")


def _native_result_is_error(message: Mapping[str, Any]) -> bool:
    if message.get("_tool_is_error") is True:
        return True
    content = message.get("content")
    if not isinstance(content, str):
        return False
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, Mapping):
        return False
    if payload.get("is_error") is True or payload.get("error"):
        return True
    return str(payload.get("status") or "").strip().lower() in {
        "cancelled",
        "denied",
        "error",
        "failed",
        "interrupted",
        "orphaned",
        "rejected",
        "timeout",
        "unavailable",
    }
