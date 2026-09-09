"""Immutable, transport-neutral contracts for Anthropic Messages requests."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import urlparse

from cognis.models.config import ModelInfo

CONTRACT_VERSION = 1
CANONICAL_JSON_VERSION = 1
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_ENVELOPE_BLOCKS = 256


class AnthropicProtocol(StrEnum):
    AUTO = "auto"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    LITELLM = "litellm"


class AnthropicAuthPolicy(StrEnum):
    API_KEY = "api_key"
    OAUTH = "oauth"


class AnthropicLocation(StrEnum):
    CONTROLLER = "controller"
    EXECUTOR = "executor"


class AnthropicContinuationStatus(StrEnum):
    CONTINUABLE = "continuable"
    NON_CONTINUABLE = "non_continuable"


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Canonical JSON object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    return materialize_json(value)


def materialize_json(value: Any) -> Any:
    """Copy immutable contract data into ordinary, finite JSON-compatible values."""

    return _materialize_json(value, path="$", active_containers=set())


def _materialize_json(value: Any, *, path: str, active_containers: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TypeError(f"JSON string at {path} is not valid UTF-8") from exc
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"JSON number at {path} must be finite")
        return value
    if isinstance(value, Mapping):
        return _materialize_json_mapping(value, path=path, active_containers=active_containers)
    if isinstance(value, (list, tuple)):
        container_id = id(value)
        if container_id in active_containers:
            raise TypeError(f"Cyclic JSON container at {path}")
        active_containers.add(container_id)
        try:
            return [
                _materialize_json(
                    item,
                    path=f"{path}[{index}]",
                    active_containers=active_containers,
                )
                for index, item in enumerate(value)
            ]
        finally:
            active_containers.remove(container_id)
    raise TypeError(f"Unsupported JSON value at {path}: {type(value).__name__}")


def _materialize_json_mapping(
    value: Mapping[Any, Any], *, path: str, active_containers: set[int]
) -> dict[str, Any]:
    container_id = id(value)
    if container_id in active_containers:
        raise TypeError(f"Cyclic JSON container at {path}")
    active_containers.add(container_id)
    try:
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object key at {path} must be a string")
            result[key] = _materialize_json(
                item,
                path=f"{path}.{key}",
                active_containers=active_containers,
            )
        return result
    finally:
        active_containers.remove(container_id)


def canonical_json(value: Any) -> str:
    """Encode JSON-compatible data with a stable versioned representation."""
    return json.dumps(
        _thaw_json(_freeze_json(value)),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_fingerprint(value: Any) -> str:
    payload = f"cognis-anthropic-canonical-json-v{CANONICAL_JSON_VERSION}:{canonical_json(value)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ModelInfoCapabilitySnapshot:
    """Immutable subset of ModelInfo that affects Anthropic request behavior."""

    model_id: str
    context_window: int
    max_output_tokens: int
    supports_tools: bool
    supports_streaming: bool
    supports_prompt_caching: bool
    supports_extended_thinking: bool
    supports_tool_search: bool
    supports_defer_loading: bool
    supports_strict_tools: bool
    supports_native_tool_search: bool
    supports_pause_turn: bool
    max_tools: int | None

    @classmethod
    def from_model_info(cls, model_info: ModelInfo) -> ModelInfoCapabilitySnapshot:
        return cls(
            model_id=model_info.model_id,
            context_window=model_info.context_window,
            max_output_tokens=model_info.max_output_tokens,
            supports_tools=model_info.supports_tools,
            supports_streaming=model_info.supports_streaming,
            supports_prompt_caching=model_info.supports_prompt_caching,
            supports_extended_thinking=model_info.supports_extended_thinking,
            supports_tool_search=model_info.supports_tool_search,
            supports_defer_loading=model_info.supports_defer_loading,
            supports_strict_tools=model_info.supports_strict_tools,
            supports_native_tool_search=model_info.supports_native_tool_search,
            supports_pause_turn=model_info.supports_pause_turn,
            max_tools=model_info.max_tools,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "supports_prompt_caching": self.supports_prompt_caching,
            "supports_extended_thinking": self.supports_extended_thinking,
            "supports_tool_search": self.supports_tool_search,
            "supports_defer_loading": self.supports_defer_loading,
            "supports_strict_tools": self.supports_strict_tools,
            "supports_native_tool_search": self.supports_native_tool_search,
            "supports_pause_turn": self.supports_pause_turn,
            "max_tools": self.max_tools,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ModelInfoCapabilitySnapshot:
        return cls(
            model_id=str(payload["model_id"]),
            context_window=int(payload["context_window"]),
            max_output_tokens=int(payload["max_output_tokens"]),
            supports_tools=bool(payload["supports_tools"]),
            supports_streaming=bool(payload["supports_streaming"]),
            supports_prompt_caching=bool(payload["supports_prompt_caching"]),
            supports_extended_thinking=bool(payload["supports_extended_thinking"]),
            supports_tool_search=bool(payload["supports_tool_search"]),
            supports_defer_loading=bool(payload["supports_defer_loading"]),
            supports_strict_tools=bool(payload["supports_strict_tools"]),
            supports_native_tool_search=bool(payload["supports_native_tool_search"]),
            supports_pause_turn=bool(payload["supports_pause_turn"]),
            max_tools=(int(payload["max_tools"]) if payload.get("max_tools") is not None else None),
        )


@dataclass(frozen=True, slots=True)
class ResolvedAnthropicRequestContext:
    """Resolved non-secret request identity and immutable capability snapshot."""

    provider_id: str
    model: str
    endpoint: str
    protocol: AnthropicProtocol
    location: AnthropicLocation
    auth_policy: AnthropicAuthPolicy
    credential_ref: str | None
    model_info: ModelInfoCapabilitySnapshot
    thinking_fingerprint: str
    chain_id: str
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not all(
            (
                self.provider_id,
                self.model,
                self.endpoint,
                self.thinking_fingerprint,
                self.chain_id,
            )
        ):
            raise ValueError("Resolved Anthropic request context fields must be non-empty")
        if self.auth_policy is AnthropicAuthPolicy.OAUTH:
            if self.location is not AnthropicLocation.CONTROLLER:
                raise ValueError("Anthropic OAuth is controller-only")
            if self.protocol is not AnthropicProtocol.ANTHROPIC_MESSAGES:
                raise ValueError("Anthropic OAuth requires the native Messages protocol")
        if self.credential_ref is not None and not self.credential_ref.startswith("$credential:"):
            raise ValueError("Anthropic credential_ref must be a $credential: reference")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "provider_id": self.provider_id,
            "model": self.model,
            "endpoint": self.endpoint,
            "protocol": self.protocol.value,
            "location": self.location.value,
            "auth_policy": self.auth_policy.value,
            "credential_ref": self.credential_ref,
            "model_info": self.model_info.to_dict(),
            "thinking_fingerprint": self.thinking_fingerprint,
            "chain_id": self.chain_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ResolvedAnthropicRequestContext:
        if payload.get("contract_version") != CONTRACT_VERSION:
            raise ValueError("Unsupported Anthropic request context contract version")
        model_info = payload.get("model_info")
        if not isinstance(model_info, Mapping):
            raise TypeError("Anthropic request context model_info must be an object")
        credential_ref = payload.get("credential_ref")
        return cls(
            provider_id=str(payload["provider_id"]),
            model=str(payload["model"]),
            endpoint=str(payload["endpoint"]),
            protocol=AnthropicProtocol(str(payload["protocol"])),
            location=AnthropicLocation(str(payload["location"])),
            auth_policy=AnthropicAuthPolicy(str(payload["auth_policy"])),
            credential_ref=str(credential_ref) if credential_ref is not None else None,
            model_info=ModelInfoCapabilitySnapshot.from_dict(model_info),
            thinking_fingerprint=str(payload["thinking_fingerprint"]),
            chain_id=str(payload["chain_id"]),
        )


@dataclass(frozen=True, slots=True)
class AnthropicProtocolResolution:
    protocol: AnthropicProtocol
    location: AnthropicLocation


def resolve_anthropic_protocol(
    requested: AnthropicProtocol,
    *,
    endpoint: str,
    auth_policy: AnthropicAuthPolicy,
    location: AnthropicLocation,
) -> AnthropicProtocolResolution:
    """Resolve the transport protocol without constructing a runtime client."""
    if auth_policy is AnthropicAuthPolicy.OAUTH:
        if location is not AnthropicLocation.CONTROLLER:
            raise ValueError("Anthropic OAuth is controller-only")
        if requested not in (AnthropicProtocol.AUTO, AnthropicProtocol.ANTHROPIC_MESSAGES):
            raise ValueError("Anthropic OAuth requires the native Messages protocol")
        return AnthropicProtocolResolution(AnthropicProtocol.ANTHROPIC_MESSAGES, location)

    if requested is AnthropicProtocol.AUTO:
        hostname = urlparse(endpoint).hostname
        protocol = (
            AnthropicProtocol.ANTHROPIC_MESSAGES
            if hostname == "api.anthropic.com"
            else AnthropicProtocol.LITELLM
        )
    else:
        protocol = requested
    return AnthropicProtocolResolution(protocol, location)


@dataclass(frozen=True, slots=True)
class AnthropicToolBinding:
    """Bijection between a provider-visible wire name and Cognis tool identity."""

    wire_name: str
    canonical_name: str
    stable_id: str
    reverse_argument_aliases: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not all((self.wire_name, self.canonical_name, self.stable_id)):
            raise ValueError("Tool binding identifiers must be non-empty")
        aliases = _freeze_json(self.reverse_argument_aliases)
        if not isinstance(aliases, Mapping):
            raise TypeError("reverse_argument_aliases must be an object")
        if any(not isinstance(key, str) for key in aliases):
            raise TypeError("Reverse argument alias keys must be strings")
        _validate_reverse_argument_aliases(aliases)
        object.__setattr__(self, "reverse_argument_aliases", aliases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wire_name": self.wire_name,
            "canonical_name": self.canonical_name,
            "stable_id": self.stable_id,
            "reverse_argument_aliases": _thaw_json(self.reverse_argument_aliases),
        }


def _validate_reverse_argument_aliases(
    aliases: Mapping[str, Any],
    *,
    ref_definitions: Mapping[str, Any] | None = None,
    allow_definitions: bool = True,
    allow_reference: bool = False,
) -> None:
    """Validate legacy flat maps and recursive alias trees without ambiguity."""
    stored_definitions = aliases.get("$cognis_refs")
    if stored_definitions is not None:
        if not allow_definitions:
            raise TypeError("Recursive argument alias definitions are only allowed at the root")
        if not isinstance(stored_definitions, Mapping):
            raise TypeError("Recursive argument alias definitions must be a mapping")
        ref_definitions = stored_definitions
    if ref_definitions is None:
        ref_definitions = {}
    canonical_names: set[str] = set()
    for wire_name, value in aliases.items():
        if wire_name == "$cognis_ref":
            if not isinstance(value, str):
                raise TypeError("Recursive argument alias references must be strings")
            if not allow_reference:
                raise TypeError("Recursive argument alias references require an alias-tree node")
            if value not in ref_definitions:
                raise ValueError("Recursive argument alias reference is not defined")
            continue
        if wire_name == "$cognis_refs":
            for ref_name, ref_aliases in value.items():
                if not isinstance(ref_name, str) or not isinstance(ref_aliases, Mapping):
                    raise TypeError(
                        "Recursive argument alias definitions require string keys and alias trees"
                    )
                _validate_reverse_argument_aliases(
                    ref_aliases,
                    ref_definitions=ref_definitions,
                    allow_definitions=False,
                    allow_reference=True,
                )
            continue
        canonical_name: str
        if isinstance(value, str):
            canonical_name = value
        elif isinstance(value, Mapping):
            original_name = value.get("original")
            children = value.get("properties", {})
            if not isinstance(original_name, str) or not isinstance(children, Mapping):
                raise TypeError("Reverse argument alias trees require original and properties")
            canonical_name = original_name
            _validate_reverse_argument_aliases(
                children,
                ref_definitions=ref_definitions,
                allow_definitions=False,
                allow_reference=True,
            )
        else:
            raise TypeError("Reverse argument aliases must map to strings or alias-tree objects")
        if canonical_name in canonical_names:
            raise ValueError("Reverse argument aliases must be bijective")
        canonical_names.add(canonical_name)


@dataclass(frozen=True, slots=True)
class CompiledAnthropicToolBundle:
    """Exact ordered native tool payload and continuation-critical bindings."""

    wire_tools: tuple[Mapping[str, Any], ...]
    bindings: tuple[AnthropicToolBinding, ...]
    server_tools: tuple[Mapping[str, Any], ...] = ()
    strict_diagnostics: tuple[str, ...] = ()
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        tools = tuple(_freeze_json(tool) for tool in self.wire_tools)
        if any(not isinstance(tool, Mapping) for tool in tools):
            raise TypeError("Anthropic wire tools must be JSON objects")
        names = [tool.get("name") for tool in tools]
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Each Anthropic wire tool must have a non-empty name")
        if len(set(names)) != len(names):
            raise ValueError("Anthropic wire tool names must be unique")
        binding_names = [binding.wire_name for binding in self.bindings]
        stable_ids = [binding.stable_id for binding in self.bindings]
        canonical_names = [binding.canonical_name for binding in self.bindings]
        client_tool_names = [
            name for tool, name in zip(tools, names, strict=True) if tool.get("type") is None
        ]
        if client_tool_names != binding_names:
            raise ValueError("Client tool bindings must exactly match client wire tool order")
        if len(set(stable_ids)) != len(stable_ids) or len(set(canonical_names)) != len(
            canonical_names
        ):
            raise ValueError("Tool stable IDs and canonical names must be unique")
        server_tools = tuple(_freeze_json(tool) for tool in self.server_tools)
        allowed_server_tools = {
            "tool_search_tool_regex_20251119": "tool_search_tool_regex",
            "tool_search_tool_bm25_20251119": "tool_search_tool_bm25",
        }
        for tool in server_tools:
            if not isinstance(tool, Mapping):
                raise TypeError("Anthropic server tools must be JSON objects")
            tool_type = tool.get("type")
            expected_name = (
                allowed_server_tools.get(tool_type) if isinstance(tool_type, str) else None
            )
            if (
                expected_name is None
                or tool.get("name") != expected_name
                or set(tool) != {"type", "name"}
            ):
                raise ValueError("Unsupported Anthropic server tool definition")
        if len({tool["name"] for tool in server_tools}) != len(server_tools):
            raise ValueError("Anthropic server tool names must be unique")
        object.__setattr__(self, "wire_tools", tools)
        object.__setattr__(self, "bindings", tuple(self.bindings))
        object.__setattr__(self, "server_tools", server_tools)
        object.__setattr__(self, "strict_diagnostics", tuple(self.strict_diagnostics))

    @property
    def fingerprint(self) -> str:
        return sha256_fingerprint(
            {
                "contract_version": self.contract_version,
                "wire_tools": _thaw_json(self.wire_tools),
                "server_tools": _thaw_json(self.server_tools),
                "bindings": [binding.to_dict() for binding in self.bindings],
            }
        )

    def to_dict(self, *, include_diagnostics: bool = True) -> dict[str, Any]:
        payload = {
            "contract_version": self.contract_version,
            "wire_tools": _thaw_json(self.wire_tools),
            "server_tools": _thaw_json(self.server_tools),
            "bindings": [binding.to_dict() for binding in self.bindings],
            "fingerprint": self.fingerprint,
        }
        if include_diagnostics:
            payload["strict_diagnostics"] = list(self.strict_diagnostics)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CompiledAnthropicToolBundle:
        if payload.get("contract_version") != CONTRACT_VERSION:
            raise ValueError("Unsupported Anthropic tool bundle contract version")
        wire_tools = payload.get("wire_tools")
        bindings = payload.get("bindings")
        if not isinstance(wire_tools, list | tuple) or not isinstance(bindings, list | tuple):
            raise TypeError("Anthropic tool bundle tools and bindings must be arrays")
        bundle = cls(
            wire_tools=tuple(wire_tools),
            server_tools=tuple(payload.get("server_tools", ())),
            bindings=tuple(
                AnthropicToolBinding(
                    wire_name=str(binding["wire_name"]),
                    canonical_name=str(binding["canonical_name"]),
                    stable_id=str(binding["stable_id"]),
                    reverse_argument_aliases=binding.get("reverse_argument_aliases", {}),
                )
                for binding in bindings
                if isinstance(binding, Mapping)
            ),
            strict_diagnostics=tuple(str(item) for item in payload.get("strict_diagnostics", ())),
        )
        supplied_fingerprint = payload.get("fingerprint")
        if supplied_fingerprint is not None and supplied_fingerprint != bundle.fingerprint:
            raise ValueError("Anthropic tool bundle fingerprint mismatch")
        return bundle


_ALLOWED_ASSISTANT_BLOCKS: dict[str, frozenset[str]] = {
    "text": frozenset({"type", "text", "citations"}),
    "thinking": frozenset({"type", "thinking", "signature"}),
    "redacted_thinking": frozenset({"type", "data"}),
    "tool_use": frozenset({"type", "id", "name", "input", "caller"}),
    "server_tool_use": frozenset({"type", "id", "name", "input"}),
    "tool_search_tool_result": frozenset({"type", "tool_use_id", "content"}),
}
_TOOL_SEARCH_SERVER_NAMES = frozenset({"tool_search_tool_regex", "tool_search_tool_bm25"})


def _validate_assistant_block(block: Mapping[str, Any]) -> Mapping[str, Any]:
    block_type = block.get("type")
    if not isinstance(block_type, str) or block_type not in _ALLOWED_ASSISTANT_BLOCKS:
        raise ValueError(f"Unsupported Anthropic assistant block: {block_type!r}")
    if not set(block).issubset(_ALLOWED_ASSISTANT_BLOCKS[block_type]):
        raise ValueError(f"Unsupported fields in Anthropic {block_type!r} block")
    required = {
        "text": ("text",),
        "thinking": ("thinking", "signature"),
        "redacted_thinking": ("data",),
        "tool_use": ("id", "name", "input"),
        "server_tool_use": ("id", "name", "input"),
        "tool_search_tool_result": ("tool_use_id", "content"),
    }[block_type]
    if any(key not in block for key in required):
        raise ValueError(f"Incomplete Anthropic {block_type!r} block")
    if block_type == "text" and not isinstance(block["text"], str):
        raise TypeError("Anthropic text blocks require string text")
    if block_type == "thinking" and (
        not isinstance(block["thinking"], str)
        or not isinstance(block["signature"], str)
        or not block["signature"]
    ):
        raise TypeError(
            "Anthropic thinking blocks require a thinking string and non-empty signature"
        )
    if block_type == "redacted_thinking" and (
        not isinstance(block["data"], str) or not block["data"]
    ):
        raise TypeError("Anthropic redacted thinking blocks require non-empty data")
    if block_type in {"tool_use", "server_tool_use"} and (
        not isinstance(block["id"], str)
        or not block["id"]
        or not isinstance(block["name"], str)
        or not block["name"]
        or not isinstance(block["input"], Mapping)
    ):
        raise TypeError(
            f"Anthropic {block_type} blocks require non-empty IDs, names, and object input"
        )
    if block_type == "tool_use" and "caller" in block:
        _validate_tool_use_caller(block["caller"])
    if block_type == "server_tool_use" and block["name"] not in _TOOL_SEARCH_SERVER_NAMES:
        raise ValueError("Unsupported Anthropic server tool")
    if block_type == "tool_search_tool_result":
        _validate_tool_search_result(block)
    return cast(Mapping[str, Any], _freeze_json(block))


def _validate_tool_use_caller(caller: Any) -> None:
    if not isinstance(caller, Mapping):
        raise TypeError("Anthropic tool_use caller must be an object")
    caller_type = caller.get("type")
    if caller_type == "direct":
        if set(caller) != {"type"}:
            raise ValueError("Anthropic direct tool_use caller has unsupported fields")
        return
    raise ValueError("Unsupported Anthropic tool_use caller")


def _validate_tool_search_result(block: Mapping[str, Any]) -> None:
    tool_use_id = block["tool_use_id"]
    content = block["content"]
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise TypeError("Anthropic tool search results require a non-empty server tool ID")
    if not isinstance(content, Mapping) or set(content) != {"type", "tool_references"}:
        raise ValueError("Anthropic tool search result content is malformed")
    if content.get("type") != "tool_search_tool_search_result":
        raise ValueError("Unsupported Anthropic tool search result content")
    references = content.get("tool_references")
    if not isinstance(references, (list, tuple)):
        raise TypeError("Anthropic tool search results require tool_references")
    for reference in references:
        if (
            not isinstance(reference, Mapping)
            or set(reference) != {"type", "tool_name"}
            or reference.get("type") != "tool_reference"
            or not isinstance(reference.get("tool_name"), str)
            or not reference["tool_name"]
        ):
            raise ValueError("Anthropic tool search result has a malformed tool reference")


@dataclass(frozen=True, slots=True)
class AnthropicNativeEnvelope:
    """Persistable native assistant output only when safe for continuation."""

    native_blocks: tuple[Mapping[str, Any], ...]
    stop_reason: str | None
    stop_details: Mapping[str, Any]
    usage: Mapping[str, Any]
    pending_client_message_id: str | None
    pending_server_message_id: str | None
    bundle_fingerprint: str
    provider_fingerprint: str
    model_fingerprint: str
    thinking_fingerprint: str
    continuation_status: AnthropicContinuationStatus = AnthropicContinuationStatus.CONTINUABLE
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        blocks = tuple(_validate_assistant_block(block) for block in self.native_blocks)
        if len(blocks) > MAX_ENVELOPE_BLOCKS:
            raise ValueError(f"Anthropic native envelope exceeds {MAX_ENVELOPE_BLOCKS} blocks")
        if not all(
            (
                self.bundle_fingerprint,
                self.provider_fingerprint,
                self.model_fingerprint,
                self.thinking_fingerprint,
            )
        ):
            raise ValueError("Anthropic continuation fingerprints must be non-empty")
        object.__setattr__(self, "native_blocks", blocks)
        client_ids = tuple(str(block["id"]) for block in blocks if block.get("type") == "tool_use")
        server_ids = tuple(
            str(block["id"]) for block in blocks if block.get("type") == "server_tool_use"
        )
        if len(set((*client_ids, *server_ids))) != len((*client_ids, *server_ids)):
            raise ValueError("Anthropic native tool-use IDs must be globally unique")
        object.__setattr__(self, "stop_details", _freeze_json(self.stop_details))
        object.__setattr__(self, "usage", _freeze_json(self.usage))
        if len(self.to_json().encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise ValueError(f"Anthropic native envelope exceeds {MAX_ENVELOPE_BYTES} bytes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "native_blocks": _thaw_json(self.native_blocks),
            "stop_reason": self.stop_reason,
            "stop_details": _thaw_json(self.stop_details),
            "usage": _thaw_json(self.usage),
            "pending_client_message_id": self.pending_client_message_id,
            "pending_server_message_id": self.pending_server_message_id,
            "bundle_fingerprint": self.bundle_fingerprint,
            "provider_fingerprint": self.provider_fingerprint,
            "model_fingerprint": self.model_fingerprint,
            "thinking_fingerprint": self.thinking_fingerprint,
            "continuation_status": self.continuation_status.value,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AnthropicNativeEnvelope:
        if payload.get("contract_version") != CONTRACT_VERSION:
            raise ValueError("Unsupported Anthropic native envelope contract version")
        return cls(
            native_blocks=tuple(payload["native_blocks"]),
            stop_reason=payload.get("stop_reason"),
            stop_details=payload.get("stop_details", {}),
            usage=payload.get("usage", {}),
            pending_client_message_id=payload.get("pending_client_message_id"),
            pending_server_message_id=payload.get("pending_server_message_id"),
            bundle_fingerprint=str(payload["bundle_fingerprint"]),
            provider_fingerprint=str(payload["provider_fingerprint"]),
            model_fingerprint=str(payload["model_fingerprint"]),
            thinking_fingerprint=str(payload["thinking_fingerprint"]),
            continuation_status=AnthropicContinuationStatus(
                payload.get("continuation_status", AnthropicContinuationStatus.CONTINUABLE)
            ),
            contract_version=CONTRACT_VERSION,
        )

    @property
    def client_tool_use_ids(self) -> tuple[str, ...]:
        """Only client tool IDs are eligible for Cognis tool-result execution."""

        return tuple(
            str(block["id"]) for block in self.native_blocks if block.get("type") == "tool_use"
        )

    @property
    def server_tool_use_ids(self) -> tuple[str, ...]:
        """Server tool IDs stay in the native transcript and never enter ToolRouter."""

        return tuple(
            str(block["id"])
            for block in self.native_blocks
            if block.get("type") == "server_tool_use"
        )

    def assert_matches(
        self,
        *,
        bundle_fingerprint: str,
        provider_fingerprint: str,
        model_fingerprint: str,
        thinking_fingerprint: str,
    ) -> None:
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError("Unsupported Anthropic native envelope contract version")
        if self.continuation_status is not AnthropicContinuationStatus.CONTINUABLE:
            raise ValueError("Anthropic native envelope is not continuable")
        expected = (
            self.bundle_fingerprint,
            self.provider_fingerprint,
            self.model_fingerprint,
            self.thinking_fingerprint,
        )
        actual = (bundle_fingerprint, provider_fingerprint, model_fingerprint, thinking_fingerprint)
        if expected != actual:
            raise ValueError("Anthropic native envelope continuation fingerprint mismatch")
