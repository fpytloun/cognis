"""Provider-facing helpers for the native Anthropic Messages transport.

This module deliberately keeps provider configuration and secret resolution out
of ``transport.py``.  Its output is a non-secret request context plus a
transport payload and the exact compiled tool bundle used for the call.
"""

from __future__ import annotations

import hashlib
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

OFFICIAL_MESSAGES_ENDPOINT = "https://api.anthropic.com/v1/messages"
CLAUDE_CODE_VERSION = "2.1.87"
CLAUDE_CODE_ENTRYPOINT = "sdk-cli"
CLAUDE_CODE_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
CLAUDE_CODE_IDENTITY_BRIDGE = "The operative agent identity follows."
CCH_SALT = "59cf53e54c78"
CCH_POSITIONS = (4, 7, 20)
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


def _convert_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system_blocks: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role in {"system", "developer"}:
            blocks = _content_to_blocks(message.get("content"))
            for block in blocks:
                if block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        block["text"] = _sanitize_system_text(text)
                    system_blocks.append(block)
            continue
        if role == "tool":
            _append_anthropic_message(
                output,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(message.get("tool_call_id") or ""),
                        "content": _content_to_text(message.get("content")),
                        **({"is_error": True} if message.get("_tool_is_error") else {}),
                    }
                ],
            )
            continue
        if role not in {"user", "assistant"}:
            continue
        blocks = _content_to_blocks(message.get("content"))
        if role == "assistant":
            native_blocks = message.get("_anthropic_native_blocks")
            if isinstance(native_blocks, list) and all(
                isinstance(block, dict) for block in native_blocks
            ):
                blocks = [dict(block) for block in native_blocks]
            else:
                blocks = [
                    *_content_to_anthropic_thinking_blocks(
                        message.get("_anthropic_thinking_blocks")
                    ),
                    *blocks,
                ]
                for tool_call in message.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    name = function.get("name")
                    if isinstance(name, str) and name:
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": str(tool_call.get("id") or ""),
                                "name": name,
                                "input": _parse_tool_arguments(function.get("arguments")),
                            }
                        )
        if blocks:
            _append_anthropic_message(output, role, blocks)
    if not output:
        output.append({"role": "user", "content": [{"type": "text", "text": ""}]})
    return system_blocks, output


def _append_anthropic_message(
    messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
) -> None:
    if messages and messages[-1].get("role") == role:
        existing = messages[-1].setdefault("content", [])
        if isinstance(existing, list):
            existing.extend(blocks)
            return
    messages.append({"role": role, "content": blocks})


def _content_to_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, str):
                blocks.append({"type": "text", "text": item})
            elif isinstance(item, dict):
                block_type = item.get("type")
                if block_type == "text" and isinstance(item.get("text"), str):
                    block = {"type": "text", "text": item["text"]}
                    _copy_cache_control(item, block)
                    blocks.append(block)
                elif block_type == "image":
                    blocks.append(dict(item))
                elif block_type == "image_url":
                    image_block = _image_url_to_anthropic_block(item.get("image_url"))
                    if image_block is not None:
                        blocks.append(image_block)
                elif block_type in {"thinking", "redacted_thinking"}:
                    blocks.extend(_content_to_anthropic_thinking_blocks([item]))
                else:
                    text = _content_to_text(item)
                    if text:
                        blocks.append({"type": "text", "text": text})
        return blocks
    text = _content_to_text(content)
    return [{"type": "text", "text": text}] if text else []


def _copy_cache_control(source: dict[str, Any], target: dict[str, Any]) -> None:
    cache_control = source.get("cache_control")
    if isinstance(cache_control, dict) and cache_control.get("type") == "ephemeral":
        copied = {"type": "ephemeral"}
        ttl = cache_control.get("ttl")
        if isinstance(ttl, str) and ttl.strip():
            copied["ttl"] = ttl.strip().lower()
        target["cache_control"] = copied


def _image_url_to_anthropic_block(raw: Any) -> dict[str, Any] | None:
    url = raw.get("url") if isinstance(raw, dict) else raw
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if url.startswith("data:"):
        header, separator, payload = url.partition(",")
        if not separator:
            return None
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": header[5:].split(";", 1)[0] or "image/png",
                "data": payload,
            },
        }
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _content_to_anthropic_thinking_blocks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    blocks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        block_type = item.get("type")
        if block_type == "thinking":
            thinking = item.get("thinking")
            if not isinstance(thinking, str) or not thinking:
                continue
            block = {"type": "thinking", "thinking": thinking}
            signature = item.get("signature")
            if isinstance(signature, str) and signature:
                block["signature"] = signature
            blocks.append(block)
        elif block_type == "redacted_thinking":
            data = item.get("data")
            if isinstance(data, str) and data:
                blocks.append({"type": "redacted_thinking", "data": data})
    return blocks


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part for item in content if (part := _content_to_text(item)))
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        if isinstance(text, str):
            return text
    return str(content)


def _parse_tool_arguments(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {}


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        return text
        return _content_to_text(content)
    return ""


def _billing_header(message_text: str) -> str:
    chars = "".join(
        message_text[index] if index < len(message_text) else "0" for index in CCH_POSITIONS
    )
    suffix = hashlib.sha256(f"{CCH_SALT}{chars}{CLAUDE_CODE_VERSION}".encode()).hexdigest()[:3]
    cch = hashlib.sha256(message_text.encode()).hexdigest()[:5]
    return (
        "x-anthropic-billing-header: "
        f"cc_version={CLAUDE_CODE_VERSION}.{suffix}; "
        f"cc_entrypoint={CLAUDE_CODE_ENTRYPOINT}; "
        f"cch={cch};"
    )


def _sanitize_system_text(text: str) -> str:
    return text.replace(
        "Here is some useful information about the environment you are running in:",
        "Environment context you are running in:",
    ).strip()
