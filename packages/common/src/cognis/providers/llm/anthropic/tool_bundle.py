"""Strict, deterministic Anthropic tool bundle compilation.

The compiler projects provider wire schemas only.  Canonical Cognis schemas
remain the execution and validation authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, cast

from cognis.providers.llm.anthropic.contracts import (
    AnthropicToolBinding,
    CompiledAnthropicToolBundle,
)

_WIRE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_WIRE_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_STRICT_TOOLS = 20
_MAX_OPTIONAL_PARAMETERS = 24
_MAX_UNION_PARAMETERS = 16
_MAX_SCHEMA_DEPTH = 128
_MAX_SCHEMA_NODES = 100_000
_ANTHROPIC_TOOL_SEARCH_TYPES = frozenset(
    {"tool_search_tool_regex_20251119", "tool_search_tool_bm25_20251119"}
)
_UNSUPPORTED_STRICT_KEYS = frozenset(
    {
        "$ref",
        "$defs",
        "definitions",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "pattern",
        "format",
        "contains",
        "dependentSchemas",
        "unevaluatedProperties",
    }
)
_OBJECT_ROOT_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "dependencies",
        "dependentRequired",
        "dependentSchemas",
        "maxProperties",
        "minProperties",
        "patternProperties",
        "properties",
        "propertyNames",
        "required",
        "unevaluatedProperties",
    }
)
_NON_OBJECT_ROOT_KEYWORDS = frozenset(
    {
        "contains",
        "items",
        "maxContains",
        "maxItems",
        "minContains",
        "minItems",
        "prefixItems",
        "uniqueItems",
    }
)
_AMBIGUOUS_ROOT_KEYWORDS = frozenset(
    {
        "$dynamicRef",
        "$recursiveRef",
        "$ref",
        "allOf",
        "anyOf",
        "else",
        "if",
        "not",
        "oneOf",
        "then",
    }
)


@dataclass(frozen=True, slots=True)
class _SourceBinding:
    visible_name: str
    canonical_name: str
    stable_id: str
    aliases: Mapping[str, Any]


class _SchemaComplexityError(ValueError):
    """Provider projection cannot safely materialize a tool schema."""


def compile_anthropic_tool_bundle(
    exposed_tools: Sequence[Mapping[str, Any]],
    *,
    alias_map: Mapping[str, str] | None = None,
    stable_id_map: Mapping[str, str] | None = None,
    argument_alias_map: Mapping[str, Mapping[str, Any]] | None = None,
    server_tools: Sequence[Mapping[str, Any]] = (),
    strict_policy: Literal["preferred", "disabled"] = "preferred",
    wire_namespace: str | None = None,
) -> CompiledAnthropicToolBundle:
    """Compile OpenAI-shaped function definitions into exact Anthropic tools.

    ``stable_id_map`` is mandatory for definitions whose internal exposure
    metadata has already been stripped.  This deliberately rejects incomplete
    bindings instead of guessing identity from a provider-facing name.
    """
    if strict_policy not in {"preferred", "disabled"}:
        raise ValueError("Unsupported Anthropic strict policy")
    prefix = _validated_namespace(wire_namespace)
    # The exposure layer may carry native server tools inline while an
    # executor/controller transport can provide the same immutable inventory
    # out-of-band. Deduplicate only byte-for-byte equivalent server entries;
    # conflicting definitions are unsafe continuation state.
    combined_tools = list(exposed_tools)
    inline_server_tools = {
        str(tool.get("name")): tool
        for tool in combined_tools
        if tool.get("type") in _ANTHROPIC_TOOL_SEARCH_TYPES
    }
    for server_tool in server_tools:
        name = server_tool.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Anthropic server tool is missing a name")
        inline = inline_server_tools.get(name)
        if inline is None:
            combined_tools.append(server_tool)
            inline_server_tools[name] = server_tool
        elif dict(inline) != dict(server_tool):
            raise ValueError("Conflicting Anthropic server tool definitions")
    server_tools, client_tools = _preflight_tools(
        combined_tools, alias_map, stable_id_map, argument_alias_map
    )
    sources = _preflight_sources(client_tools, alias_map, stable_id_map, argument_alias_map)
    wire_names = _wire_names(sources, prefix)
    # Transport appends ``server_tools`` after the client wire inventory.
    # Keep them out of ``wire_tools`` or native Messages requests would send
    # each server tool twice.
    wire_tools: list[dict[str, Any]] = []
    bindings: list[AnthropicToolBinding] = []
    diagnostics: list[str] = []
    strict_used = 0

    for source, wire_name, raw in zip(sources, wire_names, client_tools, strict=True):
        function = raw["function"]
        try:
            input_schema, normalization_reason = _normalize_input_schema(
                function.get("parameters"),
                tool_name=source.canonical_name,
            )
            reverse_aliases = _clone_bounded_json(
                source.aliases,
                tool_name=source.canonical_name,
                field_name="reverse argument aliases",
            )
            tool: dict[str, Any] = {"name": wire_name, "input_schema": input_schema}
            description = function.get("description")
            if isinstance(description, str) and description:
                tool["description"] = description
            defer_loading = function.get("defer_loading")
            if _valid_metadata("defer_loading", defer_loading):
                tool["defer_loading"] = defer_loading
            input_examples = function.get("input_examples")
            if _valid_metadata("input_examples", input_examples):
                tool["input_examples"] = _clone_bounded_json(
                    input_examples,
                    tool_name=source.canonical_name,
                    field_name="input_examples",
                )
            cache_control = function.get("cache_control")
            if tool.get("defer_loading") is not True and _valid_metadata(
                "cache_control", cache_control
            ):
                tool["cache_control"] = _clone_bounded_json(
                    cache_control,
                    tool_name=source.canonical_name,
                    field_name="cache_control",
                )

            if strict_policy == "preferred":
                strict_schema, reason = (
                    (None, normalization_reason)
                    if normalization_reason is not None
                    else _strict_projection(input_schema)
                )
                if reason is None and strict_used < _MAX_STRICT_TOOLS:
                    tool["input_schema"] = strict_schema
                    tool["strict"] = True
                    strict_used += 1
                else:
                    diagnostics.append(
                        f"{source.canonical_name}: strict disabled: "
                        f"{reason or f'tool budget exhausted ({_MAX_STRICT_TOOLS})'}"
                    )
            binding = AnthropicToolBinding(
                wire_name=wire_name,
                canonical_name=source.canonical_name,
                stable_id=source.stable_id,
                reverse_argument_aliases=reverse_aliases,
            )
        except (RecursionError, _SchemaComplexityError) as exc:
            diagnostics.append(f"{source.canonical_name}: omitted: {exc}")
            continue
        bindings.append(binding)
        wire_tools.append(tool)
    return CompiledAnthropicToolBundle(
        wire_tools=tuple(wire_tools),
        bindings=tuple(bindings),
        server_tools=tuple(server_tools),
        strict_diagnostics=tuple(diagnostics),
    )


def _preflight_tools(
    tools: Sequence[Mapping[str, Any]],
    alias_map: Mapping[str, str] | None,
    stable_id_map: Mapping[str, str] | None,
    argument_alias_map: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split provider server tools from canonical executable client tools."""
    server_tools: list[Mapping[str, Any]] = []
    client_tools: list[Mapping[str, Any]] = []
    names: set[str] = set()
    for raw in tools:
        tool_type = raw.get("type")
        if tool_type in _ANTHROPIC_TOOL_SEARCH_TYPES:
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("Anthropic server tool is missing a name")
            if set(raw) != {"type", "name"}:
                raise ValueError("Anthropic tool-search server tool has unsupported fields")
            if name in names:
                raise ValueError("Anthropic server and client tool names must not collide")
            names.add(name)
            server_tools.append(raw)
            continue
        function = raw.get("function")
        visible = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(visible, str) and visible in names:
            raise ValueError("Anthropic server and client tool names must not collide")
        if isinstance(visible, str):
            names.add(visible)
        client_tools.append(raw)
    # Keep this call here so malformed client definitions fail before the
    # server/client split can affect a continuation-critical bundle.
    _preflight_sources(client_tools, alias_map, stable_id_map, argument_alias_map)
    return server_tools, client_tools


def _preflight_sources(
    tools: Sequence[Mapping[str, Any]],
    alias_map: Mapping[str, str] | None,
    stable_id_map: Mapping[str, str] | None,
    argument_alias_map: Mapping[str, Mapping[str, Any]] | None,
) -> list[_SourceBinding]:
    sources: list[_SourceBinding] = []
    seen_visible: set[str] = set()
    seen_canonical: set[str] = set()
    seen_stable: set[str] = set()
    for raw in tools:
        function = raw.get("function")
        if raw.get("type") != "function" or not isinstance(function, Mapping):
            raise ValueError("Anthropic compiler accepts only OpenAI function definitions")
        visible = function.get("name")
        if not isinstance(visible, str) or not visible:
            raise ValueError("Tool definition is missing a visible name")
        canonical = (alias_map or {}).get(visible, visible)
        stable = function.get("x-stable-tool-id") or (stable_id_map or {}).get(visible)
        if (
            not isinstance(canonical, str)
            or not canonical
            or not isinstance(stable, str)
            or not stable
        ):
            raise ValueError(f"Tool binding is incomplete for {visible!r}")
        if visible in seen_visible or canonical in seen_canonical or stable in seen_stable:
            raise ValueError("Tool bindings must be one-to-one and collision-free")
        aliases = (argument_alias_map or {}).get(canonical, {})
        if not isinstance(aliases, Mapping):
            raise TypeError(f"Reverse argument aliases for {canonical!r} must be an object")
        seen_visible.add(visible)
        seen_canonical.add(canonical)
        seen_stable.add(stable)
        sources.append(_SourceBinding(visible, canonical, stable, aliases))
    return sources


def _normalize_input_schema(
    parameters: Any,
    *,
    tool_name: str,
) -> tuple[dict[str, Any], str | None]:
    """Project a canonical function schema to Anthropic's object-root contract."""

    if not isinstance(parameters, Mapping):
        return {"type": "object", "properties": {}}, None

    schema = cast(
        dict[str, Any],
        _clone_bounded_json(
            parameters,
            tool_name=tool_name,
            field_name="input_schema",
        ),
    )
    if not schema:
        return {"type": "object", "properties": {}}, None

    schema_type = schema.get("type")
    object_keywords = sorted(set(schema) & _OBJECT_ROOT_KEYWORDS)
    non_object_keywords = sorted(set(schema) & _NON_OBJECT_ROOT_KEYWORDS)
    composition_keywords = [key for key in ("allOf", "anyOf", "oneOf") if key in schema]
    if len(composition_keywords) > 1:
        raise ValueError(
            f"Anthropic tool {tool_name!r} input_schema has multiple top-level "
            f"compositions ({', '.join(composition_keywords)})"
        )
    if "type" in schema:
        if not isinstance(schema_type, str):
            raise ValueError(
                f"Anthropic tool {tool_name!r} input_schema.type must be the string 'object'"
            )
        if schema_type != "object":
            raise ValueError(f"Anthropic tool {tool_name!r} input_schema must have an object root")
        if non_object_keywords:
            raise ValueError(
                f"Anthropic tool {tool_name!r} input_schema has contradictory object root "
                f"keywords ({non_object_keywords[0]})"
            )
        reference_keywords = sorted(set(schema) & {"$dynamicRef", "$recursiveRef", "$ref"})
        if reference_keywords:
            raise ValueError(
                f"Anthropic tool {tool_name!r} input_schema has an ambiguous root "
                f"({reference_keywords[0]})"
            )
        if composition_keywords:
            keyword = composition_keywords[0]
            return (
                _lower_object_composition(schema, keyword=keyword, tool_name=tool_name),
                f"top-level {keyword} lowered to an object projection",
            )
        return schema, None

    ambiguous_keywords = sorted(set(schema) & _AMBIGUOUS_ROOT_KEYWORDS)
    if ambiguous_keywords:
        if len(composition_keywords) == 1 and set(ambiguous_keywords) == set(composition_keywords):
            keyword = composition_keywords[0]
            return (
                _lower_object_composition(schema, keyword=keyword, tool_name=tool_name),
                f"top-level {keyword} lowered to an object projection",
            )
        raise ValueError(
            f"Anthropic tool {tool_name!r} input_schema has an ambiguous root "
            f"({ambiguous_keywords[0]})"
        )

    if object_keywords and non_object_keywords:
        raise ValueError(
            f"Anthropic tool {tool_name!r} input_schema has contradictory object root "
            f"keywords ({non_object_keywords[0]})"
        )
    if object_keywords:
        schema["type"] = "object"
        return schema, None
    raise ValueError(f"Anthropic tool {tool_name!r} input_schema must have an explicit object root")


def _lower_object_composition(
    schema: dict[str, Any],
    *,
    keyword: str,
    tool_name: str,
) -> dict[str, Any]:
    branches = schema.get(keyword)
    if not _all_composition_branches_are_objects(branches):
        raise ValueError(
            f"Anthropic tool {tool_name!r} input_schema has an ambiguous root ({keyword})"
        )
    assert isinstance(branches, list)
    lowered = {key: value for key, value in schema.items() if key != keyword}
    lowered["type"] = "object"

    properties: dict[str, Any] = {}
    branch_property_maps: list[Mapping[str, Any]] = []
    property_names: list[str] = []
    for branch in branches:
        assert isinstance(branch, Mapping)
        branch_properties = branch.get("properties", {})
        if isinstance(branch_properties, Mapping):
            branch_property_maps.append(branch_properties)
            for property_name in branch_properties:
                name = str(property_name)
                if name not in property_names:
                    property_names.append(name)
        else:
            branch_property_maps.append({})
        _merge_schema_definitions(lowered, branch, tool_name=tool_name)

    merge_keyword = "allOf" if keyword == "allOf" else "anyOf"
    for property_name in property_names:
        variants: list[Any] = []
        identities: set[str] = set()
        missing_from_branch = False
        for branch_properties in branch_property_maps:
            if property_name not in branch_properties:
                missing_from_branch = True
                continue
            property_schema = branch_properties[property_name]
            identity = _json_identity(property_schema)
            if identity not in identities:
                identities.add(identity)
                variants.append(property_schema)
        if keyword != "allOf" and missing_from_branch:
            properties[property_name] = {}
            continue
        properties[property_name] = variants[0] if len(variants) == 1 else {merge_keyword: variants}
    base_properties = lowered.get("properties")
    if isinstance(base_properties, Mapping):
        for property_name, property_schema in base_properties.items():
            properties.setdefault(str(property_name), property_schema)
    if properties:
        lowered["properties"] = properties

    required_lists = [
        (_required_names(branch["required"], tool_name=tool_name) if "required" in branch else [])
        for branch in branches
    ]
    base_required = (
        _required_names(lowered["required"], tool_name=tool_name) if "required" in lowered else []
    )
    if keyword == "allOf":
        required = list(
            dict.fromkeys([*base_required, *(item for values in required_lists for item in values)])
        )
    elif required_lists:
        common = set(required_lists[0]).intersection(*map(set, required_lists[1:]))
        required = list(
            dict.fromkeys([*base_required, *(item for item in required_lists[0] if item in common)])
        )
    else:
        required = base_required
    if required:
        lowered["required"] = required
    else:
        lowered.pop("required", None)
    return lowered


def _merge_schema_definitions(
    target: dict[str, Any],
    branch: Mapping[str, Any],
    *,
    tool_name: str,
) -> None:
    for keyword in ("$defs", "definitions"):
        branch_definitions = branch.get(keyword)
        if not isinstance(branch_definitions, Mapping):
            continue
        target_definitions = target.setdefault(keyword, {})
        if not isinstance(target_definitions, dict):
            raise ValueError(f"Anthropic tool {tool_name!r} input_schema has invalid {keyword}")
        for name, definition in branch_definitions.items():
            existing = target_definitions.get(name)
            if existing is not None and _json_identity(existing) != _json_identity(definition):
                raise ValueError(
                    f"Anthropic tool {tool_name!r} input_schema has conflicting "
                    f"{keyword} entry {name!r}"
                )
            target_definitions[name] = definition


def _required_names(value: Any, *, tool_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(
            f"Anthropic tool {tool_name!r} input_schema.required must be an array of strings"
        )
    return list(dict.fromkeys(value))


def _json_identity(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _all_composition_branches_are_objects(branches: Any) -> bool:
    if not isinstance(branches, list) or not branches:
        return False
    for branch in branches:
        if not isinstance(branch, Mapping):
            return False
        if set(branch) & {"$dynamicRef", "$recursiveRef", "$ref"}:
            return False
        branch_type = branch.get("type")
        if branch_type == "object":
            continue
        if "type" in branch:
            return False
        object_keywords = set(branch) & _OBJECT_ROOT_KEYWORDS
        non_object_keywords = set(branch) & _NON_OBJECT_ROOT_KEYWORDS
        ambiguous_keywords = set(branch) & _AMBIGUOUS_ROOT_KEYWORDS
        if not object_keywords or non_object_keywords or ambiguous_keywords:
            return False
    return True


def _clone_bounded_json(value: Any, *, tool_name: str, field_name: str) -> Any:
    """Validate and clone bounded provider JSON without exhausting the stack.

    Tool inventories can contain third-party MCP schemas and generated rich
    contracts. A single pathological provider field must not exhaust the
    controller's Python stack and fail the whole turn.
    """

    active: set[int] = set()
    node_count = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > _MAX_SCHEMA_NODES:
            raise _SchemaComplexityError(
                f"Anthropic tool {tool_name!r} {field_name} exceeds {_MAX_SCHEMA_NODES} nodes"
            )
        if depth > _MAX_SCHEMA_DEPTH:
            raise _SchemaComplexityError(
                f"Anthropic tool {tool_name!r} {field_name} exceeds "
                f"maximum depth {_MAX_SCHEMA_DEPTH}"
            )

        if isinstance(node, Mapping):
            marker = id(node)
            if marker in active:
                raise _SchemaComplexityError(
                    f"Anthropic tool {tool_name!r} {field_name} contains a cycle"
                )
            active.add(marker)
            try:
                for key, child in node.items():
                    if not isinstance(key, str):
                        raise _SchemaComplexityError(
                            f"Anthropic tool {tool_name!r} {field_name} contains "
                            "a non-string object key"
                        )
                    visit(child, depth + 1)
            finally:
                active.remove(marker)
            return

        if isinstance(node, (list, tuple)):
            marker = id(node)
            if marker in active:
                raise _SchemaComplexityError(
                    f"Anthropic tool {tool_name!r} {field_name} contains a cycle"
                )
            active.add(marker)
            try:
                for child in node:
                    visit(child, depth + 1)
            finally:
                active.remove(marker)
            return

        if node is None or isinstance(node, (str, bool, int)):
            return
        if isinstance(node, float) and math.isfinite(node):
            return
        raise _SchemaComplexityError(
            f"Anthropic tool {tool_name!r} {field_name} contains "
            f"unsupported value {type(node).__name__}"
        )

    visit(value, 0)
    return deepcopy(value)


def _validated_namespace(namespace: str | None) -> str:
    if namespace is None:
        return ""
    if not namespace or not _WIRE_NAME.fullmatch(namespace):
        raise ValueError("Anthropic wire namespace must satisfy the wire-name grammar")
    return f"{namespace}_"


def _wire_names(sources: Sequence[_SourceBinding], prefix: str) -> list[str]:
    used: set[str] = set()
    names: list[str] = []
    for source in sources:
        original = f"{prefix}{source.visible_name}"
        base = _safe_wire_base(original)
        candidate = base
        if candidate in used or candidate != original:
            candidate = _with_hash(base, original)
        counter = 0
        while candidate in used:
            counter += 1
            candidate = _with_hash(base, f"{original}:{counter}")
        used.add(candidate)
        names.append(candidate)
    return names


def _safe_wire_base(value: str) -> str:
    cleaned = _WIRE_UNSAFE.sub("_", value).strip("_-")
    return cleaned[:128].rstrip("_-") or "tool"


def _with_hash(base: str, identity: str) -> str:
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{base[: 128 - len(suffix) - 1].rstrip('_-')}_{suffix}"


def _valid_metadata(key: str, value: Any) -> bool:
    if key == "defer_loading":
        return isinstance(value, bool)
    if key == "cache_control":
        return isinstance(value, Mapping) and value.get("type") == "ephemeral"
    return isinstance(value, list) and all(isinstance(example, Mapping) for example in value)


def _strict_projection(schema: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    optional = 0
    unions = 0

    def project(node: Any, path: str) -> tuple[Any, str | None]:
        nonlocal optional, unions
        if not isinstance(node, Mapping):
            return node, None
        unsupported = sorted(set(node) & _UNSUPPORTED_STRICT_KEYS)
        if unsupported:
            return None, f"{path} uses unsupported {unsupported[0]}"
        result = {key: deepcopy(value) for key, value in node.items()}
        node_type = result.get("type")
        if not isinstance(node_type, (str, list)) or (
            isinstance(node_type, list) and not all(isinstance(item, str) for item in node_type)
        ):
            return None, f"{path} is missing an explicit type"
        if isinstance(node_type, list):
            unions += 1
        is_object = node_type == "object" or (isinstance(node_type, list) and "object" in node_type)
        is_array = node_type == "array" or (isinstance(node_type, list) and "array" in node_type)
        if is_object:
            properties = result.get("properties", {})
            additional = result.get("additionalProperties", False)
            if not isinstance(properties, Mapping):
                return None, f"{path}.properties is not an object"
            if additional is not False:
                return None, f"{path} is a dynamic map"
            required = result.get("required", [])
            if not isinstance(required, list) or not all(
                isinstance(name, str) for name in required
            ):
                return None, f"{path}.required is invalid"
            optional += len(set(properties) - set(required))
            result["additionalProperties"] = False
            for name, child in properties.items():
                projected, reason = project(child, f"{path}.properties.{name}")
                if reason:
                    return None, reason
                result["properties"][name] = projected
        elif is_array and "items" in result:
            projected, reason = project(result["items"], f"{path}.items")
            if reason:
                return None, reason
            result["items"] = projected
        return result, None

    projected, reason = project(schema, "input_schema")
    if reason:
        return {}, reason
    if optional > _MAX_OPTIONAL_PARAMETERS:
        return {}, f"optional parameter budget exhausted ({optional}>{_MAX_OPTIONAL_PARAMETERS})"
    if unions > _MAX_UNION_PARAMETERS:
        return {}, f"union parameter budget exhausted ({unions}>{_MAX_UNION_PARAMETERS})"
    return projected, None
