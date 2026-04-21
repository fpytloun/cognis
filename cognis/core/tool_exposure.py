"""Provider-aware tool exposure for model-facing tool schemas."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any

from cognis.models.config import ModelInfo
from cognis.models.tool import ToolDefinition, stable_tool_id
from cognis.providers.llm.responses_bridge import should_use_openai_responses
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL

_VISIBLE_TOOL_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")
_MAX_VISIBLE_TOOL_NAME_LENGTH = 64


@dataclass(slots=True)
class ToolExposureResult:
    """Prepared model-facing tools and related metadata."""

    tools: list[dict[str, Any]]
    alias_map: dict[str, str]
    request_kwargs: dict[str, Any] = field(default_factory=dict)
    visible_tool_ids: set[str] = field(default_factory=set)
    debug_metadata: dict[str, Any] = field(default_factory=dict)


def prepare_tool_exposure(
    *,
    inventory_tools: list[ToolDefinition],
    controller_tool_schemas: list[dict[str, Any]],
    model: str,
    model_info: ModelInfo,
    discovered_tool_ids: set[str],
    default_visible_tool_ids: set[str] | None = None,
    allow_tool_search: bool = True,
) -> ToolExposureResult:
    """Prepare provider-specific model-facing tool schemas."""

    use_openai_responses = should_use_openai_responses(
        model=model,
        model_info=model_info,
        rollout_mode=os.getenv("COGNIS_OPENAI_RESPONSES_MODE", "auto").strip().lower(),
    )
    controller_tool_schemas_with_search = list(controller_tool_schemas)
    controller_tool_schemas_without_search = [
        schema
        for schema in controller_tool_schemas_with_search
        if schema.get("function", {}).get("name") != SEARCH_TOOLS_TOOL.name
    ]
    supports_openai_allowed_tools = bool(model_info.supports_openai_allowed_tools)
    request_kwargs: dict[str, Any] = {}
    sorted_inventory = sorted(inventory_tools, key=_tool_sort_key)
    visible_defaults = (
        default_visible_tool_ids
        if default_visible_tool_ids is not None
        else {stable_tool_id(tool) for tool in sorted_inventory if not _is_deferred_tool(tool)}
    )
    core_tools = [tool for tool in sorted_inventory if stable_tool_id(tool) in visible_defaults]
    deferred_tools = [
        tool for tool in sorted_inventory if stable_tool_id(tool) not in visible_defaults
    ]
    search_tool = next((tool for tool in core_tools if tool.name == SEARCH_TOOLS_TOOL.name), None)
    core_without_search = [tool for tool in core_tools if tool.name != SEARCH_TOOLS_TOOL.name]
    discovered_visible = [
        tool for tool in deferred_tools if stable_tool_id(tool) in discovered_tool_ids
    ]
    max_tools = model_info.max_tools
    use_anthropic_defer = bool(model_info.supports_defer_loading)
    use_openai_native_tool_search = bool(
        use_openai_responses
        and supports_openai_allowed_tools
        and allow_tool_search
        and model_info.supports_tool_search
        and model_info.supports_openai_namespace_tools
        and deferred_tools
    )
    use_openai_flat_tool_search = bool(
        use_openai_responses
        and supports_openai_allowed_tools
        and allow_tool_search
        and model_info.supports_tool_search
        and not model_info.supports_openai_namespace_tools
        and deferred_tools
    )
    use_openai_controller_search_fallback = bool(
        use_openai_responses
        and allow_tool_search
        and not supports_openai_allowed_tools
        and any(
            schema.get("function", {}).get("name") == SEARCH_TOOLS_TOOL.name
            for schema in controller_tool_schemas_with_search
        )
        and deferred_tools
    )
    if not allow_tool_search:
        filtered_controller_tool_schemas = controller_tool_schemas_without_search
    elif not use_openai_responses or use_openai_controller_search_fallback:
        filtered_controller_tool_schemas = controller_tool_schemas_with_search
    else:
        filtered_controller_tool_schemas = controller_tool_schemas_without_search

    alias_map = {
        schema.get("function", {}).get("name", ""): schema.get("function", {}).get("name", "")
        for schema in filtered_controller_tool_schemas
        if isinstance(schema.get("function", {}).get("name"), str)
    }
    controller_count = len(filtered_controller_tool_schemas)
    available_slots = None if max_tools is None else max(0, max_tools - controller_count)
    if use_openai_native_tool_search:
        alias_map = {
            schema.get("function", {}).get("name", ""): schema.get("function", {}).get("name", "")
            for schema in filtered_controller_tool_schemas
            if isinstance(schema.get("function", {}).get("name"), str)
        }
        controller_count = len(filtered_controller_tool_schemas)
        available_slots = None if max_tools is None else max(0, max_tools - controller_count)
    if use_anthropic_defer:
        strategy = "anthropic_defer_loading"
        visible_tools = core_without_search + deferred_tools
        tool_schemas = _build_inventory_schemas(
            visible_tools,
            alias_map,
            deferred_tool_ids={stable_tool_id(tool) for tool in deferred_tools},
        )
        _mark_anthropic_cache_breakpoint(
            tool_schemas,
            stable_anchor_tool_ids={stable_tool_id(tool) for tool in core_without_search},
        )
        request_kwargs = {
            "extra_headers": {"anthropic-beta": "tool-search-tool-2025-10-19"},
            "disable_parallel_tool_use": False,
        }
    elif use_openai_native_tool_search:
        strategy = "openai_responses_tool_search"
        visible_tools = core_without_search + deferred_tools
        core_schemas = _build_inventory_schemas(core_without_search, alias_map)
        tool_schemas = [
            *core_schemas,
            *_build_openai_deferred_namespaces(deferred_tools, alias_map),
            {"type": "tool_search"},
        ]
        request_kwargs = {
            "tool_choice": _openai_allowed_tools_choice(core_schemas),
            "parallel_tool_calls": True,
        }
    elif use_openai_flat_tool_search:
        strategy = "openai_responses_flat_tool_search"
        visible_tools = core_without_search + deferred_tools
        visible_schemas = _build_inventory_schemas(
            visible_tools,
            alias_map,
            deferred_tool_ids={stable_tool_id(tool) for tool in deferred_tools},
        )
        tool_schemas = [
            *visible_schemas,
            {"type": "tool_search"},
        ]
        request_kwargs = {
            "tool_choice": _openai_allowed_tools_choice(
                [
                    schema
                    for schema in visible_schemas
                    if schema.get("function", {}).get("defer_loading") is None
                ]
            ),
            "parallel_tool_calls": True,
        }
    elif use_openai_controller_search_fallback:
        strategy = "openai_responses_controller_search_fallback"
        base_tools, overflowed = _select_generic_visible_tools(
            core_tools=core_without_search,
            discovered_tools=discovered_visible,
            search_tool=search_tool,
            available_slots=available_slots,
            deferred_present=bool(deferred_tools),
        )
        visible_tools = base_tools
        tool_schemas = _build_inventory_schemas(visible_tools, alias_map)
        request_kwargs = {"tool_choice": "auto", "parallel_tool_calls": True}
        if overflowed and tool_schemas and max_tools is not None:
            tool_schemas = tool_schemas[:available_slots]
    elif use_openai_responses and deferred_tools:
        strategy = "openai_responses_full_inventory_no_defer"
        visible_tools = core_without_search + deferred_tools
        tool_schemas = _build_inventory_schemas(
            visible_tools,
            alias_map,
        )
        request_kwargs = {"tool_choice": "auto", "parallel_tool_calls": True}
    elif use_openai_responses:
        strategy = "openai_responses_full_inventory"
        visible_tools = core_without_search + deferred_tools
        tool_schemas = _build_inventory_schemas(
            visible_tools,
            alias_map,
            deferred_tool_ids={stable_tool_id(tool) for tool in deferred_tools},
        )
        request_kwargs = {"tool_choice": "auto", "parallel_tool_calls": True}
    else:
        strategy = "generic_search_tools"
        base_tools, overflowed = _select_generic_visible_tools(
            core_tools=core_without_search,
            discovered_tools=discovered_visible,
            search_tool=search_tool,
            available_slots=available_slots,
            deferred_present=bool(deferred_tools),
        )
        visible_tools = base_tools
        tool_schemas = _build_inventory_schemas(visible_tools, alias_map)
        request_kwargs = {}
        if overflowed and tool_schemas and max_tools is not None:
            tool_schemas = tool_schemas[:available_slots]

    visible_tool_ids = {stable_tool_id(tool) for tool in visible_tools}
    final_tool_schemas = _strip_internal_schema_metadata(tool_schemas)
    return ToolExposureResult(
        tools=[*filtered_controller_tool_schemas, *final_tool_schemas],
        alias_map=alias_map,
        request_kwargs=request_kwargs,
        visible_tool_ids=visible_tool_ids,
        debug_metadata={
            "strategy": strategy,
            "model": model,
            "controller_tool_count": controller_count,
            "inventory_tool_count": len(sorted_inventory),
            "core_tool_count": len(core_tools),
            "deferred_tool_count": len(deferred_tools),
            "visible_tool_count": len(visible_tools),
            "discovered_tool_count": len(discovered_visible),
            "max_tools": max_tools,
        },
    )


def _build_inventory_schemas(
    tools: list[ToolDefinition],
    alias_map: dict[str, str],
    *,
    deferred_tool_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    deferred_tool_ids = deferred_tool_ids or set()
    used_names: set[str] = set(alias_map)
    schemas: list[dict[str, Any]] = []
    for tool in tools:
        visible_name = _dedupe_visible_name(
            _tool_visible_name(tool), stable_tool_id(tool), used_names
        )
        alias_map[visible_name] = tool.name
        function_schema: dict[str, Any] = {
            "name": visible_name,
            "description": tool.description,
            "parameters": tool.parameters,
            "x-stable-tool-id": stable_tool_id(tool),
        }
        if stable_tool_id(tool) in deferred_tool_ids:
            function_schema["defer_loading"] = True
        schemas.append({"type": "function", "function": function_schema})
    return schemas


def _mark_anthropic_cache_breakpoint(
    tool_schemas: list[dict[str, Any]],
    *,
    stable_anchor_tool_ids: set[str],
) -> None:
    """Attach Anthropic tool-array cache_control to a stable schema edge.

    Prefer the last non-deferred core tool because deferred/discovered tools can
    be appended or reordered across turns as the model explores the inventory.
    """

    if not tool_schemas:
        return

    anchor_index = len(tool_schemas) - 1
    for index in range(len(tool_schemas) - 1, -1, -1):
        function = tool_schemas[index].get("function")
        if not isinstance(function, dict):
            continue
        tool_id = function.get("x-stable-tool-id")
        if isinstance(tool_id, str) and tool_id in stable_anchor_tool_ids:
            anchor_index = index
            break

    function = tool_schemas[anchor_index].get("function")
    if isinstance(function, dict):
        function["cache_control"] = {"type": "ephemeral"}


def _strip_internal_schema_metadata(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for schema in tool_schemas:
        sanitized_schema = dict(schema)
        function = sanitized_schema.get("function")
        if isinstance(function, dict) and "x-stable-tool-id" in function:
            function = dict(function)
            function.pop("x-stable-tool-id", None)
            sanitized_schema["function"] = function
        sanitized.append(sanitized_schema)
    return sanitized


def _build_openai_deferred_namespaces(
    tools: list[ToolDefinition], alias_map: dict[str, str]
) -> list[dict[str, Any]]:
    grouped: dict[str, list[ToolDefinition]] = {}
    for tool in tools:
        grouped.setdefault(_openai_namespace_key(tool), []).append(tool)

    namespaces: list[dict[str, Any]] = []
    used_namespace_names: set[str] = set(alias_map)
    for namespace_key in sorted(grouped):
        group_tools = grouped[namespace_key]
        namespace_name = _dedupe_visible_name(
            _sanitize_visible_name(namespace_key), namespace_key, used_namespace_names
        )
        namespace_tools: list[dict[str, Any]] = []
        used_names: set[str] = set(alias_map)
        for tool in group_tools:
            visible_name = _dedupe_visible_name(
                _tool_visible_name(tool), stable_tool_id(tool), used_names
            )
            alias_map[visible_name] = tool.name
            namespace_tools.append(
                {
                    "type": "function",
                    "name": visible_name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "defer_loading": True,
                }
            )
        namespaces.append(
            {
                "type": "namespace",
                "name": namespace_name,
                "description": _openai_namespace_description(group_tools[0]),
                "tools": namespace_tools,
            }
        )
    return namespaces


def _openai_allowed_tools_choice(visible_schemas: list[dict[str, Any]]) -> dict[str, Any] | str:
    return {
        "type": "allowed_tools",
        "tools": [
            *[
                {"type": "function", "name": function_name}
                for schema in visible_schemas
                if isinstance(schema.get("function"), dict)
                and isinstance((function_name := schema["function"].get("name")), str)
            ]
        ],
    }


def _openai_namespace_key(tool: ToolDefinition) -> str:
    if tool.source.type == "skill":
        return f"skill_{tool.source.skill_id or 'skill'}"
    server_name = tool.source.server_name or tool.source.server_id or "server"
    return f"mcp_{server_name}"


def _openai_namespace_description(tool: ToolDefinition) -> str:
    if tool.source.type == "skill":
        skill_id = tool.source.skill_id or "skill"
        return f"Deferred tools loaded from skill '{skill_id}'."
    server_name = tool.source.server_name or tool.source.server_id or "server"
    return f"Deferred tools loaded from MCP server '{server_name}'."


def _select_generic_visible_tools(
    *,
    core_tools: list[ToolDefinition],
    discovered_tools: list[ToolDefinition],
    search_tool: ToolDefinition | None,
    available_slots: int | None,
    deferred_present: bool,
) -> tuple[list[ToolDefinition], bool]:
    if available_slots is None:
        all_visible = list(core_tools)
        if deferred_present and search_tool is not None:
            all_visible.append(search_tool)
        all_visible.extend(discovered_tools)
        return _unique_tools(all_visible), False

    visible: list[ToolDefinition] = []
    overflowed = False
    must_include_search = (
        deferred_present or len(core_tools) + len(discovered_tools) > available_slots
    )
    for tool in discovered_tools:
        if len(visible) >= available_slots:
            overflowed = True
            break
        visible.append(tool)
    remaining_slots = max(0, available_slots - len(visible))
    reserved_for_search = (
        1 if must_include_search and search_tool is not None and remaining_slots > 0 else 0
    )
    core_budget = max(0, remaining_slots - reserved_for_search)
    for added_core, tool in enumerate(core_tools, start=1):
        if added_core > core_budget:
            overflowed = True
            break
        visible.append(tool)
    if reserved_for_search and search_tool is not None:
        visible.append(search_tool)
    remaining = max(0, available_slots - len(visible))
    for tool in discovered_tools:
        if remaining <= 0:
            overflowed = True
            break
        visible.append(tool)
        remaining -= 1
    if len(core_tools) + len(discovered_tools) > len(visible):
        overflowed = True
    return _unique_tools(visible), overflowed


def _unique_tools(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    seen: set[str] = set()
    unique: list[ToolDefinition] = []
    for tool in tools:
        tool_id = stable_tool_id(tool)
        if tool_id in seen:
            continue
        seen.add(tool_id)
        unique.append(tool)
    return unique


def _dedupe_visible_name(name: str, tool_id: str, used_names: set[str]) -> str:
    if name not in used_names:
        used_names.add(name)
        return name
    suffix = hashlib.sha1(tool_id.encode()).hexdigest()[:6]
    trimmed = name[: max(1, 64 - len(suffix) - 2)]
    deduped = f"{trimmed}__{suffix}"
    used_names.add(deduped)
    return deduped


def _is_deferred_tool(tool: ToolDefinition) -> bool:
    return tool.source.type in {"skill", "local_mcp", "intaris_mcp"}


def _tool_visible_name(tool: ToolDefinition) -> str:
    if tool.source.type == "skill" and tool.source.raw_tool_name:
        return _sanitize_visible_name(tool.source.raw_tool_name)
    return tool.name


def _sanitize_visible_name(name: str) -> str:
    cleaned = _VISIBLE_TOOL_NAME_PATTERN.sub("_", name).strip("_")
    if not cleaned:
        return "tool"
    if len(cleaned) <= _MAX_VISIBLE_TOOL_NAME_LENGTH:
        return cleaned
    suffix = hashlib.sha1(name.encode()).hexdigest()[:8]
    trimmed = cleaned[: _MAX_VISIBLE_TOOL_NAME_LENGTH - len(suffix) - 1].rstrip("_")
    return f"{trimmed}_{suffix}"


def _tool_sort_key(tool: ToolDefinition) -> tuple[int, int, str]:
    category_priority = {
        "system": 0,
        "datetime": 1,
        "workflow": 2,
        "memory": 3,
        "tool_output": 4,
        "image": 5,
        "filesystem": 6,
        "search": 7,
        "shell": 8,
        "web": 9,
        "mcp": 10,
    }
    source_priority = {
        "builtin": 0,
        "executor": 1,
        "skill": 2,
        "local_mcp": 3,
        "intaris_mcp": 4,
    }
    return (
        category_priority.get(tool.category, 50),
        source_priority.get(tool.source.type, 50),
        stable_tool_id(tool),
    )
